# -*- coding: utf-8 -*-
# Copyright 2018 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Test cros_lab_util module."""
from __future__ import print_function
import unittest

from bisect_kit import cros_lab_util


class TestCrosLab(unittest.TestCase):
  """Test functions in cros_lab_util module."""

  def test_normalize_sku_name(self):
    self.assertEqual(cros_lab_util.normalize_sku_name('foo_16GB'), 'foo_16GB')
    self.assertEqual(cros_lab_util.normalize_sku_name('foo_16Gb'), 'foo_16GB')
    self.assertEqual(
        cros_lab_util.normalize_sku_name('something_else'), 'something_else')

  def test_is_lab_dut(self):
    self.assertEqual(cros_lab_util.is_lab_dut('abc'), False)
    self.assertEqual(cros_lab_util.is_lab_dut('abc.cros'), True)
    self.assertEqual(cros_lab_util.is_lab_dut('chromeos8-foo-host1'), True)
    self.assertEqual(
        cros_lab_util.is_lab_dut('chromeos8-foo-labstation1'), True)
    self.assertEqual(cros_lab_util.is_lab_dut('cre123'), True)

  def test_dut_host_name(self):
    with self.assertRaises(AssertionError):
      cros_lab_util.dut_host_name('abc')
    self.assertEqual(cros_lab_util.dut_host_name('abc.cros'), 'abc')
    self.assertEqual(
        cros_lab_util.dut_host_name('chromeos8-foo-host1'),
        'chromeos8-foo-host1')
    self.assertEqual(
        cros_lab_util.dut_host_name('chromeos8-foo-labstation1'),
        'chromeos8-foo-labstation1')
    self.assertEqual(cros_lab_util.dut_host_name('cre123'), 'cre123')

  def test_dut_name_to_address(self):
    with self.assertRaises(AssertionError):
      cros_lab_util.dut_name_to_address('abc.cros')
    self.assertEqual(cros_lab_util.dut_name_to_address('abc'), 'abc.cros')
    self.assertEqual(
        cros_lab_util.dut_name_to_address('chromeos8-foo-host1'),
        'chromeos8-foo-host1')
    self.assertEqual(
        cros_lab_util.dut_name_to_address('chromeos8-foo-labstation1'),
        'chromeos8-foo-labstation1')
    self.assertEqual(cros_lab_util.dut_name_to_address('cre123'), 'cre123')


if __name__ == '__main__':
  unittest.main()
