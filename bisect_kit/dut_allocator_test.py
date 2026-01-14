# Copyright 2026 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Test dut_allocator module."""

import unittest
from unittest import mock

from bisect_kit import cros_util
from bisect_kit import dut_allocator
from bisect_kit import errors
from bisect_kit.dut_allocate_spec_type import DutAllocateSpec


class DutAllocatorTest(unittest.TestCase):
    """Test DutAllocator."""

    def setUp(self):
        patcher = mock.patch.object(cros_util, 'has_test_image')
        self.mock_has_test_image = patcher.start()
        self.addCleanup(patcher.stop)

        patcher = mock.patch.object(cros_util, 'is_cros_version')
        self.mock_is_cros_version = patcher.start()
        self.mock_is_cros_version.return_value = True
        self.addCleanup(patcher.stop)

        patcher = mock.patch.object(cros_util, 'is_cros_snapshot_version')
        self.mock_is_cros_snapshot_version = patcher.start()
        self.mock_is_cros_snapshot_version.return_value = True
        self.addCleanup(patcher.stop)

        patcher = mock.patch.object(dut_allocator, '_filter_dimensions_by_lab')
        self.mock_filter_dimensions_by_lab = patcher.start()
        self.mock_filter_dimensions_by_lab.side_effect = (
            lambda x, raise_unknown=False: x
        )
        self.addCleanup(patcher.stop)

        patcher = mock.patch.object(dut_allocator, '_normalize_board_name')
        self.mock_normalize_board_name = patcher.start()
        self.mock_normalize_board_name.side_effect = lambda _, board: board
        self.addCleanup(patcher.stop)

        patcher = mock.patch.object(
            dut_allocator, '_filter_dimensions_by_board'
        )
        self.mock_filter_dimensions_by_board = patcher.start()
        self.mock_filter_dimensions_by_board.side_effect = (
            lambda _, variants, __: variants
        )
        self.addCleanup(patcher.stop)

        self.spec = DutAllocateSpec(
            boards=['board1'],
            pools=['some_pool'],
            version_hints=['1.0', '2.0'],
            builder_hints=['builder1', 'builder2'],
            public=False,
        )

    @mock.patch.object(dut_allocator.DutAllocator, 'allocate_dut_from_lab')
    def test_filter_empty_builder_hints(self, mock_allocate_dut_from_lab):
        self.spec.builder_hints = ['builder1', '']
        self.mock_has_test_image.return_value = True
        mock_allocate_dut_from_lab.return_value = ('host', 'board1')

        allocator = dut_allocator.DutAllocator(self.spec, '/path/to/chromeos')
        allocator.allocate_dut()

        self.mock_has_test_image.assert_called_with(
            'builder1', '2.0', is_public_build=False
        )
        self.assertEqual(self.mock_has_test_image.call_count, 2)

    @mock.patch.object(dut_allocator.DutAllocator, 'allocate_dut_from_lab')
    def test_one_version_missing(self, mock_allocate_dut_from_lab):
        # builder1 is missing 2.0, builder2 has both
        self.mock_has_test_image.side_effect = (
            lambda builder, version, is_public_build: not (
                builder == 'builder1' and version == '2.0'
            )
        )
        mock_allocate_dut_from_lab.return_value = ('host', 'board2')

        allocator = dut_allocator.DutAllocator(self.spec, '/path/to/chromeos')
        allocator.allocate_dut()

        mock_allocate_dut_from_lab.assert_called()

        # builder1 is missing both, builder2 has both
        self.mock_has_test_image.side_effect = (
            lambda builder, version, is_public_build: builder == 'builder2'
        )
        mock_allocate_dut_from_lab.return_value = ('host', 'board2')
        allocator = dut_allocator.DutAllocator(self.spec, '/path/to/chromeos')
        allocator.allocate_dut()
        mock_allocate_dut_from_lab.assert_called()

    def test_no_builders_with_prebuilts(self):
        self.mock_has_test_image.return_value = False
        with self.assertRaisesRegex(
            errors.ArgumentError,
            'None of the builders have all the necessary prebuilts.*',
        ):
            dut_allocator.DutAllocator(self.spec, '/path/to/chromeos')

    @mock.patch.object(dut_allocator.DutAllocator, 'allocate_dut_from_lab')
    def test_success(self, mock_allocate_dut_from_lab):
        self.mock_has_test_image.return_value = True
        mock_allocate_dut_from_lab.return_value = ('host', 'board1')

        allocator = dut_allocator.DutAllocator(self.spec, '/path/to/chromeos')
        host, board = allocator.allocate_dut()

        self.assertEqual(host, 'host')
        self.assertEqual(board, 'board1')


if __name__ == '__main__':
    unittest.main()
