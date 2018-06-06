# Copyright 2017 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Git utility."""

from __future__ import print_function
import logging
import os
import re
import subprocess

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


def is_git_rev(s):
  """Is a git hash-like version string.

  It accepts shortened hash with at least 7 digits.
  """
  if not GIT_MIN_COMMIT_ID_LENGTH <= len(s) <= GIT_FULL_COMMIT_ID_LENGTH:
    return False
  return bool(re.match(r'^[0-9a-f]+$', s))


def argtype_git_rev(s):
  """Validates git hash."""
  if not is_git_rev(s):
    msg = 'should be git hash, at least %d digits' % GIT_MIN_COMMIT_ID_LENGTH
    raise cli.ArgTypeError(msg, '1a2b3c4d5e')
  return s


def is_git_root(path):
  """Is given path root of git repo."""
  return os.path.exists(os.path.join(path, '.git'))


def checkout_version(git_repo, rev):
  """git checkout.

  Args:
    git_repo: path of git repo.
    rev: git commit revision to checkout.
  """
  util.check_call('git', 'checkout', '-q', '-f', rev, cwd=git_repo)


def is_containing_commit(git_repo, rev):
  """Determines given commit exists.

  Args:
    git_repo: path of git repo.
    rev: git commit revision in query.

  Returns:
    True if rev is inside given git repo. If git_repo is not a git folder,
    returns False as well.
  """
  try:
    return util.check_output(
        'git', 'cat-file', '-t', rev, cwd=git_repo) == 'commit\n'
  except subprocess.CalledProcessError:
    return False
  except OSError:
    return False


def is_ancestor_commit(git_repo, old, new):
  """Determines `old` commit is ancestor of `new` commit.

  Args:
    git_repo: path of git repo.
    old: the ancestor commit.
    new: the descendant commit.

  Returns:
    True only if `old` is the ancestor of `new`. One commit is not considered
    as ancestor of itself.
  """
  return util.check_output(
      'git',
      'rev-list',
      '--ancestry-path',
      '-1',
      '%s..%s' % (old, new),
      cwd=git_repo) != ''


def get_revlist(git_repo, old, new):
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
  cmd = ['git', 'rev-list', '--reverse', '%s^..%s' % (old, new)]
  revlist = util.check_output(*cmd, cwd=git_repo).splitlines()
  return revlist


def get_commit_log(git_repo, rev):
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


def get_commit_hash(git_repo, rev):
  """Get git commit hash.

  Args:
    git_repo: path of git repo.
    rev: could be git tag, branch, or (shortened) commit hash

  Returns:
    full git commit hash
  """
  cmd = ['git', 'rev-parse', rev]
  git_rev = util.check_output(*cmd, cwd=git_repo).strip()
  assert git_rev
  return git_rev


def get_commit_time(git_repo, rev):
  """Get git commit timestamp.

  Args:
    git_repo: path of git repo
    rev: git commit id

  Returns:
    timestamp (int)
  """
  line = util.check_output(
      'git', 'log', '-1', '--format=%ct', rev, cwd=git_repo)
  return int(line)


def get_file_from_revision(git_repo, rev, path):
  """Get file content of given revision.

  Args:
    git_repo: path of git repo
    rev: git commit id
    path: file path

  Returns:
    file content (str)
  """
  return util.check_output(
      'git', 'show', '%s:%s' % (rev, path), cwd=git_repo, log_output=False)


def get_rev_by_time(git_repo, timestamp, *args):
  """Query commit of given time.

  Args:
    git_repo: path of git repo.
    timestamp: timestamp
    args: only the selected subset of history to query. If branch name is
        specified, only parent of the said branch is queried. If omitted, only
        queries the parent of current working directory.

  Returns:
    git commit hash
  """
  return util.check_output(
      'git',
      'log',
      '-1',
      '--before',
      str(timestamp),
      '--format=%H',
      *args,
      cwd=git_repo).strip()


def get_history(git_repo, path, after=None):
  """Get commit history of given path.

  Args:
    git_repo: path of git repo.
    path: path to query, relative to git_repo
    after: limit history after given time

  Yields:
    commit timestamp, git hash
  """
  cmd = ['git', 'log', '--reverse', '--format=%ct %H']
  if after:
    cmd += ['--after', str(after)]
  cmd.append(path)
  for line in util.check_output(*cmd, cwd=git_repo).splitlines():
    commit_time, git_rev = line.split()
    yield int(commit_time), git_rev


class Diff(object):
  """Class to describe the difference between git commits.

  Attributes:
    timestamp: commit timestamp
    path: git repo path relative to project root
    action: action to make the diff, possible value: CHECKOUT_TO, ADD, REMOVE.
    git_rev: git commit hash
  """
  CHECKOUT_TO = 'checkout_to'
  ADD = 'add'
  REMOVE = 'remove'

  def __init__(self, timestamp, path, action, git_rev=None):
    self.timestamp = timestamp
    self.path = path
    self.action = action
    self.git_rev = git_rev

  def apply(self, base_dir):
    """Applies the diff on disk.

    Args:
      base_dir: the project root where self.path is relative to
    """
    assert self.path
    git_repo = os.path.join(base_dir, self.path)
    if self.action == Diff.CHECKOUT_TO:
      checkout_version(git_repo, self.git_rev)
      return
    if self.action in ['add', 'remove']:
      raise NotImplementedError
    assert 0

  def summary(self, base_dir):
    """Summary string of this diff.

    Args:
      base_dir: the project root where self.path is relative to
    """
    if self.action == Diff.CHECKOUT_TO:
      git_repo = os.path.join(base_dir, self.path)
      summary = get_commit_log(git_repo, self.git_rev).splitlines()[0]
      return '%s %s %r' % (self.git_rev[:10], self.path, summary)
    return '%s %s' % (self.action, self.path)

  def __eq__(self, rhs):
    return (self.timestamp == rhs.timestamp and self.path == rhs.path and
            self.action == rhs.action and self.git_rev == rhs.git_rev)


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
  assert old == new or is_ancestor_commit(git_repo, old, new)
  commits = []
  # --first-parent is necessary for Android, see following link for more
  # discussion.
  # https://docs.google.com/document/d/1c8qiq14_ObRRjLT62sk9r5V5cyCGHX66dLYab4MVnks/edit#heading=h.n3i6mt2n6xuu
  for line in util.check_output(
      'git',
      'rev-list',
      '--timestamp',
      '--reverse',
      '--first-parent',
      '%s..%s' % (old, new),
      cwd=git_repo).splitlines():
    timestamp, git_rev = line.split()
    commits.append([int(timestamp), git_rev])

  # bisect-kit has a fundamental assumption that commit timestamps are
  # increasing because we sort and bisect the commits by timestamp across git
  # repos. If not increasing, we have to adjust the timestamp as workaround.
  # This might lead to bad bisect result, however the bad probability is low in
  # practice since most machines' clocks are good enough.
  if commits != sorted(commits, key=lambda x: x[0]):
    logger.warning('Commit timestamps are not increasing')
    last_timestamp = -1
    adjusted = 0
    for commit in commits:
      if commit[0] < last_timestamp:
        commit[0] = last_timestamp
        adjusted += 1

      last_timestamp = commit[0]
    logger.warning('%d timestamps adjusted', adjusted)

  return commits


def get_difflist_between_two_commit(base_dir, path, old, new):
  """Get difflist between (old, new].

  Args:
    base_dir: the project root
    path: the path relative to the project root
    old: old commit hash (exclusive)
    new: new commit hash (inclusive)

  Returns:
    list of Diff objects
  """
  git_repo = os.path.join(base_dir, path)
  difflist = []
  for timestamp, git_rev in list_commits_between_commits(git_repo, old, new):
    difflist.append(Diff(timestamp, path, Diff.CHECKOUT_TO, git_rev))
  return difflist


def get_difflist_between_two_set(base_dir, old_set, new_set):
  result = []
  for path in set(old_set) | set(new_set):
    git_repo = os.path.join(base_dir, path)
    if path in old_set and path in new_set:
      old = old_set[path]
      new = new_set[path]
      if old == new:
        # nochange, do nothing
        pass
      elif is_ancestor_commit(git_repo, old, new):
        # normal case
        for diff in get_difflist_between_two_commit(base_dir, path, old, new):
          result.append(diff)
      else:
        # maybe switch branch?
        # TODO(kcwu): handle discontinuous properly (crbug.com/827092)
        logger.warning(
            'Warning: dependency "%s" discontinuous. Not supported yet', path)
        return []
    elif path in old_set:
      # remove dependency
      # TODO(kcwu): handle removal properly (crbug.com/827092)
      logger.warning('Warning: dependency "%s" was removed. Not supported yet',
                     path)
      return []
    else:
      assert path in new_set
      # add dependency
      # TODO(kcwu): handle addition properly (crbug.com/827092)
      logger.warning('Warning: dependency "%s" was added. Not supported yet',
                     path)
      return []

  result.sort(key=lambda diff: (diff.timestamp, diff.path))

  return result
