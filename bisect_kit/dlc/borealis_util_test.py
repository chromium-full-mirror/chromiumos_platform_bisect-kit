# Copyright 2023 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Test borealis_util module."""

import pathlib
import tempfile
import unittest
from unittest import mock

from bisect_kit import errors
from bisect_kit.dlc import borealis_util


class TestBorealisUtil(unittest.TestCase):
    """Test functions in the borealis_util module."""

    def test_is_borealis_version(self):
        self.assertTrue(borealis_util.is_borealis_version('2023.02.09'))
        self.assertTrue(borealis_util.is_borealis_version('2023.02.09.031143'))
        self.assertTrue(
            borealis_util.is_borealis_version('111.2023.02.09.031143')
        )

        self.assertFalse(borealis_util.is_borealis_version('2023.02.09.1'))
        self.assertFalse(borealis_util.is_borealis_version('2023.02.09-r1'))
        self.assertFalse(
            borealis_util.is_borealis_version('borealis-dlc-2023.02.09')
        )

    def test_argtype_borealis_version(self):
        version = '2023.02.09'
        self.assertEqual(
            version, borealis_util.argtype_borealis_version(version)
        )
        version = '2023.02.09.031143'
        self.assertEqual(
            version, borealis_util.argtype_borealis_version(version)
        )
        version = '111.2023.02.09.031143'
        self.assertEqual(
            version, borealis_util.argtype_borealis_version(version)
        )

        with self.assertRaises(errors.ArgTypeError):
            borealis_util.argtype_borealis_version('2023.02.09.1')
            borealis_util.argtype_borealis_version('2023.02.09-r1')
            borealis_util.argtype_borealis_version('borealis-dlc-2023.02.09')

    @mock.patch('bisect_kit.gs_util.cp')
    @mock.patch('bisect_kit.util.check_call')
    def test_download_rootfs_and_kernel(self, mock_check_call, mock_gs_cp):
        dummy_version = '2023.02.09'
        with tempfile.TemporaryDirectory() as dummy_chromeos_root:
            borealis_util.download_rootfs_and_kernel(
                dummy_chromeos_root, dummy_version
            )
            dummy_chromeos_root = pathlib.Path(dummy_chromeos_root)
            mock_gs_cp.assert_called_once_with(
                f'gs://chromeos-localmirror-private/borealis/borealis-dlc-{dummy_version}.tar.xz',
                dummy_chromeos_root / 'tmp/images-borealis-dlc',
            )
            mock_check_call.assert_called_once_with(
                'tar',
                'xf',
                dummy_chromeos_root
                / f'tmp/images-borealis-dlc/borealis-dlc-{dummy_version}.tar.xz',
                '-C',
                dummy_chromeos_root / 'src/platform/borealis',
                cwd=(dummy_chromeos_root / 'tmp/images-borealis-dlc'),
            )


if __name__ == '__main__':
    unittest.main()
