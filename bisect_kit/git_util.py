# Copyright 2017 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Git utility."""

from __future__ import annotations
from __future__ import print_function

import bisect
import dataclasses
import logging
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
import typing

from bisect_kit import cache_util
from bisect_kit import cli
from bisect_kit import util


logger = logging.getLogger(__name__)

GIT_FULL_COMMIT_ID_LENGTH = 40

# Minimal acceptable length of git commit id.
#
# For chromium, hash collision rate over number of digits:
#   - 6 digits: 4.85%
#   - 7 digits: 0.32%
#   - 8 digits: 0.01%
# As foolproof check, 7 digits should be enough.
GIT_MIN_COMMIT_ID_LENGTH = 7


@dataclasses.dataclass(order=True, eq=True, frozen=True)
class Commit:
    """A data class represents a git commit."""

    timestamp: int = 0
    rev: str = ''
    subject: str = 'dummy subject'

    @staticmethod
    def from_rev_line(rev_line: str) -> Commit:
        # The input `rev_line` may not include the subject part, thus here we unpack
        # `subject` as a list with an asterisk. The length of the `subject` list
        # should be at most one.
        ts_str, rev, *subject = rev_line.split(' ', 2)
        return Commit(int(ts_str), rev, *subject)

    @staticmethod
    def make_commit_list(commit_tuples) -> list[Commit]:
        return [Commit(*tuple) for tuple in commit_tuples]


@dataclasses.dataclass(eq=True)
class CommitMeta:
    """A data class represents the metadata of a git commit."""

    message: str | None = None
    object: str | None = None
    tree: str | None = None
    type: str | None = None
    parent: list[str] | None = None
    author: str | None = None
    committer: str | None = None
    author_time: int | None = None
    committer_time: int | None = None

    UNKNOWN_SUMMARY: typing.ClassVar[str] = '(unknown)'

    @staticmethod
    def from_git_commit_object(git_cat_file_output: str) -> CommitMeta:
        meta_dict: dict[str, typing.Any] = {}
        header, meta_dict['message'] = git_cat_file_output.split('\n\n', 1)
        for line in header.splitlines():
            if m := re.match(r'^(object|tree|type) (\w+)', line):
                meta_dict[m.group(1)] = m.group(2)
                continue

            if m := re.match(r'^parent (\w+)', line):
                meta_dict['parent'] = line.split()[1:]
                continue

            if m := re.match(r'^(author|committer) (.*) (\d+) (\S+)$', line):
                meta_dict[m.group(1)] = m.group(2)
                meta_dict['%s_time' % m.group(1)] = int(m.group(3))
                continue
        return CommitMeta(**meta_dict)

    @staticmethod
    def get_summary(meta) -> str:
        if meta is None or meta.message is None:
            return CommitMeta.UNKNOWN_SUMMARY
        return meta.message.splitlines()[0]


@dataclasses.dataclass(order=True, eq=True, frozen=True)
class Period:
    """A data class represents an inclusive period of time."""

    begin: int = 0
    end: int = 0

    def __contains__(self, other):
        if isinstance(other, int):
            return self.begin <= other <= self.end
        if isinstance(other, Period):
            return self.begin <= other.begin and other.end <= self.end
        raise TypeError(
            'The membership test only accepts a `Period` or an `int`'
        )


def is_git_rev(rev: str) -> bool:
    """Is a git hash-like version string.

    It accepts shortened hash with at least 7 digits.
    """
    if not GIT_MIN_COMMIT_ID_LENGTH <= len(rev) <= GIT_FULL_COMMIT_ID_LENGTH:
        return False
    return bool(re.match(r'^[0-9a-f]+$', rev))


def argtype_git_rev(rev: str) -> str:
    """Validates git hash."""
    if not is_git_rev(rev):
        msg = (
            'should be git hash, at least %d digits' % GIT_MIN_COMMIT_ID_LENGTH
        )
        raise cli.ArgTypeError(msg, '1a2b3c4d5e')
    return rev


def is_git_root(path: str) -> bool:
    """Is given path root of git repo."""
    return os.path.exists(os.path.join(path, '.git'))


def is_git_bare_dir(path: str) -> bool:
    """Is inside .git folder or bare git checkout."""
    if not os.path.isdir(path):
        return False
    try:
        return (
            util.check_output(
                'git', 'rev-parse', '--is-bare-repository', cwd=path
            )
            == 'true\n'
        )
    except subprocess.CalledProcessError:
        return False


def clone(git_repo: str, repo_url: str, reference: str | None = None) -> None:
    if not os.path.exists(git_repo):
        os.makedirs(git_repo)
    cmd = ['git', 'clone', repo_url, '.']
    if reference:
        cmd += ['--reference', reference]
    util.check_call(*cmd, cwd=git_repo)


def checkout_version(git_repo: str, rev: str) -> None:
    """git checkout.

    Args:
      git_repo: path of git repo.
      rev: git commit revision to checkout.
    """
    util.check_call('git', 'checkout', '-q', '-f', rev, cwd=git_repo)


def init(git_repo: str, initial_branch: str = 'main') -> None:
    """git init.

    git_repo and its parent directories will be created if they don't exist.

    Args:
      git_repo: path of git repo.
      initial_branch: the default branch after git init
    """
    if not os.path.exists(git_repo):
        os.makedirs(git_repo)

    util.check_call(
        'git', 'init', '-q', '--initial-branch', initial_branch, cwd=git_repo
    )


def pull(git_repo: str) -> None:
    util.check_call('git', 'pull', cwd=git_repo)


def commit_file(
    git_repo: str,
    path: str,
    message: str,
    content: str,
    commit_time: str | None = None,
    author_time: str | None = None,
) -> None:
    """Commit a file.

    Args:
      git_repo: path of git repo
      path: file path, relative to git_repo
      message: commit message
      content: file content
      commit_time: commit timestamp
      author_time: author timestamp
    """
    if author_time is None:
        author_time = commit_time

    env = {}
    if author_time:
        env['GIT_AUTHOR_DATE'] = str(author_time)
    if commit_time:
        env['GIT_COMMITTER_DATE'] = str(commit_time)

    full_path = os.path.join(git_repo, path)
    dirname = os.path.dirname(full_path)
    if not os.path.exists(dirname):
        os.makedirs(dirname)
    with open(full_path, 'w') as f:
        f.write(content)

    util.check_call('git', 'add', path, cwd=git_repo)
    util.check_call(
        'git', 'commit', '-q', '-m', message, path, cwd=git_repo, env=env
    )


def config(git_repo: str, *args) -> None:
    """Wrapper of 'git config'.

    Args:
      git_repo: path of git repo.
      args: parameters pass to 'git config'
    """
    util.check_call('git', 'config', *args, cwd=git_repo)


def fetch(git_repo: str, *args, retry_prune_if_conflict: bool = False) -> None:
    """Wrapper of 'git fetch' with retry support.

    Args:
      git_repo: path of git repo.
      args: parameters pass to 'git fetch'
      retry_prune_if_conflict: retry with --prune if git references conflict
    """
    tries = 0
    while True:
        tries += 1
        stderr_lines: list[str] = []
        try:
            util.check_call(
                'git',
                'fetch',
                *args,
                cwd=git_repo,
                stderr_callback=stderr_lines.append,
            )
            return
        except subprocess.CalledProcessError:
            if tries >= 5:
                logger.error('git fetch failed too much times')
                raise
            stderr = ''.join(stderr_lines)
            # retry 5xx internal server error
            if 'The requested URL returned error: 5' in stderr:
                delay = min(60, 10 * 2**tries)
                logger.warning(
                    'git fetch failed, will retry %s seconds later', delay
                )
                time.sleep(delay)
                continue

            # We have `retry_prune_if_conflict` instead of always pruning because as
            # a bisector, we want to keep historical references as long as possible,
            # even if they have been deleted on remote server. We prune references
            # only if conflict is detected.
            if (
                '(unable to update local ref)' in stderr
                and retry_prune_if_conflict
                and '--prune' not in args
            ):
                logger.warning(
                    (
                        'git fetch failed due to conflicting references; try again with'
                        ' --prune'
                    )
                )
                args = '--prune', *args
                continue
            raise


def _adjust_timestamp_increasingly(
    commits: list[Commit], show_warning: bool = False
) -> list[Commit]:
    """Adjust commit timestamps.

    After adjust, the timestamps are increasing.

    Args:
      commits: A list of `Commit` objects.

    Returns:
      The adjusted list of `Commit` objects.
    """
    result: list[Commit] = []
    adjusted_count = 0
    last_timestamp = -1
    for commit in commits:
        if commit.timestamp < last_timestamp:
            adjusted_count += 1
        last_timestamp = max(last_timestamp, commit.timestamp)
        result.append(Commit(last_timestamp, commit.rev, commit.subject))

    if show_warning and adjusted_count > 0:
        logger.warning('Commit timestamps are not increasing')
        logger.warning('%d timestamps adjusted', adjusted_count)

    return result


class FastLookupFailed(Exception):
    """No data is cached for this query.

    The caller should fallback to the original operation.
    """


class FastLookupEntry:
    """Cached commits from one branch of given time period.

    With this class, we can look up commit via commit hash and timestamp fast.
    """

    def __init__(self, git_repo: str, branch: str):
        self.git_repo: str = git_repo
        self.branch: str = branch
        self.optimized_period: Period | None = None
        self.cached: list[Commit] = []
        self.commit_to_index: dict[str, int] = {}

    def optimize(self, period: Period):
        assert period.begin <= period.end
        if self.optimized_period and period in self.optimized_period:
            # already done
            return

        self.cached = get_revlist_by_period(self.git_repo, self.branch, period)
        self.optimized_period = period

        # Adjust timestamps, so we can do binary search by timestamp
        self.cached = _adjust_timestamp_increasingly(self.cached)

        self.commit_to_index = {
            commit.rev: i for i, commit in enumerate(self.cached)
        }

    def get_rev_by_time(self, timestamp: int) -> str | None:
        if not self.optimized_period:
            raise FastLookupFailed
        if timestamp not in self.optimized_period:
            raise FastLookupFailed

        # Note that, the return value might be different as "git rev-list" if the
        # actual commit timestamps are not fully increasing.
        idx = bisect.bisect_right(self.cached, Commit(timestamp))
        if idx == 0 and timestamp < self.cached[0].timestamp:
            return None
        if idx == len(self.cached) or self.cached[idx].timestamp != timestamp:
            idx -= 1
        return self.cached[idx].rev

    def is_containing_commit(self, rev: str) -> bool:
        if rev in self.commit_to_index:
            return True
        raise FastLookupFailed


class FastLookup:
    """Collection of FastLookupEntry"""

    def __init__(self):
        self.entries: dict[str, dict[str, FastLookupEntry]] = {}
        self.target_period: Period | None = None

    def optimize(self, period: Period):
        self.target_period = period

    def disable(self):
        self.target_period = None
        self.entries = {}

    def get_rev_by_time(
        self, git_repo: str, timestamp: int, branch: str
    ) -> str | None:
        if not self.target_period:
            raise FastLookupFailed
        if timestamp not in self.target_period:
            raise FastLookupFailed

        if git_repo not in self.entries:
            self.entries[git_repo] = {}
        if branch not in self.entries[git_repo]:
            self.entries[git_repo][branch] = FastLookupEntry(git_repo, branch)
        entry = self.entries[git_repo][branch]
        entry.optimize(self.target_period)
        return entry.get_rev_by_time(timestamp)

    def is_containing_commit(self, git_repo: str, rev: str) -> bool:
        # This function is optimized only after get_rev_by_time() is invoked.
        if git_repo not in self.entries:
            raise FastLookupFailed

        for entry in self.entries[git_repo].values():
            try:
                return entry.is_containing_commit(rev)
            except FastLookupFailed:
                pass
        raise FastLookupFailed


fast_lookup = FastLookup()


@cache_util.Cache.default_disabled
def is_containing_commit(git_repo: str, rev: str) -> bool:
    """Determines given commit exists.

    Args:
      git_repo: path of git repo.
      rev: git commit revision in query.

    Returns:
      True if rev is inside given git repo. If git_repo is not a git folder,
      returns False as well.
    """
    try:
        return fast_lookup.is_containing_commit(git_repo, rev)
    except FastLookupFailed:
        pass

    try:
        return util.check_output(
            'git', 'cat-file', '-t', rev, cwd=git_repo
        ) in ['commit\n', 'tag\n']
    except subprocess.CalledProcessError:
        return False
    except OSError:
        return False


@cache_util.Cache.default_disabled
def is_ancestor_commit(git_repo: str, old: str, new: str) -> bool:
    """Determines `old` commit is ancestor of `new` commit.

    Args:
      git_repo: path of git repo.
      old: the ancestor commit.
      new: the descendant commit.

    Returns:
      True only if `old` is the ancestor of `new`. One commit is not considered
      as ancestor of itself.
    """
    try:
        return (
            util.check_output(
                'git',
                'rev-list',
                '--ancestry-path',
                '-1',
                '%s..%s' % (old, new),
                cwd=git_repo,
            )
            != ''
        )
    except subprocess.CalledProcessError:
        return False


@cache_util.Cache.default_disabled
def get_commit_metadata(git_repo: str, rev: str) -> CommitMeta:
    """Get metadata of given commit.

    Args:
      git_repo: path of git repo.
      rev: git commit revision in query.

    Returns:
      dict of metadata, including (if available):
        tree: hash of git tree object
        parent: list of parent commits; this field is unavailable for the very
            first commit of git repo.
        author: name and email of author
        author_time: author timestamp (without timezone information)
        committer: name and email of committer
        committer_time: commit timestamp (without timezone information)
        message: commit message text
    """
    data = util.check_output(
        'git', 'cat-file', '-p', rev, cwd=git_repo, log_stdout=False
    )
    return CommitMeta.from_git_commit_object(data)


def get_batch_commit_metadata(
    git_repo: str, revs: typing.Iterable[str]
) -> dict[str, CommitMeta | None]:
    query = '\n'.join(revs)
    logger.debug('get_batch_commit_metadata %r', query)
    with tempfile.NamedTemporaryFile('w+t') as f:
        f.write(query)
        f.flush()
        # The `util.check_output_in_bytes()` function doesn't support stdin, so use
        # shell redirect instead.
        # Call binary version because we need to count size in bytes later.
        data = util.check_output_in_bytes(
            'sh', '-c', 'git cat-file --batch < ' + f.name, cwd=git_repo
        )

    metas: dict[str, CommitMeta | None] = {}
    while data:
        first_line, data = data.split(b'\n', 1)
        m = re.match(r'^(\w+) (\w+)(?: (\d+))?', first_line.decode('utf8'))
        assert m, repr(first_line)
        object_name, object_type = m.group(1, 2)
        if not m.group(3):
            metas[object_name] = None
            continue
        assert object_type in ['commit', 'tag'], (
            'unsupported object type: %s' % object_type
        )
        object_size = int(m.group(3))
        assert data[object_size] == ord(b'\n'), repr(data[object_size])
        obj, data = data[:object_size], data[object_size + 1 :]
        metas[object_name] = CommitMeta.from_git_commit_object(
            obj.decode('utf8')
        )
    return metas


def get_revlist(git_repo: str, old: str, new: str) -> list[str]:
    """Enumerates git commit between two revisions (inclusive).

    Args:
      git_repo: path of git repo.
      old: git commit revision.
      new: git commit revision.

    Returns:
      list of git revisions. The list contains the input revisions, old and new.
    """
    assert old
    assert new
    cmd = [
        'git',
        'rev-list',
        '--first-parent',
        '--reverse',
        '%s^..%s' % (old, new),
    ]
    revlist = util.check_output(*cmd, cwd=git_repo).splitlines()
    return revlist


def get_commit_log(git_repo: str, rev: str) -> str:
    """Get git commit log.

    Args:
      git_repo: path of git repo.
      rev: git commit revision.

    Returns:
      commit log message
    """
    cmd = ['git', 'log', '-1', '--format=%B', rev]
    msg = util.check_output(*cmd, cwd=git_repo)
    return msg


def get_commit_hash(git_repo: str, rev: str) -> str:
    """Get git commit hash.

    Args:
      git_repo: path of git repo.
      rev: could be git tag, branch, or (shortened) commit hash

    Returns:
      full git commit hash

    Raises:
      ValueError: `rev` is not unique or doesn't exist
    """
    try:
        # Use '^{commit}' to restrict search only commits.
        # Use '--' to avoid ambiguity, like matching rev against path name.
        output = util.check_output(
            'git', 'rev-parse', '%s^{commit}' % rev, '--', cwd=git_repo
        )
        git_rev = output.rstrip('-\n')
    except subprocess.CalledProcessError as e:
        # Do not use 'git rev-parse --disambiguate' to determine uniqueness
        # because it searches objects other than commits as well.
        raise ValueError('%s is not unique or does not exist' % rev) from e
    assert is_git_rev(git_rev)
    return git_rev


def get_commit_time(git_repo: str, rev: str, path: str | None = None) -> int:
    """Get git commit timestamp.

    Args:
      git_repo: path of git repo
      rev: git commit id, branch name, tag name, or other git object
      path: path, relative to git_repo

    Returns:
      timestamp (int)
    """
    cmd = ['git', 'log', '-1', '--format=%ct', rev]
    if path:
        cmd += ['--', path]
    line = util.check_output(*cmd, cwd=git_repo)
    return int(line)


def is_symbolic_link(git_repo: str, rev: str, path: str) -> bool:
    """Check if a file is symbolic link.

    Args:
      git_repo: path of git repo
      rev: git commit id
      path: file path

    Returns:
      True if the specified file is a symbolic link in repo.

    Raises:
      ValueError if not found
    """
    # format: 120000 blob 8735a8c1dd96ede39a21d983d5c96792fd15c1a5 default.xml
    # TODO(kcwu): handle escaped path with special characters
    parts = util.check_output(
        'git', 'ls-tree', rev, '--full-name', path, cwd=git_repo
    ).split()
    if len(parts) >= 4 and parts[3] == path:
        return stat.S_ISLNK(int(parts[0], 8))

    raise ValueError(
        'file %s is not found in repo:%s rev:%s' % (path, git_repo, rev)
    )


@cache_util.Cache.default_disabled
def get_file_from_revision(git_repo: str, rev: str, path: str) -> str:
    """Get file content of given revision.

    Args:
      git_repo: path of git repo
      rev: git commit id
      path: file path

    Returns:
      file content (str)
    """
    result = util.check_output(
        'git', 'show', '%s:%s' % (rev, path), cwd=git_repo, log_stdout=False
    )

    # It might be a symbolic link.
    # In extreme case, it's possible that filenames contain special characters,
    # like newlines. In practice, it should be safe to assume no such cases and
    # reduce disk i/o.
    if '\n' not in result and is_symbolic_link(git_repo, rev, path):
        return get_file_from_revision(git_repo, rev, result)

    return result


def list_dir_from_revision(git_repo: str, rev: str, path: str) -> list[str]:
    """Lists entries of directory of given revision.

    Args:
      git_repo: path of git repo
      rev: git commit id
      path: directory path, relative to git root

    Returns:
      list of names

    Raises:
      subprocess.CalledProcessError: if `path` doesn't exists in `rev`
    """
    return util.check_output(
        'git',
        'ls-tree',
        '--name-only',
        '%s:%s' % (rev, path),
        cwd=git_repo,
        log_stdout=False,
    ).splitlines()


def get_rev_by_time(
    git_repo: str, timestamp: int, branch: str | None, path: str | None = None
) -> str | None:
    """Query commit of given time.

    Args:
      git_repo: path of git repo.
      timestamp: timestamp
      branch: only query parent of the `branch`. If branch=None, it means 'HEAD'
          (current branch, usually).
      path: only query history of path, relative to git_repo

    Returns:
      git commit hash. None if path didn't exist at the given time.
    """
    if not branch:
        branch = 'HEAD'

    if not path:
        try:
            return fast_lookup.get_rev_by_time(git_repo, timestamp, branch)
        except FastLookupFailed:
            pass

    cmd = [
        'git',
        'rev-list',
        '--first-parent',
        '-1',
        '--before',
        str(timestamp),
        branch,
        '--',
    ]
    if path:
        cmd += [path]

    result = util.check_output(*cmd, cwd=git_repo).strip()
    return result or None


def get_revlist_by_period(
    git_repo: str, branch: str, period: Period
) -> list[Commit]:
    # Find the last commit before the begin of given period.
    text = util.check_output(
        'git',
        'rev-list',
        '--first-parent',
        '--timestamp',
        '-1',
        '--before',
        str(period.begin - 1),
        branch,
        '--',
        cwd=git_repo,
    )

    # Find commits in the period.
    text += util.check_output(
        'git',
        'rev-list',
        '--first-parent',
        '--timestamp',
        '--reverse',
        '--after',
        str(period.begin),
        '--before',
        str(period.end),
        branch,
        '--',
        cwd=git_repo,
    )

    return [Commit.from_rev_line(line) for line in text.splitlines()]


def reset_hard(git_repo: str) -> None:
    """Restore modified and deleted files.

    This is simply wrapper of "git reset --hard".

    Args:
      git_repo: path of git repo.
    """
    util.check_call('git', 'reset', '--hard', cwd=git_repo)


def list_untracked(
    git_repo: str, excludes: list[str] | None = None
) -> list[str]:
    """List untracked files and directories.

    Args:
      git_repo: path of git repo.
      excludes: files and/or directories to ignore, relative to git_repo

    Returns:
      list of paths, relative to git_repo
    """
    exclude_flags = []
    if excludes:
        for exclude in excludes:
            assert not os.path.isabs(exclude), 'should be relative'
            exclude_flags += ['--exclude', '/' + re.escape(exclude)]

    result = []
    for path in util.check_output(
        'git',
        'ls-files',
        '--others',
        '--exclude-standard',
        *exclude_flags,
        cwd=git_repo,
    ).splitlines():
        # Remove the trailing slash, which means directory.
        path = path.rstrip('/')
        result.append(path)
    return result


def distclean(git_repo: str, excludes: list[str] | None = None) -> None:
    """Clean up git repo directory.

    Restore modified and deleted files. Delete untracked files.

    Args:
      git_repo: path of git repo.
      excludes: files and/or directories to ignore, relative to git_repo
    """
    reset_hard(git_repo)

    # Delete untracked files.
    for untracked in list_untracked(git_repo, excludes=excludes):
        path = os.path.join(git_repo, untracked)
        logger.debug('delete untracked: %s', path)
        if os.path.islink(path):
            os.unlink(path)
        elif os.path.isdir(path):
            shutil.rmtree(path)
        else:
            os.unlink(path)


def get_history(
    git_repo: str,
    path: str | None = None,
    branch: str | None = None,
    after: int | None = None,
    before: int | None = None,
    padding_begin: bool = False,
    padding_end: bool = False,
    with_subject: bool = False,
) -> list[Commit]:
    """Get commit history of given path.

    `after` and `before` could be outside of lifetime of `path`. `padding` is
    used to control what to return for such cases.

    Args:
      git_repo: path of git repo.
      path: path to query, relative to git_repo
      branch: branch name or ref name
      after: limit history after given time (inclusive)
      before: limit history before given time (inclusive)
      padding_begin: If True, pads returned result with dummy record at exact
          'after' time, if 'path' existed at that time.
      padding_end: If True, pads returned result with dummy record at exact
          'before' time, if 'path' existed at that time.
      with_subject: If True, return commit subject together

    Returns:
      List of (timestamp, git hash, subject); or (timestamp, git hash) depends
      on with_subject flag. They are all events when `path` was added, removed,
      modified, and start and end time if `padding` is true. If `padding` and
      `with_subject` are both true, 'dummy subject' will be returned as padding
      history's subject.

      For each pair, at `timestamp`, the repo state is `git hash`. In other
      words, `timestamp` is not necessary the commit time of `git hash` for the
      padded entries.
    """
    log_format = '%ct %H' if not with_subject else '%ct %H %s'
    cmd = [
        'git',
        'log',
        '--reverse',
        '--first-parent',
        '--format=' + log_format,
    ]
    if after:
        cmd += ['--after', str(after)]
    if before:
        cmd += ['--before', str(before)]
    if branch:
        assert not is_git_rev(branch)
        cmd += [branch]
    if path:
        # '--' is necessary otherwise if `path` is removed in current revision, git
        # will complain it's an ambiguous argument which may be path or something
        # else (like git branch name, tag name, etc.)
        cmd += ['--', path]

    lines = util.check_output(*cmd, cwd=git_repo).splitlines()
    result = [Commit.from_rev_line(line) for line in lines]

    if padding_end:
        assert before, 'padding_end=True make no sense if before=None'
        if get_rev_by_time(git_repo, before, branch, path=path):
            before = int(before)
            if not result or result[-1].timestamp != before:
                git_rev = get_rev_by_time(git_repo, before, branch)
                assert git_rev
                result.append(Commit(before, git_rev))

    if padding_begin:
        assert after, 'padding_begin=True make no sense if after=None'
        if get_rev_by_time(git_repo, after, branch, path=path):
            after = int(after)
            if not result or result[0].timestamp != after:
                git_rev = get_rev_by_time(git_repo, after, branch)
                assert git_rev
                result.insert(0, Commit(after, git_rev))

    return result


def get_history_recursively(
    git_repo: str,
    path: str,
    after: int,
    before: int,
    parser_callback: typing.Callable[[str, str], list[str] | None],
    padding_end: bool = True,
    branch: str | None = None,
) -> list[Commit]:
    """Get commit history of given path and its dependencies.

    In comparison to get_history(), get_history_recursively also takes
    dependencies into consideration. For example, if file A referenced file B,
    get_history_recursively(A) will return commits of B in addition to A. This
    applies recursively, so commits of C will be included if file B referenced
    file C, and so on.

    This function is file type neutral. `parser_callback(filename, content)` will
    be invoked to parse file content and should return list of filename of
    dependencies. If `parser_callback` returns None (usually syntax error), the
    commit is omitted.

    Args:
      git_repo: path of git repo
      path: path to query, relative to git_repo
      after: limit history after given time (inclusive)
      before: limit history before given time (inclusive)
      parser_callback: callback to parse file content. See above comment.
      padding_end: If True, pads returned result with dummy record at exact
          'after' time, if 'path' existed at that time.
      branch: branch name or ref name

    Returns:
      list of (commit timestamp, git hash)
    """
    history = get_history(
        git_repo,
        path,
        after=after,
        before=before,
        padding_begin=True,
        branch=branch,
    )

    # Collect include information of each commit.
    includes: dict[str, set[str]] = {}
    for commit in history:
        content = get_file_from_revision(git_repo, commit.rev, path)
        parse_result = parser_callback(path, content)
        if parse_result is None:
            continue
        for include_name in parse_result:
            if include_name not in includes:
                includes[include_name] = set()
            includes[include_name].add(commit.rev)

    # Analyze the start time and end time of each include.
    dependencies = []
    for include_name, rev_set in includes.items():
        appeared = None
        for commit in history:
            if commit.rev in rev_set:
                if not appeared:
                    appeared = commit.timestamp
            else:
                if appeared:
                    # dependency file exists in time range [appeared, commit.timestamp)
                    dependencies.append(
                        (include_name, appeared, commit.timestamp - 1)
                    )
                    appeared = None

        if appeared is not None:
            dependencies.append((include_name, appeared, before))

    # Recursion and merge.
    result = list(history)
    for include, appeared, disappeared in dependencies:
        result += get_history_recursively(
            git_repo,
            include,
            appeared,
            disappeared,
            parser_callback,
            padding_end=False,
            branch=branch,
        )

    # Sort and padding.
    result.sort(key=lambda x: x.timestamp)
    if padding_end:
        result.append(Commit(before, result[-1].rev, result[-1].subject))

    # Dedup.
    result2: list[Commit] = []
    for x in result:
        if result2 and result2[-1] == x:
            continue
        result2.append(x)

    return result2


def get_branches(
    git_repo: str,
    all_branches: bool = True,
    commit: str | None = None,
    remote: bool = False,
) -> list[str]:
    """Get branches of a repository.

    Args:
      git_repo: path of git repo
      all_branches: return remote branches if is set to True
      commit: return branches containing this commit if is not None
      remote: only remote tracking branches

    Returns:
      list of branch names
    """
    cmd = ['git', 'branch', '--format=%(refname)']
    if all_branches:
        cmd += ['-a']
    if commit:
        cmd += ['--contains', commit]
    if remote:
        cmd.append('--remote')

    result = []
    for line in util.check_output(*cmd, cwd=git_repo).splitlines():
        result.append(line.strip())
    return result


def get_tags(git_repo):
    cmd = ['git', 'tag', '--list']
    result = []
    for line in util.check_output(
        *cmd, cwd=git_repo, log_stdout=False
    ).splitlines():
        result.append(line.strip())
    return result


def get_remotes(git_repo):
    """Gets lists of git remote."""
    return util.check_output('git', 'remote', cwd=git_repo).splitlines()


def get_remote_url(git_repo, remote_name):
    """Gets url of given remote."""
    return util.check_output(
        'git', 'remote', 'get-url', remote_name, cwd=git_repo
    ).strip()


def set_remote_url(git_repo, remote_name, url):
    """Sets url of given remote."""
    util.check_call('git', 'remote', 'set-url', remote_name, url, cwd=git_repo)


def list_commits_between_commits(git_repo, old, new):
    """Get all commits between (old, new].

    Args:
      git_repo: path of git repo.
      old: old commit hash (exclusive)
      new: new commit hash (inclusive)

    Returns:
      list of (timestamp, rev)
    """
    assert old and new
    if old == new:
        return []

    assert is_ancestor_commit(git_repo, old, new)
    # --first-parent is necessary for Android, see following link for more
    # discussion.
    # https://docs.google.com/document/d/1c8qiq14_ObRRjLT62sk9r5V5cyCGHX66dLYab4MVnks/edit#heading=h.n3i6mt2n6xuu
    lines = util.check_output(
        'git',
        'rev-list',
        '--timestamp',
        '--reverse',
        '--first-parent',
        '%s..%s' % (old, new),
        cwd=git_repo,
    ).splitlines()
    commits = [Commit.from_rev_line(line) for line in lines]

    # bisect-kit has a fundamental assumption that commit timestamps are
    # increasing because we sort and bisect the commits by timestamp across git
    # repos. If not increasing, we have to adjust the timestamp as workaround.
    # This might lead to bad bisect result, however the bad probability is low in
    # practice since most machines' clocks are good enough.
    commits = _adjust_timestamp_increasingly(commits, show_warning=True)

    return commits
