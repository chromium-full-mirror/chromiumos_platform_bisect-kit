#!/usr/bin/env python3
# Copyright 2025 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

import os
import subprocess
import tempfile
import unittest
from unittest import mock

# import bisect_cr_localbuild_internal
from bisect_kit import errors
from bisect_kit import gclient_util
from bisect_kit import gerrit_util
from bisect_kit import git_util
import switch_cros_cr_localbuild_internal


class TestPatchCls(unittest.TestCase):
    """
    Tests for the patch_cls function in
    switch_cros_cr_localbuild_internal.py.
    """

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.chrome_src_path = self.temp_dir.name
        git_util.init(self.chrome_src_path)
        git_util.commit_file(
            self.chrome_src_path, "TESTFILE", "MESSAGE", "CONTENT"
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    @mock.patch.object(gerrit_util, 'get_gerrit_cl_info')
    @mock.patch.object(git_util, 'fetch')
    @mock.patch.object(git_util, 'is_ancestor_commit')
    @mock.patch.object(git_util, 'cherry_pick')
    def test_patch_single_cl_success(
        self, mock_cherry_pick, mock_is_ancestor, mock_fetch, mock_get_info
    ):
        """Tests successful application of a single CL."""
        cl_number = "12345"
        chromium_patch_cls = [cl_number]
        mock_get_info.return_value = {
            "project": "chromium/src",
            "current_revision": "deadbeef",
            "revisions": {
                "deadbeef": {
                    "fetch": {
                        "http": {
                            "url": "https://chromium.googlesource.com/chromium/src.git",
                            "ref": "refs/changes/45/12345/1",
                        }
                    }
                }
            },
        }
        mock_is_ancestor.return_value = False

        switch_cros_cr_localbuild_internal.patch_cls(
            self.chrome_src_path, chromium_patch_cls
        )

        mock_get_info.assert_called_once_with(cl_number)
        mock_fetch.assert_called_once_with(
            self.chrome_src_path,
            "https://chromium.googlesource.com/chromium/src.git",
            "refs/changes/45/12345/1",
        )
        mock_is_ancestor.assert_called_once_with(
            self.chrome_src_path, "FETCH_HEAD", "HEAD"
        )
        mock_cherry_pick.assert_called_once_with(
            self.chrome_src_path, "FETCH_HEAD"
        )

    @mock.patch.object(gerrit_util, 'get_gerrit_cl_info')
    @mock.patch.object(git_util, 'fetch')
    @mock.patch.object(git_util, 'is_ancestor_commit')
    @mock.patch.object(git_util, 'cherry_pick')
    def test_patch_multiple_cls_success(
        self, mock_cherry_pick, mock_is_ancestor, mock_fetch, mock_get_info
    ):
        """Tests successful application of multiple CLs."""
        cl1 = "123"
        cl2 = "456"
        chromium_patch_cls = [cl1, cl2]
        mock_is_ancestor.return_value = False

        def get_info_side_effect(cl_num):
            if cl_num == cl1:
                return {
                    "project": "chromium/src",
                    "current_revision": "rev1",
                    "revisions": {
                        "rev1": {
                            "fetch": {"http": {"url": "url1", "ref": "ref1"}}
                        }
                    },
                }
            if cl_num == cl2:
                return {
                    "project": "chromium/src",
                    "current_revision": "rev2",
                    "revisions": {
                        "rev2": {
                            "fetch": {"http": {"url": "url2", "ref": "ref2"}}
                        }
                    },
                }
            return None

        mock_get_info.side_effect = get_info_side_effect

        switch_cros_cr_localbuild_internal.patch_cls(
            self.chrome_src_path, chromium_patch_cls
        )

        self.assertEqual(mock_get_info.call_count, 2)
        mock_get_info.assert_any_call(cl1)
        mock_get_info.assert_any_call(cl2)

        self.assertEqual(mock_fetch.call_count, 2)
        mock_fetch.assert_any_call(self.chrome_src_path, "url1", "ref1")
        mock_fetch.assert_any_call(self.chrome_src_path, "url2", "ref2")

        self.assertEqual(mock_is_ancestor.call_count, 2)
        mock_is_ancestor.assert_any_call(
            self.chrome_src_path, "FETCH_HEAD", "HEAD"
        )

        self.assertEqual(mock_cherry_pick.call_count, 2)
        mock_cherry_pick.assert_any_call(self.chrome_src_path, "FETCH_HEAD")

    @mock.patch.object(gerrit_util, 'get_gerrit_cl_info')
    @mock.patch.object(git_util, 'fetch')
    @mock.patch.object(git_util, 'is_ancestor_commit')
    @mock.patch.object(git_util, 'cherry_pick')
    def test_patch_cl_already_merged(
        self, mock_cherry_pick, mock_is_ancestor, mock_fetch, mock_get_info
    ):
        """Tests that cherry-pick is skipped if CL is already merged."""
        cl_number = "12345"
        chromium_patch_cls = [cl_number]
        mock_get_info.return_value = {
            "project": "chromium/src",
            "current_revision": "deadbeef",
            "revisions": {
                "deadbeef": {
                    "fetch": {
                        "http": {
                            "url": "https://chromium.googlesource.com/chromium/src.git",
                            "ref": "refs/changes/45/12345/1",
                        }
                    }
                }
            },
        }
        mock_is_ancestor.return_value = True

        switch_cros_cr_localbuild_internal.patch_cls(
            self.chrome_src_path, chromium_patch_cls
        )

        mock_get_info.assert_called_once_with(cl_number)
        mock_fetch.assert_called_once_with(
            self.chrome_src_path,
            "https://chromium.googlesource.com/chromium/src.git",
            "refs/changes/45/12345/1",
        )
        mock_is_ancestor.assert_called_once_with(
            self.chrome_src_path, "FETCH_HEAD", "HEAD"
        )
        mock_cherry_pick.assert_not_called()

    @mock.patch.object(gerrit_util, 'get_gerrit_cl_info')
    def test_patch_cl_fetch_info_fails(self, mock_get_info):
        """Tests failure when get_gerrit_cl_info returns None."""
        cl_number = "12345"
        chromium_patch_cls = [cl_number]
        mock_get_info.return_value = None

        with self.assertRaisesRegex(
            errors.PatchFetchError,
            f"Failed to download the chromium CL \\({cl_number}\\).",
        ):
            switch_cros_cr_localbuild_internal.patch_cls(
                self.chrome_src_path, chromium_patch_cls
            )
        mock_get_info.assert_called_once_with(cl_number)

    @mock.patch.object(gerrit_util, 'get_gerrit_cl_info')
    @mock.patch.object(git_util, 'fetch')
    @mock.patch.object(git_util, 'is_ancestor_commit')
    @mock.patch.object(git_util, 'cherry_pick')
    def test_patch_cl_cherry_pick_fails(
        self, mock_cherry_pick, mock_is_ancestor, mock_fetch, mock_get_info
    ):
        """Tests failure during cherry-pick."""
        cl_number = "12345"
        chromium_patch_cls = [cl_number]
        mock_get_info.return_value = {
            "project": "chromium/src",
            "current_revision": "deadbeef",
            "revisions": {
                "deadbeef": {"fetch": {"http": {"url": "url", "ref": "ref"}}}
            },
        }
        mock_is_ancestor.return_value = False
        mock_cherry_pick.side_effect = subprocess.CalledProcessError(
            1, "git cherry-pick"
        )

        with self.assertRaisesRegex(
            errors.PatchApplyError,
            f"Failed to apply the chromium CL \\({cl_number}\\) on [0-9a-f]{{40}}\\.",
        ):
            switch_cros_cr_localbuild_internal.patch_cls(
                self.chrome_src_path, chromium_patch_cls
            )

        mock_get_info.assert_called_once_with(cl_number)
        mock_fetch.assert_called_once_with(self.chrome_src_path, "url", "ref")
        mock_is_ancestor.assert_called_once_with(
            self.chrome_src_path, "FETCH_HEAD", "HEAD"
        )
        mock_cherry_pick.assert_called_once_with(
            self.chrome_src_path, "FETCH_HEAD"
        )


class TestSwitchMain(unittest.TestCase):
    """
    Tests for the switch_main function in
    switch_cros_cr_localbuild_internal.py.
    """

    def setUp(self):
        self.CHROME_REVISION = '123456abcdef'
        self.temp_dir = tempfile.TemporaryDirectory()
        self.chrome_root = os.path.join(self.temp_dir.name, 'chrome')
        self.chrome_src = os.path.join(self.chrome_root, 'src')
        self.chrome_mirror = os.path.join(self.temp_dir.name, 'chrome_mirror')
        os.makedirs(self.chrome_src)
        os.makedirs(self.chrome_mirror)
        git_util.init(self.chrome_src)
        git_util.commit_file(self.chrome_src, "TESTFILE", "MESSAGE", "CONTENT")

        self.common_args = [
            '--chrome-rev',
            self.CHROME_REVISION,
            '--target',
            'eve',
            '--board',
            'eve',
            '--board-cpu-arch',
            'amd64',
            '--chrome-root',
            self.chrome_root,
            '--chrome-mirror',
            self.chrome_mirror,
        ]

    def tearDown(self):
        self.temp_dir.cleanup()

    @mock.patch(
        'switch_cros_cr_localbuild_internal.cache_util.BuildArtifactsCache'
    )
    @mock.patch.object(switch_cros_cr_localbuild_internal, 'patch_cls')
    @mock.patch.object(gclient_util, 'sync')
    def test_sync_called(
        self,
        mock_sync,
        mock_patch_cls,
        mock_build_artifacts_cache,
    ):
        """Tests that sync is called when sync is enabled."""
        args = self.common_args + [
            '--sync-code-only',
        ]

        # Simulate the cache doesn't hit.
        mock_build_artifacts_cache.return_value.cache_hit.return_value = False

        switch_cros_cr_localbuild_internal.switch_main(args)

        mock_sync.assert_called_once()
        mock_patch_cls.assert_not_called()

    @mock.patch(
        'switch_cros_cr_localbuild_internal.cache_util.BuildArtifactsCache'
    )
    @mock.patch.object(switch_cros_cr_localbuild_internal, 'patch_cls')
    @mock.patch.object(gclient_util, 'sync')
    def test_sync_not_called(
        self,
        mock_sync,
        mock_patch_cls,
        mock_build_artifacts_cache,
    ):
        """Tests that sync and patch_cls are not called when sync is disabled."""
        args = self.common_args + [
            '--no-sync-code',
            '--no-deploy',
            '--no-build',
            '--allow-no-ops',
        ]
        # Simulate the cache doesn't hit.
        mock_build_artifacts_cache.return_value.cache_hit.return_value = False

        switch_cros_cr_localbuild_internal.switch_main(args)

        mock_sync.assert_not_called()
        mock_patch_cls.assert_not_called()

    @mock.patch(
        'switch_cros_cr_localbuild_internal.cache_util.BuildArtifactsCache'
    )
    @mock.patch.object(switch_cros_cr_localbuild_internal, 'patch_cls')
    @mock.patch.object(gclient_util, 'sync')
    def test_sync_not_called_with_cache(
        self,
        mock_sync,
        mock_patch_cls,
        mock_build_artifacts_cache,
    ):
        """Tests that sync and patch_cls are not called when the code is cached."""
        args = self.common_args + [
            '--sync-code-only',
        ]

        # Simulate the cache hits.
        mock_build_artifacts_cache.return_value.cache_hit.return_value = True

        switch_cros_cr_localbuild_internal.switch_main(args)

        mock_sync.assert_not_called()
        mock_patch_cls.assert_not_called()

    @mock.patch(
        'switch_cros_cr_localbuild_internal.cache_util.BuildArtifactsCache'
    )
    @mock.patch.object(switch_cros_cr_localbuild_internal, 'patch_cls')
    @mock.patch.object(gclient_util, 'sync')
    def test_patch_cl_called(
        self,
        mock_sync,
        mock_patch_cls,
        mock_build_artifacts_cache,
    ):
        """Tests that patch_cls is called when --chromium-patch-cl is provided."""
        CL_NUMBER = "12345"
        args = self.common_args + [
            '--chromium-patch-cl',
            CL_NUMBER,
            '--sync-code-only',
        ]

        # Cache should not be used with a custom chromium CL.
        mock_build_artifacts_cache.assert_not_called()

        switch_cros_cr_localbuild_internal.switch_main(args)

        self.assertEqual(mock_sync.call_count, 2)
        mock_sync.assert_has_calls(
            [
                mock.call(self.chrome_root, revision=self.CHROME_REVISION),
                mock.call(self.chrome_root),
            ]
        )
        mock_patch_cls.assert_called_once_with(self.chrome_src, [CL_NUMBER])

    @mock.patch(
        'switch_cros_cr_localbuild_internal.cache_util.BuildArtifactsCache'
    )
    @mock.patch.object(switch_cros_cr_localbuild_internal, 'patch_cls')
    @mock.patch.object(gclient_util, 'sync')
    def test_gn_extra_args_inhibit_cache(
        self,
        mock_sync,
        mock_patch_cls,
        mock_build_artifacts_cache,
    ):
        """Tests that cache is not used when --gn-extra-args is provided."""
        args = self.common_args + [
            '--gn-extra-args',
            'dcheck_always_on=true',
            '--sync-code-only',
        ]

        switch_cros_cr_localbuild_internal.switch_main(args)

        # Cache should not be used with custom GN args.
        mock_build_artifacts_cache.assert_not_called()
        mock_sync.assert_called_once()
        mock_patch_cls.assert_not_called()


if __name__ == '__main__':
    unittest.main()
