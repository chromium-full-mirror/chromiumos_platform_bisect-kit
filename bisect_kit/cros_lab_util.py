# -*- coding: utf-8 -*-
# Copyright 2018 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Module to manipulate machines in chromeos test lab."""
from __future__ import print_function
import contextlib
import errno
import logging

from bisect_kit import util

logger = logging.getLogger(__name__)

# Magic keyword for automatic DUT allocation.
LAB_DUT = ':lab:'


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
  except OSError, e:
    if e.errno == errno.ENOENT:
      logger.error('"atest" not installed? see go/lab-tools for setup steps')
    raise


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
  """
  atest_cmd('host', 'mod', '--lock', '--lock_reason', reason, host)

  # make sure it's locked by me
  hosts = list_host(host=host)
  assert hosts[host]['Lock Reason'] == reason


def unlock_host(host):
  """Unlocks host in the lab.

  Args:
    host: host name
  """
  atest_cmd('host', 'mod', '--unlock', host)


def allocate_host(reason, board=None, sku=None, extra_labels=None):
  """Allocates a host from the lab for bisect.

  Args:
    reason: lock reason
    board: allocation criteria: chromeos board name;
        not used if `sku` is not None.
    sku: allocation criteria: machine sku; `sku` will override `board`.
    extra_labels: more allocation criteria on labels

  Returns:
    host name in the lab; None if failed.
  """
  labels = ['pool:suites']
  if sku:
    labels.append('sku:' + sku)
  else:
    assert board
    labels.append('board:' + board)

  if extra_labels:
    labels += extra_labels

  hosts = list_host(labels=labels, locked=False, statuses=['Ready'])
  if not hosts:
    logger.error('no hosts meet requirements')
    return None
  dut = hosts.popitem()[1]

  lock_host(dut['Host'], reason)
  return dut['Host']


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
