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
import errno
import json
import logging
import os
import queue
import re
import shutil
import signal
import subprocess
import threading
import time
import urllib.parse

from bisect_kit import common
from bisect_kit import cros_util
from bisect_kit import errors
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

SWARMING_CLIENT = os.path.expanduser('~/luci-py/client/swarming.py')
SWARMING_SERVER = 'https://chromeos-swarming.appspot.com'
LEASE_DURATION = 1439 * 60
LEASE_ATTEMPT_TIME = 600


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


def is_lab_dut(dut):
    if is_dut_needs_forwarded(dut):
        return True
    return dut.endswith('.cros')


def dut_host_name(dut):
    """Converts DUT address to host name (aka dut_name).

    Args:
      dut: DUT address; assume the address is returned from this module
    """
    if is_dut_needs_forwarded(dut):
        return dut
    assert dut.endswith('.cros')
    return dut[:-5]


def dut_name_to_address(dut_name):
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
        stdout_lines = []
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
    if service_account_json:
        args = ['-service-account-json', service_account_json] + list(args)
    return _run_labtools_command('skylab', cmd, *args, **kwargs)


def crosfleet_cmd(cmd, *args, **kwargs):
    """Wrapper of crosfleet cli.

    Args:
      cmd: crosfleet command, such as 'dut info'.
      args: additional arguments passed to crosfleet command line
      kwargs: additional arguments to control how to run the command
    """
    service_account_json = os.environ.get('SKYLAB_CLOUD_SERVICE_ACCOUNT_JSON')
    if service_account_json:
        args = ['-service-account-json', service_account_json] + list(args)
    cmd = cmd.split(' ')
    return _run_labtools_command('crosfleet', *cmd, *args, **kwargs)


def crosfleet_dut_info(dut_name):
    """Run crosfleet dut info.

    Args:
      dut_name: A skylab dut name without .cros suffix.
    """
    duts = json.loads(crosfleet_cmd('dut info', '-json', dut_name)).get(
        'DUTs', []
    )
    if len(duts) > 0:
        return duts[0]
    return None


def swarming_query(method, limit=None, verbose_log=True):
    """Wrapper of 'swarming.py query'.

    This is the lower level query interface. You can see available methods by
    running `swarming.py query-list`.

    Args:
      method: the API method
      limit: limit number of result
      verbose_log: log more if True

    Returns:
      result dict; the content is defined by each method call.
    """
    service_account_json = os.environ.get('SKYLAB_CLOUD_SERVICE_ACCOUNT_JSON')
    cmd = [SWARMING_CLIENT, 'query', '--swarming', SWARMING_SERVER, method]
    if not common.under_luci_context() and service_account_json:
        cmd += ['--auth-service-account-json', service_account_json]
    if limit:
        cmd += ['--limit', str(limit)]
    data = util.check_output(*cmd, retry=5, log_stdout=verbose_log)
    return json.loads(data)


def swarming_cancel(task_id):
    """Cancels swarming task.

    Args:
      task_id: task id
    """
    service_account_json = os.environ.get('SKYLAB_CLOUD_SERVICE_ACCOUNT_JSON')
    cmd = [
        SWARMING_CLIENT,
        'cancel',
        '--swarming',
        SWARMING_SERVER,
        '--kill-running',
        task_id,
    ]
    if not common.under_luci_context() and service_account_json:
        cmd += ['--auth-service-account-json', service_account_json]
    util.check_call(*cmd, retry=5)


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


def _simplify_bot_dimensions(dimensions_pairs):
    # Convert list to dict, so it's easier to access.
    dimensions = {}
    for dimension in dimensions_pairs:
        assert dimension['key'] not in dimensions
        dimensions[dimension['key']] = dimension['value']
    return dimensions


def swarming_bots_dimensions():
    method = 'bots/dimensions'
    bots_dimensions = swarming_query(method, verbose_log=False).get(
        'bots_dimensions', []
    )
    return _simplify_bot_dimensions(bots_dimensions)


def swarming_bots_list(
    dimensions,
    is_dead=False,
    quarantined=False,
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
    method = 'bots/list'
    query = [('dimensions', d) for d in dimensions]
    if is_dead is not None:
        query.append(('is_dead', 'TRUE' if is_dead else 'FALSE'))
    if quarantined is not None:
        query.append(('quarantined', 'TRUE' if quarantined else 'FALSE'))
    if is_busy is not None:
        query.append(('is_busy', 'TRUE' if is_busy else 'FALSE'))

    if query:
        # doseq=True because dimensions could be multiple.
        method += '?' + urllib.parse.urlencode(query, doseq=True)

    bots = []
    for bot in swarming_query(method, limit=limit, verbose_log=verbose_log).get(
        'items', []
    ):
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
        'drone_server dimension in swarming is not valid. This satlab does not have latest update. Trying with external_ip dimension.'
    )

    satlab_ip = bots[0]['external_ip']
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
    method = 'bot/%s/tasks' % bot_id
    return swarming_query(method, limit=limit).get('items', [])


def swarming_task_request(task_id, verbose_log=True):
    """Gets the request of given task.

    Args:
      task_id: task id
      verbose_log: log more if True

    Returns:
      task request dict
    """
    assert task_id
    method = 'task/%s/request' % task_id
    return swarming_query(method, verbose_log=verbose_log)


def swarming_task_result(task_id, verbose_log=True):
    """Gets the current result of given task.

    Args:
      task_id: task id
      verbose_log: log more if True

    Returns:
      task result dict
    """
    assert task_id
    method = 'task/%s/result' % task_id
    return swarming_query(method, verbose_log=verbose_log)


def is_skylab_dut(dut_name):
    """Determines whether the DUT is in skylab.

    Args:
      dut_name: the DUT name
    """
    # TODO(kcwu): unify host, dut, and dut_name once we completely migrated to
    #             skylab
    bots = swarming_bots_list(['dut_name:' + dut_name])
    return bool(bots)


def is_dut_leased_by_me(status):
    if not status['is_leased']:
        logger.debug(
            '%s current task: %s, is not leased',
            status['dut_name'],
            status['task_name'],
        )
        return False

    if not status['is_leased_by_me']:
        logger.debug('%s is not leased by me', status['dut_name'])
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
    m = re.search(r'.*(http\S+/builders/test_runner/dut_leaser/b(\d+))', text)
    if m:
        info['link'] = m.group(1)
        info['buildbucket_build_id'] = m.group(2)
    return info


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

    assert task_id or build_id, 'lease is pending but nothing to cancel'


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


async def async_lease(dut_name, reason, duration=None):
    if not duration:
        duration = LEASE_DURATION
    args = [
        '-minutes',
        '%d' % (duration // 60),
        '-reason',
        reason,
        '-host',
        dut_name_to_address(dut_name),
    ]
    proc = await create_async_crosfleet_proc(
        'dut lease',
        *args,
        # combine two streams, so the code becomes simpler
        stderr=subprocess.STDOUT,
    )

    pending_lease = None
    try:
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            line = line.decode('utf8')
            logger.debug('lease(%s): %s', dut_name, line)
            if not pending_lease:
                pending_lease = extract_lease_info(line)
            if 'INFRA_FAILURE' in line:
                pending_lease = None
                return None

        await proc.wait()
        if proc.returncode != 0:
            logger.warning(
                'lease(%s): crosfleet command failed: %s',
                dut_name,
                proc.returncode,
            )
            raise errors.ExternalError(
                'crosfleet command failed unexpectedly: %s' % proc.returncode
            )
        if not pending_lease:
            raise errors.InternalError(
                'not found lease info; crosfleet output format changed?'
            )

        if is_satlab_dut(dut_name):
            write_satlab_ssh_config(dut_name)

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
            status = query_lease_status(dut_name, verbose_log=False)
            if is_dut_leased_by_me(status):
                logger.info('lease: %s', pending_lease)
                break
        else:
            raise errors.DutLeaseException(
                'lease verification failed: %s' % status
            )

        pending_lease = None
        return dut_name
    except asyncio.CancelledError:
        logger.debug('lease(%s): got CancelledError', dut_name)
        raise
    except errors.DutLeaseException:
        logger.exception(
            'lease(%s): lease task state is unexpected. discard', dut_name
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
                proc.kill()
                await proc.wait()
            except ProcessLookupError:
                # The process may be already dead; ignore this error silently.
                # Especially, our code and the process got ctrl-c at the same time and
                # race.
                pass
        if pending_lease:
            cancel_pending_lease(pending_lease)


async def async_lease_with_retry(dut_name, reason, duration=None):
    tries = 0
    while True:
        try:
            tries += 1
            return await async_lease(dut_name, reason, duration)
        except errors.ExternalError:
            if tries < 3:
                delay = 60
                logger.warning(
                    'async_lease failed, will retry %d seconds later', delay
                )
                time.sleep(delay)
                continue
            raise


def make_lease_reason(session):
    # skylab only accepts reason up to 30 characters.
    return 'bisector: %s' % session[:18]


def crosfleet_lease_dut(dut_name, reason, duration=None, timeout=None):
    """Lease DUT from crosfleet.

    Args:
      dut_name: DUT name
      duration: lease duration, in seconds. Default is LEASE_DURATION if None.
      timeout: lease wait limit, in seconds.
      reason: lease reason

    Returns:
      dict of lease status if successful, otherwise None
    """
    logger.debug('crosfleet_lease_dut %s', dut_name)
    if not duration:
        duration = LEASE_DURATION
    stdout_lines = []
    stderr_lines = []
    orphan_task = True
    try:
        crosfleet_cmd(
            'dut lease',
            '-minutes',
            '%d' % (duration // 60),
            '-reason',
            reason,
            '-host',
            dut_name_to_address(dut_name),
            '-json',
            stdout_callback=stdout_lines.append,
            stderr_callback=stderr_lines.append,
            timeout=timeout,
        )

        pending_lease = extract_lease_info(''.join(stderr_lines))
        # wait the state propagated
        for _ in range(10):
            time.sleep(5)
            status = query_lease_status(dut_name)
            if is_dut_leased_by_me(status):
                logger.info('lease: %s', pending_lease)
                break
        else:
            raise errors.DutLeaseException(
                'lease verification failed: %s' % status
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

    logger.info('leased dut %s for %.1f hours', dut_name, duration // 60 / 60.0)
    return status


def crosfleet_release_dut(dut_name):
    """Release DUT which was leased by me.

    Args:
      dut_name: DUT name
    """
    if not is_dut_leased_by_me(query_lease_status(dut_name)):
        raise errors.DutLeaseException(
            '%s is not leased, or not leased by me' % dut_name
        )

    crosfleet_cmd('dut abandon', dut_name)

    # wait the state propagated
    for _ in range(10):
        time.sleep(5)
        if not is_dut_leased_by_me(query_lease_status(dut_name)):
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
    return calendar.timegm(time.strptime(timestamp, '%Y-%m-%dT%H:%M:%S.%f'))


def query_lease_status(dut_name, verbose_log=True):
    """Query DUT lease status.

    Args:
      dut_name: DUT name
      verbose_log: log more if True

    Returns:
      a dict indicates the detailed lease status:
        bot_id: the bot id of given `dut_name`
        task_name: current task name or None
        task_id: current task id or None
        is_leased: whether the DUT is leased now (bool). If false, following
            fields are not available.
        leased_by: user id
        is_leased_by_me: whether is leased by current account
        duration: lease duration in seconds
        start_time: lease start timestamp
    """
    status = {'dut_name': dut_name}

    bots = swarming_bots_list(['dut_name:' + dut_name], verbose_log=False)
    if not bots:
        raise errors.DutLeaseException('dut_name:%s not found' % dut_name)
    assert len(bots) == 1
    bot = bots[0]
    status['bot_id'] = bot['bot_id']
    status['task_name'] = bot.get('task_name')
    status['task_id'] = (
        bot.get('task_id') or None
    )  # convert empty string to None
    status['is_leased'] = (
        status['task_name'] == 'lease task'
        or status['task_name']
        and 'dut_leaser' in status['task_name']
    )
    if not status['is_leased']:
        return status

    service_account_json = os.environ.get('SKYLAB_CLOUD_SERVICE_ACCOUNT_JSON')
    if service_account_json:
        with open(service_account_json) as f:
            service_account_data = json.load(f)
        user = service_account_data['client_email']
    else:
        user = os.environ['USER']
    assert user

    request = swarming_task_request(status['task_id'], verbose_log=verbose_log)
    status['leased_by'] = request['authenticated']
    status['is_leased_by_me'] = bool(status['leased_by'])
    status['duration'] = int(request['properties']['execution_timeout_secs'])

    result = swarming_task_result(status['task_id'], verbose_log=verbose_log)
    status['start_time'] = parse_timestamp_from_swarming(result['started_ts'])
    return status


def _notify_error_from_lease_keeper(exception, error_queue):
    error_queue.put(exception)
    # SIGINT will be translated to KeyboardInterrupt.
    os.kill(os.getpid(), signal.SIGINT)


def lease_keeper(dut_name, reason, quit_event, error_queue):
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
    """

    try:
        previous_status = {}
        while True:
            lease_status = query_lease_status(dut_name, verbose_log=False)
            if not (
                lease_status['is_leased'] and lease_status['is_leased_by_me']
            ):
                if previous_status and previous_status.get('task_id'):
                    task_url = (
                        'https://chromeos-swarming.appspot.com/task?id=%s'
                        % previous_status['task_id']
                    )
                    raise errors.DutLeaseException(
                        'the DUT was leased by me %s , but now is not unexpectedly: %s'
                        % (task_url, lease_status)
                    )
                raise errors.DutLeaseException(
                    'the DUT is expected to be leased by me, but not: %s'
                    % lease_status
                )
            previous_status = lease_status

            expire_time = lease_status['start_time'] + lease_status['duration']
            # We need buffer time (like querying lease status, network latency, etc.)
            # before actual lease request, 100 must be enough.
            to_lease_again = expire_time - 100

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

            logger.info("Previous lease is near expiration, let's lease again")
            this_lease = crosfleet_lease_dut(dut_name, reason)
            if not this_lease:
                raise errors.DutLeaseException(
                    'Lease is expired and unable to extend the lease'
                )

            # leased, but need to verify in case of race condition
            tasks = swarming_bot_tasks(lease_status['bot_id'], limit=5)
            assert len(tasks) >= 2
            assert tasks[0]['task_id'] == this_lease['task_id']
            for i, task in enumerate(tasks[1:], 1):
                if task['task_id'] == lease_status['task_id']:
                    if i > 1:
                        logger.error('detected race condition')
                        logger.error('cancel our recent lease task')
                        crosfleet_release_dut(dut_name)
                        raise errors.DutLeaseException(
                            'there are other tasks between our two leases; race condition?'
                        )
                    break
            else:
                logger.error('not found previous lease task in task list')
                logger.error('cancel our recent lease task')
                crosfleet_release_dut(dut_name)
                raise errors.DutLeaseException(
                    'not found previous lease task in task list'
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
def dut_lease_manager(dut, reason):
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
    error_queue = queue.Queue()
    try:
        thread = threading.Thread(
            target=lease_keeper, args=(host, reason, quit_event, error_queue)
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


@contextlib.contextmanager
def dut_manager(dut, reason, allocator):
    """Context manager to handle host allocation.

    If `dut` is magic value, ":lab:", the context manager will call `allocator`
    to allocate a host and release the host upon leaving the scope.

    For other value of `dut`, the context manager simply yields `dut` and does
    nothing else.

    Args:
      dut: dut address
      reason: lease reason
      allocator: host allocation callback function.
        If None, defer dut allocation.

    Raise:
      errors.NoDutAvailable() if failed to allocate a DUT.

    Yields:
      The allocated DUT or ":lab:" if defer allocation.
    """
    if dut == LAB_DUT:
        # Defer allocation.
        if not allocator:
            yield LAB_DUT
            return

        host = allocator()
        if not host:
            raise errors.NoDutAvailable('unable to allocate DUT')

        try:
            # Append (partial) domain name of the lab
            with dut_lease_manager(dut_name_to_address(host), reason):
                yield dut_name_to_address(host)
        finally:
            logger.info('auto release DUT %s', host)
            crosfleet_release_dut(host)
    else:
        with dut_lease_manager(dut, reason):
            yield dut


def reboot_via_servo(dut):
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

    servo_info = util.dict_get(
        crosfleet_dut_info(dut_host_name(dut)),
        'LabSetup',
        'chromeosMachineLse',
        'deviceLse',
        'dut',
        'peripherals',
        'servo',
    )
    servo_host = util.dict_get(servo_info, 'servoHostname')
    servo_port = util.dict_get(servo_info, 'servoPort')
    if not servo_host or not servo_port:
        logger.debug('this DUT has no servo?')
        return False

    servo_port = str(servo_port)
    try:
        util.ssh_cmd(servo_host, 'start', 'servod', 'PORT=' + servo_port)
    except subprocess.CalledProcessError:
        pass  # servod is already running

    util.ssh_cmd(servo_host, 'servodutil', 'wait-for-active', '-p', servo_port)
    util.ssh_cmd(
        servo_host, 'dut-control', '--port', servo_port, 'power_state:reset'
    )
    return True


def repair(dut):
    """Requests lab automatic DUT repairing.

    Note, repairing is unavailable to some DUTs due to infra or some other
    reasons.

    Args:
      dut: dut address

    Returns:
      True if repaired. False if failed or unable to repair.
    """
    if not is_lab_dut(dut):
        logger.warning('%s is not DUT in lab; skip repair', dut)
        return False
    # TODO(kcwu): support 'skylab repair'
    logger.warning('skylab repair is not implemented yet')
    return False


def write_satlab_ssh_config(satlab_dut: str, satlab_ip: str = ''):
    '''Write the required ssh config for sshing into a satlab dut

    Args:
        satlab_dut: The satlab dut host names
        satlab_ip: The satlab ip if already known
    '''
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
