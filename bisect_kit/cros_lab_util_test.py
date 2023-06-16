# Copyright 2018 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Test cros_lab_util module."""

import subprocess
import unittest
from unittest import mock

from bisect_kit import common
from bisect_kit import cros_lab_util
from bisect_kit import cros_util
from bisect_kit import errors
from bisect_kit import util


class TestCrosLab(unittest.TestCase):
    """Test functions in cros_lab_util module."""

    def test_normalize_sku_name(self):
        self.assertEqual(
            cros_lab_util.normalize_sku_name('foo_16GB'), 'foo_16GB'
        )
        self.assertEqual(
            cros_lab_util.normalize_sku_name('foo_16Gb'), 'foo_16GB'
        )
        self.assertEqual(
            cros_lab_util.normalize_sku_name('something_else'), 'something_else'
        )

    def test_is_lab_dut(self):
        self.assertEqual(cros_lab_util.is_lab_dut('abc'), False)
        self.assertEqual(cros_lab_util.is_lab_dut('abc.cros'), True)
        self.assertEqual(cros_lab_util.is_lab_dut('chromeos8-foo-host1'), True)
        self.assertEqual(
            cros_lab_util.is_lab_dut('chromeos8-foo-labstation1'), True
        )
        self.assertEqual(cros_lab_util.is_lab_dut('cre123'), True)
        self.assertEqual(
            cros_lab_util.is_lab_dut('satlab-dummy'),
            True,
        )

    def test_dut_host_name(self):
        with self.assertRaises(AssertionError):
            cros_lab_util.dut_host_name('abc')
        self.assertEqual(cros_lab_util.dut_host_name('abc.cros'), 'abc')
        self.assertEqual(
            cros_lab_util.dut_host_name('chromeos8-foo-host1'),
            'chromeos8-foo-host1',
        )
        self.assertEqual(
            cros_lab_util.dut_host_name('chromeos8-foo-labstation1'),
            'chromeos8-foo-labstation1',
        )
        self.assertEqual(cros_lab_util.dut_host_name('cre123'), 'cre123')

    def test_dut_name_to_address(self):
        with self.assertRaises(AssertionError):
            cros_lab_util.dut_name_to_address('abc.cros')
        self.assertEqual(cros_lab_util.dut_name_to_address('abc'), 'abc.cros')
        self.assertEqual(
            cros_lab_util.dut_name_to_address('chromeos8-foo-host1'),
            'chromeos8-foo-host1',
        )
        self.assertEqual(
            cros_lab_util.dut_name_to_address('chromeos8-foo-labstation1'),
            'chromeos8-foo-labstation1',
        )
        self.assertEqual(cros_lab_util.dut_name_to_address('cre123'), 'cre123')

    def test_convert_to_known_satlab_ip(self):
        self.assertIsNotNone(
            cros_lab_util.convert_to_known_satlab_ip('satlab_prefix-dummy')
        )

        self.assertEqual(
            cros_lab_util.convert_to_known_satlab_ip('satlab-unknown'),
            None,
        )

    def test_is_rootfs_verification_on(self):
        with mock.patch.object(util, 'ssh_cmd', return_value='/dev/dm-0'):
            self.assertTrue(
                cros_lab_util.is_rootfs_verification_on('dummy_DUT')
            )

        with mock.patch.object(util, 'ssh_cmd', return_value='/dev/nvme0n1p3'):
            self.assertFalse(
                cros_lab_util.is_rootfs_verification_on('dummy_DUT')
            )

        with self.assertRaises(errors.SshConnectionError):
            with mock.patch.object(
                util, 'ssh_cmd', side_effect=errors.SshConnectionError
            ):
                cros_lab_util.is_rootfs_verification_on('dummy_DUT')

        with self.assertRaises(subprocess.CalledProcessError):
            with mock.patch.object(
                util,
                'ssh_cmd',
                side_effect=subprocess.CalledProcessError(255, 'ssh_cmd'),
            ):
                cros_lab_util.is_rootfs_verification_on('dummy_DUT')

    def test_enable_rootfs_verification(self):
        with mock.patch.object(
            cros_lab_util, 'is_rootfs_verification_on', return_value=True
        ):
            self.assertTrue(
                cros_lab_util.enable_rootfs_verification(
                    'dummy_DUT', common.DEFAULT_CHROMEOS_ROOT
                )
            )

        with mock.patch.object(
            util, 'ssh_cmd', side_effect=errors.SshConnectionError
        ):
            self.assertFalse(
                cros_lab_util.enable_rootfs_verification(
                    'dummy_DUT', common.DEFAULT_CHROMEOS_ROOT
                )
            )

        with mock.patch.object(
            cros_lab_util, 'is_rootfs_verification_on', return_value=True
        ), mock.patch.object(
            cros_util, 'query_dut_short_version', side_effect='short_version'
        ), mock.patch.object(
            cros_util, 'query_dut_board', side_effect='board'
        ), mock.patch.object(
            cros_util, 'version_to_full', side_effect='full_version'
        ), mock.patch.object(
            cros_util, 'search_image', side_effect=cros_util.ImageInfo()
        ), mock.patch.object(
            cros_util, 'provision_image', side_effect=[]
        ):
            self.assertTrue(
                cros_lab_util.enable_rootfs_verification(
                    'dummy_DUT', common.DEFAULT_CHROMEOS_ROOT
                )
            )


if __name__ == '__main__':
    unittest.main()
