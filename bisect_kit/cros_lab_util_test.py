# -*- coding: utf-8 -*-
# Copyright 2018 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Test cros_lab_util module."""
from __future__ import print_function
import unittest

import mock

from bisect_kit import cros_lab_util
from bisect_kit import errors


class TestCrosLab(unittest.TestCase):
  """Test functions in cros_lab_util module."""

  def test_dut_manager(self):
    allocator = mock.Mock(return_value='allocated_dut')
    with mock.patch('bisect_kit.cros_lab_util.unlock_host') as mock_unlock:
      with cros_lab_util.dut_manager('foobar', allocator) as dut:
        self.assertEqual(dut, 'foobar')
      self.assertFalse(allocator.called)
      self.assertFalse(mock_unlock.called)

    with mock.patch('bisect_kit.cros_lab_util.unlock_host') as mock_unlock:
      with cros_lab_util.dut_manager(':lab:', allocator) as dut:
        self.assertEqual(dut, 'allocated_dut.cros')
        self.assertFalse(mock_unlock.called)
      self.assertTrue(allocator.called)
      mock_unlock.assert_called_with('allocated_dut')


# Make sure we don't call atest by accident.
@mock.patch('bisect_kit.cros_lab_util.atest_cmd', None)
class TestSeekHost(unittest.TestCase):
  """Test cros_lab_util.seek_host."""

  def setUp(self):
    # Prepare fake data.
    self.ready_host = {
        'Host': 'H1',
        'Labels': set(['sku:S1', 'pool:P1']),
        'Locked': False,
        'Status': 'Ready',
    }
    self.locked_by_us = {
        'Host': 'H2',
        'Labels': set(['sku:S1', 'pool:P1']),
        'Locked': True,
        'Lock Reason': 'bisect-kit: foo',
        'Status': 'Ready',
    }
    self.locked_by_others = {
        'Host': 'H3',
        'Labels': set(['sku:S1', 'pool:P1']),
        'Locked': True,
        'Lock Reason': 'foo',
        'Status': 'Ready',
    }
    self.running_host = {
        'Host': 'H4',
        'Labels': set(['sku:S1', 'pool:P1']),
        'Locked': False,
        'Status': 'Running',
    }
    self.bad_host = {
        'Host': 'H5',
        'Labels': set(['sku:S1', 'pool:P1']),
        'Locked': False,
        'Status': 'Repair Failed',
    }
    self.p2_host = {
        'Host': 'H6',
        'Labels': set(['sku:S1', 'pool:P2']),
        'Locked': False,
        'Status': 'Ready',
    }
    # Just in case new/rare status appears in the future.
    self.mystery_host = {
        'Host': 'H7',
        'Labels': set(['sku:S1', 'pool:P1']),
        'Locked': False,
        'Status': 'Dance',
    }

  @mock.patch('bisect_kit.cros_lab_util.list_host')
  def test_simple_case(self, list_host):
    # simple case
    list_host.return_value = {'H1': self.ready_host}
    hosts = cros_lab_util.seek_host(['P1'], sku='S1')
    self.assertEqual(hosts, [self.ready_host])

  @mock.patch('bisect_kit.cros_lab_util.list_host')
  def test_locked_by_us(self, list_host):
    list_host.return_value = {'H2': self.locked_by_us}
    hosts = cros_lab_util.seek_host(['P1'], sku='S1')
    # We expect other bisector will unlock after job done.
    self.assertEqual(hosts, [self.locked_by_us])

  @mock.patch('bisect_kit.cros_lab_util.list_host')
  def test_locked_by_others(self, list_host):
    list_host.return_value = {'H3': self.locked_by_others}
    hosts = cros_lab_util.seek_host(['P1'], sku='S1')
    # We assume that hosts locked by others won't be back.
    self.assertEqual(hosts, [])

  @mock.patch('bisect_kit.cros_lab_util.list_host')
  def test_bad_host(self, list_host):
    list_host.return_value = {'H5': self.bad_host}
    hosts = cros_lab_util.seek_host(['P1'], sku='S1')
    self.assertEqual(hosts, [])

  @mock.patch('bisect_kit.cros_lab_util.list_host')
  def test_unknown_state(self, list_host):
    list_host.return_value = {'H7': self.mystery_host}
    hosts = cros_lab_util.seek_host(['P1'], sku='S1')
    self.assertEqual(hosts, [self.mystery_host])

  @mock.patch('bisect_kit.cros_lab_util.list_host')
  def test_wrong_sku(self, list_host):
    list_host.return_value = {}
    with self.assertRaises(errors.ExternalError):
      cros_lab_util.seek_host(['P1'], sku='S1')

  @mock.patch('bisect_kit.cros_lab_util.list_host')
  def test_state_priority(self, list_host):
    list_host.return_value = {
        'H1': self.ready_host,
        'H2': self.locked_by_us,
        'H4': self.running_host
    }
    hosts = cros_lab_util.seek_host(['P1'], sku='S1')
    self.assertEqual(hosts,
                     [self.ready_host, self.running_host, self.locked_by_us])

  @mock.patch('bisect_kit.cros_lab_util.list_host')
  def test_pool_priority(self, list_host):

    def fake_list_host(host=None, labels=None):
      assert host is None
      assert labels
      if 'pool:P1' in labels:
        return {'H1': self.ready_host}
      if 'pool:P2' in labels:
        return {'H6': self.p2_host}
      assert 0

    list_host.side_effect = fake_list_host
    hosts = cros_lab_util.seek_host(['P1', 'P2'], sku='S1')
    self.assertEqual(hosts, [self.ready_host, self.p2_host])

    hosts = cros_lab_util.seek_host(['P2', 'P1'], sku='S1')
    self.assertEqual(hosts, [self.p2_host, self.ready_host])


if __name__ == '__main__':
  unittest.main()
