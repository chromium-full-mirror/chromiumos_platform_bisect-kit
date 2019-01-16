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

from bisect_kit import errors
from bisect_kit import util

logger = logging.getLogger(__name__)

# Magic keyword for automatic DUT allocation.
LAB_DUT = ':lab:'


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
    host list'. Field 'Labels' is parsed into set; other fields are plain
    strings. For example:

    {
      'chromeos1-row1-rack1-host1': {
        'Status': 'Ready',
        'Locked': 'False',
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


def host_priority(info, pool_labels):
  """Determine host priority according to pool preference.

  Args:
    info: host info
    pool_labels: list of desired pool labels

  Returns:
    Priority. Lower value means higher priority. None if not in `pool_labels`.
  """
  for i, label in enumerate(pool_labels):
    for host_label in info['Labels']:
      if label.lower() == host_label.lower():
        return i
  return None


def allocate_host(reason,
                  host=None,
                  model=None,
                  sku=None,
                  pools=None,
                  extra_labels=None):
  """Allocates a host from the lab for bisect.

  Args:
    reason: lock reason
    host: allocation criteria: host name. If specified, other criteria are
        ignored.
    model: allocation criteria: chromeos device model name
    sku: allocation criteria: device sku
    pools: allocation criteria: allocate hosts from given pools; ordered by
        priority
    extra_labels: more allocation criteria on labels

  Returns:
    host name in the lab; None if failed.
  """
  pool_labels = []

  if host:
    hosts = list_host(host=host)
    assert hosts
  else:
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
    for host, info in list_host(labels=labels).items():
      # Filters by desired pools.
      if host_priority(info, pool_labels) is None:
        continue
      hosts[host] = info

    if not hosts:
      logger.error('no hosts meet requirements: %s', labels)
      raise errors.ExternalError(
          'no DUTs (%s) in pool=%s; incorrect model or sku name?' % (labels,
                                                                     pools))

  candidate_hosts = {}
  status_counter = collections.Counter()
  for host, info in hosts.items():
    if info.get('Locked') == 'True':
      status_counter['Locked'] += 1
    else:
      status_counter[info['Status']] += 1
      if info['Status'] == 'Ready':
        candidate_hosts[host] = info
  logger.info('host status: %s', status_counter)
  if not candidate_hosts:
    logger.error('no unlocked hosts are in Ready state')
    return None

  target_host = sorted(
      candidate_hosts,
      key=lambda host: host_priority(candidate_hosts[host], pool_labels))[0]

  info = lock_host(target_host, reason)
  if info['Status'] != 'Ready':
    unlock_host(target_host)
    raise Exception(
        'unexpected host status=%s, a race condition?' % info['Status'])

  return target_host


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
