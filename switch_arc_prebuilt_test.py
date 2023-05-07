# Copyright 2018 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Test switch_arc_prebuilt script."""

import unittest
from unittest import mock

import switch_arc_prebuilt


@mock.patch('bisect_kit.common.config_logging', mock.Mock())
class TestSwitchArcPrebuilt(unittest.TestCase):
    """Test switch_arc_prebuilt.py."""

    @mock.patch('bisect_kit.cros_util.is_good_dut', lambda *_: True)
    @mock.patch('bisect_kit.cros_util.query_dut_lsb_release')
    @mock.patch('bisect_kit.arc_util.push_prebuilt_to_device')
    def test_main(self, push_prebuilt_to_device, query_dut_lsb_release):
        dut = 'this_is_dut'
        flavor = 'flavor'
        bid = '123456789'
        query_dut_lsb_release.return_value = {
            'CHROMEOS_ARC_VERSION': flavor + '_' + bid
        }

        switch_arc_prebuilt.main(['--flavor', flavor, '--dut', dut, bid])
        push_prebuilt_to_device.assert_called_with(dut, flavor, bid)


if __name__ == '__main__':
    unittest.main()
