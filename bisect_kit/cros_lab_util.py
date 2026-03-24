# Copyright 2018 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Module to manipulate machines in chromeos test lab.

Terminology to refer a DUT in bisect-kit:
  dut: (in bisect-kit everywhere)
      It means the network address of a DUT, which is ssh-able.
  dut_name: (for swarming and skylab api) also known as "host" name
      Name of DUT in the lab, in format like chromeosX-rowX-rackX-hostX.
      "dut_name" is almost used only in this module. It needs conversion to
      "dut" for rest of bisect-kit code (dut_name_to_address).
"""

from __future__ import annotations

import asyncio
import calendar
import contextlib
import dataclasses
import datetime
import errno
import json
import logging
import os
import queue
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import timeit
import typing
import uuid

from bisect_kit import common
from bisect_kit import cros_util
from bisect_kit import errors
from bisect_kit import scheduke_util
from bisect_kit import util
import sshconf


logger = logging.getLogger(__name__)

# Magic keyword for automatic DUT allocation.
LAB_DUT = ':lab:'

# Regexes for the supported hostnames without .cros suffix.
# TODO(b/242070463): remove the name_to_address logic if possible.
LAB_DUT_EXCEPTIONS = [
    r'chromeos8-.+-host.+',
    r'chromeos8-.+-labstation.+',
    r'cre.+',
]

# Prefixes of bisecor task identifier string. On Prod/Staging/Dev environments,
# the previs should be a host name so an identifier will be an URL.
BISECTOR_HOST_PROD = 'https://crosperf.googleplex.com/'
BISECTOR_HOST_STAGING = 'https://crosperf-staging.googleplex.com/'
BISECTOR_HOST_DEV = 'https://crosperf-dev.googleplex.com/'
BISECTOR_LOCAL_TEST_PREFIX = 'cros-bisect:'

PROD_SERVICE_ACCOUNT = (
    'bisect-runner@crosperf.google.com.iam.gserviceaccount.com'
)
DEV_SERVICE_ACCOUNT = (
    'bisect-runner@crosperf-dev.google.com.iam.gserviceaccount.com'
)
STAGING_SERVICE_ACCOUNT = (
    'bisect-runner@crosperf-staging.google.com.iam.gserviceaccount.com'
)

SWARMING_PRPC_SERVER = 'chromeos-swarming.appspot.com'
DEVICE_LEASE_SERVICE_PRPC_SERVER = (
    'device-lease-service-prod-bbx5lsj5jq-uc.a.run.app'
)
# https://chromium.googlesource.com/infra/luci/luci-py/+/733d5d3b5299408cbc6a5e5b33b1a461ea9e0acd/appengine/swarming/proto/api_v2/swarming.proto#162
SWARMING_PRPC_QUERY_ALL = 10
SWARMING_PRPC_DEFAULT_LIMIT = 1000
LEASE_DURATION_SECONDS = 1439 * 60
LEASE_ATTEMPT_TIME = 600


class CrosfleetDutLease(typing.TypedDict, total=False):
    """A data represents the DUT lease status fetched from crosfleet command."""

    dut_hostname: str
    model: str
    board: str
    servo_hostname: str
    servo_port: str
    servo_serial: str
    mins_remaining: str
    lease_id: str


@dataclasses.dataclass(eq=True)
class LeaseStatus:
    """A data represents the swarming DUT lease status.

    Attributes:
        dut_name: The swarming dut name.
        bot_id: The bot id of given `dut_name`.
        task_name: Current task name or None.
        task_id: Current task id or None.
        is_leased: Whether the DUT is leased now (bool). If false, following
            fields are not available.
        leased_by: User id leases the dut.
        end_time: Timestamp at which this lease ends. (seconds since unix epoch time)
    """

    dut_name: str
    bot_id: typing.Optional[str] = None
    task_name: typing.Optional[str] = None
    task_id: typing.Optional[str] = None
    lease_id_in_dls: typing.Optional[str] = None
    is_leased: bool = False
    leased_by: typing.Optional[str] = None
    end_time: typing.Optional[float] = None


def normalize_sku_name(sku):
    """Normalize SKU name.

    Args:
      sku: SKU name

    Returns:
      normalized SKU name
    """
    # SKU names end with ram size like "16GB". "Gb" suffix is obsoleted.
    return re.sub(r'\d+GB$', lambda m: m.group().upper(), sku, flags=re.I)


def is_satlab_dut(dut_host: str) -> bool:
    """Returns if the DUT is a satlab Dut.

    Args:
      dut_host: dut host

    Returns:
      True if the dut is under a satlab
    """
    return dut_host.startswith('satlab')


def is_dut_needs_forwarded(dut: str) -> bool:
    """Returns if the DUT needs to be forwarded in the chroot.

    Args:
      dut: dut host.

    Returns:
      True if the dut needs to be forwarded.
    """
    for r in LAB_DUT_EXCEPTIONS:
        if re.match(r, dut):
            return True
    if is_satlab_dut(dut):
        return True
    return False


def is_lab_dut(dut: str):
    if is_dut_needs_forwarded(dut):
        return True
    return dut.endswith('.cros')


def dut_host_name(dut: str):
    """Converts DUT address to host name (aka dut_name).

    Args:
      dut: DUT address; assume the address is returned from this module
    """
    if is_dut_needs_forwarded(dut):
        return dut
    assert dut.endswith('.cros')
    return dut[:-5]


def dut_name_to_address(dut_name: str) -> str:
    """Converts DUT name to DUT address.

    Args:
      dut_name: DUT name, which is the host name in the lab
    """
    assert '.' not in dut_name
    if is_dut_needs_forwarded(dut_name):
        return dut_name
    return dut_name + '.cros'


def _run_labtools_command(name, *args, **kwargs):
    try:
        # Because skylab_lease_dut() need to capture output even for
        # KeyboardInterrupt, we cannot use util.check_output here.
        stdout_lines: list[str] = []
        if 'stdout_callback' not in kwargs:
            kwargs = kwargs.copy()
            kwargs['stdout_callback'] = stdout_lines.append
        util.check_call(name, *args, **kwargs)
        return ''.join(stdout_lines)
    except OSError as e:
        if e.errno == errno.ENOENT:
            logger.error(
                '"%s" not installed? see go/lab-tools for setup steps', name
            )
        raise errors.ExternalError(str(e))


def skylab_cmd(cmd, *args, **kwargs):
    """Wrapper of skylab cli 'skylab'.

    Args:
      cmd: skylab command, like 'lease-dut', 'release-duts', etc.
      args: additional arguments passed to skylab command line
      kwargs: additional arguments to control how to run the command
    """
    service_account_json = os.environ.get('SKYLAB_CLOUD_SERVICE_ACCOUNT_JSON')
    full_args = list(args)
    if service_account_json:
        full_args = ['-service-account-json', service_account_json] + full_args
    return _run_labtools_command('skylab', cmd, *full_args, **kwargs)


def crosfleet_cmd(cmd, *args, **kwargs):
    """Wrapper of crosfleet cli.

    Args:
      cmd: crosfleet command, such as 'dut info'.
      args: additional arguments passed to crosfleet command line
      kwargs: additional arguments to control how to run the command
    """
    service_account_json = os.environ.get('SKYLAB_CLOUD_SERVICE_ACCOUNT_JSON')
    full_args = list(args)
    if service_account_json:
        full_args = ['-service-account-json', service_account_json] + full_args
    cmd_list = cmd.split(' ')
    return _run_labtools_command('crosfleet', *cmd_list, *full_args, **kwargs)


def crosfleet_dut_info(dut_name) -> CrosfleetDutLease:
    """Run crosfleet dut info.

    Args:
      dut_name: A skylab dut name without .cros suffix.
    Returns:
      A dict of dut info.
    """
    info = {}
    # Sample stdout:
    #
    # DUT_HOSTNAME=chromeos8-row13-rack17-host2
    # MODEL=willow
    # BOARD=jacuzzi
    # SERVO_HOSTNAME=chromeos8-row13-rack17-labstation1
    # SERVO_PORT=9998
    # SERVO_SERIAL=SERVOV4P1-S-2204211229
    #
    for line in crosfleet_cmd('dut info', dut_name).split('\n'):
        m = re.fullmatch(r'(\S+)=(\S+)', line)
        if m:
            info[m.group(1).lower()] = m.group(2)
    return info


def crosfleet_dut_leases() -> dict[str, CrosfleetDutLease]:
    """Run crosfleet dut leases.

    Returns:
      A dict of dut info.
    """
    info: dict[str, CrosfleetDutLease] = {}
    # Sample stdout:
    #
    # ===
    # DUT_HOSTNAME=chromeos8-row13-rack17-host2
    # MODEL=willow
    # BOARD=jacuzzi
    # SERVO_HOSTNAME=chromeos8-row13-rack17-labstation1
    # SERVO_PORT=9998
    # SERVO_SERIAL=SERVOV4P1-S-2204211229
    # MINS_REMAINING=1438.64
    # LEASE_ID=73967319
    #
    # DUT_HOSTNAME=chromeos8-row13-rack17-host2
    # MODEL=willow
    # BOARD=jacuzzi
    # SERVO_HOSTNAME=chromeos8-row13-rack17-labstation1
    # SERVO_PORT=9998
    # SERVO_SERIAL=SERVOV4P1-S-2204211229
    # MINS_REMAINING=1438.64
    # LEASE_ID=73967319
    # ===
    current_section_info: dict[str, str] = {}
    error = False
    output = crosfleet_cmd('dut leases')
    for line in output.split('\n'):
        if not line and current_section_info:
            dut_name = current_section_info.get('dut_hostname', '')
            if dut_name:
                info[dut_name] = typing.cast(
                    CrosfleetDutLease, current_section_info
                )
            else:
                error = True
                logger.error(
                    '[crosfleet dut leases] Invalid section: %s',
                    current_section_info,
                )
            current_section_info = {}
        m = re.fullmatch(r'([^=\s]+)=(\S+)', line)
        if m:
            current_section_info[m.group(1).lower()] = m.group(2)

    if not error and current_section_info:
        dut_name = current_section_info.get('dut_hostname', '')
        if dut_name:
            info[dut_name] = typing.cast(
                CrosfleetDutLease, current_section_info
            )
        else:
            error = True
            logger.error(
                '[crosfleet dut leases] Invalid section: %s',
                current_section_info,
            )

    if error:
        logger.info('`crosfleet dut leases` Output:===\n%s\n===', output)
    return info


def _grpc(
    server: str,
    method: str,
    content: dict,
    log_stdout: bool = True,
    use_id_token: bool = False,
    verbose: bool = False,
) -> str:
    """Call a grpc method.

    Args:
      server (str): server host name.
      method (str): grpc method name.
      content (dict): content send to grpc method.
      log_stdout (bool): Whether to write the stdout output of the child process
          to log.
      use_id_token (bool): Whether to use ID token.
      verbose (bool): Whether to run in verbose mode.

    Returns:
      A string, the result of grpc call.
    """
    service_account_json = os.environ.get('SKYLAB_CLOUD_SERVICE_ACCOUNT_JSON')
    cmd = [
        'prpc',
        'call',
    ]
    if verbose:
        cmd += ['-verbose']
    if use_id_token:
        cmd += ['-use-id-token']
    if not common.under_luci_context() and service_account_json:
        cmd += ['-service-account-json', service_account_json]
    cmd += [server, method]
    # TODO(zjchang): To communicate with subprocess without opening new file.
    with tempfile.TemporaryFile(mode='w', suffix='.json') as temp:
        json.dump(content, temp)
        temp.flush()
        temp.seek(0)
        return util.check_output(
            *cmd,
            stdin=temp,
            log_stdout=log_stdout,
        )


def swarming_grpc(method: str, content: dict, log_stdout: bool = True) -> str:
    """Call swarming prpc.

    Args:
      method (str): grpc method name.
      content (dict): content send to grpc method.
      log_stdout (bool): Whether to write the stdout output of the child process
          to log.

    Returns:
      A string, the result of grpc call.
    """
    return _grpc(SWARMING_PRPC_SERVER, method, content, log_stdout)


def device_lease_service_grpc(
    method: str, content: dict, log_stdout: bool = True
) -> str:
    """Call device lease service prpc.

    Args:
      method (str): grpc method name.
      content (dict): content send to grpc method.
      log_stdout (bool): Whether to write the stdout output of the child process
          to log.

    Returns:
      A string, the result of grpc call.
    """
    return _grpc(
        DEVICE_LEASE_SERVICE_PRPC_SERVER,
        method,
        content,
        log_stdout,
        use_id_token=True,
        verbose=True,
    )


def swarming_cancel(task_id):
    """Cancels swarming task.

    Args:
      task_id: task id

    Returns:
        A dict swarming.v2.CancelResponse, including
            canceled (bool)
            is_running (bool)
    """
    method = 'swarming.v2.Tasks.CancelTask'
    content = {'task_id': task_id, 'kill_running': True}
    return json.loads(swarming_grpc(method, content))


def bb_cancel(build_id, reason):
    """Cancels buildbucket build.

    Args:
      build_id: build id
      reason: cancel reason
    """
    service_account_json = os.environ.get('SKYLAB_CLOUD_SERVICE_ACCOUNT_JSON')
    cmd = ['bb', 'cancel', '-reason', reason]
    if service_account_json:
        cmd += ['-service-account-json', service_account_json]
    cmd.append(build_id)
    util.check_call(*cmd)


def scheduke_cancel(lease_id):
    """Cancels a scheduke lease via "crosfleet dut abandon"

    (b/343902491)
    When leasing via Scheduke, the buildbucket task id is not returned because
    the task may not been brought up yet.
    In the case, we can only cancel it via "crosfleet dut abandon".
    """
    logger.debug('canceling the lease of %s by Scheduke', lease_id)
    try:
        crosfleet_cmd('dut abandon', '-lease-ids', lease_id)
    except Exception as e:
        logger.warning(
            'failed to cancel a scheduke lease for %s: %s', lease_id, e
        )


def _simplify_bot_dimensions(dimensions_pairs):
    # Convert list to dict, so it's easier to access.
    dimensions = {}
    for dimension in dimensions_pairs:
        assert dimension['key'] not in dimensions
        dimensions[dimension['key']] = dimension['value']
    return dimensions


def swarming_bots_dimensions():
    method = 'swarming.v2.Bots.GetBotDimensions'
    return _simplify_bot_dimensions(
        json.loads(swarming_grpc(method, {}, log_stdout=False))[
            'botsDimensions'
        ]
    )


def swarming_bot_info(bot_id, verbose_log=True):
    del verbose_log
    method = 'swarming.v2.Bots.GetBot'
    content = {'bot_id': bot_id}
    bot = json.loads(swarming_grpc(method, content))
    bot['dimensions'] = _simplify_bot_dimensions(bot['dimensions'])
    return bot


def swarming_bots_list(
    dimensions,
    is_dead=None,
    quarantined=None,
    is_busy=None,
    limit=None,
    verbose_log=True,
):
    """Lists swarming bots.

    Args:
      dimensions: dimensions constraints (list)
      is_dead: filter by "is_dead" state; None means don't care
      quarantined: filter by "quarantined" state; None means don't care
      is_busy: filter by "is_busy" state; None means don't care
      limit: limit number of bots
      verbose_log: log more if True

    Returns:
      list of bots dict
    """
    dimensions = [dict(zip(['key', 'value'], d.split(':'))) for d in dimensions]
    if not limit:
        limit = SWARMING_PRPC_DEFAULT_LIMIT
    method = 'swarming.v2.Bots.ListBots'
    content = {'dimensions': dimensions, 'limit': limit}

    # b/341190373: swarming requires boolean parameters to be in string
    # forms ("TRUE" or "FALSE"). Note that it's case sensitive.
    def bool_to_string(value: bool) -> str:
        return str(value).upper()

    if is_dead is not None:
        content['isDead'] = bool_to_string(is_dead)
    if quarantined is not None:
        content['quarantined'] = bool_to_string(quarantined)
    if is_busy is not None:
        content['isBusy'] = bool_to_string(is_busy)

    if verbose_log:
        logger.debug('swarming.v2.Bots.ListBots payload: %s', content)
    bots = []
    for bot in json.loads(swarming_grpc(method, content)).get('items', []):
        bot['dimensions'] = _simplify_bot_dimensions(bot['dimensions'])
        bots.append(bot)
    return bots


_known_satlab_prefix_to_ip_list = {'satlab_prefix': 'satlab_ip'}


def convert_to_known_satlab_ip(dut_name: str) -> str | None:
    '''Get the IP of a known satlab dut

    Args:
        dut_name: satlab dut name

    Returns:
        ip if the dut is known, None for unknown dut
    '''
    # The known_satlab_prefix_to_ip_list can be updated by internal config
    for dut_prefix, satlab_ip in _known_satlab_prefix_to_ip_list.items():
        if dut_name.startswith(dut_prefix):
            return satlab_ip
    return None


def store_known_satlab_prefix(satlab_prefix: str, satlab_ip: str):
    '''Store known satlab prefix and ips in memory for quick retrieval

    Args:
        satlab_prefix: satlab DUT prefix
        satlab_ip: satlab IP
    '''
    can_convert_satlab_prefix_to_ip = convert_to_known_satlab_ip(satlab_prefix)

    if can_convert_satlab_prefix_to_ip is None:
        _known_satlab_prefix_to_ip_list.update({satlab_prefix: satlab_ip})


def swarming_dut_satlab_ip(dut: str) -> str:
    '''Get the satlab ip of a satlab dut

    Args:
        dut: satlab dut name

    Returns:
        The satlab IP of the dut

    Raises:
          errors.ExternalError: If Satlab IP query fails
    '''
    assert is_satlab_dut(dut)
    known_satlab_ip = convert_to_known_satlab_ip(dut)
    if known_satlab_ip:
        return known_satlab_ip

    logger.debug('Querying IP for satlab DUT: %s', dut)
    bots = swarming_bots_list(['dut_name:' + dut])
    if len(bots) != 1:
        raise errors.ExternalError('Satlab IP query failed for DUT: %s' % dut)
    satlab_ip = bots[0]['dimensions']['drone_server'][0]
    if util.is_valid_ip(satlab_ip):
        return satlab_ip

    logger.debug(
        'drone_server dimension in swarming is not valid. This satlab does not have latest update. Trying with externalIp dimension.'
    )

    satlab_ip = bots[0]['externalIp']
    wrong_satlab_ip_prefix = '104'
    if satlab_ip.startswith(wrong_satlab_ip_prefix):
        raise errors.ExternalError(
            'Swarming external IP query for DUT %s returned %s which is not the correct satlab IP. Please try again later.'
            % (dut, satlab_ip)
        )
    return satlab_ip


def swarming_bot_tasks(bot_id, limit=None):
    """Lists recent tasks of given bot.

    Args:
      bot_id: bot id
      limit: limit number of tasks

    Returns:
      list of task dict
    """
    assert bot_id
    if not limit:
        limit = SWARMING_PRPC_DEFAULT_LIMIT
    method = 'swarming.v2.Bots.ListBotTasks'
    content = {
        'bot_id': bot_id,
        'state': SWARMING_PRPC_QUERY_ALL,
        'limit': limit,
    }
    return json.loads(swarming_grpc(method, content))['items']


def is_skylab_dut(dut_name):
    """Determines whether the DUT is in skylab.

    Args:
      dut_name: the DUT name
    """
    # TODO(kcwu): unify host, dut, and dut_name once we completely migrated to
    #             skylab
    bots = swarming_bots_list(['dut_name:' + dut_name])
    return bool(bots)


def is_dut_leased(status: LeaseStatus):
    if not status.is_leased:
        logger.debug(
            '%s current task: %s, is not leased',
            status.dut_name,
            status.task_name,
        )
        return False

    return True


def extract_lease_info(text):
    """Parse inflight lease info from output of crosfleet dut lease.

    You colud find the detailed format from:
      https://chromium.googlesource.com/infra/infra/+/e560ebb6baf0/go/src/infra/
      cmd/crosfleet/internal/dut/lease.go#112
    """
    info = {}
    # The job is wrapped as a buildbucket build.
    m = re.search(r'(http\S+/builders/test_runner/dut_leaser/b(\d+))', text)
    if m:
        info['link'] = m.group(1)
        info['buildbucket_build_id'] = m.group(2)

    # In crosfleet V2, the lease is hand over to Scheduke, so no
    # buildbucket_build_id would be returned.
    # Note: crosfleet output contains a typo 'lace' until crrev.com/c/5975613
    # lands.
    m = re.search(
        r'Internal Scheduke l(e?)ase ID \(can be used for cancellation\): (\d+)',
        text,
    )
    if m:
        info['scheduke_lease_id'] = m.group(2)

    return info


def extract_lease_property(line):
    """Extract a key value pair from output of crosfleet dut lease."""
    m = re.match(r'^(\w+)=(\S+)$', line)
    if m:
        return m.group(1), m.group(2)
    return None, None


def cancel_pending_lease(pending_lease):
    assert pending_lease
    task_id = pending_lease.get('swarming_task_id')
    if task_id:
        logger.debug('cancel the lease task %s', task_id)
        swarming_cancel(task_id)

    build_id = pending_lease.get('buildbucket_build_id')
    if build_id:
        logger.debug('cancel the lease build %s', build_id)
        bb_cancel(build_id, 'cancel pending lease')

    scheduke_lease_id = pending_lease.get('scheduke_lease_id')
    if scheduke_lease_id:
        scheduke_cancel(scheduke_lease_id)

    assert (
        task_id or build_id or scheduke_lease_id
    ), 'lease is pending but nothing to cancel'


def enumerate_dimension_combinations(
    dimensions: list[str], variants: list[str], pools: list[str] | None
) -> list[list[str]]:
    """Returns a list of acceptable dimension combinations for the given parameters."""
    result = []
    if pools:
        for pool in pools:
            for variant in variants:
                result.append(dimensions + [variant, pool])
    else:
        for variant in variants:
            result.append(dimensions + [variant])
    return result


async def create_async_crosfleet_proc(cmd: str, *args, **kwargs):
    cmd_args = ['crosfleet'] + cmd.split(' ')
    service_account_json = os.environ.get('SKYLAB_CLOUD_SERVICE_ACCOUNT_JSON')
    if service_account_json:
        cmd_args += ['-service-account-json', service_account_json]
    cmd_args += list(args)
    logger.debug('run %r', cmd_args)
    return await asyncio.create_subprocess_exec(
        *cmd_args,
        stdout=kwargs.get('stdout', subprocess.PIPE),
        stderr=kwargs.get('stderr', subprocess.PIPE),
    )


class AsyncDutLeaser:
    """A class to lease lab DUTs asynchronously."""

    def __init__(
        self,
        dimensions,
        variants,
        pools,
        boards_with_prebuilt,
        reason,
        lease_duration_seconds=None,
        timeout=None,
    ):
        self._dimensions: list[str] = dimensions
        self._variants: list[str] = variants
        self._pools: list[str] | None = pools
        self._boards_with_prebuilt: list[str] | None = boards_with_prebuilt
        self._reason: str = reason
        self._lease_duration_seconds: int = (
            lease_duration_seconds or LEASE_DURATION_SECONDS
        )
        self._timeout: int | None = timeout

        # Guard self._is_dut_leased
        self._lock = asyncio.Lock()

        # This variable is guarded by self._lock
        self._leased_dut_name: str | None = None

        # Dict from DUT dimensions to human readable DUT availability info.
        self._dut_availability_info: dict[str, str] = {}

    @property
    def dut_availability_info(self) -> dict[str, str]:
        return self._dut_availability_info

    async def lease_parallelly(self):
        tasks = []
        for dimensions in enumerate_dimension_combinations(
            self._dimensions, self._variants, self._pools
        ):
            tasks.append(
                asyncio.create_task(self._lease_with_retry(dimensions))
            )
        logger.info(
            'trying to lease DUTs: dimensions=%s, variants=%s, pools=%s',
            self._dimensions,
            self._variants,
            self._pools,
        )
        # b/296872055: The returned value of asyncio.wait are sets, so if
        # multiple coroutines are completed at the same time, we don't know
        # which one is completed first. As a result, we record the first
        # leased dut in a member variable instead of relying on the return
        # values.
        await asyncio.wait(
            tasks, timeout=self._timeout, return_when=asyncio.FIRST_COMPLETED
        )
        # Cancel ongoing tasks. Unlike asyncio.wait_for(), asyncio.wait()
        # doesn't cancel tasks when a timeout occurs.
        for task in tasks:
            task.cancel('Already leased a DUT.')
        if not self._leased_dut_name:
            # Timeout happens.
            logger.info(
                'timeout, failed to lease DUTs: : dimensions=%s, variants=%s, pools=%s',
                self._dimensions,
                self._variants,
                self._pools,
            )
            return None
        logger.info('leased %s', self._leased_dut_name)
        return self._leased_dut_name

    async def _lease(self, dimensions):
        args = [
            '-minutes',
            '%d' % (self._lease_duration_seconds // 60),
            '-reason',
            self._reason,
        ]
        for dimension in dimensions:
            args.append('-dim')
            args.append(dimension)
        proc = await create_async_crosfleet_proc(
            'dut lease',
            *args,
            # combine two streams, so the code becomes simpler
            stderr=subprocess.STDOUT,
        )

        pending_lease = None
        try:
            lease_properties = {}
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                line = line.decode('utf8')
                logger.debug('lease(%s): %s', dimensions, line)

                if re.search(r'\bFound \d+ DUT\(s\)', line):
                    async with self._lock:
                        self._dut_availability_info[str(dimensions)] = (
                            line.strip()
                        )

                if not pending_lease:
                    pending_lease = extract_lease_info(line)
                if 'INFRA_FAILURE' in line:
                    pending_lease = None
                    return None

                key, value = extract_lease_property(line)
                if key:
                    lease_properties[key] = value

            await proc.wait()
            if proc.returncode != 0:
                logger.warning(
                    'lease(%s): crosfleet command failed: %s',
                    dimensions,
                    proc.returncode,
                )
                raise errors.ExternalError(
                    'crosfleet command failed unexpectedly: %s'
                    % proc.returncode
                )
            if not pending_lease:
                raise errors.InternalError(
                    'not found lease info; crosfleet output format changed?'
                )

            dut_name = lease_properties.get('DUT_HOSTNAME')
            board_name = lease_properties.get('BOARD')
            if not dut_name or not board_name:
                logger.error(
                    'lease(%s): unexpected lease property format: %s',
                    dimensions,
                    lease_properties,
                )
                return None

            if is_satlab_dut(dut_name):
                write_satlab_ssh_config(dut_name)

            if (
                self._boards_with_prebuilt
                and board_name not in self._boards_with_prebuilt
            ):
                logger.warning(
                    'lease(%s): the leased DUT has no prebuilt image; '
                    'return it and lease another one later',
                    dut_name,
                )
                return None

            if not cros_util.is_good_dut(dut_name_to_address(dut_name)):
                logger.warning(
                    'lease(%s): the leased DUT is broken; '
                    'return it and lease another one later',
                    dut_name,
                )
                return None

            # wait the state propagated
            for _ in range(10):
                await asyncio.sleep(5)
                status = query_lease_status(dut_name)
                if is_dut_leased(status):
                    logger.info('lease: %s', pending_lease)
                    break
            else:
                raise errors.DutLeaseException(
                    'lease verification failed: %s' % dut_name
                )

            # (b/290040570)
            # When the even loop ends, all pending tasks are canceled. So
            # normally asyncio.CancelledError is caught and the pending lease is
            # canceled in the "finally" clause below. However there is a race
            # condition that if two tasks reach here (almost) simultaneously,
            # both of them got completely soon. The later task has no chance to
            # receive the CancelledError before returning. In that case, the
            # second task would not return the leased DUT to lab.
            # Using a variable self._leased_dut_name to fix the issue.
            async with self._lock:
                if self._leased_dut_name:
                    logger.debug(
                        'a dut %s has been leased, %s will be released',
                        self._leased_dut_name,
                        dut_name,
                    )
                else:
                    logger.debug('first leased dut: %s', dut_name)
                    self._leased_dut_name = dut_name
                    pending_lease = None

                return dut_name
        except asyncio.CancelledError:
            logger.debug('lease(%s): got CancelledError', dimensions)
            raise
        except errors.DutLeaseException:
            logger.exception(
                'lease(%s): lease task state is unexpected. discard', dimensions
            )
            return None
        finally:
            if proc.returncode is None:
                # We call asyncio.run() frequently. In other words, the event loop is
                # short lived. Although asyncio.subprocess will cancel and terminate the
                # process during event loop shutdown, the exit handler may be invoked
                # after event loop is closed (too late and bounce exception).
                # https://bugs.python.org/issue41320
                try:
                    logger.debug(
                        'killing unfinished proc which is trying to lease %s',
                        dimensions,
                    )
                    proc.kill()
                    await proc.wait()
                except ProcessLookupError:
                    # The process may be already dead; ignore this error silently.
                    # Especially, our code and the process got ctrl-c at the same time and
                    # race.
                    pass
            if pending_lease:
                logger.debug('cleaning up pending lease %s', dimensions)
                cancel_pending_lease(pending_lease)

    async def _lease_with_retry(self, dimensions):
        # Retry until we hit the timeout set in lease_parallelly().
        while True:
            try:
                return await self._lease(dimensions)
            except errors.ExternalError:
                delay = 120
                logger.warning(
                    'lease failed, will retry %d seconds later', delay
                )
                await asyncio.sleep(delay)
            except Exception:
                logger.exception("Error on leasing DUT.")
                return None


def make_lease_reason(session):
    # skylab only accepts reason up to 30 characters.
    return 'bisector: %s' % session[:18]


def crosfleet_lease_dut(
    dut_name, reason, lease_duration_seconds=None, timeout=None
):
    """Lease DUT from crosfleet.

    Args:
      dut_name: DUT name
      lease_duration_seconds: lease duration, in seconds. Default is LEASE_DURATION_SECONDS if None.
      timeout: lease wait limit, in seconds.
      reason: lease reason

    Returns:
      dict of lease status if successful, otherwise None
    """
    logger.debug('crosfleet_lease_dut %s', dut_name)
    if not lease_duration_seconds:
        lease_duration_seconds = LEASE_DURATION_SECONDS
    stdout_lines: list[str] = []
    stderr_lines: list[str] = []
    orphan_task = True
    try:
        crosfleet_cmd(
            'dut lease',
            '-minutes',
            '%d' % (lease_duration_seconds // 60),
            '-reason',
            reason,
            '-host',
            dut_name_to_address(dut_name),
            stdout_callback=stdout_lines.append,
            stderr_callback=stderr_lines.append,
            timeout=timeout,
        )

        pending_lease = extract_lease_info(''.join(stderr_lines))
        # wait the state propagated
        for _ in range(10):
            time.sleep(5)
            status = query_lease_status(dut_name)
            if is_dut_leased(status):
                logger.info('lease for %s: %s', dut_name, pending_lease)
                break
        else:
            raise errors.DutLeaseException(
                'lease verification failed: %s' % dataclasses.asdict(status)
            )

        orphan_task = False
    except subprocess.CalledProcessError:
        stderr = ''.join(stderr_lines)
        if 'INFRA_FAILURE' in stderr:
            logger.warning('unable to lease DUT within time limit')
            orphan_task = False
            return None
        logger.exception('crosfleet dut lease failed')
        raise
    finally:
        if orphan_task:
            pending_lease = extract_lease_info(''.join(stderr_lines))
            if pending_lease:
                cancel_pending_lease(pending_lease)

    logger.info(
        'leased dut %s for %.1f hours',
        dut_name,
        lease_duration_seconds // 60 / 60.0,
    )
    return status


def crosfleet_release_dut(dut_name: str):
    """Release DUT which was leased by me.

    Args:
      dut_name: DUT name
    """
    if not is_dut_leased(query_lease_status(dut_name)):
        raise errors.DutLeaseException(
            '%s is not leased, or not leased by me' % dut_name
        )

    leases = crosfleet_dut_leases()
    lease_info: CrosfleetDutLease = leases.get(dut_name, {})
    lease_id = lease_info.get('lease_id', '')
    if lease_id:
        try:
            crosfleet_cmd('dut abandon', '-lease-ids', lease_id)
        except subprocess.CalledProcessError as e:
            logger.warning(
                'Failed on `crosfleet dut abandon` with lease-id %s: %s',
                lease_id,
                e,
            )
            logger.warning(
                '`crosfleet` command is potentially too old to support '
                + 'lease-id. Falling back to hostname just in case.'
            )
            crosfleet_cmd('dut abandon', dut_name)
    else:
        logger.warning(
            'Lease ID cound not be fetched. Trying abandoning without lease '
            + 'ID.'
        )
        logger.warning('Leases: %s', leases)
        crosfleet_cmd('dut abandon', dut_name)

    # wait the state propagated
    for _ in range(10):
        time.sleep(5)
        if not is_dut_leased(query_lease_status(dut_name)):
            break
    else:
        raise errors.DutLeaseException('release verification failed')

    logger.info('released dut %s', dut_name)


def parse_timestamp_from_swarming(timestamp):
    """Parses timestamp obtained from swarming api.

    Note this discard fraction seconds. It's okay since resolution in seconds is
    good enough for our usage.

    Args:
      timestamp: the timestamp string

    Returns:
      unix timestamp (seconds since unix epoch time)
    """
    return calendar.timegm(time.strptime(timestamp, '%Y-%m-%dT%H:%M:%S.%fZ'))


def query_lease_status(dut_name) -> LeaseStatus:
    """Query DUT lease status.

    Args:
      dut_name: DUT name

    Returns:
      The lease status of DUT.
    """
    status = LeaseStatus(dut_name=dut_name)

    # There are theree ways to manage DUT lease.
    # * DLS (device lease service):
    #   Lower-layer DUT management service. Manages the leases. Provides
    #   "ExtendLease" API.
    # * FrontDoor service:
    #   Higher-layer DUT management service. Manages the leases by calling DLS
    #   internally. Does not provides an API to extend lease. Has the
    #   information of leased username.
    # * crosfleet command:
    #   Command line program. Calling FrontDoor service API.
    #
    # The problem here is
    # - lease-extension can be done only via DLS
    # - DLS and FD have their own databases
    # As the result, FD will have wrong expiration time after we extend the
    # lease via DLS, which doesn't update the information in FD database.
    #
    # So that we use:
    # 1) If the lease has never extended
    #    -> use the information from FD
    # 2) If the lease has extended
    #    -> use the information from DLS + lease user from FD
    #
    # TODO(yoshiki): consollidate to use DLS for the all DUT oparations.

    task_states_from_dls = _get_task_states_from_device_lease_service(dut_name)
    if not task_states_from_dls:
        # If DeviceLeaseService doesn't have any record, the DUT is not leased.
        status.is_leased = False
        return status

    status.lease_id_in_dls = task_states_from_dls['id']
    status.end_time = datetime.datetime.fromisoformat(
        task_states_from_dls['expirationTime']
    ).timestamp()

    if task_states_from_dls['releasedTime'] != '0001-01-01T00:00:00Z':
        # The DUT has been released.
        status.is_leased = False
        return status

    bisect_value = task_states_from_dls.get("userPayload", {}).get(
        'cros_bisect', ''
    )
    if bisect_value:
        # If the record in the DLS has the metadata, it means the lease was
        # extended before. In that case, the record in the scheduke may not
        # refrect actual (= extended) end-time, because the database in
        # scheduke was not changed after the extension. So we use the
        # information from DLS.
        # Note that: the record in DLS doesn't have a field of leasing user.
        status.is_leased = True
        return status

    # If not, the lease has not been extended. We use the information from the
    # scheduke to determine the leasing user.
    # TODO(yoshiki): rewrite the lease logic not to use the leasing user info.

    task_states = scheduke_util.get_task_states_from_frontdoor(
        users=[scheduke_util.get_active_gcloud_user()], device_names=[dut_name]
    )
    if task_states.tasks:
        status.is_leased = True
        status.task_id = task_states.tasks[0].task_state_id
        status.end_time = task_states.tasks[0].end_time / 1000000
    else:
        status.is_leased = False

    return status


def _notify_error_from_lease_keeper(exception, error_queue):
    error_queue.put(exception)
    # SIGINT will be translated to KeyboardInterrupt.
    os.kill(os.getpid(), signal.SIGINT)


def _cancel_lease_and_raise_exception(dut_name, error_message):
    logger.error(error_message)
    logger.error('cancel our recent lease task')
    crosfleet_release_dut(dut_name)
    raise errors.DutLeaseException(error_message)


def _get_task_states_from_device_lease_service(
    device_id: str,
):
    """Returns a ReadTaskStatesResponse for the given params."""
    method = 'chromiumos.test.api.DeviceLeaseService.ListDeviceLeases'
    content = {
        'device_id': device_id,
        'page_size': 100,
    }
    result = json.loads(device_lease_service_grpc(method, content))
    lease_records = result.get('leaseRecords', [])
    if lease_records:
        return lease_records[0]
    return None


def _extend_lease(lease_id, session_id):
    def _generate_bisector_identifier():
        """Generate the string representing the current bisector task.

        On prod/staging/dev environment, returns URL of the task.
        Otherwise, returns the string including the bisect session ID.
        """
        active_gcloud_user = scheduke_util.get_active_gcloud_user()
        if active_gcloud_user == PROD_SERVICE_ACCOUNT:
            return '%s%s' % (BISECTOR_HOST_PROD, session_id)
        if active_gcloud_user == STAGING_SERVICE_ACCOUNT:
            return '%s%s' % (BISECTOR_HOST_STAGING, session_id)
        if active_gcloud_user == DEV_SERVICE_ACCOUNT:
            return '%s%s' % (BISECTOR_HOST_DEV, session_id)
        return '%s%s:%s' % (
            BISECTOR_LOCAL_TEST_PREFIX,
            active_gcloud_user,
            session_id,
        )

    method = 'chromiumos.test.api.DeviceLeaseService.ExtendLease'
    content = {
        'idempotency_key': str(uuid.uuid4()),
        'lease_id': lease_id,
        # Extend the lease by 43200s = 12h.
        'extend_duration': '43200s',
        'user_payload': {
            # Add the metadata to mark this lease as a request by the bisector.
            'cros_bisect': _generate_bisector_identifier(),
        },
    }
    return json.loads(device_lease_service_grpc(method, content))


def lease_keeper(dut_name, reason, quit_event, error_queue, session_id):
    """Monitors and maintains skylab lease status.

    This function checks DUT lease status periodically in order to ensure the DUT
    is leased by me.
      - If the lease duration is near expiration, it will lease again beforehand.
      - If the DUT is not leased by me, it aborts current program.

    Args:
      dut_name: DUT name which is leased by me.
      reason: lease reason
      quit_event: a threading.Event object, which indicates program stop event.
      error_queue: a queue.Queue object, for passing exception to main thread.
      session_id: an ID of the current bisect task.
    """

    # TODO(b/394220130) Remove `reason` or use it for easier debugging.
    del reason

    try:
        previous_status: LeaseStatus | None = None
        while True:
            lease_status = query_lease_status(dut_name)
            if previous_status and (
                previous_status.lease_id_in_dls != lease_status.lease_id_in_dls
            ):
                # Checking if the latest lease is still by us. There are rare
                # cases where the lease was unexpectedly released. It's
                # probably due to the infra bug: b/379001772.
                if previous_status.task_id and lease_status.task_id:
                    previous_task_url = (
                        'https://chromeos-swarming.appspot.com/task?id=%s'
                        % previous_status.task_id
                    )
                    current_task_url = (
                        'https://chromeos-swarming.appspot.com/task?id=%s'
                        % lease_status.task_id
                    )
                    raise errors.DutLeaseException(
                        'the DUT was unexpectedly taken by another bisector '
                        'task (ID: %s, swarming: %s) before finishing the '
                        'current lease (ID: %s, swarming: %s)'
                        % (
                            previous_status.lease_id_in_dls,
                            previous_task_url,
                            lease_status.lease_id_in_dls,
                            current_task_url,
                        )
                    )
                raise errors.DutLeaseException(
                    'the DUT was unexpectedly taken by another task (ID: %s) '
                    'before finishing the current lease (ID: %s)'
                    % (
                        previous_status.lease_id_in_dls,
                        lease_status.lease_id_in_dls,
                    )
                )

            if not lease_status.is_leased:
                if previous_status and previous_status.task_id:
                    task_url = (
                        'https://chromeos-swarming.appspot.com/task?id=%s'
                        % previous_status.task_id
                    )
                    raise errors.DutLeaseException(
                        'the DUT was leased by me (ID: %s, swarming :%s), but '
                        'now is not unexpectedly: %s'
                        % (
                            previous_status.lease_id_in_dls,
                            task_url,
                            lease_status,
                        )
                    )
                raise errors.DutLeaseException(
                    'the DUT was expected to be leased by me, but now is not: %s'
                    % lease_status
                )
            previous_status = lease_status

            # We need buffer time (like querying lease status, network latency, etc.)
            # before actual lease request, 100 seconds must be enough.
            assert lease_status.end_time is not None
            to_lease_again = lease_status.end_time - 100

            now = time.time()
            if to_lease_again > now:
                # Verify the lease status as checkpoint every 10 minutes.
                wait_duration = min(600, to_lease_again - now)
                logger.debug(
                    'wait %.1f minutes to check lease status again',
                    wait_duration / 60,
                )
                quit_event.wait(wait_duration)
                if quit_event.is_set():
                    logger.debug('got quit_event, lease_keeper exit')
                    return
                continue

            result = _extend_lease(lease_status.lease_id_in_dls, session_id)

            if not result.get("leaseId", ""):
                raise errors.DutLeaseException(
                    f"Failed to extend lease: {result}"
                )

    except (
        errors.DutLeaseException,
        errors.ExternalError,
        subprocess.CalledProcessError,
    ) as e:
        logger.exception('swarming api failed or bad lease state, abort')
        _notify_error_from_lease_keeper(e, error_queue)
    except Exception as e:
        logger.exception('unhandled exception in lease keeper, abort')
        _notify_error_from_lease_keeper(e, error_queue)


@contextlib.contextmanager
def dut_lease_status_monitor(dut, reason, session_id):
    """Takes care DUT lease status.

    If the DUT is leased from skylab, this manager will spawn a thread to monitor
    lease status (see lease_keeper for detail) when the control flow is in the
    manager's scope.
    """
    if not is_lab_dut(dut):
        # not DUT in the lab, do nothing
        yield
        return

    host = dut_host_name(dut)
    if not is_skylab_dut(host):
        # not DUT in skylab, do nothing
        yield
        return

    quit_event = threading.Event()
    error_queue: queue.Queue[Exception] = queue.Queue()
    try:
        thread = threading.Thread(
            target=lease_keeper,
            args=(host, reason, quit_event, error_queue, session_id),
        )
        thread.start()
        yield
    except KeyboardInterrupt:
        try:
            e = error_queue.get_nowait()
            # Re-raise the actual exception got from lease_keeper thread.
            raise e from None
        except queue.Empty:
            # No exception from lease_keeper, do nothing
            pass
        raise
    finally:
        quit_event.set()
        logger.debug('wait keeper thread terminated')
        thread.join()
        logger.debug('keeper thread is terminated')


def reboot_via_servo(dut: str) -> bool:
    """Reboot a DUT in the lab via servo.

    When this function returns, the reboot process may not finish yet. The caller
    is responsible to wait boot complete.

    Args:
      dut: dut address

    Returns:
      True if reboot is triggered
    """
    if not is_lab_dut(dut):
        return False

    dut_info = crosfleet_dut_info(dut_host_name(dut))
    servo_host = dut_info.get('servo_hostname', None)
    servo_port = dut_info.get('servo_port', None)
    if not servo_host or not servo_port:
        logger.debug('this DUT has no servo?')
        return False

    try:
        util.ssh_cmd(servo_host, 'start', 'servod', 'PORT=' + servo_port)
    except subprocess.CalledProcessError:
        pass  # servod is already running

    util.ssh_cmd(
        servo_host,
        'servodutil',
        'wait-for-active',
        '-p',
        servo_port,
        connect_timeout=120,
    )
    util.ssh_cmd(
        servo_host, 'dut-control', '--port', servo_port, 'power_state:reset'
    )
    return True


def repair(dut: str, chromeos_root: str = ''):
    """Try to fix a DUT with different repair mechanisms.

    Note, repairing is unavailable to some DUTs due to infra or some other
    reasons.

    Args:
      dut: dut address
      chromeos_root: ChromeOS root

    Returns:
      True if repaired. False if failed or unable to repair.
    """
    if not is_lab_dut(dut):
        logger.warning('%s is not DUT in lab; skip repair', dut)
        return False

    try:
        logger.debug('Reset using servo for repair')
        # Perform a normal reboot to avoid servod command failure
        cros_util.reboot(dut)
        reboot_via_servo(dut)
        cros_util.wait_reboot_done(dut)
    except (errors.SshConnectionError, subprocess.CalledProcessError) as e:
        logger.error('reboot via servo failed %s', e)
        return False

    if cros_util.is_good_dut(dut):
        return True

    if chromeos_root == '':
        chromeos_root = common.get_default_chromeos_root()

    logger.debug('Reflashing the DUT with existing ChromeOS image for repair')
    reflash_dut(dut, chromeos_root)
    if cros_util.is_good_dut(dut):
        return True

    # TODO(kcwu): support 'skylab repair'
    logger.warning('skylab repair is not implemented yet')
    return False


def write_satlab_ssh_config(satlab_dut: str, satlab_ip: str = ''):
    """Write the required ssh config for sshing into a satlab dut

    Args:
        satlab_dut: The satlab dut host names
        satlab_ip: The satlab ip if already known
    """
    ssh_config_template_path = os.path.expanduser('~/.ssh/config_template')
    ssh_config_path = os.path.expanduser('~/.ssh/config')
    if os.path.isfile(ssh_config_template_path):
        shutil.copy(ssh_config_template_path, ssh_config_path)
        os.chmod(ssh_config_path, 0o600)
    else:
        raise errors.InternalError(
            "~/.ssh/config_template file not found which is required for writing satlab ssh config"
        )

    ssh_config = sshconf.read_ssh_config(ssh_config_path)

    if 'satlab' not in ssh_config.hosts():
        raise errors.InternalError(
            "The ssh config for satlab is not deployed yet"
        )

    # Get the satlab ip if it is unknown
    if satlab_ip == '':
        satlab_ip = swarming_dut_satlab_ip(satlab_dut)
    logger.debug('Satlab ip: %s', satlab_ip)

    store_known_satlab_prefix(satlab_dut, satlab_ip)

    ssh_config.set('satlab', Hostname=satlab_ip)

    ssh_config.save()


def is_rootfs_verification_on(dut: str) -> bool:
    """Check if a DUT has rootfs verification on

    Args:
        dut: dut host name

    Returns:
        True if the rootfs verificaion is enabled on the DUT

    Raises:
        subprocess.CalledProcessError or errors.SshConnectionError in case of ssh failure
    """
    output = util.ssh_cmd(dut, "rootdev")
    if output.startswith("/dev/dm"):
        return True
    return False


def reflash_dut(
    dut: str, chromeos_root: str, disable_rootfs_verification: bool = False
) -> bool:
    """Reflashes the DUT with existing chromeos Image

    Args:
        dut: dut host name
        chromeos_root: ChromeOS root
        disable_rootfs_verification: flag to specify if root verfication needs to be disabled

    Retruns:
        True if the reflash is successful, False otherwise
    """
    logger.info('Trying to reflash the DUT with existing image')
    try:
        cros_short_version = cros_util.query_dut_prebuilt_version(dut)
        dut_board = cros_util.query_dut_board(dut)
        cros_full_version = cros_util.version_to_full(
            dut_board, cros_short_version
        )
        image_info = cros_util.search_image(
            dut_board,
            cros_short_version,
            # TODO(b/441146529): Implement public board bisection support.
            is_public_build=False,
        )
    except (subprocess.CalledProcessError, errors.SshConnectionError):
        logger.exception('Failed to get DUT information')
        return False

    try:
        cros_util.provision_image(
            chromeos_root,
            dut,
            dut_board,
            image_info,
            # TODO(b/441146529): Implement public board bisection support.
            is_public_build=False,
            version=cros_full_version,
            disable_rootfs_verification=disable_rootfs_verification,
        )
    except errors.ExternalError:
        logger.exception('Reflash DUT with existing image failed')
        return False

    return True


def enable_rootfs_verification(dut: str, chromeos_root: str) -> bool:
    """Enable rootfs verification on a DUT

    Args:
        dut: dut host name
        chromeos_root: ChromeOS root

    Returns:
        True if the rootfs verificaion is enabled on the DUT, False otherwise
    """
    try:
        if is_rootfs_verification_on(dut):
            logger.debug('Rootfs verification is already on')
            return True
    except (subprocess.CalledProcessError, errors.SshConnectionError):
        logger.debug('Failed to get rootfs verification info from DUT')

    start_time = timeit.default_timer()
    logger.debug('Trying to enable rootfs verification on the DUT')
    if not reflash_dut(dut, chromeos_root):
        logger.exception('Enable rootfs verification failed')
        return False
    end_time = timeit.default_timer()
    time_elapsed_in_minutes = (end_time - start_time) / 60
    logger.debug(
        'Enabled rootfs verifaction on in %.2f minutes', time_elapsed_in_minutes
    )
    return True
