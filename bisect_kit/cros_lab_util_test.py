# -*- coding: utf-8 -*-
# Copyright 2018 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Test cros_lab_util module."""
from __future__ import print_function
import unittest

import mock

from bisect_kit import cros_lab_util


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


if __name__ == '__main__':
  unittest.main()
