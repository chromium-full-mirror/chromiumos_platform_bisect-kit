# Copyright 2023 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

import unittest
from unittest import mock

from bisect_kit import cros_lab_util
from bisect_kit import dut_allocator
from bisect_kit import errors


class TestSampleAvailableBots(unittest.TestCase):
    """Test _sample_available_bots() and _sample_available_bots_with_retry() functions"""

    @mock.patch('bisect_kit.cros_lab_util.swarming_bots_list')
    def test_sample_select_all_duts(self, mock_swarming_bots_list):
        available_bots = [f'bot{k}' for k in range(10)]
        mock_swarming_bots_list.side_effect = (
            available_bots[:5],
            available_bots[5:],
        )

        dimensions = ['d1', 'd2']
        variants = ['v1', 'v2']
        sample_num = len(available_bots)
        # pylint: disable=protected-access
        acquired_bots = dut_allocator._sample_available_bots(
            dimensions, variants, None, sample_num
        )

        self.assertEqual(len(acquired_bots), sample_num)
        self.assertCountEqual(acquired_bots, available_bots)

    @mock.patch('bisect_kit.cros_lab_util.swarming_bots_list')
    def test_sample_select_partial(self, mock_swarming_bots_list):
        available_bots = [f'bot{k}' for k in range(10)]
        mock_swarming_bots_list.side_effect = (
            available_bots[:5],
            available_bots[5:],
        )

        dimensions = ['d1', 'd2']
        variants = ['v1', 'v2']
        sample_num = len(available_bots) // 2
        # pylint: disable=protected-access
        acquired_bots = dut_allocator._sample_available_bots(
            dimensions, variants, None, sample_num
        )

        self.assertEqual(len(acquired_bots), sample_num)
        self.assertLess(set(acquired_bots), set(available_bots))

    @mock.patch('bisect_kit.cros_lab_util.swarming_bots_list')
    def test_sample_select_not_sufficient(self, mock_swarming_bots_list):
        available_bots = [f'bot{k}' for k in range(4)]
        mock_swarming_bots_list.side_effect = (
            available_bots[:2],
            available_bots[2:],
        )

        dimensions = ['d1', 'd2']
        variants = ['v1', 'v2']
        sample_num = 10
        # pylint: disable=protected-access
        acquired_bots = dut_allocator._sample_available_bots(
            dimensions, variants, None, sample_num
        )

        self.assertEqual(len(acquired_bots), len(available_bots))
        self.assertCountEqual(acquired_bots, available_bots)

    @mock.patch('bisect_kit.cros_lab_util.swarming_bots_list')
    def test_sample_select_empty(self, mock_swarming_bots_list):
        mock_swarming_bots_list.return_value = []

        dimensions = ['d1', 'd2']
        variants = ['v1', 'v2']
        sample_num = 10
        # pylint: disable=protected-access
        acquired_bots = dut_allocator._sample_available_bots(
            dimensions, variants, None, sample_num
        )

        self.assertEqual(len(acquired_bots), 0)
        self.assertCountEqual(acquired_bots, [])

    @mock.patch('bisect_kit.cros_lab_util.swarming_bots_list')
    def test_sample_select_filtered(self, mock_swarming_bots_list):
        available_bots = [f'bot{k}' for k in range(10)]
        mock_swarming_bots_list.side_effect = (
            available_bots[:5],
            available_bots[5:],
        )

        dimensions = ['d1', 'd2']
        variants = ['v1', 'v2']
        sample_num = len(available_bots)

        def filter_func(name):
            return int(name[3:]) < 5

        # pylint: disable=protected-access
        acquired_bots = dut_allocator._sample_available_bots(
            dimensions, variants, None, sample_num, filter_func
        )

        filtered_bots = list(filter(filter_func, available_bots))
        self.assertEqual(len(acquired_bots), len(filtered_bots))
        self.assertCountEqual(acquired_bots, filtered_bots)

    @mock.patch('bisect_kit.cros_lab_util.swarming_bots_list')
    def test_sample_multiple_pools(self, mock_swarming_bots_list):
        available_bots = [f'bot{k}' for k in range(20)]
        mock_swarming_bots_list.side_effect = (
            available_bots[:5],
            available_bots[5:10],
            available_bots[10:15],
            available_bots[15:20],
        )

        dimensions = ['d1', 'd2']
        variants = ['v1', 'v2']
        pools = ['p1', 'p2']
        sample_num = len(available_bots)

        # pylint: disable=protected-access
        acquired_bots = dut_allocator._sample_available_bots(
            dimensions,
            variants,
            pools,
            sample_num,
        )

        self.assertEqual(len(acquired_bots), len(available_bots))
        self.assertCountEqual(acquired_bots, available_bots)

    def test_sample_available_bots_with_retry_value_error(self):
        available_bots = [f'bot{k}' for k in range(10)]

        dimensions = ['d1', 'd2']
        variants = ['v1', 'v2']
        sample_num = len(available_bots)
        max_attempts = 0

        with self.assertRaises(ValueError):
            # pylint: disable=protected-access
            dut_allocator._sample_available_bots_with_retry(
                dimensions,
                variants,
                None,
                sample_num,
                max_attempts=max_attempts,
            )

    @mock.patch('time.sleep')
    @mock.patch.object(cros_lab_util, 'swarming_bots_list')
    def test_sample_available_bots_with_retry(
        self, mock_swarming_bots_lists, mock_sleep
    ):
        available_bots_v1 = [f'bot{k}' for k in range(5)]
        available_bots_v2 = [f'bot{k}' for k in range(6, 10)]
        mock_swarming_bots_lists.side_effect = [
            available_bots_v1,
            available_bots_v2,
        ]

        dimensions = ['d1', 'd2']
        variants = ['v1', 'v2']
        sample_num = 10
        max_attempts = 3

        # pylint: disable=protected-access
        acquired_bots = dut_allocator._sample_available_bots_with_retry(
            dimensions, variants, None, sample_num, max_attempts=max_attempts
        )
        self.assertCountEqual(
            acquired_bots, available_bots_v1 + available_bots_v2
        )
        mock_sleep.assert_not_called()
        mock_swarming_bots_lists.assert_has_calls(
            [
                mock.call(
                    ['d1', 'd2', 'v1'],
                    is_dead=False,
                    quarantined=False,
                    is_busy=False,
                    limit=mock.ANY,
                ),
                mock.call(
                    ['d1', 'd2', 'v2'],
                    is_dead=False,
                    quarantined=False,
                    is_busy=False,
                    limit=mock.ANY,
                ),
            ]
        )

    @mock.patch('time.sleep')
    @mock.patch.object(cros_lab_util, 'swarming_bots_list')
    def test_sample_available_bots_with_retry_once(
        self, mock_swarming_bots_lists, mock_sleep
    ):
        available_bots = [f'bot{k}' for k in range(10)]
        mock_swarming_bots_lists.side_effect = [
            [],
            available_bots,
        ]

        dimensions = ['d1', 'd2']
        variants = ['v1']
        sample_num = len(available_bots)
        max_attempts = 3

        # pylint: disable=protected-access
        acquired_bots = dut_allocator._sample_available_bots_with_retry(
            dimensions, variants, None, sample_num, max_attempts=max_attempts
        )
        self.assertCountEqual(acquired_bots, available_bots)
        mock_sleep.assert_called_once()
        mock_swarming_bots_lists.assert_has_calls(
            [
                mock.call(
                    ['d1', 'd2', 'v1'],
                    is_dead=False,
                    quarantined=False,
                    is_busy=False,
                    limit=mock.ANY,
                ),
                mock.call(
                    ['d1', 'd2', 'v1'],
                    is_dead=False,
                    quarantined=False,
                    is_busy=False,
                    limit=mock.ANY,
                ),
            ]
        )

    @mock.patch('time.sleep')
    @mock.patch.object(dut_allocator, '_sample_available_bots')
    def test_sample_available_bots_with_retry_not_available(
        self, mock_sample_available_bots, mock_sleep
    ):
        available_bots = [f'bot{k}' for k in range(10)]
        mock_sample_available_bots.return_value = []

        dimensions = ['d1', 'd2']
        variants = ['v1', 'v2']
        sample_num = len(available_bots)
        max_attempts = 3

        with self.assertRaises(errors.NoDutAvailable):
            # pylint: disable=protected-access
            dut_allocator._sample_available_bots_with_retry(
                dimensions,
                variants,
                None,
                sample_num,
                max_attempts=max_attempts,
            )

        mock_sleep.assert_called()
        mock_sample_available_bots.assert_called()
