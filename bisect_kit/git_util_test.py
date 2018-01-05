# Copyright 2017 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Test git_util module."""

from __future__ import print_function
import os
import unittest
import tempfile
import shutil
import subprocess

from bisect_kit import cli
from bisect_kit import git_util


class TestGitOperation(unittest.TestCase):
  """Tests git_util module with real git operations without mock."""

  def setUp(self):
    self.git_repo = tempfile.mkdtemp()

    subprocess.check_call(['git', 'init', '-q'], cwd=self.git_repo)

    self.commit = [
        self.add_commit('2017-01-01T00:00:00', 'commit 1', 'file', 'commit 1'),
        self.add_commit('2017-01-02T00:00:00', 'commit 2', 'file', 'commit 2'),
        self.add_commit('2017-01-03T00:00:00', 'commit 3', 'file', 'commit 3'),
        self.add_commit('2017-01-04T00:00:00', 'commit 4', 'file', 'commit 4'),
        self.add_commit('2017-01-05T00:00:00', 'commit 5', 'file', 'commit 5'),
    ]

  def tearDown(self):
    shutil.rmtree(self.git_repo)

  def add_commit(self, commit_time, message, path, content, author_time=None):
    """Adds a commit to the test git repo

    This is for testing, so it is simplified that only allow changing one file
    at a commit.

    Returns:
        Commit hash.
    """

    if author_time is None:
      author_time = commit_time
    env = dict(GIT_AUTHOR_DATE=author_time, GIT_COMMITTER_DATE=commit_time)

    full_path = os.path.join(self.git_repo, path)
    with open(full_path, 'w') as f:
      f.write(content)

    subprocess.check_call(['git', 'add', path], cwd=self.git_repo)
    p = subprocess.Popen(
        ['git', 'commit', '-q', '-F', '-', path],
        stdin=subprocess.PIPE,
        cwd=self.git_repo,
        env=env)
    p.communicate(message)
    assert p.returncode == 0

    git_rev = subprocess.check_output(
        ['git', 'rev-parse', 'HEAD'], cwd=self.git_repo).strip()
    assert len(git_rev) == git_util.GIT_FULL_COMMIT_ID_LENGTH
    return git_rev

  def test_is_containing_commit(self):
    self.assertTrue(
        git_util.is_containing_commit(self.git_repo, self.commit[0]))
    self.assertTrue(
        git_util.is_containing_commit(self.git_repo, self.commit[1]))
    self.assertTrue(
        git_util.is_containing_commit(self.git_repo, self.commit[0][:10]))
    self.assertFalse(
        git_util.is_containing_commit(self.git_repo, self.commit[0][:-10] +
                                      'a' * 10))

  def test_get_revlist(self):
    self.assertEqual(
        git_util.get_revlist(self.git_repo, self.commit[1], self.commit[3]),
        [self.commit[1], self.commit[2], self.commit[3]])

  def test_checkout_version(self):
    full_path = os.path.join(self.git_repo, 'file')
    self.assertEqual(open(full_path).read(), 'commit 5')
    git_util.checkout_version(self.git_repo, self.commit[1])
    self.assertEqual(open(full_path).read(), 'commit 2')

  def test_get_commit_log(self):
    self.assertIn('commit 2',
                  git_util.get_commit_log(self.git_repo, self.commit[1]))

  def test_get_commit_hash(self):
    tag_name = 'foo'
    subprocess.check_call(
        ['git', 'tag', tag_name, self.commit[2]], cwd=self.git_repo)
    self.assertEqual(
        git_util.get_commit_hash(self.git_repo, tag_name), self.commit[2])


class TestGitUtil(unittest.TestCase):
  """Tests logic part of git_util module.

  This only tests logic. git operations are mocked if necessary.
  """

  def test_is_git_rev(self):
    self.assertTrue(git_util.is_git_rev('1234567'))
    self.assertTrue(git_util.is_git_rev('12345abcdef'))
    self.assertTrue(git_util.is_git_rev('a' * 40))

    self.assertFalse(git_util.is_git_rev('123456'))
    self.assertFalse(git_util.is_git_rev('12345ABCDEF'))
    self.assertFalse(git_util.is_git_rev('a' * 41))

  def test_argtype_git_rev(self):
    rev = 'deadbeef'
    self.assertEqual(git_util.argtype_git_rev(rev), rev)
    with self.assertRaises(cli.ArgTypeError):
      git_util.argtype_git_rev('hello')


if __name__ == '__main__':
  unittest.main()
