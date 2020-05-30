# -*- coding: utf-8 -*-
# Copyright 2017 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Test git_util module."""

from __future__ import print_function
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from bisect_kit import cli
from bisect_kit import git_util


class GitOperation:
  """Git operations for testing."""

  def __init__(self, git_repo):
    self.git_repo = git_repo
    self.commits = []
    self.default_filename = 'file'

  def init(self):
    """Git init."""

    if not os.path.exists(self.git_repo):
      os.makedirs(self.git_repo)

    subprocess.check_call(['git', 'init', '-q'], cwd=self.git_repo)

    # Remove git hooks in order to save time.
    shutil.rmtree(os.path.join(self.git_repo, '.git', 'hooks'))

  def add_commit(self,
                 commit_time,
                 message,
                 path,
                 content,
                 link_target=None,
                 author_time=None):
    """Adds a commit to the test git repo.

    This is for testing, so it is simplified that only allow changing one file
    at a commit.

    Args:
      commit_time: commit time
      message: commit message
      path: file path of this commit, relative to git root
      content: file content; 'git rm' if None
      link_target: create a symbolic link path link to target,
      author_time: author time. Default to commit time if not specified.

    Returns:
      Commit hash.
    """
    if author_time is None:
      author_time = commit_time
    env = dict(
        GIT_AUTHOR_DATE=str(author_time), GIT_COMMITTER_DATE=str(commit_time))

    full_path = os.path.join(self.git_repo, path)

    if link_target:
      assert content is None
      dirname = os.path.dirname(full_path)
      if not os.path.exists(dirname):
        os.makedirs(dirname)
      subprocess.check_call(['ln', '-s', link_target, path], cwd=self.git_repo)
      subprocess.check_call(['git', 'add', path], cwd=self.git_repo)
    elif content is None:
      subprocess.check_call(['git', 'rm', path], cwd=self.git_repo)
    else:
      dirname = os.path.dirname(full_path)
      if not os.path.exists(dirname):
        os.makedirs(dirname)
      with open(full_path, 'w') as f:
        f.write(content)
      subprocess.check_call(['git', 'add', path], cwd=self.git_repo)

    p = subprocess.Popen(['git', 'commit', '-q', '-F', '-', path],
                         stdin=subprocess.PIPE,
                         cwd=self.git_repo,
                         env=env)
    p.communicate(message.encode('utf-8'))
    assert p.returncode == 0

    git_rev = subprocess.check_output(['git', 'rev-parse', 'HEAD'],
                                      cwd=self.git_repo,
                                      encoding='utf8')
    git_rev = git_rev.strip()

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
          self.add_commit(commit_time, 'commit %d' % i, self.default_filename,
                          'commit %d' % i))


class TestGitOperationReadOnly(unittest.TestCase):
  """Tests git_util module with real git operations without mock."""

  @classmethod
  def setUpClass(cls):
    cls.git_repo = tempfile.mkdtemp()

    cls.git = GitOperation(cls.git_repo)
    cls.git.init()
    cls.git.create_commits(5)
    cls.commits = cls.git.commits

  @classmethod
  def tearDownClass(cls):
    shutil.rmtree(cls.git_repo)

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

  def test_get_commit_metadata(self):
    self.assertNotIn(
        'parent', git_util.get_commit_metadata(self.git_repo, self.commits[0]))

    self.assertEqual(
        git_util.get_commit_metadata(self.git_repo, self.commits[1])['parent'],
        [self.commits[0]])

  def test_get_revlist(self):
    self.assertEqual(
        git_util.get_revlist(self.git_repo, self.commits[1], self.commits[3]),
        [self.commits[1], self.commits[2], self.commits[3]])

  def test_get_commit_log(self):
    self.assertIn('commit 2',
                  git_util.get_commit_log(self.git_repo, self.commits[1]))

  def test_get_rev_by_time(self):
    # simple case
    self.assertEqual(
        git_util.get_rev_by_time(self.git_repo, '2017-01-03T12:00:00', None),
        self.commits[2])

    # boundary case: equal timestamp
    self.assertEqual(
        git_util.get_rev_by_time(self.git_repo, '2017-01-03T00:00:00', None),
        self.commits[2])

  def test_get_history(self):
    timestamp = 1483286400  # 2017-01-02T00:00:00
    day = 86400

    # Normal case.
    self.assertEqual(
        git_util.get_history(
            self.git_repo,
            self.git.default_filename,
            after=timestamp + 100,
            before=timestamp + day * 2 + 100),
        [(timestamp + day * 1, self.commits[2]),
         (timestamp + day * 2, self.commits[3])])

    # Test boundary condition (inclusive).
    self.assertEqual(
        git_util.get_history(
            self.git_repo,
            self.git.default_filename,
            after=timestamp,
            before=timestamp + day * 2), [
                (timestamp, self.commits[1]),
                (timestamp + day * 1, self.commits[2]),
                (timestamp + day * 2, self.commits[3]),
            ])

    # Padding.
    self.assertEqual(
        git_util.get_history(
            self.git_repo,
            self.git.default_filename,
            after=timestamp + 100,
            before=timestamp + day * 2 + 100,
            padding_begin=True,
            padding_end=True), [
                (timestamp + 100, self.commits[1]),
                (timestamp + day * 1, self.commits[2]),
                (timestamp + day * 2, self.commits[3]),
                (timestamp + day * 2 + 100, self.commits[3]),
            ])

  def test_get_history_before_first_commit(self):
    # timestamp of the first commit of git repo
    time = 1483200000

    self.assertEqual(
        git_util.get_history(
            self.git_repo,
            self.git.default_filename,
            after=time - 10,
            before=time + 10), [
                (time, self.commits[0]),
            ])

    self.assertEqual(
        git_util.get_history(
            self.git_repo,
            self.git.default_filename,
            after=time - 10,
            before=time + 10,
            padding_begin=True,
            padding_end=True), [
                (time, self.commits[0]),
                (time + 10, self.commits[0]),
            ])

    self.assertEqual(
        git_util.get_history(
            self.git_repo,
            self.git.default_filename,
            after=time - 10,
            before=time - 5,
            padding_begin=True,
            padding_end=True), [])

    self.assertEqual(
        git_util.get_history(
            self.git_repo,
            'not-exist-file',
            after=time - 10,
            before=time + 10,
            padding_begin=True,
            padding_end=True), [])


class TestGitOperation(unittest.TestCase):
  """Tests git_util module with real git operations without mock."""

  def setUp(self):
    self.git_repo = tempfile.mkdtemp()

    self.git = GitOperation(self.git_repo)
    self.git.init()

  def tearDown(self):
    shutil.rmtree(self.git_repo)

  def prepare_merged_repository(self):
    # Generates following commit graph:
    #  |
    #  *  commit C (merge A B)  (2017-02-05)
    #  |\
    #  | *  commit B on branch foo (2017-02-02)
    #  * |  commit A (2017-02-01)
    #  |/
    #  *  commits[0] (2017-01-01)
    #  |
    self.git.create_commits(1)
    branch_foo = 'foo'
    subprocess.check_call(['git', 'branch', branch_foo], cwd=self.git_repo)
    commit_a = self.git.add_commit('2017-02-01T00:00:00', 'commit A', 'file2',
                                   'commit A')
    subprocess.check_call(['git', 'checkout', branch_foo], cwd=self.git_repo)
    commit_b = self.git.add_commit('2017-02-02T00:00:00', 'commit B',
                                   self.git.default_filename, 'commit B')
    subprocess.check_call(['git', 'checkout', 'master'], cwd=self.git_repo)
    # --no-edit: git may complain EDITOR is unset
    subprocess.check_call(['git', 'merge', '--no-edit', branch_foo],
                          cwd=self.git_repo,
                          env=dict(GIT_COMMITTER_DATE='2017-02-05T00:00:00'))

    commit_c = subprocess.check_output(['git', 'rev-parse', 'HEAD'],
                                       cwd=self.git_repo,
                                       encoding='utf8')
    commit_c = commit_c.strip()
    return [commit_a, commit_b, commit_c]

  def test_git_init(self):
    # reinit existing git folder
    self.assertTrue(git_util.is_git_root(self.git_repo))
    git_util.init(self.git_repo)
    self.assertTrue(git_util.is_git_root(self.git_repo))

    # init on not existing path
    shutil.rmtree(self.git_repo)
    self.assertFalse(os.path.exists(self.git_repo))
    git_util.init(self.git_repo)
    self.assertTrue(git_util.is_git_root(self.git_repo))

  def test_is_git_bare_dir(self):
    # git_repo is a normal git work dir.
    self.assertFalse(git_util.is_git_bare_dir(self.git_repo))

    # non-exist dir
    shutil.rmtree(self.git_repo)
    self.assertFalse(git_util.is_git_bare_dir(self.git_repo))

    # empty dir
    os.makedirs(self.git_repo)
    self.assertFalse(git_util.is_git_bare_dir(self.git_repo))

    # git bare dir
    subprocess.check_call(['git', 'init', '-q', '--bare'], cwd=self.git_repo)
    self.assertTrue(git_util.is_git_bare_dir(self.git_repo))

  def test_get_commit_hash(self):
    self.git.create_commits(3)
    tag_name = 'foo'
    subprocess.check_call(['git', 'tag', tag_name, self.git.commits[2]],
                          cwd=self.git_repo)
    self.assertEqual(
        git_util.get_commit_hash(self.git_repo, tag_name), self.git.commits[2])

  def test_checkout_version(self):
    self.git.create_commits(5)
    full_path = os.path.join(self.git_repo, self.git.default_filename)
    with open(full_path) as f:
      self.assertEqual(f.read(), 'commit 5')
    git_util.checkout_version(self.git_repo, self.git.commits[1])
    with open(full_path) as f:
      self.assertEqual(f.read(), 'commit 2')

  def test_get_file_from_revision(self):
    path = 'foo'
    time = 1500000000
    commit_1 = self.git.add_commit(time, 'msg', path, 'a')
    commit_2 = self.git.add_commit(time + 100, 'msg', path + 'other', '')
    commit_3 = self.git.add_commit(time + 200, 'msg', path, 'b')
    self.assertEqual(
        git_util.get_file_from_revision(self.git_repo, commit_1, path), 'a')
    self.assertEqual(
        git_util.get_file_from_revision(self.git_repo, commit_2, path), 'a')
    self.assertEqual(
        git_util.get_file_from_revision(self.git_repo, commit_3, path), 'b')

  def test_list_dir_from_revision(self):
    time = 1500000000
    commit_1 = self.git.add_commit(time + 100, 'msg', 'path/a', '')
    commit_2 = self.git.add_commit(time + 200, 'msg', 'path/b', '')
    commit_3 = self.git.add_commit(time + 300, 'msg', 'path/foo/c', '')
    commit_4 = self.git.add_commit(time + 400, 'msg', 'path/b', None)
    commit_5 = self.git.add_commit(time + 500, 'msg', 'path/foo/c', None)
    self.assertCountEqual(
        git_util.list_dir_from_revision(self.git_repo, commit_1, 'path'), ['a'])
    self.assertCountEqual(
        git_util.list_dir_from_revision(self.git_repo, commit_2, 'path'),
        ['a', 'b'])
    self.assertCountEqual(
        git_util.list_dir_from_revision(self.git_repo, commit_3, 'path'),
        ['a', 'b', 'foo'])
    self.assertCountEqual(
        git_util.list_dir_from_revision(self.git_repo, commit_4, 'path'),
        ['a', 'foo'])
    self.assertCountEqual(
        git_util.list_dir_from_revision(self.git_repo, commit_5, 'path'), ['a'])

    with self.assertRaises(subprocess.CalledProcessError):
      git_util.list_dir_from_revision(self.git_repo, commit_1, 'path/foo')

  def test_get_commit_time(self):
    path = self.git.default_filename
    time = 1500000000
    commit_1 = self.git.add_commit(
        time + 100, 'msg', path, 'a', author_time=time)
    commit_2 = self.git.add_commit(
        time + 300, 'msg', path, 'b', author_time=time + 200)
    self.assertEqual(
        git_util.get_commit_time(self.git_repo, commit_1, path), time + 100)
    self.assertEqual(
        git_util.get_commit_time(self.git_repo, commit_2, path), time + 300)

  def test_get_rev_by_time_merge(self):
    commit_a, _, commit_c = self.prepare_merged_repository()

    # simple case
    self.assertEqual(
        git_util.get_rev_by_time(self.git_repo, '2017-02-10T00:00:00', None),
        commit_c)

    # Not commit B because it is on different branch.
    self.assertEqual(
        git_util.get_rev_by_time(self.git_repo, '2017-02-03T00:00:00', None),
        commit_a)

  def test_get_branches(self):
    commit_a, commit_b, commit_c = self.prepare_merged_repository()
    self.assertCountEqual(
        git_util.get_branches(self.git_repo),
        ['refs/heads/foo', 'refs/heads/master'])
    self.assertCountEqual(
        git_util.get_branches(self.git_repo, commit=commit_a),
        ['refs/heads/master'])
    self.assertCountEqual(
        git_util.get_branches(self.git_repo, commit=commit_b),
        ['refs/heads/foo', 'refs/heads/master'])
    self.assertCountEqual(
        git_util.get_branches(self.git_repo, commit=commit_c),
        ['refs/heads/master'])

  def test_reset_hard(self):
    self.git.create_commits(1)
    path = os.path.join(self.git_repo, self.git.default_filename)
    with open(path) as f:
      content = f.read()

    # Nothing happened.
    git_util.reset_hard(self.git_repo)
    with open(path) as f:
      self.assertEqual(f.read(), content)

    # Modified.
    with open(path, 'w') as f:
      f.write(content + 'other data')
    git_util.reset_hard(self.git_repo)
    with open(path) as f:
      self.assertEqual(f.read(), content)

    # Deleted.
    os.unlink(path)
    git_util.reset_hard(self.git_repo)
    with open(path) as f:
      self.assertEqual(f.read(), content)

  def test_list_untracked(self):
    self.assertEqual(git_util.list_untracked(self.git_repo), [])

    def touch_file(path):
      dirname = os.path.dirname(path)
      if not os.path.exists(dirname):
        os.makedirs(dirname)
      with open(path, 'w') as f:
        f.write('foobar')

    file_untracked_1 = 'untracked-file'
    file_untracked_2 = 'untracked-dir/untracked-file'
    file_to_ignore = 'untracked-file-to-ignore'
    touch_file(os.path.join(self.git_repo, file_untracked_1))
    touch_file(os.path.join(self.git_repo, file_untracked_2))
    touch_file(os.path.join(self.git_repo, file_to_ignore))

    self.assertEqual(
        set(git_util.list_untracked(self.git_repo, excludes=[file_to_ignore])),
        set([file_untracked_1, file_untracked_2]))

  def test_get_history_rebirth(self):
    time = 1500000000
    path = 'foo'

    # `path` has been removed and added back
    c1 = self.git.add_commit(time + 10, 'msg', path, '')
    c2 = self.git.add_commit(time + 20, 'msg', path, None)
    c3 = self.git.add_commit(time + 30, 'msg', path, '')
    c4 = self.git.add_commit(time + 40, 'msg', path, None)
    c5 = self.git.add_commit(time + 50, 'msg', path, '')

    self.assertEqual(
        git_util.get_history(self.git_repo, path, after=time, before=time + 60),
        [
            (time + 10, c1),
            (time + 20, c2),
            (time + 30, c3),
            (time + 40, c4),
            (time + 50, c5),
        ])

  def test_get_history_recursively(self):
    time = 1500000000

    c1 = self.git.add_commit(time + 10, 'msg', 'a', '')
    c2 = self.git.add_commit(time + 20, 'msg', 'b', '')
    c3 = self.git.add_commit(time + 30, 'msg', 'c', '')
    c4 = self.git.add_commit(time + 40, 'msg', 'a', 'b')
    c5 = self.git.add_commit(time + 50, 'msg', 'a', 'b c')
    c6 = self.git.add_commit(time + 60, 'msg', 'a', 'c')
    c7 = self.git.add_commit(time + 70, 'msg', 'b', 'c')
    c8 = self.git.add_commit(time + 80, 'msg', 'a', 'b')
    c9 = self.git.add_commit(time + 90, 'msg', 'c', '  ')

    def parse(_, content):
      return content.split()

    self.assertEqual(
        git_util.get_history_recursively(self.git_repo, 'c', time, time + 100,
                                         parse), [(time + 30, c3),
                                                  (time + 90, c9),
                                                  (time + 100, c9)])
    self.assertEqual(
        git_util.get_history_recursively(self.git_repo, 'b', time, time + 100,
                                         parse), [(time + 20, c2),
                                                  (time + 70, c7),
                                                  (time + 90, c9),
                                                  (time + 100, c9)])

    self.assertEqual(
        git_util.get_history_recursively(self.git_repo, 'a', time, time + 100,
                                         parse), [(time + 10, c1),
                                                  (time + 40, c4),
                                                  (time + 50, c5),
                                                  (time + 60, c6),
                                                  (time + 80, c8),
                                                  (time + 90, c9),
                                                  (time + 100, c9)])

  def test_get_history_recursively2(self):

    def parse(_, content):
      return content.split()

    time = 1500000000
    _c1 = self.git.add_commit(time + 10, 'msg', 'f2', '')
    _c2 = self.git.add_commit(time + 20, 'msg', 'f3', '')
    c3 = self.git.add_commit(time + 30, 'msg', 'f1', 'f2 f3')
    c4 = self.git.add_commit(time + 40, 'msg', 'f2', ' ')

    self.assertEqual(
        git_util.get_history_recursively(self.git_repo, 'f1', time, time + 100,
                                         parse), [(time + 30, c3),
                                                  (time + 40, c4),
                                                  (time + 100, c4)])

    c5 = self.git.add_commit(time + 50, 'msg', 'f3', ' ')
    self.assertEqual(
        git_util.get_history_recursively(self.git_repo, 'f1', time, time + 100,
                                         parse), [(time + 30, c3),
                                                  (time + 40, c4),
                                                  (time + 50, c5),
                                                  (time + 100, c5)])

    c6 = self.git.add_commit(time + 60, 'msg', 'f2', '  ')
    self.assertEqual(
        git_util.get_history_recursively(self.git_repo, 'f1', time, time + 100,
                                         parse), [(time + 30, c3),
                                                  (time + 40, c4),
                                                  (time + 50, c5),
                                                  (time + 60, c6),
                                                  (time + 100, c6)])

  def test_get_history_recursively_ordering(self):
    # All commits have identical commit time.
    time = 1500000000

    commits = []
    for i in range(10):
      commit = self.git.add_commit(time, 'msg', 'foo', str(i))
      commits.append((time, commit))

    # The commit ordering should be preserved.
    self.assertEqual(
        git_util.get_history_recursively(self.git_repo, 'foo', time,
                                         time, lambda *_: []), commits)

  def test_is_symbolic_link(self):
    time = 1500000000
    _commit1 = self.git.add_commit(time, 'msg', 'foo', 'foo')
    commit2 = self.git.add_commit(time + 1, 'msg', 'bar', 'bar')
    _commit3 = self.git.add_commit(time + 2, 'msg', 'bar', None)
    commit4 = self.git.add_commit(
        time + 3, 'msg', 'bar', None, link_target='foo')
    commit5 = self.git.add_commit(
        time + 4, 'msg', 'dir/baz', None, link_target='foo')

    self.assertFalse(git_util.is_symbolic_link(self.git_repo, commit2, 'bar'))
    self.assertTrue(git_util.is_symbolic_link(self.git_repo, commit4, 'bar'))
    with self.assertRaises(ValueError):
      self.assertFalse(
          git_util.is_symbolic_link(self.git_repo, commit4, 'dir/baz'))
    self.assertTrue(
        git_util.is_symbolic_link(self.git_repo, commit5, 'dir/baz'))


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


class TestFastLookup(unittest.TestCase):
  """Tests FastLookup and FastLookupEntry class."""

  git_repo = '/dummy/git/repo/path'
  branch = 'dummy-branch'
  commits = [
      (100, 'aaaaa'),
      (200, 'bbbbb'),
      (300, 'ccccc'),
      (400, 'ddddd'),
      (500, 'eeeee'),
  ]

  def test_disabled(self):
    lookup = git_util.FastLookup()
    with self.assertRaises(git_util.FastLookupFailed):
      lookup.get_rev_by_time(self.git_repo, self.branch, 123456789)
    with self.assertRaises(git_util.FastLookupFailed):
      lookup.is_containing_commit(self.git_repo, '012345abcdef')
    with self.assertRaises(git_util.FastLookupFailed):
      lookup.is_ancestor_commit(self.git_repo, '123456', 'abcdef')

  def test_get_rev_by_time(self):
    with mock.patch(
        'bisect_kit.git_util.get_revlist_by_period', return_value=self.commits):
      lookup = git_util.FastLookup()
      lookup.optimize((100, 500))
      self.assertEqual(
          lookup.get_rev_by_time(self.git_repo, 100, self.branch), 'aaaaa')
      self.assertEqual(
          lookup.get_rev_by_time(self.git_repo, 199, self.branch), 'aaaaa')
      self.assertEqual(
          lookup.get_rev_by_time(self.git_repo, 200, self.branch), 'bbbbb')
      self.assertEqual(
          lookup.get_rev_by_time(self.git_repo, 201, self.branch), 'bbbbb')

      # Outside the optimized range.
      with self.assertRaises(git_util.FastLookupFailed):
        lookup.get_rev_by_time(self.git_repo, 50, self.branch)

  def test_is_containing_commit(self):
    with mock.patch(
        'bisect_kit.git_util.get_revlist_by_period', return_value=self.commits):
      lookup = git_util.FastLookup()
      lookup.optimize((100, 500))
      lookup.get_rev_by_time(self.git_repo, 100, self.branch)
      self.assertTrue(lookup.is_containing_commit(self.git_repo, 'bbbbb'))
      self.assertTrue(lookup.is_containing_commit(self.git_repo, 'ccccc'))

      with self.assertRaises(git_util.FastLookupFailed):
        lookup.is_containing_commit(self.git_repo, 'abcdef')

  def test_is_ancestor_commit(self):
    with mock.patch(
        'bisect_kit.git_util.get_revlist_by_period', return_value=self.commits):
      lookup = git_util.FastLookup()
      lookup.optimize((100, 500))
      lookup.get_rev_by_time(self.git_repo, 100, self.branch)
      self.assertTrue(
          lookup.is_ancestor_commit(self.git_repo, 'bbbbb', 'ccccc'))
      self.assertFalse(
          lookup.is_ancestor_commit(self.git_repo, 'ccccc', 'ccccc'))
      self.assertFalse(
          lookup.is_ancestor_commit(self.git_repo, 'ccccc', 'bbbbb'))

      # Outside the optimized range.
      with self.assertRaises(git_util.FastLookupFailed):
        lookup.is_ancestor_commit(self.git_repo, 'ccccc', 'abcdef')


if __name__ == '__main__':
  unittest.main()
