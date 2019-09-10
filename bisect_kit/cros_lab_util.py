# -*- coding: utf-8 -*-
# Copyright 2018 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Module to manipulate machines in chromeos test lab."""
from __future__ import print_function
import collections
import contextlib
import errno
import logging
import subprocess

from bisect_kit import cros_util
from bisect_kit import errors
from bisect_kit import util

logger = logging.getLogger(__name__)

# Magic keyword for automatic DUT allocation.
LAB_DUT = ':lab:'

# DUT state classification for allocation:
#  - ready state: desired
#  - good states: lock and wait
#  - transient states: wait
#  - bad states: ignore them
# For locked hosts, treat them as transient or bad state depending on locked
# by whom.
LOCKED_BY_US = 'locked-by-us'
LOCKED_BY_OTHERS = 'locked-by-others'
READY_STATE = 'Ready'
GOOD_STATES = ['Running']
TRANSIENT_STATES = [
    'Verifying', 'Pending', 'Provisioning', 'Repairing', 'Resetting',
    LOCKED_BY_US
]
BAD_STATES = ['Repair Failed', LOCKED_BY_OTHERS]


def is_lab_dut(dut):
  return dut.endswith('.cros')


def dut_host_name(dut):
  """Converts DUT address to host name.

  Args:
    dut: DUT address; assume the address is returned from this module
  """
  assert dut.endswith('.cros')
  return dut[:-5]


def atest_cmd(topic, action, *args):
  """Wrapper of autotest cli 'atest'.

  Args:
    topic: atest topic, like 'host', 'label', etc.
    action: action on the `topic`, like 'list', 'add', etc.
    args: additional arguments passed to atest command line

  Returns:
    content of stdout
  """
  try:
    return util.check_output('atest', topic, action, *args)
  except OSError as e:
    if e.errno == errno.ENOENT:
      logger.error('"atest" not installed? see go/lab-tools for setup steps')
    raise errors.ExternalError(str(e))


def list_host(host=None, labels=None, statuses=None, locked=None):
  """List DUTs with criteria in the lab.

  Args:
    host: matching from the given DUT only, if not None
    labels: only list hosts with given criteria of DUT label(s)
    statuses: only list hosts with given criteria of DUT status (list)
    locked: only list locked/unlocked hosts if `locked` is True or False.
        Default (None) is don't care.

  Returns:
    A dict mapping host name to host info. "host info" is parsed dict of 'atest
    host list'. Field 'Labels' is parsed into set, 'Locked' is normalized to
    bool; other fields are plain strings. For example:

    {
      'chromeos1-row1-rack1-host1': {
        'Status': 'Ready',
        'Locked': False,
        'Locked by': 'None',
        'Labels': set(['board:daisy', 'webcam', 'servo', 'pool:suites',
                       'sku:snow_arm_v7l_rev4_2Gb']),
      },
      'chromeos1-row1-rack1-host2': ...,
    }
  """
  atest_args = ['--parse']
  if host:
    atest_args.append(host)
  if labels:
    atest_args.append('--label=' + ','.join(labels))
  if statuses:
    atest_args.append('--status=' + ','.join(statuses))
  if locked is not None:
    if locked:
      atest_args.append('--locked')
    else:
      atest_args.append('--unlocked')

  result = {}
  for line in atest_cmd('host', 'list', *atest_args).splitlines():
    entry = {}
    for item in line.split('|'):
      key, value = item.split('=', 1)
      if key == 'Labels':
        value = set(value.split(', '))
      elif key == 'Locked':
        value = (value == 'True')
      elif key == 'Shard':
        if value == 'None':
          value = None
      entry[key] = value
    result[entry['Host']] = entry

  return result


def lock_host(host, reason):
  """Locks a host in the lab.

  Args:
    host: host name
    reason: lock reason

  Returns:
    host info of locked host
  """
  atest_cmd('host', 'mod', '--lock', '--lock_reason', reason, host)

  hosts = list_host(host=host)
  info = hosts[host]

  # make sure it's locked by me
  assert info['Lock Reason'] == reason

  return info


def unlock_host(host):
  """Unlocks host in the lab.

  Args:
    host: host name
  """
  atest_cmd('host', 'mod', '--unlock', host)


def make_lock_reason(session):
  return 'bisect-kit: %s' % session


def is_locked_by_us(info):
  return (info.get('Locked') and
          info.get('Lock Reason', '').startswith('bisect-kit'))


def _host_priority(pool_labels, info):
  """Determine host priority according to status and pool.

  The priority is:
    ready > good states > transient states
    and tie-breaking by pool

  Args:
    pool_labels: list of desired pool labels
    info: host info

  Returns:
    Priority. Lower value means higher priority.
  """
  state = normalize_host_state(info)

  pool_priority = None
  for i, label in enumerate(pool_labels):
    for host_label in info['Labels']:
      if label.lower() == host_label.lower():
        pool_priority = i
        break
  assert pool_priority is not None

  if state == READY_STATE:
    return 0, pool_priority
  if state in GOOD_STATES:
    return 1, GOOD_STATES.index(state), pool_priority
  if state in TRANSIENT_STATES:
    return 2, TRANSIENT_STATES.index(state), pool_priority

  assert state not in BAD_STATES, 'bad states should not reach this function'

  # unknown state
  logger.warning('Unknown state: %s', state)
  return 3, pool_priority


def normalize_host_state(info):
  if info['Locked'] and info['Status'] not in BAD_STATES:
    if is_locked_by_us(info):
      return LOCKED_BY_US
    else:
      return LOCKED_BY_OTHERS
  return info['Status']


def seek_host(pools, model=None, sku=None, extra_labels=None):
  """Seeks DUT candidate to use in the lab.

  Args:
    pools: search criteria: host must be in the given pools; ordered by priority
    model: allocation criteria: chromeos device model name
    sku: allocation criteria: device sku
    extra_labels: more allocation criteria on labels

  Returns:
    potential DUT candidates (list of host info), which are either ready to use
    or need waiting; ordered by priority. Empty list if all hosts meet the
    criteria are in bad state.

  Raises:
    errors.ExternalError: no hosts in the given pools (no matter any states)
  """
  pool_labels = []

  if not pools:
    raise ValueError('pools should not be empty')
  for pool in pools:
    pool_labels.append('pool:' + pool)

  labels = []
  if model:
    labels.append('model:' + model)
  if sku:
    labels.append('sku:' + sku)
  if extra_labels:
    labels += extra_labels

  hosts = {}
  for pool_label in pool_labels:
    hosts.update(list_host(labels=labels + [pool_label]))

  if not hosts:
    logger.error('no hosts meet requirements: %s', labels)
    raise errors.ExternalError(
        'no DUTs (%s) in pool=%s; incorrect model or sku name?' % (labels,
                                                                   pools))

  candidates = []
  status_counter = collections.Counter()
  for info in hosts.values():
    state = normalize_host_state(info)
    status_counter[state] += 1
    if state not in BAD_STATES:
      candidates.append(info)
  logger.info('host status: %s', status_counter)

  candidates.sort(key=lambda info: _host_priority(pool_labels, info))
  return candidates


@contextlib.contextmanager
def dut_manager(dut, allocator):
  """Context manager to handle host allocation.

  If `dut` is magic value, ":lab:", the context manager will call `allocator`
  to allocate a host and unlock the host upon leaving the scope.

  For other value of `dut`, the context manager simply yields `dut` and does
  nothing else.

  Args:
    dut: dut address
    allocator: host allocation callback function

  Returns:
    context manager, which lock and unlock host if necessary
  """
  if dut == LAB_DUT:
    host = allocator()
    if not host:
      yield None
      return

    try:
      # Append (partial) domain name of the lab
      yield host + '.cros'
    finally:
      logger.info('auto unlock DUT %s', host)
      unlock_host(host)
  else:
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
  logger.info('repair %s', dut)

  host = dut_host_name(dut)
  hosts = list_host(host=host)
  info = hosts[host]

  board = None
  model = None
  for label in info['Labels']:
    if label.startswith('board:'):
      board = label[6:]
    if label.startswith('model:'):
      model = label[6:]
  if not board or not model:
    logger.error(
        'Necessary information unavailable (board=%r model=%r), '
        'unable to repair ', board, model)
    return False

  try:
    # Should specify DUT hostname (without .cros suffix), otherwise it would
    # confuse AFE crbug/967609.
    result = util.check_output('deploy', 'repair', '--board', board, '--model',
                               model, host)
  except subprocess.CalledProcessError:
    logger.exception('deploy repair failed')
    return False
  if '0 failures' not in result:
    logger.error('deploy repair failed')
    return False

  assert cros_util.is_good_dut(dut)
  return True
