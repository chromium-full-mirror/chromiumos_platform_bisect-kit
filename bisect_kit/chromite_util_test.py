# Copyright 2025 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Test chromite_util module."""

import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from bisect_kit import chromite_util
from bisect_kit import git_util
from bisect_kit import util


class TestChromiteUtil(unittest.TestCase):
    """Test chromite_util functions."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_find_chromite_dir_cros_repo(self):
        # pylint: disable=protected-access

        (self.root / '.repo').mkdir()
        (self.root / 'chromite' / '.git').mkdir(parents=True)
        self.assertEqual(
            chromite_util._find_chromite_dir(self.root),
            self.root / 'chromite',
        )
        self.assertEqual(
            chromite_util._find_chromite_dir(self.root / 'some/subdir'),
            self.root / 'chromite',
        )

    def test_find_chromite_dir_chromium_gclient(self):
        # pylint: disable=protected-access

        (self.root / '.gclient').touch()
        (self.root / 'src/third_party/chromite/.git').mkdir(parents=True)
        self.assertEqual(
            chromite_util._find_chromite_dir(self.root),
            self.root / 'src/third_party/chromite',
        )

    def test_find_chromite_dir_chromium_submodule_root(self):
        # pylint: disable=protected-access

        (self.root / '.gitmodules').touch()
        (self.root / 'chromite/.git').mkdir(parents=True)
        self.assertEqual(
            chromite_util._find_chromite_dir(self.root),
            self.root / 'chromite',
        )

    def test_find_chromite_dir_chromium_submodule_src(self):
        # pylint: disable=protected-access

        (self.root / 'src/third_party/chromite/.git').mkdir(parents=True)
        (self.root / 'src/.gitmodules').touch()
        self.assertEqual(
            chromite_util._find_chromite_dir(self.root),
            self.root / 'src/third_party/chromite',
        )

    def test_find_chromite_dir_chromium_citc(self):
        # pylint: disable=protected-access

        (self.root / 'src/third_party/chromite').mkdir(parents=True)
        (self.root / 'src/third_party/chromite/__init__.py').touch()
        (self.root / '.citc').mkdir(parents=True)
        self.assertEqual(
            chromite_util._find_chromite_dir(self.root / 'src'),
            self.root / 'src/third_party/chromite',
        )

    def test_find_chromite_dir_not_found(self):
        # pylint: disable=protected-access

        self.assertIsNone(chromite_util._find_chromite_dir(self.root))

    def test_is_ancestor_or_equal(self):
        # pylint: disable=protected-access
        git_repo = self.root / 'repo'
        git_util.init(git_repo)

        git_util.commit_file(git_repo, 'file', 'commit 1', 'content 1')
        c1 = git_util.get_commit_hash(git_repo, 'HEAD')
        git_util.commit_file(git_repo, 'file', 'commit 2', 'content 2')
        c2 = git_util.get_commit_hash(git_repo, 'HEAD')
        git_util.commit_file(git_repo, 'file', 'commit 3', 'content 3')
        c3 = git_util.get_commit_hash(git_repo, 'HEAD')

        # Equal
        self.assertTrue(chromite_util._is_ancestor_or_equal(git_repo, c1, c1))
        self.assertTrue(chromite_util._is_ancestor_or_equal(git_repo, c2, c2))

        # Ancestor
        self.assertTrue(chromite_util._is_ancestor_or_equal(git_repo, c1, c2))
        self.assertTrue(chromite_util._is_ancestor_or_equal(git_repo, c1, c3))
        self.assertTrue(chromite_util._is_ancestor_or_equal(git_repo, c2, c3))

        # Not ancestor
        self.assertFalse(chromite_util._is_ancestor_or_equal(git_repo, c2, c1))
        self.assertFalse(chromite_util._is_ancestor_or_equal(git_repo, c3, c1))
        self.assertFalse(chromite_util._is_ancestor_or_equal(git_repo, c3, c2))

    def test_is_ancestor_or_equal_invalid_hash(self):
        # pylint: disable=protected-access
        git_repo = self.root / 'repo'
        git_util.init(git_repo)
        git_util.commit_file(git_repo, 'file', 'commit 1', 'content 1')
        c1 = git_util.get_commit_hash(git_repo, 'HEAD')

        with self.assertRaises(ValueError):
            chromite_util._is_ancestor_or_equal(git_repo, 'invalid-hash', c1)
        with self.assertRaises(ValueError):
            chromite_util._is_ancestor_or_equal(git_repo, c1, 'invalid-hash')
        with self.assertRaises(ValueError):
            chromite_util._is_ancestor_or_equal(
                git_repo, 'invalid-hash-1', 'invalid-hash-2'
            )
        # Short hash is invalid.
        with self.assertRaises(ValueError):
            chromite_util._is_ancestor_or_equal(git_repo, c1[:9], c1)

    @mock.patch.object(git_util, 'is_git_root', return_value=True)
    @mock.patch.object(git_util, 'get_commit_hash')
    @mock.patch.object(chromite_util, '_is_ancestor_or_equal')
    def test_get_required_python_version(
        self, mock_is_ancestor, mock_get_commit_hash, _
    ):
        # pylint: disable=protected-access
        chromite_dir = self.root / 'chromite'
        mock_get_commit_hash.return_value = 'some_hash'

        # Case 1: Current python is < 3.12, no override needed.
        with mock.patch.object(sys, 'version_info', (3, 11, 0)):
            mock_is_ancestor.return_value = False
            self.assertIsNone(
                chromite_util._get_required_python_version(chromite_dir)
            )

        # Case 2: Current python is 3.12, but chromite is old.
        with mock.patch.object(sys, 'version_info', (3, 12, 0)):
            mock_is_ancestor.return_value = False
            self.assertEqual(
                chromite_util._get_required_python_version(chromite_dir),
                ['python3.11'],
            )

        # Case 3: Current python is 3.12, and chromite is new enough.
        with mock.patch.object(sys, 'version_info', (3, 12, 0)):
            mock_is_ancestor.return_value = True
            self.assertIsNone(
                chromite_util._get_required_python_version(chromite_dir)
            )

    @mock.patch.object(chromite_util, '_find_chromite_dir')
    @mock.patch.object(chromite_util, '_get_required_python_version')
    @mock.patch.object(os.path, 'exists')
    @mock.patch.object(os, 'symlink')
    def test_run_with_python_version(
        self, mock_symlink, mock_exists, mock_get_version, mock_find_dir
    ):
        # pylint: disable=protected-access
        mock_func = mock.Mock()
        cwd = '/some/path'

        # Case 1: No python override needed.
        mock_get_version.return_value = None
        chromite_util._run_with_python_version(
            mock_func, 'arg1', 'arg2', cwd=cwd
        )
        mock_func.assert_called_once_with('arg1', 'arg2', cwd=cwd)
        mock_find_dir.assert_called_once_with(Path(cwd))

        # Case 2: Override needed, desired python exists.
        mock_func.reset_mock()
        mock_find_dir.reset_mock()
        mock_get_version.return_value = ['python3.11']
        mock_exists.return_value = True
        chromite_util._run_with_python_version(
            mock_func, 'arg1', 'arg2', cwd=cwd
        )
        mock_func.assert_called_once()
        # Check that env was modified
        self.assertIn('env', mock_func.call_args.kwargs)
        env = mock_func.call_args.kwargs['env']
        self.assertTrue(env['PATH'].startswith('/tmp/'))
        mock_symlink.assert_called_once()

    @mock.patch.object(chromite_util, '_run_with_python_version')
    def test_check_call(self, mock_execute):
        chromite_util.check_call('ls', '-l')
        mock_execute.assert_called_once_with(util.check_call, 'ls', '-l')

    @mock.patch.object(chromite_util, '_run_with_python_version')
    def test_check_output(self, mock_execute):
        chromite_util.check_output('ls', '-l')
        mock_execute.assert_called_once_with(util.check_output, 'ls', '-l')


if __name__ == '__main__':
    unittest.main()
