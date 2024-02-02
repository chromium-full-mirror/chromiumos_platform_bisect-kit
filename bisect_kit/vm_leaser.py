# Copyright 2024 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
from __future__ import annotations

import dataclasses
import json
import os
import tempfile
import time
from typing import Optional
import uuid

from bisect_kit import core
from bisect_kit import cros_util
from bisect_kit import util
from google.api_core.client_options import ClientOptions
from google.api_core.extended_operation import ExtendedOperation
from google.cloud import compute_v1
from google.oauth2 import service_account
from googleapiclient import discovery


# TODO(zjchang): extend to multiple projects
# TODO(zjchang): use typed API instead of discovery.build
VMLEASER_DEFAULT_PROJECT = 'google.com:crosperf-dev'
VMLEASER_DEFAULT_NETWORK = 'global/networks/default'
VMLEASER_DEFAULT_MACHINE_TYPE = 'n2-standard-4'
VMLEASER_DEFAULT_DISK_SIZE = 20
VMLEASER_DEFAULT_REGION = 'us-central1-a'
VMLEASER_PROD_SERVER = 'vmleaser.api.cr.dev:443'
VM_DEFAULT_IMAGE_CLOUD_PROJECT = 'chromeos-gce-tests'

MAX_RETRIES = 25
RETRY_PERIOD_SECONDS = 15


@dataclasses.dataclass
class VMLeaseResult:
    """A data class represents the lease result of VM machine."""

    lease_id: str
    private_host: str
    public_host: str
    gce_project: str
    gce_region: str
    gce_image_project: str
    gce_image_name: str


def get_default_credential() -> service_account.Credentials:
    """Returns default credential to call cloud APIs."""
    return service_account.Credentials.from_service_account_file(
        os.environ.get('SKYLAB_CLOUD_SERVICE_ACCOUNT_JSON')
    )


def get_default_client_option() -> ClientOptions:
    """Returns default ClientOptions for cloud client."""
    return ClientOptions(credentials_file=get_default_credential())


def grpc(server: str, method: str, content: dict) -> str:
    """Call prpc.

    Args:
      server: server name.
      method: grpc method name.
      content: content send to grpc method.

    Returns:
      A string, the result of grpc call.
    """
    service_account_json = os.environ.get('SKYLAB_CLOUD_SERVICE_ACCOUNT_JSON')
    cmd = [
        'prpc',
        'call',
        server,
        method,
    ]
    if service_account_json:
        cmd += ['-service-account-json', service_account_json]

    with tempfile.TemporaryFile(mode='w', suffix='.json') as temp:
        json.dump(content, temp)
        temp.flush()
        temp.seek(0)
        return util.check_output(
            *cmd,
            stdin=temp,
        )


def _to_vm_leaser_image_link(gcp_project: str, image_name: str) -> str:
    """Converts the gce image name to vm leaser format.

    Args:
        gcp_project: GCP project for the image.
        image_name: GCE image name.

    Returns:
        Image link used for vm_leaser API.
    """
    return f'projects/{gcp_project}/global/images/{image_name}'


def lease_vm(
    gs_vm_image_path: str, board: str, full_version: str
) -> VMLeaseResult:
    """Lease a VM machine.

    Args:
        gs_vm_image_path: The gs path of GCE tarball.
        board: ChromeOS board name.
        full_version: ChromeOS version of gs_vm_image_path.

    Returns:
        The lease result.
    """
    method = 'chromiumos.test.api.VMLeaserService.LeaseVM'
    # TODO(zjchang): make other options adjustable.
    # TODO(zjchang): add lease_vm into switch time.
    inserted_image = insert_image(gs_vm_image_path, board, full_version)
    content = {
        'host_reqs': {
            'gce_image': _to_vm_leaser_image_link(
                VMLEASER_DEFAULT_PROJECT, inserted_image
            ),
            'gce_project': VMLEASER_DEFAULT_PROJECT,
            'gce_region': VMLEASER_DEFAULT_REGION,
            'gce_network': VMLEASER_DEFAULT_NETWORK,
            'gce_machine_type': VMLEASER_DEFAULT_MACHINE_TYPE,
            'gce_disk_size': VMLEASER_DEFAULT_DISK_SIZE,
        }
    }
    response = json.loads(grpc(VMLEASER_PROD_SERVER, method, content))
    assert response['leaseId']
    assert response['vm']['address']['host']
    assert response['vm']['gceRegion']
    result = VMLeaseResult(
        lease_id=response['leaseId'],
        private_host=response['vm']['address']['host'],
        public_host='',
        gce_project=VMLEASER_DEFAULT_PROJECT,
        gce_region=response['vm']['gceRegion'],
        gce_image_project=VMLEASER_DEFAULT_PROJECT,
        gce_image_name=inserted_image,
    )
    result.public_host = get_vm_public_ip(result)
    return result


def release_vm(vm: VMLeaseResult) -> bool:
    """Lease a VM machine.

    Args:
      vm (VMLeaseResult): The vm to be released.

    Returns:
      Boolean, True if release succeeded.
    """
    method = 'chromiumos.test.api.VMLeaserService.ReleaseVM'
    content = {
        'leaseId': vm.lease_id,
        'gceRegion': vm.gce_region,
        'gceProject': vm.gce_project,
    }
    response = json.loads(grpc(VMLEASER_PROD_SERVER, method, content))
    return response.get('leaseId') == vm.lease_id


def _get_public_ip(gce_instance) -> Optional[str]:
    """Get the public IP from gce instance object.

    Args:
        gce_instance: compute#instance object in API.

    Returns:
        The public IP address, or None if not found.
    """
    for interface in gce_instance['networkInterfaces']:
        for config in interface['accessConfigs']:
            if config['name'] == 'External NAT':
                return config['natIP']
    return None


def insert_image(
    gs_path: str,
    board: str,
    full_version: str,
    gcp_project: str = VMLEASER_DEFAULT_PROJECT,
) -> str:
    """Inserts image from GS to GCE.

    Args:
        gs_path: gs path of GCE image tarball.
        board: ChromeOS board name, used for tagging.
        full_version: ChromeOS version, used for tagging.
        gcp_project: GCP project to insert image.

    Returns:
        Image name created.
    """
    assert cros_util.is_cros_or_snapshot_version(full_version)
    assert gs_path.startswith('gs://')

    client = compute_v1.ImagesClient(client_options=get_default_client_option())
    gs_path = 'https://storage.googleapis.com/' + gs_path[len('gs://') :]
    version_tag = full_version.lower().replace(".", "-").replace("~", "-")
    name = f'{board}-{version_tag}-{str(uuid.uuid4())[:8]}'
    request = compute_v1.InsertImageRequest(
        project=gcp_project,
        image_resource=compute_v1.types.Image(
            name=name,
            raw_disk=compute_v1.types.RawDisk(source=gs_path),
            labels={
                'board': board,
                'version': version_tag,
            },
        ),
    )
    client.insert(request=request)
    # Generally it takes around 3 mins to get new image ready.
    time.sleep(90)
    for _ in range(0, MAX_RETRIES):
        status = get_image(name, gcp_project=gcp_project).status
        if status == 'READY':
            break
        time.sleep(RETRY_PERIOD_SECONDS)
    return name


def get_image(
    image_name: str, gcp_project: str = VMLEASER_DEFAULT_PROJECT
) -> compute_v1.types.compute.Image:
    """Gets GCE image info.

    Args:
        image_name: GCE image name.
        gcp_project: GCP project to get the image.

    Returns:
        GCE Image object.
    """
    client = compute_v1.ImagesClient(client_options=get_default_client_option())
    request = compute_v1.GetImageRequest(
        project=gcp_project,
        image=image_name,
    )
    return client.get(request=request)


def delete_image(
    image_name: str, gcp_project: str = VMLEASER_DEFAULT_PROJECT
) -> ExtendedOperation:
    """Deletes image from gce image list.

    Args:
        image_name: Image name that wants to be deleted.
        gcp_project: GCP project to remove image.

    Returns:
        ExtendedOperation object.
    """
    client = compute_v1.ImagesClient(client_options=get_default_client_option())
    request = compute_v1.DeleteImageRequest(
        project=gcp_project,
        image=image_name,
    )
    return client.delete(request=request)


def get_vm_public_ip(vm: VMLeaseResult) -> str:
    """Get the public of vm.

    Args:
      vm: The vm to be queried.

    Returns:
      public IP address.
    """
    service = discovery.build(
        'compute', 'v1', credentials=get_default_credential()
    )
    response = (
        service.instances()
        .get(project=vm.gce_project, zone=vm.gce_region, instance=vm.lease_id)
        .execute()
    )

    return _get_public_ip(response)


def get_vm_name_from_public_ip(
    ip: str, gcp_project: str = VMLEASER_DEFAULT_PROJECT
) -> Optional[str]:
    """Get the VM name from public IP.

    Args:
      ip: The public IP for VM.
      gcp_project: GCP project to find GCE instances.

    Returns:
      VM name, or None if not found.
    """
    service = discovery.build(
        'compute', 'v1', credentials=get_default_credential()
    )
    request = service.instances().aggregatedList(project=gcp_project)
    while request is not None:
        response = request.execute()
        zone = response.get('items', {})
        for zone_element in zone.values():
            for instance in zone_element.get('instances', []):
                public_ip = _get_public_ip(instance)
                if public_ip == ip:
                    return instance['name']
        request = service.instances().aggregatedList_next(
            previous_request=request, previous_response=response
        )
    return None


def allocate_dut(
    spec: core.DutAllocateSpec, gs_vm_image_path: str, full_version: str
) -> VMLeaseResult:
    """Allocate a VM DUT.

    Args:
        spec: dut allocation spec.
        gs_vm_image_path: GS path of GCE image tarball.
        full_version: full ChromeOS version.

    Returns:
        Leased VM info.
    """
    # pool is ignored
    spec.pool = None
    assert cros_util.is_cros_or_snapshot_version(full_version)
    assert spec.board
    assert not spec.model
    assert not spec.sku
    assert not spec.dut_name
    assert not spec.satlab_ip
    if not spec.builder_hint:
        spec.builder_hint = spec.board
    vm = lease_vm(gs_vm_image_path, spec.board, full_version)
    return vm
