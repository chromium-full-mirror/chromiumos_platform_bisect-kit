# Copyright 2017 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Git utility."""

from __future__ import print_function
import logging
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
  """
  try:
    return util.check_output(
        'git', 'cat-file', '-t', rev, cwd=git_repo) == 'commit\n'
  except subprocess.CalledProcessError:
    return False


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
