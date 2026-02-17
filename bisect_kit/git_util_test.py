# Copyright 2017 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Test git_util module."""

import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from bisect_kit import errors
from bisect_kit import git_util


PathLike = os.PathLike | str


class GitOperation:
    """Git operations for testing."""

    def __init__(self, git_repo):
        self.git_repo = git_repo
        self.revs = []
        self.default_filename = 'file'

    def init(self, initial_branch='main'):
        """Git init."""

        if not os.path.exists(self.git_repo):
            os.makedirs(self.git_repo)

        subprocess.check_call(
            ['git', 'init', '-q', '--initial-branch', initial_branch],
            cwd=self.git_repo,
        )

        # Remove git hooks in order to save time.
        shutil.rmtree(os.path.join(self.git_repo, '.git', 'hooks'))

    def add_commit(
        self,
        commit_time,
        message,
        path,
        content,
        link_target=None,
        author_time=None,
    ):
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
        env = {
            "GIT_AUTHOR_DATE": str(author_time),
            "GIT_COMMITTER_DATE": str(commit_time),
        }

        full_path = os.path.join(self.git_repo, path)

        if link_target:
            assert content is None
            dirname = os.path.dirname(full_path)
            if not os.path.exists(dirname):
                os.makedirs(dirname)
            subprocess.check_call(
                ['ln', '-s', link_target, path], cwd=self.git_repo
            )
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

        p = subprocess.Popen(
            ['git', 'commit', '-q', '-F', '-', path],
            stdin=subprocess.PIPE,
            cwd=self.git_repo,
            env=env,
        )
        p.communicate(message.encode('utf-8'))
        assert p.returncode == 0

        git_rev = subprocess.check_output(
            ['git', 'rev-parse', 'HEAD'], cwd=self.git_repo, encoding='utf8'
        )
        git_rev = git_rev.strip()

        assert len(git_rev) == git_util.GIT_FULL_COMMIT_ID_LENGTH
        return git_rev

    def create_commits(self, num):
        """Creates dummy commits.

        Created commit hashes are added to `self.revs`.

        Args:
          num: number of commits to create
        """
        for i in range(1, num + 1):
            commit_time = '2017-01-%02dT00:00:00+08:00' % i
            self.revs.append(
                self.add_commit(
                    commit_time,
                    'commit %d' % i,
                    self.default_filename,
                    'commit %d' % i,
                )
            )


class TestGitOperationReadOnly(unittest.TestCase):
    """Tests git_util module with real git operations without mock."""

    git_repo: str
    git: GitOperation
    revs: list[str]

    @classmethod
    def setUpClass(cls):
        cls.git_repo = tempfile.mkdtemp()

        cls.git = GitOperation(cls.git_repo)
        cls.git.init()
        cls.git.create_commits(5)
        cls.revs = cls.git.revs

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.git_repo)

    def test_is_containing_commit(self):
        self.assertTrue(
            git_util.is_containing_commit(self.git_repo, self.revs[0])
        )
        self.assertTrue(
            git_util.is_containing_commit(self.git_repo, self.revs[1])
        )
        self.assertTrue(
            git_util.is_containing_commit(self.git_repo, self.revs[0][:10])
        )
        self.assertFalse(
            git_util.is_containing_commit(
                self.git_repo, self.revs[0][:-10] + 'a' * 10
            )
        )

    def test_is_ancestor_commit(self):
        self.assertTrue(
            git_util.is_ancestor_commit(
                self.git_repo, self.revs[0], self.revs[1]
            )
        )
        self.assertTrue(
            git_util.is_ancestor_commit(
                self.git_repo, self.revs[0], self.revs[2]
            )
        )
        self.assertFalse(
            git_util.is_ancestor_commit(
                self.git_repo, self.revs[0], self.revs[0]
            )
        )
        self.assertFalse(
            git_util.is_ancestor_commit(
                self.git_repo, self.revs[2], self.revs[0]
            )
        )
        # Non-existent commits
        self.assertFalse(
            git_util.is_ancestor_commit(self.git_repo, 'a' * 40, self.revs[0])
        )
        self.assertFalse(
            git_util.is_ancestor_commit(self.git_repo, self.revs[0], 'a' * 40)
        )
        self.assertFalse(
            git_util.is_ancestor_commit(self.git_repo, 'b' * 40, 'a' * 40)
        )
        self.assertFalse(
            git_util.is_ancestor_commit(self.git_repo, 'a' * 40, 'a' * 40)
        )

    def test_get_commit_metadata(self):
        self.assertIsNone(
            git_util.get_commit_metadata(self.git_repo, self.revs[0]).parent
        )

        self.assertEqual(
            git_util.get_commit_metadata(self.git_repo, self.revs[1]).parent,
            [self.revs[0]],
        )

    def test_get_batch_commit_metadata(self):
        bad_obj = 'foobar'
        result = git_util.get_batch_commit_metadata(
            self.git_repo, self.revs + [bad_obj]
        )
        self.assertEqual(result[self.revs[1]].parent, [self.revs[0]])
        self.assertEqual(result[self.revs[3]].parent, [self.revs[2]])
        self.assertIsNone(result[bad_obj])

    def test_get_revlist(self):
        self.assertEqual(
            git_util.get_revlist(self.git_repo, self.revs[1], self.revs[3]),
            [self.revs[1], self.revs[2], self.revs[3]],
        )

    def test_get_commit_log(self):
        self.assertIn(
            'commit 2', git_util.get_commit_log(self.git_repo, self.revs[1])
        )

    def test_get_rev_by_time(self):
        # simple case
        self.assertEqual(
            git_util.get_rev_by_time(
                self.git_repo, '2017-01-03T12:00:00', None
            ),
            self.revs[2],
        )

        # boundary case: equal timestamp
        self.assertEqual(
            git_util.get_rev_by_time(
                self.git_repo, '2017-01-03T00:00:00+08:00', None
            ),
            self.revs[2],
        )

    def test_get_history(self):
        timestamp = 1483286400  # 2017-01-02T00:00:00+08:00
        day = 86400

        # Normal case.
        self.assertEqual(
            git_util.get_history(
                self.git_repo,
                self.git.default_filename,
                after=timestamp + 100,
                before=timestamp + day * 2 + 100,
            ),
            git_util.Commit.make_commit_list(
                [
                    (timestamp + day * 1, self.revs[2]),
                    (timestamp + day * 2, self.revs[3]),
                ]
            ),
        )

        # Test boundary condition (inclusive).
        self.assertEqual(
            git_util.get_history(
                self.git_repo,
                self.git.default_filename,
                after=timestamp,
                before=timestamp + day * 2,
            ),
            git_util.Commit.make_commit_list(
                [
                    (timestamp, self.revs[1]),
                    (timestamp + day * 1, self.revs[2]),
                    (timestamp + day * 2, self.revs[3]),
                ]
            ),
        )

        # Padding.
        self.assertEqual(
            git_util.get_history(
                self.git_repo,
                self.git.default_filename,
                after=timestamp + 100,
                before=timestamp + day * 2 + 100,
                padding_begin=True,
                padding_end=True,
            ),
            git_util.Commit.make_commit_list(
                [
                    (timestamp + 100, self.revs[1]),
                    (timestamp + day * 1, self.revs[2]),
                    (timestamp + day * 2, self.revs[3]),
                    (timestamp + day * 2 + 100, self.revs[3]),
                ]
            ),
        )

    def test_get_history_before_first_commit(self):
        # timestamp of the first commit of git repo
        time = 1483200000

        self.assertEqual(
            git_util.get_history(
                self.git_repo,
                self.git.default_filename,
                after=time - 10,
                before=time + 10,
            ),
            git_util.Commit.make_commit_list(
                [
                    (time, self.revs[0]),
                ]
            ),
        )

        self.assertEqual(
            git_util.get_history(
                self.git_repo,
                self.git.default_filename,
                after=time - 10,
                before=time + 10,
                padding_begin=True,
                padding_end=True,
            ),
            git_util.Commit.make_commit_list(
                [
                    (time, self.revs[0]),
                    (time + 10, self.revs[0]),
                ]
            ),
        )

        self.assertEqual(
            git_util.get_history(
                self.git_repo,
                self.git.default_filename,
                after=time - 10,
                before=time - 5,
                padding_begin=True,
                padding_end=True,
            ),
            [],
        )

        self.assertEqual(
            git_util.get_history(
                self.git_repo,
                'not-exist-file',
                after=time - 10,
                before=time + 10,
                padding_begin=True,
                padding_end=True,
            ),
            [],
        )


class TestGitOperation(unittest.TestCase):
    """Tests git_util module with real git operations without mock."""

    def setUp(self):
        self.git_repo = tempfile.mkdtemp()

        self.git = GitOperation(self.git_repo)
        self.git.init()

    def tearDown(self):
        shutil.rmtree(self.git_repo)

    def _create_file(
        self, file_path: PathLike, lines: list[str] | None = None
    ) -> pathlib.Path:
        file_path = pathlib.Path(self.git_repo) / file_path
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.touch(exist_ok=True)
        if lines:
            with file_path.open('w') as f:
                for line in lines:
                    print(line, file=f)
        return file_path

    def _touch_files(self, file_list: list[PathLike]) -> list[pathlib.Path]:
        path_list = []
        for file_path in file_list:
            path_list.append(self._create_file(file_path))
        return path_list

    def _create_gitignore(
        self, file_list: list[PathLike], *, self_ignore: bool = False
    ) -> pathlib.Path:
        gitignore_file = '.gitignore'
        if self_ignore:
            file_list = file_list + [gitignore_file]
        return self._create_file(gitignore_file, file_list)

    def _prepare_merged_repository(self):
        # Generates following commit graph:
        #  |
        #  *  rev C (merge A B)  (2017-02-05)
        #  |\
        #  | *  rev B on branch foo (2017-02-02)
        #  * |  rev A (2017-02-01)
        #  |/
        #  *  revs[0] (2017-01-01)
        #  |
        self.git.create_commits(1)
        branch_foo = 'foo'
        subprocess.check_call(['git', 'branch', branch_foo], cwd=self.git_repo)
        rev_a = self.git.add_commit(
            '2017-02-01T00:00:00+08:00', 'commit A', 'file2', 'commit A'
        )
        subprocess.check_call(
            ['git', 'checkout', branch_foo], cwd=self.git_repo
        )
        rev_b = self.git.add_commit(
            '2017-02-02T00:00:00+08:00',
            'commit B',
            self.git.default_filename,
            'commit B',
        )
        subprocess.check_call(['git', 'checkout', 'main'], cwd=self.git_repo)
        # --no-edit: git may complain EDITOR is unset
        subprocess.check_call(
            ['git', 'merge', '--no-edit', branch_foo],
            cwd=self.git_repo,
            env={"GIT_COMMITTER_DATE": '2017-02-05T00:00:00+08:00'},
        )

        rev_c = subprocess.check_output(
            ['git', 'rev-parse', 'HEAD'], cwd=self.git_repo, encoding='utf8'
        )
        rev_c = rev_c.strip()
        return [rev_a, rev_b, rev_c]

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
        git_util.git(['init', '-q', '--bare'], cwd=self.git_repo)
        self.assertTrue(git_util.is_git_bare_dir(self.git_repo))

    def test_get_commit_hash(self):
        self.git.create_commits(3)
        tag_name = 'foo'
        subprocess.check_call(
            ['git', 'tag', tag_name, self.git.revs[2]], cwd=self.git_repo
        )
        self.assertEqual(
            git_util.get_commit_hash(self.git_repo, tag_name), self.git.revs[2]
        )

    def test_checkout_version(self):
        self.git.create_commits(5)
        full_path = os.path.join(self.git_repo, self.git.default_filename)
        with open(full_path) as f:
            self.assertEqual(f.read(), 'commit 5')
        git_util.checkout_version(self.git_repo, self.git.revs[1])
        with open(full_path) as f:
            self.assertEqual(f.read(), 'commit 2')

    def test_cherry_pick(self):
        # Create a base commit
        base_commit_msg = 'Base commit'
        base_content = 'Initial content'
        self.git.add_commit(
            '2023-01-01T00:00:00',
            base_commit_msg,
            self.git.default_filename,
            base_content,
        )

        # Create a new branch and a commit to pick
        subprocess.check_call(
            ['git', 'checkout', '-b', 'feature_branch'], cwd=self.git_repo
        )
        commit_to_pick_msg = 'Feature commit'
        commit_to_pick_content = 'Feature content added'
        commit_to_pick_hash = self.git.add_commit(
            '2023-01-02T00:00:00',
            commit_to_pick_msg,
            'feature_file.txt',
            commit_to_pick_content,
        )

        # Switch back to main and cherry-pick
        subprocess.check_call(['git', 'checkout', 'main'], cwd=self.git_repo)
        git_util.cherry_pick(self.git_repo, commit_to_pick_hash)

        # Verify the cherry-pick
        self.assertEqual(
            git_util.get_file_from_revision(
                self.git_repo, 'HEAD', 'feature_file.txt'
            ),
            commit_to_pick_content,
        )
        self.assertIn(
            commit_to_pick_msg, git_util.get_commit_log(self.git_repo, 'HEAD')
        )

    def test_get_file_from_revision(self):
        path = 'foo'
        time = 1500000000
        rev_1 = self.git.add_commit(time, 'msg', path, 'a')
        rev_2 = self.git.add_commit(time + 100, 'msg', path + 'other', '')
        rev_3 = self.git.add_commit(time + 200, 'msg', path, 'b')
        self.assertEqual(
            git_util.get_file_from_revision(self.git_repo, rev_1, path), 'a'
        )
        self.assertEqual(
            git_util.get_file_from_revision(self.git_repo, rev_2, path), 'a'
        )
        self.assertEqual(
            git_util.get_file_from_revision(self.git_repo, rev_3, path), 'b'
        )

    def test_list_dir_from_revision(self):
        time = 1500000000
        rev_1 = self.git.add_commit(time + 100, 'msg', 'path/a', '')
        rev_2 = self.git.add_commit(time + 200, 'msg', 'path/b', '')
        rev_3 = self.git.add_commit(time + 300, 'msg', 'path/foo/c', '')
        rev_4 = self.git.add_commit(time + 400, 'msg', 'path/b', None)
        rev_5 = self.git.add_commit(time + 500, 'msg', 'path/foo/c', None)
        self.assertCountEqual(
            git_util.list_dir_from_revision(self.git_repo, rev_1, 'path'), ['a']
        )
        self.assertCountEqual(
            git_util.list_dir_from_revision(self.git_repo, rev_2, 'path'),
            ['a', 'b'],
        )
        self.assertCountEqual(
            git_util.list_dir_from_revision(self.git_repo, rev_3, 'path'),
            ['a', 'b', 'foo'],
        )
        self.assertCountEqual(
            git_util.list_dir_from_revision(self.git_repo, rev_4, 'path'),
            ['a', 'foo'],
        )
        self.assertCountEqual(
            git_util.list_dir_from_revision(self.git_repo, rev_5, 'path'), ['a']
        )

        with self.assertRaises(subprocess.CalledProcessError):
            git_util.list_dir_from_revision(self.git_repo, rev_1, 'path/foo')

    def test_get_commit_time(self):
        path = self.git.default_filename
        time = 1500000000
        rev_1 = self.git.add_commit(
            time + 100, 'msg', path, 'a', author_time=time
        )
        rev_2 = self.git.add_commit(
            time + 300, 'msg', path, 'b', author_time=time + 200
        )
        self.assertEqual(
            git_util.get_commit_time(self.git_repo, rev_1, path), time + 100
        )
        self.assertEqual(
            git_util.get_commit_time(self.git_repo, rev_2, path), time + 300
        )

    def test_get_rev_by_time_merge(self):
        rev_a, _, rev_c = self._prepare_merged_repository()

        # simple case
        self.assertEqual(
            git_util.get_rev_by_time(
                self.git_repo, '2017-02-10T00:00:00+08:00', None
            ),
            rev_c,
        )

        # Not commit B because it is on different branch.
        self.assertEqual(
            git_util.get_rev_by_time(
                self.git_repo, '2017-02-03T00:00:00+08:00', None
            ),
            rev_a,
        )

    def test_get_branches(self):
        rev_a, rev_b, rev_c = self._prepare_merged_repository()
        self.assertCountEqual(
            git_util.get_branches(self.git_repo),
            ['refs/heads/foo', 'refs/heads/main'],
        )
        self.assertCountEqual(
            git_util.get_branches(self.git_repo, commit=rev_a),
            ['refs/heads/main'],
        )
        self.assertCountEqual(
            git_util.get_branches(self.git_repo, commit=rev_b),
            ['refs/heads/foo', 'refs/heads/main'],
        )
        self.assertCountEqual(
            git_util.get_branches(self.git_repo, commit=rev_c),
            ['refs/heads/main'],
        )

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

    def test_clean(self):
        untracked_list = [
            'untracked-file-1',
            'untracked-folder/untracked-file-1',
        ]
        excluded_list = [
            'untracked-file-2',
            'untracked-folder/untracked-file-2',
        ]
        ignored_list = [
            'untracked-file-3',
            'untracked-folder/untracked-file-3',
        ]

        # pylint: disable=unbalanced-tuple-unpacking
        untracked, untracked_in_folder = self._touch_files(untracked_list)
        excluded, excluded_in_folder = self._touch_files(excluded_list)
        ignored, ignored_in_folder = self._touch_files(ignored_list)
        gitignore = self._create_gitignore(ignored_list, self_ignore=True)

        git_util.clean(
            self.git_repo, is_dry_run=True, exclude_list=excluded_list
        )

        self.assertTrue(untracked.exists())
        self.assertTrue(untracked_in_folder.exists())
        self.assertTrue(excluded.exists())
        self.assertTrue(excluded_in_folder.exists())
        self.assertTrue(ignored.exists())
        self.assertTrue(ignored_in_folder.exists())
        self.assertTrue(gitignore.exists())

        git_util.clean(
            self.git_repo,
            remove_ignored=False,
            remove_folder=False,
            is_dry_run=False,
            exclude_list=excluded_list,
        )

        self.assertFalse(untracked.exists())
        self.assertTrue(untracked_in_folder.exists())
        self.assertTrue(excluded.exists())
        self.assertTrue(excluded_in_folder.exists())
        self.assertTrue(ignored.exists())
        self.assertTrue(ignored_in_folder.exists())
        self.assertTrue(gitignore.exists())

        git_util.clean(
            self.git_repo,
            remove_ignored=False,
            remove_folder=True,
            is_dry_run=False,
            exclude_list=excluded_list,
        )

        self.assertFalse(untracked.exists())
        self.assertFalse(untracked_in_folder.exists())
        self.assertTrue(excluded.exists())
        self.assertTrue(excluded_in_folder.exists())
        self.assertTrue(ignored.exists())
        self.assertTrue(ignored_in_folder.exists())
        self.assertTrue(gitignore.exists())

        git_util.clean(
            self.git_repo,
            remove_ignored=True,
            remove_folder=True,
            is_dry_run=False,
            exclude_list=excluded_list,
        )

        self.assertFalse(untracked.exists())
        self.assertFalse(untracked_in_folder.exists())
        self.assertTrue(excluded.exists())
        self.assertTrue(excluded_in_folder.exists())
        self.assertFalse(ignored.exists())
        self.assertFalse(ignored_in_folder.exists())
        self.assertFalse(gitignore.exists())

    def test_list_untracked(self):
        self.assertEqual(git_util.list_untracked(self.git_repo), [])

        untracked_list = [
            'untracked-file-1',
            'untracked-folder/untracked-file-1',
        ]
        excluded_list = [
            'untracked-file-2',
            'untracked-folder/untracked-file-2',
        ]
        ignored_list = [
            'untracked-file-3',
            'untracked-folder/untracked-file-3',
        ]

        self._touch_files(untracked_list + excluded_list + ignored_list)
        self._create_gitignore(ignored_list, self_ignore=True)

        self.assertSetEqual(
            set(
                git_util.list_untracked(
                    self.git_repo, exclude_list=excluded_list
                )
            ),
            set(untracked_list),
        )

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
            git_util.get_history(
                self.git_repo, path, after=time, before=time + 60
            ),
            git_util.Commit.make_commit_list(
                [
                    (time + 10, c1),
                    (time + 20, c2),
                    (time + 30, c3),
                    (time + 40, c4),
                    (time + 50, c5),
                ]
            ),
        )

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
            git_util.get_history_recursively(
                self.git_repo,
                'c',
                time,
                time + 100,
                parse,
            ),
            git_util.Commit.make_commit_list(
                [
                    (time + 30, c3),
                    (time + 90, c9),
                    (time + 100, c9),
                ]
            ),
        )
        self.assertEqual(
            git_util.get_history_recursively(
                self.git_repo,
                'b',
                time,
                time + 100,
                parse,
            ),
            git_util.Commit.make_commit_list(
                [
                    (time + 20, c2),
                    (time + 70, c7),
                    (time + 90, c9),
                    (time + 100, c9),
                ]
            ),
        )

        self.assertEqual(
            git_util.get_history_recursively(
                self.git_repo,
                'a',
                time,
                time + 100,
                parse,
            ),
            git_util.Commit.make_commit_list(
                [
                    (time + 10, c1),
                    (time + 40, c4),
                    (time + 50, c5),
                    (time + 60, c6),
                    (time + 80, c8),
                    (time + 90, c9),
                    (time + 100, c9),
                ]
            ),
        )

    def test_get_history_recursively2(self):
        def parse(_, content):
            return content.split()

        time = 1500000000
        _c1 = self.git.add_commit(time + 10, 'msg', 'f2', '')
        _c2 = self.git.add_commit(time + 20, 'msg', 'f3', '')
        c3 = self.git.add_commit(time + 30, 'msg', 'f1', 'f2 f3')
        c4 = self.git.add_commit(time + 40, 'msg', 'f2', ' ')

        self.assertEqual(
            git_util.get_history_recursively(
                self.git_repo,
                'f1',
                time,
                time + 100,
                parse,
            ),
            git_util.Commit.make_commit_list(
                [
                    (time + 30, c3),
                    (time + 40, c4),
                    (time + 100, c4),
                ]
            ),
        )

        c5 = self.git.add_commit(time + 50, 'msg', 'f3', ' ')
        self.assertEqual(
            git_util.get_history_recursively(
                self.git_repo,
                'f1',
                time,
                time + 100,
                parse,
            ),
            git_util.Commit.make_commit_list(
                [
                    (time + 30, c3),
                    (time + 40, c4),
                    (time + 50, c5),
                    (time + 100, c5),
                ]
            ),
        )

        c6 = self.git.add_commit(time + 60, 'msg', 'f2', '  ')
        self.assertEqual(
            git_util.get_history_recursively(
                self.git_repo,
                'f1',
                time,
                time + 100,
                parse,
            ),
            git_util.Commit.make_commit_list(
                [
                    (time + 30, c3),
                    (time + 40, c4),
                    (time + 50, c5),
                    (time + 60, c6),
                    (time + 100, c6),
                ]
            ),
        )

    def test_get_history_recursively_ordering(self):
        # All commits have identical commit time.
        time = 1500000000

        commits = []
        for i in range(10):
            commit = self.git.add_commit(time, 'msg', 'foo', str(i))
            commits.append(git_util.Commit(time, commit))

        # The commit ordering should be preserved.
        self.assertEqual(
            git_util.get_history_recursively(
                self.git_repo, 'foo', time, time, lambda *_: []
            ),
            commits,
        )

    def test_is_symbolic_link(self):
        time = 1500000000
        _rev_1 = self.git.add_commit(time, 'msg', 'foo', 'foo')
        rev_2 = self.git.add_commit(time + 1, 'msg', 'bar', 'bar')
        _rev_3 = self.git.add_commit(time + 2, 'msg', 'bar', None)
        rev_4 = self.git.add_commit(
            time + 3, 'msg', 'bar', None, link_target='foo'
        )
        rev_5 = self.git.add_commit(
            time + 4, 'msg', 'dir/baz', None, link_target='foo'
        )

        self.assertFalse(git_util.is_symbolic_link(self.git_repo, rev_2, 'bar'))
        self.assertTrue(git_util.is_symbolic_link(self.git_repo, rev_4, 'bar'))
        with self.assertRaises(ValueError):
            git_util.is_symbolic_link(self.git_repo, rev_4, 'dir/baz')
        self.assertTrue(
            git_util.is_symbolic_link(self.git_repo, rev_5, 'dir/baz')
        )


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
        with self.assertRaises(errors.ArgTypeError):
            git_util.argtype_git_rev('hello')


class TestFastLookup(unittest.TestCase):
    """Tests FastLookup and FastLookupEntry class."""

    git_repo = '/dummy/git/repo/path'
    branch = 'dummy-branch'
    commits = git_util.Commit.make_commit_list(
        [
            (100, 'aaaaa'),
            (200, 'bbbbb'),
            (300, 'ccccc'),
            (400, 'ddddd'),
            (500, 'eeeee'),
        ]
    )

    def test_disabled(self):
        lookup = git_util.FastLookup()
        with self.assertRaises(git_util.FastLookupFailed):
            lookup.get_rev_by_time(self.git_repo, self.branch, 123456789)
        with self.assertRaises(git_util.FastLookupFailed):
            lookup.is_containing_commit(self.git_repo, '012345abcdef')

    def test_get_rev_by_time(self):
        with mock.patch(
            'bisect_kit.git_util.get_revlist_by_period',
            return_value=self.commits,
        ):
            lookup = git_util.FastLookup()
            lookup.optimize(git_util.Period(100, 500))
            self.assertEqual(
                lookup.get_rev_by_time(self.git_repo, 100, self.branch), 'aaaaa'
            )
            self.assertEqual(
                lookup.get_rev_by_time(self.git_repo, 199, self.branch), 'aaaaa'
            )
            self.assertEqual(
                lookup.get_rev_by_time(self.git_repo, 200, self.branch), 'bbbbb'
            )
            self.assertEqual(
                lookup.get_rev_by_time(self.git_repo, 201, self.branch), 'bbbbb'
            )

            # Outside the optimized range.
            with self.assertRaises(git_util.FastLookupFailed):
                lookup.get_rev_by_time(self.git_repo, 50, self.branch)

    def test_is_containing_commit(self):
        with mock.patch(
            'bisect_kit.git_util.get_revlist_by_period',
            return_value=self.commits,
        ):
            lookup = git_util.FastLookup()
            lookup.optimize(git_util.Period(100, 500))
            lookup.get_rev_by_time(self.git_repo, 100, self.branch)
            self.assertTrue(lookup.is_containing_commit(self.git_repo, 'bbbbb'))
            self.assertTrue(lookup.is_containing_commit(self.git_repo, 'ccccc'))

            with self.assertRaises(git_util.FastLookupFailed):
                lookup.is_containing_commit(self.git_repo, 'abcdef')


if __name__ == '__main__':
    unittest.main()
