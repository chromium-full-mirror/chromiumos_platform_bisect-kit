# Copyright 2024 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
from __future__ import annotations

import dataclasses
import json
import logging
import os
import tempfile
import time
from typing import Optional
import uuid

from bisect_kit import util
from google.api_core.client_options import ClientOptions
from google.api_core.exceptions import NotFound
from google.api_core.extended_operation import ExtendedOperation
from google.cloud import compute_v1
from google.oauth2 import service_account


logger = logging.getLogger(__name__)

# TODO(zjchang): extend to multiple projects
# TODO(zjchang): use typed API instead of discovery.build
VMLEASER_DEFAULT_PROJECT = 'google.com:crosperf-dev'
VMLEASER_DEFAULT_NETWORK = 'global/networks/default'
VMLEASER_DEFAULT_MACHINE_TYPE = 'n2-standard-4'
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


def _get_disk_size(board: str) -> int:
    """Return the VM disk size to create in GiB per the given board.

    These values are as of 2025-08-22 to run all the tast tests,
    from https://chromium.googlesource.com/infra/infra/+/11f04943f680b0710fa2fa2a8b7d3e4ec63368eb/go/src/infra/cros/cmd/cros_test_runner/internal/executors/cros_vm_provision_executor.go#354
    """

    if "betty" in board:
        # 47GiB is more than 50GB, which is required by bruschetta tests.
        return 47
    if "reven-vmtest" in board:
        return 20
    if "amd64-generic" in board:
        return 26

    return 13


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
    gs_vm_image_path: str, board: str, vm_cros_version: str
) -> VMLeaseResult:
    """Lease a VM machine.

    Args:
        gs_vm_image_path: The gs path of GCE tarball.
        board: ChromeOS board name.
        vm_cros_version: ChromeOS version (maybe followed by bisection info).

    Returns:
        The lease result.
    """
    method = 'chromiumos.test.api.VMLeaserService.LeaseVM'
    # TODO(zjchang): make other options adjustable.
    # TODO(zjchang): add lease_vm into switch time.
    inserted_image = insert_image(gs_vm_image_path, board, vm_cros_version)
    content = {
        # prpc may retry automatically and is unable to remove timeout creation,
        # use this key to compute vm name to ensure everytime it creates the
        # VM on cloud with same instance name.
        'idempotency_key': str(uuid.uuid4()),
        'host_reqs': {
            'gce_image': _to_vm_leaser_image_link(
                VMLEASER_DEFAULT_PROJECT, inserted_image
            ),
            'gce_project': VMLEASER_DEFAULT_PROJECT,
            'gce_region': VMLEASER_DEFAULT_REGION,
            'gce_network': VMLEASER_DEFAULT_NETWORK,
            'gce_machine_type': VMLEASER_DEFAULT_MACHINE_TYPE,
            'gce_disk_size': _get_disk_size(board),
        },
    }
    logger.info('Leasing a VM: %s', json.dumps(content))

    response = json.loads(grpc(VMLEASER_PROD_SERVER, method, content))
    assert response['leaseId']
    lease_id = response['leaseId']

    for _ in range(0, MAX_RETRIES):
        if 'host' in response['vm']['address']:
            break

        logger.info(
            'Incomplete result on leasing a VMs. Retrying in %d sec',
            RETRY_PERIOD_SECONDS,
        )
        time.sleep(RETRY_PERIOD_SECONDS)

        response = json.loads(grpc(VMLEASER_PROD_SERVER, method, content))
        assert response['leaseId'] == lease_id

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
    result.public_host = get_vm_public_ip(result) or ''
    logger.info('Leased a VM: %s in %s.', result.public_host, result.gce_region)
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
    for _ in range(0, 10):
        response = json.loads(grpc(VMLEASER_PROD_SERVER, method, content))
        assert response.get('leaseId') == vm.lease_id
        time.sleep(5)
        if _get_vm_instance(vm) is None:
            return True
    logger.warning('cannot delete vm: %s', vm)
    return False


def _get_public_ip(gce_instance: compute_v1.Instance) -> Optional[str]:
    """Get the public IP from gce instance object.

    Args:
        gce_instance: compute#instance object in API.

    Returns:
        The public IP address, or None if not found.
    """
    for interface in gce_instance.network_interfaces:
        for config in interface.access_configs:
            if config.name == 'External NAT':
                return config.nat_i_p
    return None


def insert_image(
    gs_path: str,
    board: str,
    vm_cros_version: str,
    gcp_project: str = VMLEASER_DEFAULT_PROJECT,
) -> str:
    """Inserts image from GS to GCE.

    Args:
        gs_path: gs path of GCE image tarball.
        board: ChromeOS board name, used for tagging.
        vm_cros_version: ChromeOS version (maybe followed by bisection info),
            used for tagging.
        gcp_project: GCP project to insert image.

    Returns:
        Image name created.
    """
    assert gs_path.startswith('gs://')

    client = compute_v1.ImagesClient(client_options=get_default_client_option())
    gs_path = 'https://storage.googleapis.com/' + gs_path[len('gs://') :]

    # Sanitize
    version_tag = (
        vm_cros_version.lower()
        .replace(".", "-")
        .replace("~", "-")
        .replace("/", "-")
    )

    # Generate an image name and make sure it's <63 chars.
    GCP_IMAGE_NAME_MAX_LEN = 63
    version_tag_max_len = GCP_IMAGE_NAME_MAX_LEN - len(board) - 1 - 1 - 8
    name = (
        f'{board}-{version_tag[:version_tag_max_len]}-{str(uuid.uuid4())[:8]}'
    )
    assert len(name) <= GCP_IMAGE_NAME_MAX_LEN

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


def _get_vm_instance(vm: VMLeaseResult) -> Optional[compute_v1.Instance]:
    """Get the compute instance object.

    Args:
      vm: The vm to be queried.

    Returns:
      A compute_v1.Instance object from API, or None if not found.
    """
    client = compute_v1.InstancesClient(
        client_options=get_default_client_option()
    )
    request = compute_v1.GetInstanceRequest(
        project=vm.gce_project,
        zone=vm.gce_region,
        instance=vm.lease_id,
    )
    try:
        return client.get(request)
    except NotFound:
        return None


def get_vm_public_ip(vm: VMLeaseResult) -> Optional[str]:
    """Get the public of vm.

    Args:
      vm: The vm to be queried.

    Returns:
      public IP address.
    """
    instance = _get_vm_instance(vm)
    if instance is None:
        return None
    return _get_public_ip(instance)


def allocate_dut(
    board: str, gs_vm_image_path: str, vm_cros_version: str
) -> VMLeaseResult:
    """Allocate a VM DUT.

    Args:
        booard: board name.
        gs_vm_image_path: GS path of GCE image tarball.
        vm_cros_version: ChromeOS version (maybe followed by bisection info).

    Returns:
        Leased VM info.
    """
    return lease_vm(gs_vm_image_path, board, vm_cros_version)
