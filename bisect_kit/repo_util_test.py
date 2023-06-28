# Copyright 2018 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Test repo_util module."""

import os
import shutil
import tempfile
import unittest
from unittest import mock

from bisect_kit import errors
from bisect_kit import git_util_test
from bisect_kit import repo_util


class TestRepoOperation(unittest.TestCase):
    """Tests repo_util module with real repo operations without mock."""

    def _create_dummy_repo(self):
        self.manifest_branch = 'main'
        git1_dir = os.path.join(self.gitbase, 'g1.git')
        self.git1 = git_util_test.GitOperation(git1_dir)
        self.git1.init()
        self.git1.create_commits(3)

        git2_dir = os.path.join(self.gitbase, 'g2.git')
        self.git2 = git_util_test.GitOperation(git2_dir)
        self.git2.init()
        self.git2.create_commits(3)

        manifest_git_dir = os.path.join(self.gitbase, 'm')
        self.manifest_git = git_util_test.GitOperation(manifest_git_dir)
        self.manifest_git.init(initial_branch=self.manifest_branch)
        manifest = """<!--?xml version="1.0" encoding="UTF-8"?-->
    <manifest>
        <remote name="mydisk" fetch="file://%s"></remote>
        <default revision="refs/heads/main" remote="mydisk"></default>
        <project name="g1.git" remote="mydisk" path="dir1/g1"></project>
        <project name="g2.git" path="dir2/g2" revision="%s"></project>
        <project name="g3.git" path="dir3/g3" groups="notdefault"></project>
    </manifest>
    """ % (
            self.gitbase,
            self.git2.revs[1],
        )
        self.manifest_git.add_commit(
            '2017-01-01T00:00:00', 'add manifest', 'default.xml', manifest
        )

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.repo_dir = os.path.join(self.tmp_dir, 'repo')
        os.mkdir(self.repo_dir)

        self.gitbase = os.path.join(self.tmp_dir, 'gitbase')
        self._create_dummy_repo()

    def tearDown(self):
        shutil.rmtree(self.tmp_dir)

    def test_repo_init_and_sync(self):
        repo_util.init(
            self.repo_dir,
            'file://%s' % self.manifest_git.git_repo,
            manifest_branch=self.manifest_branch,
        )
        repo_util.sync(self.repo_dir)
        assert os.path.exists(os.path.join(self.repo_dir, 'dir1', 'g1', 'file'))
        assert os.path.exists(os.path.join(self.repo_dir, 'dir2', 'g2', 'file'))

        self.assertEqual(
            repo_util.get_current_branch(self.repo_dir), 'refs/heads/main'
        )


class TestManifestParser(unittest.TestCase):
    """Tests repo_util.ManifestParser."""

    def test_parse_manifest_android(self):
        """Test features used by android manifest."""
        manifest = r"""<?xml version="1.0" encoding="UTF-8"?>
    <manifest>
      <remote name="goog" fetch=".." />
      <default revision="main" remote="goog" />
      <project path="frameworks/av" name="platform/frameworks/av"
               revision="foobar" />
      <project path="frameworks/base" name="platform/frameworks/base" />
      <project path="frameworks/foo" name="platform/frameworks/foo"
               groups="notdefault" />
      <project name="path/with/trailing/slash/" />
    </manifest>
    """

        manifest_url = 'https://example.com/manifest'
        manifest_dir = '/dummy/path'  # doesn't matter since io mocked
        with mock.patch(
            'bisect_kit.repo_util.get_manifest_url', return_value=manifest_url
        ):
            p = repo_util.ManifestParser(manifest_dir)
            root = p.parse_single_xml(manifest, allow_include=False)
            result = p.process_parsed_result(root)

        self.assertEqual(result['frameworks/av'].path, 'frameworks/av')
        self.assertEqual(
            result['frameworks/av'].repo_url,
            'https://example.com/platform/frameworks/av',
        )
        self.assertEqual(result['frameworks/av'].rev, 'foobar')

        self.assertEqual(result['frameworks/base'].rev, 'main')

        self.assertNotIn('frameworks/foo', result)

        self.assertEqual(
            result['path/with/trailing/slash'].path, 'path/with/trailing/slash'
        )
        self.assertEqual(
            result['path/with/trailing/slash'].repo_url,
            'https://example.com/path/with/trailing/slash',
        )

    def test_parse_manifest_bad(self):
        manifest_url = 'https://example.com/manifest'
        manifest_dir = '/dummy/path'  # doesn't matter since io mocked

        with mock.patch(
            'bisect_kit.repo_util.get_manifest_url', return_value=manifest_url
        ):
            p = repo_util.ManifestParser(manifest_dir)

            with self.assertRaises(errors.InternalError):
                root = p.parse_single_xml(
                    r"""<?xml version="1.0" encoding="UTF-8"?>
            <manifest>
              <include name="file.xml" />
            </manifest>
            """,
                    allow_include=False,
                )
                p.process_parsed_result(root)

            with self.assertRaises(errors.InternalError):
                root = p.parse_single_xml(
                    r"""<?xml version="1.0" encoding="UTF-8"?>
            <manifest>
              <project path="src/scripts" name="chromiumos/platform/crosutils"
                       remote="goog" revision="foo" />
            </manifest>
            """,
                    allow_include=False,
                )
                p.process_parsed_result(root)


if __name__ == '__main__':
    unittest.main()
