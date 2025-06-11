# Copyright 2022 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Test eval_cros_tast.py script"""

import unittest
from unittest import mock

from bisect_kit import errors
from bisect_kit import git_util
from bisect_kit import repo_util
from bisect_kit import testing
import eval_cros_tast


class TestEvalCrosTast(unittest.TestCase):
    """Test eval_cros_tast."""

    def test_parse_results_json(self):
        self.assertEqual(
            eval_cros_tast.parse_results_json(
                testing.get_testdata_path('tast_test_result/pass'),
                'example.Pass',
            ),
            (True, None),
        )

        self.assertEqual(
            eval_cros_tast.parse_results_json(
                testing.get_testdata_path('tast_test_result/custom_tast_pass'),
                'bisector.Bisect',
            ),
            (True, None),
        )

        self.assertEqual(
            eval_cros_tast.parse_results_json(
                testing.get_testdata_path('tast_test_result/fail'),
                'example.Fail',
            ),
            (False, 'Finally, a fatal error'),
        )

        self.assertEqual(
            eval_cros_tast.parse_results_json(
                testing.get_testdata_path('tast_test_result/custom_tast_fail'),
                'bisector.Bisect',
            ),
            (False, 'Finally, a fatal error'),
        )

        with self.assertRaises(errors.ExternalError):
            eval_cros_tast.parse_results_json(
                testing.get_testdata_path('tast_test_result/pass'),
                'bad.testname',
            )


class TestAddTastPatchCl(unittest.TestCase):
    """Test add_tast_patch_cl function."""

    CHROMEOS_ROOT = '/path/to/chromeos_root'
    TAST_RUNTIME_REPO = '/path/to/chromeos_root/src/platform/tast'
    TAST_TESTS_PRIVATE_REPO = (
        '/path/to/chromeos_root/src/platform/tast-tests-private'
    )
    TAST_TESTS_REPO = '/path/to/chromeos_root/src/platform/tast-tests'

    @mock.patch.object(repo_util, 'cherry_pick')
    @mock.patch.object(git_util, 'reset_hard')
    @mock.patch.object(git_util, 'checkout_version')
    @mock.patch.object(git_util, 'reset_hard_to_remote_branch')
    @mock.patch.object(git_util, 'checkout_branch')
    def test_only_patch_cl(
        self,
        mock_checkout_branch,
        mock_reset_hard_to_remote_branch,
        mock_checkout_version,
        mock_reset_hard,
        mock_cherry_pick,
    ):
        """Test when only tast_patch_cl is provided."""
        tast_patch_cls = ['crrev.com/c/12345', 'crrev.com/c/67890']
        eval_cros_tast.add_tast_patch_cl(
            self.CHROMEOS_ROOT,
            tast_patch_cls=tast_patch_cls,
            tast_revision=None,
            tast_private_revision=None,
            tast_runtime_revision=None,
        )

        mock_checkout_branch.assert_any_call(self.TAST_RUNTIME_REPO, 'main')
        mock_reset_hard_to_remote_branch.assert_any_call(
            self.TAST_RUNTIME_REPO, 'cros/main'
        )
        mock_checkout_branch.assert_any_call(
            self.TAST_TESTS_PRIVATE_REPO, 'main'
        )
        mock_reset_hard_to_remote_branch.assert_any_call(
            self.TAST_TESTS_PRIVATE_REPO, 'cros-internal/main'
        )
        mock_checkout_branch.assert_any_call(self.TAST_TESTS_REPO, 'main')
        mock_reset_hard_to_remote_branch.assert_any_call(
            self.TAST_TESTS_REPO, 'cros/main'
        )
        mock_cherry_pick.assert_any_call(
            self.TAST_TESTS_REPO, tast_patch_cls[0]
        )
        mock_cherry_pick.assert_any_call(
            self.TAST_TESTS_REPO, tast_patch_cls[1]
        )
        mock_checkout_version.assert_not_called()
        mock_reset_hard.assert_not_called()

    @mock.patch.object(repo_util, 'cherry_pick')
    @mock.patch.object(git_util, 'reset_hard')
    @mock.patch.object(git_util, 'checkout_version')
    @mock.patch.object(git_util, 'reset_hard_to_remote_branch')
    @mock.patch.object(git_util, 'checkout_branch')
    def test_patch_cl_and_specific_revisions(
        self,
        mock_checkout_branch,
        mock_reset_hard_to_remote_branch,
        mock_checkout_version,
        mock_reset_hard,
        mock_cherry_pick,
    ):
        """Test when tast_patch_cl and specific revisions are provided."""
        tast_patch_cl = 'crrev.com/c/12345'
        tast_revision = 'aaaaaaaaaa'
        tast_private_revision = 'bbbbbbbbbb'
        tast_runtime_revision = 'cccccccccc'

        eval_cros_tast.add_tast_patch_cl(
            self.CHROMEOS_ROOT,
            tast_patch_cls=[tast_patch_cl],
            tast_revision=tast_revision,
            tast_private_revision=tast_private_revision,
            tast_runtime_revision=tast_runtime_revision,
        )

        mock_checkout_version.assert_any_call(
            self.TAST_RUNTIME_REPO, tast_runtime_revision
        )
        mock_reset_hard.assert_any_call(self.TAST_RUNTIME_REPO)
        mock_checkout_version.assert_any_call(
            self.TAST_TESTS_PRIVATE_REPO, tast_private_revision
        )
        mock_reset_hard.assert_any_call(self.TAST_TESTS_PRIVATE_REPO)
        mock_checkout_version.assert_any_call(
            self.TAST_TESTS_REPO, tast_revision
        )
        mock_reset_hard.assert_any_call(self.TAST_TESTS_REPO)
        mock_cherry_pick.assert_called_once_with(
            self.TAST_TESTS_REPO, tast_patch_cl
        )
        mock_checkout_branch.assert_not_called()
        mock_reset_hard_to_remote_branch.assert_not_called()

    @mock.patch.object(repo_util, 'cherry_pick')
    @mock.patch.object(git_util, 'reset_hard')
    @mock.patch.object(git_util, 'checkout_version')
    @mock.patch.object(git_util, 'reset_hard_to_remote_branch')
    @mock.patch.object(git_util, 'checkout_branch')
    def test_only_specific_revisions(
        self,
        mock_checkout_branch,
        mock_reset_hard_to_remote_branch,
        mock_checkout_version,
        mock_reset_hard,
        mock_cherry_pick,
    ):
        """Test when only specific revisions are provided (no patch CL)."""
        tast_revision = 'aaaaaaaaaa'
        tast_private_revision = 'bbbbbbbbbb'
        tast_runtime_revision = 'cccccccccc'

        eval_cros_tast.add_tast_patch_cl(
            self.CHROMEOS_ROOT,
            tast_patch_cls=[],
            tast_revision=tast_revision,
            tast_private_revision=tast_private_revision,
            tast_runtime_revision=tast_runtime_revision,
        )

        mock_checkout_version.assert_any_call(
            self.TAST_RUNTIME_REPO, tast_runtime_revision
        )
        mock_reset_hard.assert_any_call(self.TAST_RUNTIME_REPO)
        mock_checkout_version.assert_any_call(
            self.TAST_TESTS_PRIVATE_REPO, tast_private_revision
        )
        mock_reset_hard.assert_any_call(self.TAST_TESTS_PRIVATE_REPO)
        mock_checkout_version.assert_any_call(
            self.TAST_TESTS_REPO, tast_revision
        )
        mock_reset_hard.assert_any_call(self.TAST_TESTS_REPO)
        mock_cherry_pick.assert_not_called()
        mock_checkout_branch.assert_not_called()
        mock_reset_hard_to_remote_branch.assert_not_called()

    @mock.patch.object(repo_util, 'cherry_pick')
    @mock.patch.object(git_util, 'reset_hard')
    @mock.patch.object(git_util, 'checkout_version')
    @mock.patch.object(git_util, 'reset_hard_to_remote_branch')
    @mock.patch.object(git_util, 'checkout_branch')
    def test_revisions_are_main(
        self,
        mock_checkout_branch,
        mock_reset_hard_to_remote_branch,
        mock_checkout_version,
        mock_reset_hard,
        mock_cherry_pick,
    ):
        """Test when all revision arguments are explicitly 'main'."""
        eval_cros_tast.add_tast_patch_cl(
            self.CHROMEOS_ROOT,
            tast_patch_cls=[],
            tast_revision='main',
            tast_private_revision='main',
            tast_runtime_revision='main',
        )

        # Assertions for TAST_RUNTIME_REPO
        mock_checkout_branch.assert_any_call(self.TAST_RUNTIME_REPO, 'main')
        mock_reset_hard_to_remote_branch.assert_any_call(
            self.TAST_RUNTIME_REPO, 'cros/main'
        )
        # Assertions for TAST_TESTS_PRIVATE_REPO
        mock_checkout_branch.assert_any_call(
            self.TAST_TESTS_PRIVATE_REPO, 'main'
        )
        mock_reset_hard_to_remote_branch.assert_any_call(
            self.TAST_TESTS_PRIVATE_REPO, 'cros-internal/main'
        )
        # Assertions for TAST_TESTS_REPO
        mock_checkout_branch.assert_any_call(self.TAST_TESTS_REPO, 'main')
        mock_reset_hard_to_remote_branch.assert_any_call(
            self.TAST_TESTS_REPO, 'cros/main'
        )

        mock_checkout_version.assert_not_called()
        mock_reset_hard.assert_not_called()
        mock_cherry_pick.assert_not_called()

    @mock.patch.object(repo_util, 'cherry_pick')
    @mock.patch.object(git_util, 'reset_hard')
    @mock.patch.object(git_util, 'checkout_version')
    def test_no_arguments(
        self, mock_checkout_version, mock_reset_hard, mock_cherry_pick
    ):
        """Test when no arguments are provided."""
        eval_cros_tast.add_tast_patch_cl(
            self.CHROMEOS_ROOT, [], None, None, None
        )
        mock_checkout_version.assert_not_called()
        mock_reset_hard.assert_not_called()
        mock_cherry_pick.assert_not_called()


if __name__ == '__main__':
    unittest.main()
