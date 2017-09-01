# Copyright 2017 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Test bisect_git script."""

from __future__ import print_function
import unittest

import mock

from bisect_kit import cli
import bisect_git


@mock.patch('bisect_kit.common.config_logging', mock.Mock())
class TestGitDomain(unittest.TestCase):
  """Test GitDomain class."""

  def setUp(self):
    # All git operations should have been mocked, nobody will access this path
    # on disk.
    self.git_repo = '/non/existing/git/repo/path'

  def test_basic(self):
    """Tests basic functionality."""
    bisector = cli.BisectorCommandLineInterface(bisect_git.GitDomain)

    start = 12345678000
    fake_commits = map(str, range(start, start + 10))
    old, new = fake_commits[0], fake_commits[-1]

    def is_containing_commit(git_repo, rev):
      self.assertEqual(git_repo, self.git_repo)
      return rev in fake_commits

    def get_revlist(git_repo, old, new):
      self.assertEqual(git_repo, self.git_repo)
      self.assertEqual(old, fake_commits[0])
      self.assertEqual(new, fake_commits[-1])
      return fake_commits

    with mock.patch('bisect_kit.cli.argtype_dir_path', lambda x: x):
      with mock.patch.multiple(
          'bisect_kit.git_util',
          is_containing_commit=is_containing_commit,
          get_revlist=get_revlist):
        bisector.main('init', '--old', old, '--new', new, '--git_repo',
                      self.git_repo)

    bisector.main('config', 'switch', 'abcd')
    bisector.main('config', 'eval', 'efgh')

    with mock.patch('bisect_kit.util.Popen') as mock_popen:
      run_mocks = [mock.Mock(), mock.Mock()]
      run_mocks[0].wait.return_value = 0
      run_mocks[1].wait.return_value = 0
      mock_popen.side_effect = run_mocks
      bisector.main('run', old)

      switch_env = mock_popen.call_args_list[0][1]['env']
      self.assertEqual(switch_env.get('GIT_REV'), old)
      self.assertEqual(switch_env.get('GIT_REPO'), self.git_repo)

      eval_env = mock_popen.call_args_list[1][1]['env']
      self.assertEqual(eval_env.get('GIT_REV'), old)
      self.assertEqual(eval_env.get('GIT_REPO'), self.git_repo)
    bisector.main('view')


if __name__ == '__main__':
  unittest.main()
