# Copyright 2017 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Test bisect_git script."""

import shutil
import tempfile
import unittest
from unittest import mock

import bisect_git
from bisect_kit import bisector_cli
from bisect_kit import dut_manager as dut_manager_module
from bisect_kit import git_util_test
from bisect_kit import testing


@mock.patch('bisect_kit.common.config_logging', mock.Mock())
class TestGitDomain(unittest.TestCase):
    """Test GitDomain class."""

    def setUp(self):
        self.session_base_patcher = testing.SessionBasePatcher()
        self.session_base_patcher.patch()
        self.git_repo = tempfile.mkdtemp()
        self.git = git_util_test.GitOperation(self.git_repo)
        self.git.init()

    def tearDown(self):
        shutil.rmtree(self.git_repo)
        self.session_base_patcher.reset()

    def test_basic(self):
        """Tests basic functionality."""
        dut_manager = dut_manager_module.DutManager(
            'PreAllocate',
            states=None,
            dut_allocate_spec=None,
            pre_allocated_dut='pre_allocated_dut',
            should_auto_allocate=False,
        )
        bisector = bisector_cli.BisectorCommandLine(
            bisect_git.GitDomain, dut_manager
        )

        self.git.create_commits(10)
        revs = self.git.revs
        old, new = revs[1], revs[-1]

        bisector.main(
            'init', '--old', old, '--new', new, '--git-repo', self.git_repo
        )

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
