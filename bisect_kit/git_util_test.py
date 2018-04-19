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


class GitOperation(object):
  """Git operations for testing."""

  def __init__(self, git_repo):
    self.git_repo = git_repo
    self.commits = []

  def init(self):
    """Git init."""

    if not os.path.exists(self.git_repo):
      os.makedirs(self.git_repo)

    subprocess.check_call(['git', 'init', '-q'], cwd=self.git_repo)

  def add_commit(self, commit_time, message, path, content, author_time=None):
    """Adds a commit to the test git repo.

    This is for testing, so it is simplified that only allow changing one file
    at a commit.

    Args:
      commit_time: commit time
      message: commit message
      path: file path of this commit, relative to git root
      content: file content
      author_time: author time. Default to commit time if not specified.

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

  def create_commits(self, num):
    """Creates dummy commits.

    Created commit hashes are added to `self.commits`.

    Args:
      num: number of commits to create
    """
    for i in range(1, num + 1):
      commit_time = '2017-01-%02dT00:00:00' % i
      self.commits.append(
          self.add_commit(commit_time, 'commit %d' % i, 'file',
                          'commit %d' % i))


class TestGitOperation(unittest.TestCase):
  """Tests git_util module with real git operations without mock."""

  def setUp(self):
    self.git_repo = tempfile.mkdtemp()

    self.git = GitOperation(self.git_repo)
    self.git.init()
    self.git.create_commits(5)
    self.commits = self.git.commits

  def tearDown(self):
    shutil.rmtree(self.git_repo)

  def test_is_containing_commit(self):
    self.assertTrue(
        git_util.is_containing_commit(self.git_repo, self.commits[0]))
    self.assertTrue(
        git_util.is_containing_commit(self.git_repo, self.commits[1]))
    self.assertTrue(
        git_util.is_containing_commit(self.git_repo, self.commits[0][:10]))
    self.assertFalse(
        git_util.is_containing_commit(self.git_repo,
                                      self.commits[0][:-10] + 'a' * 10))

  def test_is_ancestor_commit(self):
    self.assertTrue(
        git_util.is_ancestor_commit(self.git_repo, self.commits[0],
                                    self.commits[1]))
    self.assertTrue(
        git_util.is_ancestor_commit(self.git_repo, self.commits[0],
                                    self.commits[2]))
    self.assertFalse(
        git_util.is_ancestor_commit(self.git_repo, self.commits[0],
                                    self.commits[0]))
    self.assertFalse(
        git_util.is_ancestor_commit(self.git_repo, self.commits[2],
                                    self.commits[0]))

  def test_get_revlist(self):
    self.assertEqual(
        git_util.get_revlist(self.git_repo, self.commits[1], self.commits[3]),
        [self.commits[1], self.commits[2], self.commits[3]])

  def test_checkout_version(self):
    full_path = os.path.join(self.git_repo, 'file')
    self.assertEqual(open(full_path).read(), 'commit 5')
    git_util.checkout_version(self.git_repo, self.commits[1])
    self.assertEqual(open(full_path).read(), 'commit 2')

  def test_get_commit_log(self):
    self.assertIn('commit 2',
                  git_util.get_commit_log(self.git_repo, self.commits[1]))

  def test_get_commit_hash(self):
    tag_name = 'foo'
    subprocess.check_call(
        ['git', 'tag', tag_name, self.commits[2]], cwd=self.git_repo)
    self.assertEqual(
        git_util.get_commit_hash(self.git_repo, tag_name), self.commits[2])


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
