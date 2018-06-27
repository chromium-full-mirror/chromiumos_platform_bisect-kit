# -*- coding: utf-8 -*-
# Copyright 2018 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Test repo_util module."""

from __future__ import print_function
import os
import shutil
import tempfile
import unittest

from bisect_kit import repo_util
from bisect_kit import git_util_test


class TestRepoOperation(unittest.TestCase):
  """Tests repo_util module with real repo operations without mock."""

  def _create_dummy_repo(self):
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
    self.manifest_git.init()
    manifest = '''<!--?xml version="1.0" encoding="UTF-8"?-->
    <manifest>
        <remote name="mydisk" fetch="file://%s"></remote>
        <default revision="refs/heads/master" remote="mydisk"></default>
        <project name="g1.git" remote="mydisk" path="dir1/g1"></project>
        <project name="g2.git" path="dir2/g2" revision="%s"></project>
        <project name="g3.git" path="dir3/g3" groups="notdefault"></project>
    </manifest>
    ''' % (self.gitbase, self.git2.commits[1])
    self.manifest_git.add_commit('2017-01-01T00:00:00', 'add manifest',
                                 'default.xml', manifest)

  def setUp(self):
    self.tmp_dir = tempfile.mkdtemp()
    self.repo_dir = os.path.join(self.tmp_dir, 'repo')
    os.mkdir(self.repo_dir)

    self.gitbase = os.path.join(self.tmp_dir, 'gitbase')
    self._create_dummy_repo()

  def tearDown(self):
    shutil.rmtree(self.tmp_dir)

  def test_repo_init_and_sync(self):
    repo_util.init(self.repo_dir, 'file://%s' % self.manifest_git.git_repo)
    repo_util.sync(self.repo_dir)
    assert os.path.exists(os.path.join(self.repo_dir, 'dir1', 'g1', 'file'))
    assert os.path.exists(os.path.join(self.repo_dir, 'dir2', 'g2', 'file'))

    self.assertEqual(
        repo_util.get_current_branch(self.repo_dir), 'refs/heads/master')

  def test_parse_repo(self):
    repo_util.init(self.repo_dir, 'file://%s' % self.manifest_git.git_repo)
    result = repo_util.parse_repo_set(self.repo_dir, 'default.xml')
    self.assertEqual(result.get('dir1/g1', 'not-exist'), None)
    self.assertEqual(result.get('dir2/g2'), self.git2.commits[1])
    self.assertEqual(result.get('dir3/g3', 'not-exist'), 'not-exist')


if __name__ == '__main__':
  unittest.main()
