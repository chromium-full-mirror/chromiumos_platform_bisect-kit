# Copyright 2018 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Repo utility.

This module provides wrapper for "repo" (a Google-built repository management
tool that runs on top of git) and related utility functions.
"""

from __future__ import print_function
import logging
import os
import re
import xml.etree.ElementTree

from bisect_kit import git_util
from bisect_kit import util

logger = logging.getLogger(__name__)


def init(repo_dir,
         manifest_url,
         manifest_branch=None,
         manifest_name=None,
         repo_url=None):
  """Repo init.

  Args:
    repo_dir: root directory of repo
    manifest_url: manifest repository location
    manifest_branch: manifest branch or revision
    manifest_name: initial manifest file name
    repo_url: repo repository location
  """
  cmd = ['repo', 'init', '--manifest-url', manifest_url]
  if manifest_name:
    cmd += ['--manifest-name', manifest_name]
  if manifest_branch:
    cmd += ['--manifest-branch', manifest_branch]
  if repo_url:
    cmd += ['--repo-url', repo_url]
  util.check_call(*cmd, cwd=repo_dir)


def sync(repo_dir, jobs=16, manifest_name=None, current_branch=False):
  """Repo sync.

  Args:
    repo_dir: root directory of repo
    jobs: projects to fetch simultaneously
    manifest_name: filename of manifest
    current_branch: fetch only current branch
  """
  cmd = ['repo', 'sync', '-q', '--force-sync']
  if jobs:
    cmd += ['-j', str(jobs)]
  if manifest_name:
    cmd += ['--manifest-name', manifest_name]
  if current_branch:
    cmd += ['--current-branch']
  util.check_call(*cmd, cwd=repo_dir)


def abandon(repo_dir, branch_name):
  """Repo abandon.

  Args:
    repo_dir: root directory of repo
    branch_name: branch name to abandon
  """
  # Ignore errors if failed, which means the branch didn't exist beforehand.
  util.call('repo', 'abandon', branch_name, cwd=repo_dir)


def info(repo_dir, query):
  """Repo info.

  Args:
    repo_dir: root directory of repo
    query: key to query
  """
  for line in util.check_output('repo', 'info', '.', cwd=repo_dir).splitlines():
    key, value = map(str.strip, line.split(':'))
    if key == query:
      return value
  assert 0


def get_current_branch(repo_dir):
  """Get manifest branch of existing repo directory."""
  return info(repo_dir, 'Manifest branch')


def get_manifest_groups(repo_dir):
  """Get manifest group of existing repo directory."""
  return info(repo_dir, 'Manifest groups')


class ManifestManager(object):
  """Abstract class for manifest operations."""

  def sync_disk_state(self, rev):
    """Switches disk state to given version.

    Args:
      rev: revision
    """
    raise NotImplementedError

  def enumerate_manifest(self, old, new):
    """Enumerates major versions with manifest.

    Args:
      old: start version (inclusive)
      new: last version (inclusive)

    Returns:
      list of major versions
    """
    raise NotImplementedError

  def fetch_manifest(self, rev):
    raise NotImplementedError


def parse_repo_set(repo_dir, manifest_name):
  """Parse repo manifest.

  This will ignore 'notdefault' groups in the manifest.

  Args:
    repo_dir: root directory of repo
    manifest_name: name of manifest, which also means a filename relative to
        manifest directory

  Returns:
    (dict) parsed manifest, which is mapping from path to revisions
  """
  manifest_path = os.path.join(repo_dir, '.repo', 'manifests', manifest_name)
  root = xml.etree.ElementTree.parse(manifest_path)
  result = {}
  for project in root.findall('.//project'):
    if 'notdefault' in project.get('groups', ''):
      continue
    path = project.get('path', project.get('name'))  # default path is its name
    rev = project.get('revision')
    result[path] = rev
  return result


class DependencyManager(object):
  """Dependency manager."""

  def __init__(self, repo_dir, manifest_manager):
    self.repo_dir = repo_dir
    self.manifest_manager = manifest_manager

  @staticmethod
  def _parse_rev(rev):
    m = re.match(r'^([^,]+),([^,]+)\+(\d+)$', rev)
    assert m
    return m.group(1), m.group(2), int(m.group(3))

  def get_revlist(self, old, new):
    logger.info('get_revlist')
    revlist = []
    major_revs = self.manifest_manager.enumerate_manifest(old, new)
    for i, major_rev in enumerate(major_revs):
      revlist.append(major_rev)
      if i + 1 == len(major_revs):
        continue

      old_mf = self.manifest_manager.fetch_manifest(major_revs[i])
      new_mf = self.manifest_manager.fetch_manifest(major_revs[i + 1])
      old_set = parse_repo_set(self.repo_dir, old_mf)
      new_set = parse_repo_set(self.repo_dir, new_mf)

      self.manifest_manager.fetch_git_repos(major_revs[i + 1])
      difflist = git_util.get_difflist_between_two_set(self.repo_dir, old_set,
                                                       new_set)
      # Theoretically, "old,new+N" equals to "new" if N=len(old diff). But it's
      # not always true in practice because of racing and incorrect diff
      # replay. So still add it.
      for j, _diff in enumerate(difflist, 1):
        rev = '%s,%s+%d' % (major_revs[i], major_revs[i + 1], j)
        revlist.append(rev)
    return revlist

  def switch(self, rev):
    if '+' in rev:
      rev_old, rev_new, idx = self._parse_rev(rev)

      old_mf = self.manifest_manager.fetch_manifest(rev_old)
      new_mf = self.manifest_manager.fetch_manifest(rev_new)
      old_set = parse_repo_set(self.repo_dir, old_mf)
      new_set = parse_repo_set(self.repo_dir, new_mf)
      difflist = git_util.get_difflist_between_two_set(self.repo_dir, old_set,
                                                       new_set)
      assert 1 <= idx <= len(difflist)
      difflist = difflist[:idx]
    else:
      rev_old = rev
      old_mf = self.manifest_manager.fetch_manifest(rev_old)
      difflist = []

    self.manifest_manager.sync_disk_state(rev_old)

    for i, diff in enumerate(difflist, 1):
      logger.debug('[%d] applying "%s"', i, diff.summary(self.repo_dir))
      diff.apply(self.repo_dir)
    # TODO support <copyfile> and <linkfile> tags

  def view_rev_diff(self, old, new):
    if '+' in old:
      old_base, old_next, old_idx = self._parse_rev(old)
    else:
      old_base, old_next, old_idx = old, None, 0

    if '+' in new:
      new_base, new_next, new_idx = self._parse_rev(new)
    else:
      new_base, new_next, new_idx = new, None, 0

    major_revs = self.manifest_manager.enumerate_manifest(old_base, new_base)
    if major_revs.index(old_base) + 1 == major_revs.index(new_base):
      old_next = new_base
      if new_idx == 0:
        new_base = old_base
        new_idx = -1
    elif major_revs.index(old_base) == major_revs.index(new_base):
      old_next = new_next

    if old_base == new_base and old_next:
      old_mf = self.manifest_manager.fetch_manifest(old_base)
      new_mf = self.manifest_manager.fetch_manifest(old_next)
      old_set = parse_repo_set(self.repo_dir, old_mf)
      new_set = parse_repo_set(self.repo_dir, new_mf)
      difflist = git_util.get_difflist_between_two_set(self.repo_dir, old_set,
                                                       new_set)
      if new_idx == -1:
        new_idx = len(difflist)

      for i in range(old_idx, new_idx + 1):
        if i == 0:
          print('{base}'.format(base=old_base))
          continue
        diff = difflist[i - 1]
        print('{base},{next}+{idx} {timestamp} {summary}'.format(
            base=old_base,
            next=old_next,
            idx=i,
            timestamp=diff.timestamp,
            summary=diff.summary(self.repo_dir)))
