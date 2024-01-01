# Copyright 2024 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
import dataclasses
import json
import os
import tempfile

from bisect_kit import common
from bisect_kit import util
from google.oauth2 import service_account
from googleapiclient import discovery


# TODO(zjchang): extend to multiple projects
VMLEASER_DEFAULT_PROJECT = 'google.com:crosperf-dev'
VMLEASER_DEFAULT_NETWORK = 'global/networks/default'
VMLEASER_DEFAULT_MACHINE_TYPE = 'n2-standard-4'
VMLEASER_DEFAULT_DISK_SIZE = 20
VMLEASER_DEFAULT_REGION = 'us-central1-a'
VMLEASER_PROD_SERVER = 'vmleaser.api.cr.dev:443'


@dataclasses.dataclass
class VMLeaseResult:
    """A data class represents the lease result of VM machine."""

    lease_id: str
    private_host: str
    public_host: str
    gce_project: str
    gce_region: str


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
    if not common.under_luci_context() and service_account_json:
        cmd += ['-service-account-json', service_account_json]

    with tempfile.TemporaryFile(mode='w', suffix='.json') as temp:
        json.dump(content, temp)
        temp.flush()
        temp.seek(0)
        return util.check_output(
            *cmd,
            stdin=temp,
        )


def lease_vm(gce_image: str) -> VMLeaseResult:
    """Lease a VM machine.

    Args:
      gce_image: The image name using to boot VM.

    Returns:
      VMLeaseResult: The lease result.
    """
    method = 'chromiumos.test.api.VMLeaserService.LeaseVM'
    # TODO(zjchang): make other options adjustable.
    content = {
        'host_reqs': {
            'gce_image': gce_image,
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


def get_vm_public_ip(vm: VMLeaseResult) -> str:
    """Get the public of vm.

    Args:
      vm: The vm to be queried.

    Returns:
      public IP address.
    """
    credentials = service_account.Credentials.from_service_account_file(
        os.environ.get('SKYLAB_CLOUD_SERVICE_ACCOUNT_JSON')
    )
    service = discovery.build('compute', 'v1', credentials=credentials)
    response = (
        service.instances()
        .get(project=vm.gce_project, zone=vm.gce_region, instance=vm.lease_id)
        .execute()
    )

    for interface in response['networkInterfaces']:
        for config in interface['accessConfigs']:
            if config['name'] == 'External NAT':
                return config['natIP']
    return None
