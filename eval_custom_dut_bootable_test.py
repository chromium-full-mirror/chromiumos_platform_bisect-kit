# Copyright 2023 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Test eval_cros_tast.py script"""

import unittest
from unittest import mock

from bisect_kit import cros_util
from bisect_kit import errors
import eval_custom_dut_bootable


class TestEvalCustomDutBootable(unittest.TestCase):
    """Test eval_custom_dut_bootable"""

    def test_is_dut_bootable(self):
        with mock.patch.object(cros_util, 'reboot', return_value=None):
            self.assertTrue(
                eval_custom_dut_bootable.is_dut_bootable('dummy_dut')
            )

        with mock.patch.object(
            cros_util, 'reboot', side_effect=errors.SshConnectionError
        ):
            self.assertFalse(
                eval_custom_dut_bootable.is_dut_bootable('dummy_dut')
            )

        with mock.patch.object(
            cros_util, 'reboot', side_effect=errors.ExternalError
        ):
            self.assertFalse(
                eval_custom_dut_bootable.is_dut_bootable('dummy_dut')
            )


if __name__ == '__main__':
    unittest.main()
