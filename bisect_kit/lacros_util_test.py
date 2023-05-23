# Copyright 2022 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Test lacros_util module."""

import pathlib
import unittest
from unittest import mock

from bisect_kit import cros_util
from bisect_kit import errors
from bisect_kit import lacros_util


class TestLacrosUtil(unittest.TestCase):
    """Test functions for lacros_util module."""

    def test_get_lacros_folder_name(self):
        self.assertEqual(
            lacros_util.get_lacros_folder_name('x86_64'),
            lacros_util.BOARD_ARCHITECTURES['x86_64']['lacros_dir'],
        )

        self.assertEqual(
            lacros_util.get_lacros_folder_name('aarch64'),
            lacros_util.BOARD_ARCHITECTURES['aarch64']['lacros_dir'],
        )

        with self.assertRaises(errors.InternalError):
            lacros_util.get_lacros_folder_name('unknown_architecture')

    def test_get_device_architecture_arg_for_gn(self):
        self.assertEqual(
            # pylint: disable=protected-access
            lacros_util._get_device_architecture_arg_for_gn('x86_64'),
            lacros_util.BOARD_ARCHITECTURES['x86_64']['arg_for_gn'],
        )

        self.assertEqual(
            # pylint: disable=protected-access
            lacros_util._get_device_architecture_arg_for_gn('aarch64'),
            lacros_util.BOARD_ARCHITECTURES['aarch64']['arg_for_gn'],
        )

        with self.assertRaises(errors.InternalError):
            # pylint: disable=protected-access
            lacros_util._get_device_architecture_arg_for_gn(
                'unknown_architecture'
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

    def test_get_RBE_environment_variables(self):
        # pylint: disable=protected-access
        env = lacros_util._get_RBE_environment_variables()

        self.assertTrue('RBE_credential_file' in env)
        self.assertEqual(
            env['RBE_credential_file'],
            pathlib.Path.home() / 'bisect-runner.json',
        )

        self.assertTrue('RBE_use_application_default_credentials' in env)
        self.assertEqual(env['RBE_use_application_default_credentials'], 'true')

        self.assertTrue('RBE_use_gce_credentials' in env)
        self.assertEqual(env['RBE_use_gce_credentials'], 'false')

        self.assertTrue('RBE_automatic_auth' in env)
        self.assertEqual(env['RBE_automatic_auth'], 'false')


if __name__ == '__main__':
    unittest.main()
