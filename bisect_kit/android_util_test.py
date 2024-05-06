# Copyright 2018 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Test android_util module."""

import unittest
from unittest import mock

from bisect_kit import android_util
from bisect_kit import testing


class TestAndroidUtil(unittest.TestCase):
    """Test functions in android_util module."""

    def test_is_android_build_id(self):
        assert android_util.is_android_build_id('123456')
        assert not android_util.is_android_build_id('foobar')

    def test_get_build_ids_between(self):
        data = '{"ids":["4683388","4683367","4683364","4683353","4683349"]}'
        with mock.patch.object(
            android_util, 'fetch_android_build_data', return_value=data
        ):
            self.assertEqual(
                android_util.get_build_ids_between(
                    'foo-branch', 'aosp_arm64-eng', '4683349', '4683388'
                ),
                ['4683349', '4683353', '4683364', '4683367', '4683388'],
            )

        # check sort order
        data = '{"ids":["10000001","9999999"]}'
        with mock.patch.object(
            android_util, 'fetch_android_build_data', return_value=data
        ):
            self.assertEqual(
                android_util.get_build_ids_between(
                    'foo-branch', 'aosp_arm64-eng', '9999999', '10000001'
                ),
                ['9999999', '10000001'],
            )

    def test_is_good_build(self):
        with testing.mock_function_by_file(
            android_util,
            'fetch_android_build_data',
            'android-build-status-4683364.json',
        ):
            self.assertFalse(
                android_util.is_good_build(
                    'foo-branch', 'build_test', '4683364'
                )
            )
            self.assertTrue(
                android_util.is_good_build(
                    'foo-branch', 'aosp_x86-eng', '4683364'
                )
            )

    def test_get_flavor(self):
        self.assertEqual(
            android_util.get_flavor('bertha', 'x86_64'), 'bertha_x86_64-user'
        )
        self.assertEqual(
            android_util.get_flavor('bertha', 'arm64'), 'bertha_arm64-user'
        )
        self.assertEqual(
            android_util.get_flavor('bertha', 'arm'), 'bertha_arm-user'
        )
        self.assertEqual(
            android_util.get_flavor('bertha', 'unknown_arch'), None
        )


if __name__ == '__main__':
    unittest.main()
