# -*- coding: utf-8 -*-
# Copyright 2018 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Test switch_arc_prebuilt script."""

from __future__ import print_function
import unittest

import mock

import switch_arc_prebuilt


@mock.patch('bisect_kit.common.config_logging', mock.Mock())
class TestSwitchArcPrebuilt(unittest.TestCase):
  """Test switch_arc_prebuilt.py."""

  @mock.patch('bisect_kit.cli.argtype_dir_path', lambda x: x)
  @mock.patch('bisect_kit.cros_util.query_dut_lsb_release')
  @mock.patch('bisect_kit.arc_util.query_flavor')
  @mock.patch('bisect_kit.arc_util.push_prebuilt_to_device')
  def test_main(self, push_prebuilt_to_device, query_flavor,
                query_dut_lsb_release):
    android_root = '/path/to/android/repo'
    dut = 'this_is_dut'
    flavor = 'detected_flavor'
    bid = '123456789'
    query_flavor.return_value = flavor
    query_dut_lsb_release.return_value = {
        'CHROMEOS_ARC_VERSION': flavor + '_' + bid
    }

    switch_arc_prebuilt.main(['--android_root', android_root, dut, bid])
    push_prebuilt_to_device.assert_called_with(android_root, dut, flavor, bid)


if __name__ == '__main__':
  unittest.main()
