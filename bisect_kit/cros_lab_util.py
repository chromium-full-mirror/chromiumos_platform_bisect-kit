# -*- coding: utf-8 -*-
# Copyright 2018 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Module to manipulate machines in chromeos test lab."""
from __future__ import print_function
import calendar
import contextlib
import errno
import json
import logging
import os
import re
import signal
import subprocess
import threading
import time

from six.moves import urllib

from bisect_kit import errors
from bisect_kit import util

logger = logging.getLogger(__name__)

# Magic keyword for automatic DUT allocation.
LAB_DUT = ':lab:'

SWARMING_SERVER = 'https://chromeos-swarming.appspot.com'
# Use 2800 minutes (roughly 2 days) because the limit is not clear
# (crbug/1011612).
LEASE_DURATION = 2800 * 60
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


def is_lab_dut(dut):
  return dut.endswith('.cros')


def dut_host_name(dut):
  """Converts DUT address to host name.

  Args:
    dut: DUT address; assume the address is returned from this module
  """
  assert dut.endswith('.cros')
  return dut[:-5]


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
      logger.error('"%s" not installed? see go/lab-tools for setup steps', name)
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


def swarming_query(method, limit=None):
  """Wrapper of 'swarming.py query'.

  This is the lower level query interface. You can see available methods by
  running `swarming.py query-list`.

  Args:
    method: the API method
    limit: limit number of result

  Returns:
    result dict; the content is defined by each method call.
  """
  cmd = ['swarming.py', 'query', '--swarming', SWARMING_SERVER, method]
  if limit:
    cmd += ['--limit', str(limit)]
  data = util.check_output(*cmd)
  return json.loads(data)


def swarming_cancel(task_id):
  """Cancels swarming task.

  Args:
    task_id: task id
  """
  util.check_call('swarming.py', 'cancel', '--swarming', SWARMING_SERVER,
                  task_id)


def _simplify_bot_dimensions(bot):
  # Convert list to dict, so it's easier to access.
  dimensions = {}
  for dimension in bot['dimensions']:
    assert dimension['key'] not in dimensions
    dimensions[dimension['key']] = dimension['value']
  bot['dimensions'] = dimensions


def swarming_bots_list(dimensions, is_busy=None):
  """Lists swarming bots.

  Args:
    dimensions: dimensions constraints (list)
    is_busy: filter by "is_busy" state; None means don't care

  Returns:
    list of bots dict
  """
  method = 'bots/list'
  query = [('is_dead', 'FALSE'), ('quarantined', 'FALSE')]
  query += [('dimensions', d) for d in dimensions]
  if is_busy is not None:
    query.append(('is_busy', 'TRUE' if is_busy else 'FALSE'))

  if query:
    # doseq=True because dimensions could be multiple.
    # pylint: disable=redundant-keyword-arg
    method += '?' + urllib.parse.urlencode(query, doseq=True)

  bots = []
  for bot in swarming_query(method).get('items', []):
    _simplify_bot_dimensions(bot)
    bots.append(bot)
  return bots


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


def swarming_task_request(task_id):
  """Gets the request of given task.

  Args:
    task_id: task id

  Returns:
    task request dict
  """
  assert task_id
  method = 'task/%s/request' % task_id
  return swarming_query(method)


def swarming_task_result(task_id):
  """Gets the current result of given task.

  Args:
    task_id: task id

  Returns:
    task result dict
  """
  assert task_id
  method = 'task/%s/result' % task_id
  return swarming_query(method)


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
    logger.debug('%s current task: %s, is not leased', status['dut_name'],
                 status['task_name'])
    return False

  if not status['is_leased_by_me']:
    logger.debug('%s is not leased by me', status['dut_name'])
    return False

  return True


def skylab_lease_dut(dut_name, duration=None, timeout=None):
  """Lease DUT from skylab.

  Args:
    dut_name: DUT name
    duration: lease duration, in seconds. Default is LEASE_DURATION if None.
    timeout: lease wait limit, in seconds.

  Returns:
    dict of lease status if successful, otherwise None
  """
  logger.debug('skylab_lease_dut %s', dut_name)
  if not duration:
    duration = LEASE_DURATION
  stdout_lines = []
  stderr_lines = []
  orphan_task = True
  try:
    skylab_cmd(
        'lease-dut',
        '-minutes',
        '%d' % (duration // 60),
        dut_name,
        stdout_callback=stdout_lines.append,
        stderr_callback=stderr_lines.append,
        timeout=timeout)
    orphan_task = False
  except subprocess.CalledProcessError:
    stderr = ''.join(stderr_lines)
    if 'EXPIRED' in stderr:
      logger.warning('unable to lease DUT within time limit')
      orphan_task = False
      return None
    logger.exception('skylab lease-dut failed')
    raise
  finally:
    if orphan_task:
      m = re.search(r'Created lease task.*task\?id=(\w+)',
                    ''.join(stdout_lines))
      if m:
        task_id = m.group(1)
        logger.info('cancel the lease task %s', task_id)
        swarming_cancel(task_id)

  # wait the state propagated
  for _ in range(10):
    time.sleep(5)
    status = query_lease_status(dut_name)
    if is_dut_leased_by_me(status):
      break
  else:
    raise errors.DutLeaseException('lease verification failed: %s' % status)

  logger.info('leased dut %s for %.1f hours', dut_name, duration // 60 / 60.0)
  return status


def skylab_release_dut(dut_name):
  """Release DUT which was leased by me.

  Args:
    dut_name: DUT name
  """
  if not is_dut_leased_by_me(query_lease_status(dut_name)):
    raise errors.DutLeaseException(
        '%s is not leased, or not leased by me' % dut_name)

  skylab_cmd('release-duts', dut_name)

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


def query_lease_status(dut_name):
  """Query DUT lease status.

  Args:
    dut_name: DUT name

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

  bots = swarming_bots_list(['dut_name:' + dut_name])
  if not bots:
    raise errors.ExternalError('dut_name:%s not found' % dut_name)
  assert len(bots) == 1
  bot = bots[0]
  status['bot_id'] = bot['bot_id']
  status['task_name'] = bot.get('task_name')
  status['task_id'] = bot.get('task_id') or None  # convert empty string to None
  status['is_leased'] = (status['task_name'] == 'lease task')
  if not status['is_leased']:
    return status

  service_account_json = os.environ.get('SKYLAB_CLOUD_SERVICE_ACCOUNT_JSON')
  if service_account_json:
    service_account_data = json.load(open(service_account_json))
    user = service_account_data['client_email']
  else:
    user = os.environ['USER']
  assert user

  request = swarming_task_request(status['task_id'])
  status['leased_by'] = request['authenticated']
  status['is_leased_by_me'] = (user in status['leased_by'])
  status['duration'] = int(request['properties']['execution_timeout_secs'])

  result = swarming_task_result(status['task_id'])
  status['start_time'] = parse_timestamp_from_swarming(result['started_ts'])
  return status


def lease_keeper(dut_name, quit_event):
  """Monitors and maintains skylab lease status.

  This function checks DUT lease status periodically in order to ensure the DUT
  is leased by me.
    - If the lease duration is near expiration, it will lease again beforehand.
    - If the DUT is not leased by me, it aborts current program.

  Args:
    dut_name: DUT name which is leased by me.
    quit_event: a threading.Event object, which indicates program stop event.
  """

  try:
    previous_status = {}
    while True:
      lease_status = query_lease_status(dut_name)
      if not (lease_status['is_leased'] and lease_status['is_leased_by_me']):
        if previous_status and previous_status.get('task_id'):
          task_url = ('https://chromeos-swarming.appspot.com/task?id=%s' %
                      previous_status['task_id'])
          raise errors.DutLeaseException(
              'the DUT was leased by me %s , but now is not unexpectedly: %s' %
              (task_url, lease_status))
        else:
          raise errors.DutLeaseException(
              'the DUT is expected to be leased by me, but not: %s' %
              lease_status)
      previous_status = lease_status

      expire_time = lease_status['start_time'] + lease_status['duration']
      # We need buffer time (like querying lease status, network latency, etc.)
      # before actual lease request, 100 must be enough.
      to_lease_again = expire_time - 100

      now = time.time()
      if to_lease_again > now:
        # Verify the lease status as checkpoint every 10 minutes.
        wait_duration = min(600, to_lease_again - now)
        logger.debug('wait %.1f minutes to check lease status again',
                     wait_duration / 60)
        quit_event.wait(wait_duration)
        if quit_event.is_set():
          logger.debug('got quit_event, lease_keeper exit')
          return
        continue

      logger.info("Previous lease is near expiration, let's lease again")
      this_lease = skylab_lease_dut(dut_name)
      if not this_lease:
        raise errors.DutLeaseException('unable to extend the lease')

      # leased, but need to verify in case of race condition
      tasks = swarming_bot_tasks(lease_status['bot_id'], limit=5)
      assert len(tasks) >= 2
      assert tasks[0]['task_id'] == this_lease['task_id']
      for i, task in enumerate(tasks[1:], 1):
        if task['task_id'] == lease_status['task_id']:
          if i > 1:
            logger.error('detected race condition')
            logger.error('cancel our recent lease task')
            skylab_release_dut(dut_name)
            raise errors.DutLeaseException(
                'there are other tasks between our two leases; race condition?')
          break
      else:
        logger.error('not found previous lease task in task list')
        logger.error('cancel our recent lease task')
        skylab_release_dut(dut_name)
        raise errors.DutLeaseException(
            'not found previous lease task in task list')
  except errors.DutLeaseException:
    logger.exception('bad lease state, abort')
    # SIGINT will be translated to KeyboardInterrupt.
    os.kill(os.getpid(), signal.SIGINT)
    return


@contextlib.contextmanager
def dut_lease_manager(dut):
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
  try:
    thread = threading.Thread(target=lease_keeper, args=(host, quit_event))
    thread.start()
    yield
  finally:
    quit_event.set()
    logger.debug('wait keeper thread terminated')
    thread.join()
    logger.debug('keeper thread is terminated')


@contextlib.contextmanager
def dut_manager(dut, allocator):
  """Context manager to handle host allocation.

  If `dut` is magic value, ":lab:", the context manager will call `allocator`
  to allocate a host and release the host upon leaving the scope.

  For other value of `dut`, the context manager simply yields `dut` and does
  nothing else.

  Args:
    dut: dut address
    allocator: host allocation callback function

  Returns:
    context manager, which lease and release host if necessary
  """
  if dut == LAB_DUT:
    host = allocator()
    if not host:
      yield None
      return

    try:
      # Append (partial) domain name of the lab
      with dut_lease_manager(host + '.cros'):
        yield host + '.cros'
    finally:
      logger.info('auto release DUT %s', host)
      skylab_release_dut(host)
  else:
    with dut_lease_manager(dut):
      yield dut


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
