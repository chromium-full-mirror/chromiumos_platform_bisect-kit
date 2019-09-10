# -*- coding: utf-8 -*-
# Copyright 2017 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Test bisect_git script."""

from __future__ import print_function
import shutil
import tempfile
import unittest

import mock

from bisect_kit import bisector_cli
from bisect_kit import git_util_test
import bisect_git


@mock.patch('bisect_kit.common.config_logging', mock.Mock())
class TestGitDomain(unittest.TestCase):
  """Test GitDomain class."""

  def setUp(self):
    self.git_repo = tempfile.mkdtemp()
    self.git = git_util_test.GitOperation(self.git_repo)
    self.git.init()

  def tearDown(self):
    shutil.rmtree(self.git_repo)

  def test_basic(self):
    """Tests basic functionality."""
    bisector = bisector_cli.BisectorCommandLine(bisect_git.GitDomain)

    self.git.create_commits(10)
    commits = self.git.commits
    old, new = commits[1], commits[-1]

    bisector.main('init', '--old', old, '--new', new, '--git_repo',
                  self.git_repo)

    bisector.main('config', 'switch', 'true')
    bisector.main('config', 'eval', 'true')

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
