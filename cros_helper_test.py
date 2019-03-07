# -*- coding: utf-8 -*-
# Copyright 2019 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Test cros_helper.py script"""

from __future__ import print_function
import argparse
import unittest

import mock

from bisect_kit import errors
import cros_helper


# Make sure we mocked all calls to atest.
@mock.patch('bisect_kit.cros_lab_util.atest_cmd', None)
class TestCmdAllocateDut(unittest.TestCase):
  """Test cmd_allocate_dut.

  Because cmd_allocate_dut() is command line entry point and catches all
  exceptions, we test its testing friendly helper, do_allocate_dut(), instead.
  """

  def setUp(self):
    self.session = 'foo'
    self.hosts = []

    # Prepare fake data.
    self.ready_host = {
        'Host': 'ReadyHost',
        'Labels': set(['sku:S1', 'pool:P1']),
        'Locked': False,
        'Status': 'Ready',
    }
    self.running_host = {
        'Host': 'RunningHost',
        'Labels': set(['sku:S1', 'pool:P1']),
        'Locked': False,
        'Status': 'Running',
    }
    self.bad_host = {
        'Host': 'BadHost',
        'Labels': set(['sku:S1', 'pool:P1']),
        'Locked': False,
        'Status': 'Repair Failed',
    }
    self.resetting_host = {
        'Host': 'ResettingHost',
        'Labels': set(['sku:S1', 'pool:P1']),
        'Locked': False,
        'Status': 'Resetting',
    }

    # Patch functions used by this test suite.
    patcher = mock.patch('bisect_kit.cros_lab_util.lock_host')
    self.lock_host = patcher.start()
    self.addCleanup(patcher.stop)
    patcher = mock.patch('bisect_kit.cros_lab_util.unlock_host')
    self.unlock_host = patcher.start()
    self.addCleanup(patcher.stop)
    patcher = mock.patch('bisect_kit.cros_lab_util.list_host')
    self.list_host = patcher.start()
    self.addCleanup(patcher.stop)

    # Default fake implementations.
    # Each test case may override if necessary.
    self.list_host.side_effect = self._list_host
    self.lock_host.side_effect = self._lock_host
    self.unlock_host.side_effect = self._unlock_host

  def _create_opts(self, locked_dut=None):
    # Other options are irrelevant to the main logic; fill with dummy values.
    return argparse.Namespace(
        sku='S1',
        session=self.session,
        pools='P1',
        model=None,
        locked_dut=locked_dut,
        label=None)

  def _list_host(self, host=None, labels=None, statuses=None, locked=None):
    result = {}
    for info in self.hosts:
      if host is not None and info['Host'] != host:
        continue
      if statuses is not None and info['Status'] not in statuses:
        continue
      if locked is not None and info['Locked'] != locked:
        continue
      if labels and not set(labels).issubset(info['Labels']):
        continue
      result[info['Host']] = info
    return result

  def _lock_host(self, host, reason):
    for info in self.hosts:
      if info['Host'] == host:
        assert not info['Locked']
        info.update({'Locked': True, 'Lock Reason': reason})
        return info
    raise ValueError('cannot lock non-existent host: ' + host)

  def _unlock_host(self, host):
    for info in self.hosts:
      if info['Host'] == host:
        assert info['Locked']
        info.update({'Locked': False, 'Lock Reason': None})
        return info
    raise ValueError('cannot unlock non-existent host: ' + host)

  def _change_and_lock(self, **kwargs):

    def func(host, reason):
      for info in self.hosts:
        if info['Host'] != host:
          continue
        for k, v in kwargs.items():
          info[k] = v
      return self._lock_host(host, reason)

    return func

  def test_dut_available(self):
    opts = self._create_opts(locked_dut=None)

    self.hosts = [self.ready_host]
    self.assertEqual(
        cros_helper.do_allocate_dut(opts), ('ready', self.ready_host['Host']))
    self.assertTrue(self.ready_host['Locked'])

  def test_all_bad(self):
    opts = self._create_opts(locked_dut=None)

    self.hosts = [self.bad_host]
    with self.assertRaises(errors.NoDutAvailable):
      cros_helper.do_allocate_dut(opts)

  def test_wrong_sku(self):
    opts = self._create_opts(locked_dut=None)

    self.ready_host['Labels'].remove('sku:S1')
    self.ready_host['Labels'].add('sku:S2')
    self.hosts = [self.ready_host]
    with self.assertRaises(errors.ExternalError):
      cros_helper.do_allocate_dut(opts)

  def test_all_transient(self):
    opts = self._create_opts(locked_dut=None)

    self.hosts = [self.resetting_host]
    self.assertEqual(cros_helper.do_allocate_dut(opts), ('wait', None))
    self.assertFalse(self.resetting_host['Locked'])

  def test_lock_running(self):
    self.hosts = [self.running_host]

    opts = self._create_opts(locked_dut=None)
    self.assertEqual(
        cros_helper.do_allocate_dut(opts), ('wait', self.running_host['Host']))
    self.assertTrue(self.running_host['Locked'])

  def test_locked_ready(self):
    self.hosts = [self.running_host]
    opts = self._create_opts(locked_dut=None)
    cros_helper.do_allocate_dut(opts)

    # The locked host became ready.
    self.running_host['Status'] = 'Ready'

    opts = self._create_opts(locked_dut=self.running_host['Host'] + '.cros')
    self.assertEqual(
        cros_helper.do_allocate_dut(opts), ('ready', self.running_host['Host']))
    self.assertTrue(self.running_host['Locked'])

  def test_locked_disappeared(self):
    self.hosts = [self.running_host, self.resetting_host]
    opts = self._create_opts(locked_dut=None)
    cros_helper.do_allocate_dut(opts)

    # Running_host disappeared.
    self.hosts = [self.resetting_host]

    opts = self._create_opts(locked_dut=self.running_host['Host'] + '.cros')
    self.assertEqual(cros_helper.do_allocate_dut(opts), ('wait', None))

  def test_locked_broken(self):
    self.hosts = [self.running_host]
    opts = self._create_opts(locked_dut=None)
    cros_helper.do_allocate_dut(opts)

    # The host we are waiting for became broken.
    self.running_host['Status'] = 'Repair Failed'

    opts = self._create_opts(locked_dut=self.running_host['Host'] + '.cros')
    with self.assertRaises(errors.NoDutAvailable):
      cros_helper.do_allocate_dut(opts)

  def test_alternative_ready(self):
    self.hosts = [self.running_host]
    opts = self._create_opts(locked_dut=None)
    cros_helper.do_allocate_dut(opts)

    self.hosts.append(self.ready_host)

    opts = self._create_opts(locked_dut=self.running_host['Host'] + '.cros')
    self.assertEqual(
        cros_helper.do_allocate_dut(opts), ('ready', self.ready_host['Host']))
    self.assertTrue(self.ready_host['Locked'])

  def test_alternative_ready_temporarily(self):
    self.hosts = [self.running_host]
    opts = self._create_opts(locked_dut=None)
    cros_helper.do_allocate_dut(opts)

    self.hosts.append(self.ready_host)
    self.lock_host.side_effect = self._change_and_lock(Status='Running')

    opts = self._create_opts(locked_dut=self.running_host['Host'] + '.cros')
    self.assertEqual(
        cros_helper.do_allocate_dut(opts), ('wait', self.running_host['Host']))
    self.assertTrue(self.running_host['Locked'])
    self.assertFalse(self.ready_host['Locked'])

  def test_locked_by_others(self):
    self.hosts = [self.running_host]
    opts = self._create_opts(locked_dut=None)
    cros_helper.do_allocate_dut(opts)

    self.running_host['Lock Reason'] = 'stolen'

    opts = self._create_opts(locked_dut=self.running_host['Host'] + '.cros')
    with self.assertRaises(errors.ExternalError):
      cros_helper.do_allocate_dut(opts)

  def test_typical_runs(self):
    # Initially, all hosts are not ready.
    self.hosts = [self.running_host, self.resetting_host, self.bad_host]

    # wait for a running host
    opts = self._create_opts(locked_dut=None)
    self.assertEqual(
        cros_helper.do_allocate_dut(opts), ('wait', self.running_host['Host']))
    self.assertTrue(self.running_host['Locked'])

    # retry several times and still wait the same host
    opts = self._create_opts(locked_dut=self.running_host['Host'] + '.cros')
    for _ in range(3):
      self.assertEqual(
          cros_helper.do_allocate_dut(opts),
          ('wait', self.running_host['Host']))
      self.assertTrue(self.running_host['Locked'])

    # The running host is finally ready.
    self.running_host['Status'] = 'Ready'
    self.assertEqual(
        cros_helper.do_allocate_dut(opts), ('ready', self.running_host['Host']))
    self.assertTrue(self.running_host['Locked'])


if __name__ == '__main__':
  unittest.main()
