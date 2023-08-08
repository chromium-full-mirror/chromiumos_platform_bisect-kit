# Copyright 2023 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Test dlc_util module."""

import subprocess
import unittest
from unittest import mock

from bisect_kit.dlc import dlc_util


class TestDlcUtil(unittest.TestCase):
    """Test functions in the dlc_util module."""

    @mock.patch('bisect_kit.util.ssh_cmd')
    def test_is_installable_ok(self, mock_ssh_cmd):
        result = dlc_util.is_installable('dummy-dlc', 'dummy-dut')
        self.assertTrue(result)
        mock_ssh_cmd.assert_called_once()

    @mock.patch('bisect_kit.util.ssh_cmd')
    def test_is_installable_error(self, mock_ssh_cmd):
        mock_ssh_cmd.side_effect = subprocess.CalledProcessError(
            255, 'dummy-cmd'
        )
        result = dlc_util.is_installable('dummy-dlc', 'dummy-dut')
        self.assertFalse(result)
        mock_ssh_cmd.assert_called_once()
