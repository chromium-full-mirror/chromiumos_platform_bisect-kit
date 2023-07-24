# Copyright 2023 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Test borealis_util module."""

import json
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

    @mock.patch('bisect_kit.gs_util.ls')
    def test_build_revlist_from_gs(self, mock_gs_ls):
        mock_gs_ls.side_effect = [
            [
                'gs://chromeos-localmirror-private/borealis/borealis-dlc-2023.02.08.031142.tar.xz',
                'gs://chromeos-localmirror-private/borealis/borealis-dlc-2023.02.09.031143.tar.xz',
                'gs://chromeos-localmirror-private/borealis/borealis-dlc-2023.02.10.031144.tar.xz',
                'gs://chromeos-localmirror-private/borealis/borealis-dlc-2023.02.11.031145.tar.xz',
                'gs://chromeos-localmirror-private/borealis/borealis-dlc-2023.02.12.031146.tar.xz',
            ]
        ]
        revlist, details = borealis_util.build_revlist_from_gs(
            '2023.02.09', '2023.02.11'
        )
        self.assertListEqual(
            revlist,
            ['2023.02.09.031143', '2023.02.10.031144', '2023.02.11.031145'],
        )
        self.assertDictEqual(details, {})

    def test_extract_version_from_commit_summary(self):
        self.assertEqual(
            '2023.02.09.031143',
            borealis_util.extract_version_from_commit_summary(
                '    VERSION-PIN: updating version pin borealis-dlc to 2023.02.09.031143'
            ),
        )
        self.assertEqual(
            '2023.02.09.031143',
            borealis_util.extract_version_from_commit_summary(
                '    borealis-dlc: Automatic uprev to 2023.02.09.031143-r1.'
            ),
        )

        self.assertEqual(
            None,
            borealis_util.extract_version_from_commit_summary(
                '    VERSION-PIN: updating version pin borealis-dlc-chroot to 2023.02.09.031143'
            ),
        )
        self.assertEqual(
            None,
            borealis_util.extract_version_from_commit_summary(
                '    borealis-dlc-chroot: Automatic uprev to 2023.02.09.031143-r1.'
            ),
        )

    @mock.patch('bisect_kit.dlc.borealis_util.build_revlist_from_gs')
    def test_extract_revlist(self, mock_build_revlist_from_gs):
        input_details = json.loads(
            """\
            {
              "R111-15000.0.0~R111-15001.0.0/1020": {
                "actions": [
                  {
                    "timestamp": 1686000000,
                    "action_type": "commit",
                    "path": "src/private-overlays/chromeos-partner-overlay",
                    "repo_url": "https://repo-url/",
                    "rev": "f9c930000110f78fada1ae381d397ba5c0000000",
                    "text": "commit text",
                    "commit_summary": "VERSION-PIN: updating version pin borealis-dlc to 2023.02.09.031143"
                  }
                ]
              },
              "R111-15000.0.0~R111-15001.0.0/1021": {
                "actions": [
                  {
                    "timestamp": 1686000001,
                    "action_type": "commit",
                    "path": "src/path",
                    "repo_url": "https://repo-url/",
                    "rev": "f9c930000110f78fada1ae381d397ba5c0000001",
                    "text": "commit text",
                    "commit_summary": "commit summary"
                  }
                ]
              },
              "R111-15000.0.0~R111-15001.0.0/1022": {
                "actions": [
                  {
                    "timestamp": 1686000002,
                    "action_type": "commit",
                    "path": "src/private-overlays/chromeos-partner-overlay",
                    "repo_url": "https://repo-url/",
                    "rev": "f9c930000110f78fada1ae381d397ba5c0000002",
                    "text": "commit text",
                    "commit_summary": "VERSION-PIN: updating version pin borealis-dlc to 2023.02.10.031144"
                  }
                ]
              }
            }
            """
        )
        expected_revlist = ['2023.02.09.031143', '2023.02.10.031144']
        expected_details = {
            '2023.02.09.031143': input_details[
                'R111-15000.0.0~R111-15001.0.0/1020'
            ],
            '2023.02.10.031144': input_details[
                'R111-15000.0.0~R111-15001.0.0/1022'
            ],
        }
        mock_build_revlist_from_gs.side_effect = lambda r1, r2: ([r1, r2], {})

        revlist, details = borealis_util.extract_revlist(input_details)
        self.assertListEqual(revlist, expected_revlist)
        self.assertDictEqual(details, expected_details)


if __name__ == '__main__':
    unittest.main()
