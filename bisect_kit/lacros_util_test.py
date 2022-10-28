# -*- coding: utf-8 -*-
# Copyright 2022 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Test lacros_util module."""

import unittest
from unittest import mock

from bisect_kit import cros_util
from bisect_kit import errors
from bisect_kit import lacros_util
from bisect_kit import util


class TestLacrosUtil(unittest.TestCase):
    """Test functions for lacros_util module."""

    def test_get_dut_cpu_architecture(self):
        with mock.patch.object(util, 'ssh_cmd', return_value=f'x86_64\n'):
            self.assertEqual(
                lacros_util.get_dut_cpu_architecture('x64_dut'), 'x86_64'
            )

        with mock.patch.object(util, 'ssh_cmd', return_value=f'aarch64\n'):
            self.assertEqual(
                lacros_util.get_dut_cpu_architecture('arm_dut'), 'aarch64'
            )

        with self.assertRaises(errors.SshConnectionError):
            lacros_util.get_dut_cpu_architecture('not_sshable_dut')

    def test_get_lacross_folder_name(self):
        with mock.patch.object(
            lacros_util, 'get_dut_cpu_architecture', return_value='x86_64'
        ):
            self.assertEqual(
                lacros_util.get_lacross_folder_name('x64_dut'),
                lacros_util.BOARD_ARCHITECTURES['x86_64']['lacros_dir'],
            )

        with mock.patch.object(
            lacros_util, 'get_dut_cpu_architecture', return_value='aarch64'
        ):
            self.assertEqual(
                lacros_util.get_lacross_folder_name('arm_dut'),
                lacros_util.BOARD_ARCHITECTURES['aarch64']['lacros_dir'],
            )

        with mock.patch.object(
            lacros_util,
            'get_dut_cpu_architecture',
            return_value='unknown_architecture',
        ):
            with self.assertRaises(errors.InternalError):
                lacros_util.get_lacross_folder_name('unknown_architecture_dut')

    def test_get_device_architecture_arg_for_gn(self):
        # Disabling protected member access cros lint error
        # pylint: disable=W0212
        with mock.patch.object(
            lacros_util, 'get_dut_cpu_architecture', return_value='x86_64'
        ):
            self.assertEqual(
                lacros_util._get_device_architecture_arg_for_gn('x64_dut'),
                lacros_util.BOARD_ARCHITECTURES['x86_64']['arg_for_gn'],
            )

        with mock.patch.object(
            lacros_util, 'get_dut_cpu_architecture', return_value='aarch64'
        ):
            self.assertEqual(
                lacros_util._get_device_architecture_arg_for_gn('arm_dut'),
                lacros_util.BOARD_ARCHITECTURES['aarch64']['arg_for_gn'],
            )

        with mock.patch.object(
            lacros_util,
            'get_dut_cpu_architecture',
            return_value='unknown_architecture',
        ):
            with self.assertRaises(errors.InternalError):
                lacros_util._get_device_architecture_arg_for_gn(
                    'unknown_architecture_dut'
                )

    def test_is_prebuilt_path_available(self):
        lacros_image_url = (
            'gs://chrome-unsigned/desktop-5c0tCh/'
            '100.0.4896.6/lacros64/lacros.zip'
        )
        with mock.patch.object(
            cros_util, 'gsutil_ls', return_value=[lacros_image_url]
        ):
            self.assertEqual(
                lacros_util.is_prebuilt_path_available(lacros_image_url), True
            )

        with mock.patch.object(cros_util, 'gsutil_ls', return_value=[]):
            self.assertEqual(
                lacros_util.is_prebuilt_path_available(
                    'not_available_image_url'
                ),
                False,
            )

    def test_get_lacros_prebuilt_versions_between(self):

        dummy_gs_paths = [
            'gs://chrome-unsigned/desktop-5c0tCh/0.0.1.0/lacros64/lacros.zip',
            'gs://chrome-unsigned/desktop-5c0tCh/0.0.1.1/lacros64/lacros.zip',
            'gs://chrome-unsigned/desktop-5c0tCh/0.0.2.0/lacros64/lacros.zip',
            'gs://chrome-unsigned/desktop-5c0tCh/0.0.2.1/lacros64/lacros.zip',
        ]

        with mock.patch.object(
            lacros_util, 'get_lacros_gs_path_list', return_value=dummy_gs_paths
        ):
            self.assertEqual(
                lacros_util.get_lacros_prebuilt_versions_between(
                    '0.0.1.0', '0.0.2.0', 'lacros64'
                ),
                [
                    '0.0.1.0',
                    '0.0.2.0',
                ],
            )

            self.assertEqual(
                lacros_util.get_lacros_prebuilt_versions_between(
                    '0.0.1.0', '0.0.1.1', 'lacros64'
                ),
                [
                    '0.0.1.0',
                    '0.0.1.1',
                ],
            )

            self.assertEqual(
                lacros_util.get_lacros_prebuilt_versions_between(
                    '0.0.1.0', '0.0.2.1', 'lacros64'
                ),
                ['0.0.1.0', '0.0.2.0', '0.0.2.1'],
            )

        gs_paths_101 = [
            'gs://chrome-unsigned/desktop-5c0tCh/101.0.4950.0/lacros64/lacros.zip',
            'gs://chrome-unsigned/desktop-5c0tCh/101.0.4951.0/lacros64/lacros.zip',
            'gs://chrome-unsigned/desktop-5c0tCh/101.0.4951.2/lacros64/lacros.zip',
            'gs://chrome-unsigned/desktop-5c0tCh/101.0.4951.3/lacros64/lacros.zip',
        ]

        gs_paths_102 = [
            'gs://chrome-unsigned/desktop-5c0tCh/102.0.4962.0/lacros64/lacros.zip',
            'gs://chrome-unsigned/desktop-5c0tCh/102.0.4962.2/lacros64/lacros.zip',
            'gs://chrome-unsigned/desktop-5c0tCh/102.0.4962.3/lacros64/lacros.zip',
        ]

        gs_paths_103 = [
            'gs://chrome-unsigned/desktop-5c0tCh/103.0.5060.0/lacros64/lacros.zip',
            'gs://chrome-unsigned/desktop-5c0tCh/103.0.5060.3/lacros64/lacros.zip',
            'gs://chrome-unsigned/desktop-5c0tCh/103.0.5060.5/lacros64/lacros.zip',
            'gs://chrome-unsigned/desktop-5c0tCh/103.0.5060.4/lacros64/lacros.zip',
        ]

        gs_paths_merged = gs_paths_101 + gs_paths_102 + gs_paths_103

        with mock.patch.object(
            lacros_util, 'get_lacros_gs_path_list', return_value=gs_paths_merged
        ):
            self.assertEqual(
                lacros_util.get_lacros_prebuilt_versions_between(
                    '101.0.4950.0', '103.0.5060.0', 'lacros64'
                ),
                [
                    '101.0.4950.0',
                    '101.0.4951.0',
                    '102.0.4962.0',
                    '103.0.5060.0',
                ],
            )

            self.assertEqual(
                lacros_util.get_lacros_prebuilt_versions_between(
                    '101.0.4950.0', '101.0.4951.2', 'lacros64'
                ),
                ['101.0.4950.0', '101.0.4951.0', '101.0.4951.2'],
            )

            self.assertEqual(
                lacros_util.get_lacros_prebuilt_versions_between(
                    '101.0.4951.0', '102.0.4962.3', 'lacros64'
                ),
                [
                    '101.0.4951.0',
                    '102.0.4962.0',
                    '102.0.4962.2',
                    '102.0.4962.3',
                ],
            )

            self.assertEqual(
                lacros_util.get_lacros_prebuilt_versions_between(
                    '103.0.5060.0', '103.0.5060.5', 'lacros64'
                ),
                [
                    '103.0.5060.0',
                    '103.0.5060.3',
                    '103.0.5060.4',
                    '103.0.5060.5',
                ],
            )


if __name__ == '__main__':
    unittest.main()
