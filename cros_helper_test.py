# Copyright 2023 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Test cros_helper module."""

import unittest
from unittest import mock

from bisect_kit import dut_allocator
import cros_helper


class DummyException(Exception):
    """A dummy exception class for testing."""


class TestCmdAllocateDut(unittest.TestCase):
    """Test cmd_allocate_dut()."""

    @mock.patch.object(dut_allocator, 'allocate_dut')
    def test_success(self, mock_allocate_dut):
        mock_allocate_dut.return_value = ('host', 'board1')
        result = cros_helper.allocate_dut(None, None)
        self.assertEqual(
            result,
            cros_helper.AllocateDutResult(
                result='ready',
                leased_dut='host.cros',
                board='board1',
            ),
        )

    @mock.patch.object(dut_allocator, 'allocate_dut')
    def test_fail(self, mock_allocate_dut):
        mock_allocate_dut.side_effect = DummyException('blah')
        result = cros_helper.allocate_dut(None, None)
        self.assertEqual(
            result,
            cros_helper.AllocateDutResult(
                result='failed',
                exception='DummyException',
                text='blah',
            ),
        )
