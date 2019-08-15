# -*- coding: utf-8 -*-
# Copyright 2017 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Git utility."""

from __future__ import print_function
import logging
import os
import re
import subprocess
import time

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


def is_git_bare_dir(path):
  """Is inside .git folder or bare git checkout."""
  if not os.path.isdir(path):
    return False
  try:
    return util.check_output(
        'git', 'rev-parse', '--is-bare-repository', cwd=path) == 'true\n'
  except subprocess.CalledProcessError:
    return False


def clone(git_repo, repo_url, reference=None):
  if not os.path.exists(git_repo):
    os.makedirs(git_repo)
  cmd = ['git', 'clone', repo_url, '.']
  if reference:
    cmd += ['--reference', reference]
  util.check_call(*cmd, cwd=git_repo)


def checkout_version(git_repo, rev):
  """git checkout.

  Args:
    git_repo: path of git repo.
    rev: git commit revision to checkout.
  """
  util.check_call('git', 'checkout', '-q', '-f', rev, cwd=git_repo)


def init(git_repo):
  """git init.

  git_repo and its parent directories will be created if they don't exist.

  Args:
    git_repo: path of git repo.
  """
  if not os.path.exists(git_repo):
    os.makedirs(git_repo)

  util.check_call('git', 'init', '-q', cwd=git_repo)


def commit_file(git_repo,
                path,
                message,
                content,
                commit_time=None,
                author_time=None):
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
      'git', 'commit', '-q', '-m', message, path, cwd=git_repo, env=env)


def config(git_repo, *args):
  """Wrapper of 'git config'.

  Args:
    git_repo: path of git repo.
    args: parameters pass to 'git config'
  """
  util.check_call('git', 'config', *args, cwd=git_repo)


def fetch(git_repo, *args):
  """Wrapper of 'git fetch' with retry support.

  Args:
    git_repo: path of git repo.
    args: parameters pass to 'git fetch'
  """
  for tries in range(5):
    if tries > 0:
      delay = min(60, 10 * 2**tries)
      logger.warning('git fetch failed, will retry %s seconds later', delay)
      time.sleep(delay)

    stderr_lines = []
    try:
      util.check_call(
          'git',
          'fetch',
          *args,
          cwd=git_repo,
          stderr_callback=stderr_lines.append)
      break
    except subprocess.CalledProcessError:
      stderr = ''.join(stderr_lines)
      # only retry 5xx internal server error
      if 'The requested URL returned error: 5' not in stderr:
        raise
  else:
    # Reached retry limit but haven't succeeded.
    # In other words, there must be exceptions raised inside above loop.
    logger.error('git fetch failed too much times')
    # It's okay to raise because we are in the same scope as above loop.
    # pylint: disable=misplaced-bare-raise
    raise


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


def get_commit_metadata(git_repo, rev):
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
  meta = {}
  data = util.check_output(
      'git', 'cat-file', '-p', rev, cwd=git_repo, log_stdout=False)
  header, meta['message'] = data.split('\n\n', 1)
  for line in header.splitlines():
    m = re.match(r'^tree (\w+)', line)
    if m:
      meta['tree'] = m.group(1)
      continue

    m = re.match(r'^parent (\w+)', line)
    if m:
      meta['parent'] = line.split()[1:]
      continue

    m = re.match(r'^(author|committer) (.*) (\d+) (\S+)$', line)
    if m:
      meta[m.group(1)] = m.group(2)
      meta['%s_time' % m.group(1)] = int(m.group(3))
      continue
  return meta


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

  Raises:
    ValueError: `rev` is not unique or doesn't exist
  """
  try:
    # Use '^{commit}' to restrict search only commits.
    # Use '--' to avoid ambiguity, like matching rev against path name.
    output = util.check_output(
        'git', 'rev-parse', '%s^{commit}' % rev, '--', cwd=git_repo)
    git_rev = output.rstrip('-\n')
  except subprocess.CalledProcessError:
    # Do not use 'git rev-parse --disambiguate' to determine uniqueness
    # because it searches objects other than commits as well.
    raise ValueError('%s is not unique or does not exist' % rev)
  assert is_git_rev(git_rev)
  return git_rev


def get_commit_time(git_repo, rev, path):
  """Get git commit timestamp.

  Args:
    git_repo: path of git repo
    rev: git commit id, branch name, tag name, or other git object
    path: path, relative to git_repo

  Returns:
    timestamp (int)
  """
  line = util.check_output(
      'git', 'log', '-1', '--format=%ct', rev, '--', path, cwd=git_repo)
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
      'git', 'show', '%s:%s' % (rev, path), cwd=git_repo, log_stdout=False)


def list_dir_from_revision(git_repo, rev, path):
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
      log_stdout=False).splitlines()


def get_rev_by_time(git_repo, timestamp, branch, path=None):
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

  cmd = [
      'git',
      'rev-list',
      '--first-parent',
      '-1',
      '--before',
      str(timestamp),
      branch,
  ]
  if path:
    cmd += ['--', path]

  result = util.check_output(*cmd, cwd=git_repo).strip()
  return result or None


def get_history(git_repo,
                path,
                branch=None,
                after=None,
                before=None,
                padding=False):
  """Get commit history of given path.

  `after` and `before` could be outside of lifetime of `path`. `padding` is
  used to control what to return for such cases.

  Args:
    git_repo: path of git repo.
    path: path to query, relative to git_repo
    branch: branch name or ref name
    after: limit history after given time (inclusive)
    before: limit history before given time (inclusive)
    padding: If True, pads returned result with dummy record at exact 'after'
        and 'before' time, if 'path' existed at that time. Otherwise, only
        returns real commits.

  Returns:
    List of (timestamp, git hash); They are all events when `path` was added,
    removed, modified, and start and end time if `padding` is true.

    For each pair, at `timestamp`, the repo state is `git hash`. In other
    words, `timestamp` is not necessary the commit time of `git hash` for the
    padded entries.
  """
  cmd = ['git', 'log', '--reverse', '--first-parent', '--format=%ct %H']
  if after:
    cmd += ['--after', str(after)]
  if before:
    cmd += ['--before', str(before)]
  if branch:
    assert not is_git_rev(branch)
    cmd += [branch]
  # '--' is necessary otherwise if `path` is removed in current revision, git
  # will complain it's an ambiguous argument which may be path or something
  # else (like git branch name, tag name, etc.)
  cmd += ['--', path]

  result = []
  for line in util.check_output(*cmd, cwd=git_repo).splitlines():
    commit_time, git_rev = line.split()
    result.append((int(commit_time), git_rev))

  if padding:
    assert before or after, "padding=True make no sense if they are both None"
    if before is not None and get_rev_by_time(
        git_repo, before, branch, path=path):
      before = int(before)
      if not result or result[-1][0] != before:
        git_rev = get_rev_by_time(git_repo, before, branch)
        assert git_rev
        result.append((before, git_rev))
    if after is not None and get_rev_by_time(
        git_repo, after, branch, path=path):
      after = int(after)
      if not result or result[0][0] != after:
        git_rev = get_rev_by_time(git_repo, after, branch)
        assert git_rev
        result.insert(0, (after, git_rev))

  return result


def get_history_recursively(git_repo, path, after, before, parser_callback):
  """Get commit history of given path and its dependencies.

  In comparison to get_history(), get_history_recursively also takes
  dependencies into consideration. For example, if file A referenced file B,
  get_history_recursively(A) will return commits of B in addition to A. This
  applies recursively, so commits of C will be included if file B referenced
  file C, and so on.

  This function is file type neutral. `parser_callback(filename, content)` will
  be invoked to parse file content and should return list of filename of
  dependencies.

  Args:
    git_repo: path of git repo
    path: path to query, relative to git_repo
    after: limit history after given time (inclusive)
    before: limit history before given time (inclusive)
    parser_callback: callback to parse file content. See above comment.

  Returns:
    list of (commit timestamp, git hash)
  """
  history = get_history(
      git_repo, path, after=after, before=before, padding=True)

  # Collect include information of each commit.
  includes = {}
  for commit_time, git_rev in history:
    content = get_file_from_revision(git_repo, git_rev, path)
    for include_name in parser_callback(path, content):
      if include_name not in includes:
        includes[include_name] = set()
      includes[include_name].add(git_rev)

  # Analyze the start time and end time of each include.
  dependencies = []
  for include in includes:
    appeared = None
    for commit_time, git_rev in history:
      if git_rev in includes[include]:
        if not appeared:
          appeared = commit_time
      else:
        if appeared:
          dependencies.append((include, appeared, commit_time))
          appeared = None

    if appeared is not None:
      dependencies.append((include, appeared, before))

  # Recursion and merge.
  result = list(history)
  for include, appeared, disappeared in dependencies:
    result += get_history_recursively(git_repo, include, appeared, disappeared,
                                      parser_callback)

  # Sort and dedup.
  result2 = []
  for x in sorted(result, key=lambda x: x[0]):
    if result2 and result2[-1] == x:
      continue
    result2.append(x)

  return result2


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
