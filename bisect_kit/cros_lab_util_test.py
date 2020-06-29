# -*- coding: utf-8 -*-
# Copyright 2018 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Test cros_lab_util module."""
from __future__ import print_function
import unittest

from bisect_kit import cros_lab_util


class TestCrosLab(unittest.TestCase):
  """Test functions in cros_lab_util module."""

  def test_normalize_board_name(self):
    self.assertEqual(cros_lab_util.normalize_board_name('eve'), 'eve')
    self.assertEqual(
        cros_lab_util.normalize_board_name('eve-kernelnext'), 'eve')
    self.assertEqual(
        cros_lab_util.normalize_board_name('veyron_minnie'), 'veyron_minnie')
    self.assertEqual(
        cros_lab_util.normalize_board_name('veyron_minnie-kernelnext'),
        'veyron_minnie')

  def test_normalize_sku_name(self):
    self.assertEqual(cros_lab_util.normalize_sku_name('foo_16GB'), 'foo_16GB')
    self.assertEqual(cros_lab_util.normalize_sku_name('foo_16Gb'), 'foo_16GB')
    self.assertEqual(
        cros_lab_util.normalize_sku_name('something_else'), 'something_else')


if __name__ == '__main__':
  unittest.main()
