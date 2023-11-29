# Copyright 2017 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Test switch_git script."""

import unittest
from unittest import mock

import switch_git


@mock.patch('bisect_kit.common.config_logging', mock.Mock())
class TestSwitchGit(unittest.TestCase):
    """Test switch_git.py."""

    def test_main(self):
        with mock.patch('bisect_kit.cli.argtype_dir_path', lambda x: x):
            with mock.patch(
                'bisect_kit.git_util.checkout_version'
            ) as mock_func:
                git_repo = '/path/to/git/repo'
                rev = '123456789'
                switch_git.main(['--git-repo', git_repo, rev])

                mock_func.assert_called_with(git_repo, rev)


if __name__ == '__main__':
    unittest.main()
