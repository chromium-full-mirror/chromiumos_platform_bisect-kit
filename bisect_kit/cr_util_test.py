# Copyright 2017 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Test cr_util module"""

import os
import tempfile
import textwrap
import unittest
from unittest import mock

from bisect_kit import cr_util
from bisect_kit import gclient_util
from bisect_kit import git_util
from bisect_kit import git_util_test


class TestCrUtil(unittest.TestCase):
    """Test cr_util functions."""

    def test_is_chrome_version(self):
        assert cr_util.is_chrome_version('59.0.3065.0')

        assert not cr_util.is_chrome_version('59.0.3065')
        assert not cr_util.is_chrome_version('59.0.3065.0.0')

    def test_is_commit_position(self):
        assert cr_util.is_commit_position('refs/heads/main@{#123456}')
        assert cr_util.is_commit_position('refs/branch-heads/6713@{#1}')

        assert not cr_util.is_commit_position('131.0.6741.0')
        assert not cr_util.is_commit_position('131.0.6741.0~131.0.6742.0/50')

    def test_is_version_lesseq(self):
        assert cr_util.is_version_lesseq('75.0.3731.0', '75.0.3731.0')
        assert cr_util.is_version_lesseq('75.0.3731.0', '75.0.3732.0')
        assert not cr_util.is_version_lesseq('75.0.3732.0', '75.0.3731.0')

        assert cr_util.is_version_lesseq('74.0.3726.0', '75.0.3731.0')

    def test_is_direct_relative_version(self):
        assert cr_util.is_direct_relative_version('74.0.3726.0', '75.0.3731.0')
        assert cr_util.is_direct_relative_version('75.0.3731.0', '74.0.3726.0')
        assert cr_util.is_direct_relative_version('73.0.3683.0', '75.0.3731.0')
        assert not cr_util.is_direct_relative_version(
            '73.0.3683.1', '75.0.3731.0'
        )
        assert not cr_util.is_direct_relative_version(
            '75.0.3731.0', '73.0.3683.1'
        )

    def test_extract_branch_from_version(self):
        self.assertEqual(
            cr_util.extract_branch_from_version('59.0.3064.0'), '3064'
        )

    def test_extract_milestone_from_version(self):
        self.assertEqual(
            cr_util.extract_milestone_from_version('59.0.3064.0'), '59'
        )

    def test_get_ancestors(self):
        # Disabling protected member access cros lint error
        # pylint: disable=protected-access
        chrome_version_list = [
            '100.0.1.0',
            '100.0.1.1',
            '100.0.2.0',
            '100.0.2.1',
            '100.0.3.0',
            '100.0.3.1',
            '100.0.3.2',
        ]

        self.assertEqual(
            cr_util._get_ancestors('100.0.3.1', chrome_version_list),
            ['100.0.1.0', '100.0.2.0', '100.0.3.0', '100.0.3.1'],
        )
        self.assertEqual(
            cr_util._get_ancestors('100.0.2.1', chrome_version_list),
            ['100.0.1.0', '100.0.2.0', '100.0.2.1'],
        )

    def test_get_lca(self):
        chrome_version_list = [
            '100.0.1.0',
            '100.0.1.1',
            '100.0.2.0',
            '100.0.2.1',
            '100.0.3.0',
            '100.0.3.1',
            '100.0.3.2',
        ]
        self.assertEqual(
            cr_util.get_lca('100.0.3.1', '100.0.1.1', chrome_version_list),
            '100.0.1.0',
        )
        self.assertEqual(
            cr_util.get_lca('100.0.1.1', '100.0.3.1', chrome_version_list),
            '100.0.1.0',
        )
        self.assertEqual(
            cr_util.get_lca('100.0.1.1', '100.0.1.1', chrome_version_list),
            '100.0.1.1',
        )
        self.assertEqual(
            cr_util.get_lca('100.0.2.0', '100.0.3.1', chrome_version_list),
            '100.0.2.0',
        )

    def test_get_RBE_environment_variables(self):
        os.environ['SKYLAB_CLOUD_SERVICE_ACCOUNT_JSON'] = 'bisect_runner_path'

        # pylint: disable=protected-access
        env = cr_util.get_RBE_environment_variables()

        self.assertTrue('RBE_credential_file' in env)
        self.assertEqual(env['RBE_credential_file'], 'bisect_runner_path')

        self.assertTrue('RBE_use_application_default_credentials' in env)
        self.assertEqual(env['RBE_use_application_default_credentials'], 'true')

        self.assertTrue('RBE_use_gce_credentials' in env)
        self.assertEqual(env['RBE_use_gce_credentials'], 'false')

        self.assertTrue('RBE_automatic_auth' in env)
        self.assertEqual(env['RBE_automatic_auth'], 'false')

    def _prepare_fake_chrome_repository(self, fake_chrome_src) -> list[str]:
        git = git_util_test.GitOperation(fake_chrome_src)
        git.init(initial_branch=gclient_util.DEFAULT_BRANCH_NAME)
        commits = [
            git.add_commit(
                '2024-01-01T10:00:00',
                textwrap.dedent(
                    '''\
                Foo

                Cr-Commit-Position: refs/heads/main@{#1001}
                '''
                ),
                'foo',
                'foo contents',
            ),
            git.add_commit(
                '2024-01-02T10:00:00',
                textwrap.dedent(
                    '''\
                Revert "Foo"

                Original change's description:
                > Foo
                >
                > Cr-Commit-Position: refs/heads/main@{#1001}

                Cr-Commit-Position: refs/heads/main@{#1002}
                '''
                ),
                'foo',
                None,
            ),
            git.add_commit(
                '2024-01-03T10:00:00',
                textwrap.dedent(
                    '''\
                Bar

                Cr-Commit-Position: refs/heads/main@{#1003}
                '''
                ),
                'bar',
                'bar contents',
            ),
        ]
        return commits

    def test_query_git_rev_by_commit_position_remotely(self):
        self.assertEqual(
            'e261b16ec7149db6debc70a47f93b20abc8de334',
            cr_util.query_git_rev_by_commit_position_remotely('308022'),
        )
        self.assertEqual(
            'e261b16ec7149db6debc70a47f93b20abc8de334',
            cr_util.query_git_rev_by_commit_position_remotely(308022),
        )

    def test_query_git_rev_by_commit_position(self):
        with tempfile.TemporaryDirectory() as fake_chrome_src:
            commits = self._prepare_fake_chrome_repository(fake_chrome_src)
            self.assertEqual(
                commits[0],
                cr_util.query_git_rev_by_commit_position(
                    fake_chrome_src, 'refs/heads/main@{#1001}'
                ),
            )
            self.assertEqual(
                commits[1],
                cr_util.query_git_rev_by_commit_position(
                    fake_chrome_src, 'refs/heads/main@{#1002}'
                ),
            )
            self.assertEqual(
                commits[2],
                cr_util.query_git_rev_by_commit_position(
                    fake_chrome_src, 'refs/heads/main@{#1003}'
                ),
            )

    def test_get_commit_position(self):
        with tempfile.TemporaryDirectory() as fake_chrome_src:
            commits = self._prepare_fake_chrome_repository(fake_chrome_src)
            self.assertEqual(
                'refs/heads/main@{#1001}',
                cr_util.get_commit_position(
                    git_util.get_commit_metadata(fake_chrome_src, commits[0])
                ),
            )
            self.assertEqual(
                'refs/heads/main@{#1002}',
                cr_util.get_commit_position(
                    git_util.get_commit_metadata(fake_chrome_src, commits[1])
                ),
            )
            self.assertEqual(
                'refs/heads/main@{#1003}',
                cr_util.get_commit_position(
                    git_util.get_commit_metadata(fake_chrome_src, commits[2])
                ),
            )

    def test_build_revlist(self):
        with tempfile.TemporaryDirectory() as fake_chrome_src:
            commits = self._prepare_fake_chrome_repository(fake_chrome_src)
            revlist, details = cr_util.build_revlist(
                fake_chrome_src,
                'refs/heads/main@{#1001}',
                'refs/heads/main@{#1003}',
            )
            self.assertListEqual(
                revlist,
                [
                    'refs/heads/main@{#1001}',
                    'refs/heads/main@{#1002}',
                    'refs/heads/main@{#1003}',
                ],
            )
            self.assertDictEqual(
                details,
                {
                    'refs/heads/main@{#1001}': {
                        'actions': [
                            {
                                'action_type': 'commit',
                                'commit_summary': 'Foo',
                                'path': 'src',
                                'repo_url': 'https://chromium.googlesource.com/chromium/src.git',
                                'rev': commits[0],
                            },
                        ]
                    },
                    'refs/heads/main@{#1002}': {
                        'actions': [
                            {
                                'action_type': 'commit',
                                'commit_summary': 'Revert "Foo"',
                                'path': 'src',
                                'repo_url': 'https://chromium.googlesource.com/chromium/src.git',
                                'rev': commits[1],
                            },
                        ]
                    },
                    'refs/heads/main@{#1003}': {
                        'actions': [
                            {
                                'action_type': 'commit',
                                'commit_summary': 'Bar',
                                'path': 'src',
                                'repo_url': 'https://chromium.googlesource.com/chromium/src.git',
                                'rev': commits[2],
                            },
                        ]
                    },
                },
            )


class SimpleChromeShellTest(unittest.TestCase):
    """Test simple_chrome_shell function."""

    @mock.patch('bisect_kit.util.check_output')
    def test_simple_chrome_shell_internal(self, mock_check_output):
        # pylint: disable=protected-access
        cr_util._simple_chrome_shell(
            '/path/to/chrome',
            'board_name',
            command=['build', 'chrome'],
            is_public_build=False,
        )
        mock_check_output.assert_called_once()
        call_args, _ = mock_check_output.call_args
        self.assertIn('--internal', call_args)
        self.assertNotIn('--use-external-config', call_args)

    @mock.patch('bisect_kit.util.check_output')
    def test_simple_chrome_shell_gn_extra_args(self, mock_check_output):
        # pylint: disable=protected-access
        cr_util._simple_chrome_shell(
            '/path/to/chrome',
            'board_name',
            command=['build', 'chrome'],
            is_public_build=False,
            gn_extra_args='dcheck_always_on=true',
        )
        mock_check_output.assert_called_once()
        call_args, _ = mock_check_output.call_args
        self.assertIn('--gn-extra-args', call_args)
        self.assertIn('dcheck_always_on=true', call_args)

    @mock.patch('bisect_kit.util.check_output')
    def test_simple_chrome_shell_public(self, mock_check_output):
        # pylint: disable=protected-access
        cr_util._simple_chrome_shell(
            '/path/to/chrome',
            'board_name',
            command=['build', 'chrome'],
            is_public_build=True,
        )
        mock_check_output.assert_called_once()
        call_args, _ = mock_check_output.call_args
        self.assertNotIn('--internal', call_args)
        self.assertIn('--use-external-config', call_args)

    @mock.patch('bisect_kit.util.check_output')
    def test_simple_chrome_shell_kwargs_internal(self, mock_check_output):
        # pylint: disable=protected-access
        cr_util._simple_chrome_shell(
            '/path/to/chrome',
            'board_name',
            command=['build', 'chrome'],
            is_public_build=False,
        )
        mock_check_output.assert_called_once()
        call_args, _ = mock_check_output.call_args
        self.assertIn('--internal', call_args)
        self.assertNotIn('--use-external-config', call_args)


if __name__ == '__main__':
    unittest.main()
