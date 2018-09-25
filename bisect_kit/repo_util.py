# -*- coding: utf-8 -*-
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
import urlparse
import xml.etree.ElementTree

from bisect_kit import codechange
from bisect_kit import git_util
from bisect_kit import util

logger = logging.getLogger(__name__)


def get_manifest_url(manifest_dir):
  """Get manifest URL of repo project.

  Args:
    manifest_dir: path of manifest directory

  Returns:
    manifest URL.
  """
  url = util.check_output(
      'git', 'config', 'remote.origin.url', cwd=manifest_dir)
  url = re.sub(r'^persistent-(https?://)', r'\1', url)
  return url


def find_repo_root(path):
  """Find the root path of a repo project

  Args:
    path: path

  Returns:
    project root if path is inside a repo project; otherwise None
  """
  path = os.path.abspath(path)
  while not os.path.exists(os.path.join(path, '.repo')):
    if path == '/':
      return None
    path = os.path.dirname(path)
  return path


def init(repo_dir,
         manifest_url,
         manifest_branch=None,
         manifest_name=None,
         repo_url=None,
         reference=None,
         mirror=False):
  """Repo init.

  Args:
    repo_dir: root directory of repo
    manifest_url: manifest repository location
    manifest_branch: manifest branch or revision
    manifest_name: initial manifest file name
    repo_url: repo repository location
    reference: location of mirror directory
    mirror: indicates repo mirror
  """
  root = find_repo_root(repo_dir)
  if root and root != repo_dir:
    raise Exception(
        '%s should not be inside another repo project at %s' % (repo_dir, root))

  cmd = ['repo', 'init', '--manifest-url', manifest_url]
  if manifest_name:
    cmd += ['--manifest-name', manifest_name]
  if manifest_branch:
    cmd += ['--manifest-branch', manifest_branch]
  if repo_url:
    cmd += ['--repo-url', repo_url]
  if reference:
    cmd += ['--reference', reference]
  if mirror:
    cmd.append('--mirror')
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

  return None


def get_current_branch(repo_dir):
  """Get manifest branch of existing repo directory."""
  return info(repo_dir, 'Manifest branch')


def get_manifest_groups(repo_dir):
  """Get manifest group of existing repo directory."""
  return info(repo_dir, 'Manifest groups')


class ManifestParser(object):
  """Enumerates historical manifest files and parses them."""

  def __init__(self, manifest_dir):
    self.manifest_dir = manifest_dir
    self.manifest_url = get_manifest_url(self.manifest_dir)

  def parse_single_xml(self, content, allow_include=False):
    root = xml.etree.ElementTree.fromstring(content)
    if not allow_include and root.find('include') is not None:
      raise ValueError(
          'Expects self-contained manifest. <include> is not allowed')
    return root

  def parse_xml_recursive(self, git_rev, path):
    content = git_util.get_file_from_revision(self.manifest_dir, git_rev, path)
    root = self.parse_single_xml(content, allow_include=True)

    result = xml.etree.ElementTree.Element('manifest')
    for node in root:
      if node.tag == 'include':
        for subnode in self.parse_xml_recursive(git_rev, node.get('name')):
          result.append(subnode)
      else:
        result.append(node)
    return result

  def process_parsed_result(self, root):
    result = {}
    default = root.find('default')
    if default is None:
      default = {}

    remote_fetch_map = {}
    for remote in root.findall('.//remote'):
      name = remote.get('name')
      fetch_url = urlparse.urljoin(self.manifest_url, remote.get('fetch'))
      if urlparse.urlparse(fetch_url).path not in ('', '/'):
        # TODO(kcwu): support remote url with sub folders
        raise ValueError(
            'only support git repo at root path of remote server: %s' %
            fetch_url)
      remote_fetch_map[name] = fetch_url

    assert root.find('include') is None

    for project in root.findall('.//project'):
      if 'notdefault' in project.get('groups', ''):
        continue
      for subproject in project.findall('.//project'):
        logger.warning('nested project %s.%s is not supported and ignored',
                       project.get('name'), subproject.get('name'))

      # default path is its name
      path = project.get('path', project.get('name'))
      revision = project.get('revision', default.get('revision'))

      remote_name = project.get('remote', default.get('remote'))
      if remote_name not in remote_fetch_map:
        raise ValueError('unknown remote name=%s' % remote_name)
      fetch_url = remote_fetch_map.get(remote_name)
      repo_url = urlparse.urljoin(fetch_url, project.get('name'))

      result[path] = codechange.PathSpec(path, repo_url, revision)
    return result

  def enumerate_manifest_commits(self, start_time, end_time, path):

    def parse_dependencies(path, content):
      del path  # unused
      root = self.parse_single_xml(content, allow_include=True)
      for include in root.findall('.//include'):
        yield include.get('name')

    return git_util.get_history_recursively(self.manifest_dir, path, start_time,
                                            end_time, parse_dependencies)


class RepoMirror(codechange.CodeStorage):
  """Repo git mirror."""

  def __init__(self, mirror_dir):
    self.mirror_dir = mirror_dir

  def _url_to_cache_dir(self, url):
    # Here we assume remote fetch url is always at root of server url, so we can
    # simply treat whole path as repo project name.
    path = urlparse.urlparse(url).path
    assert path[0] == '/'
    return '%s.git' % path[1:]

  def cached_git_root(self, repo_url):
    cache_path = self._url_to_cache_dir(repo_url)
    return os.path.join(self.mirror_dir, cache_path)

  def _load_project_list(self, project_root):
    repo_project_list = os.path.join(project_root, '.repo', 'project.list')
    return open(repo_project_list).readlines()

  def _save_project_list(self, project_root, lines):
    repo_project_list = os.path.join(project_root, '.repo', 'project.list')
    with open(repo_project_list, 'w') as f:
      f.write(''.join(sorted(lines)))

  def add_to_project_list(self, project_root, path, repo_url):
    lines = self._load_project_list(project_root)

    line = path + '\n'
    if line not in lines:
      lines.append(line)

    self._save_project_list(project_root, lines)

  def remove_from_project_list(self, project_root, path):
    lines = self._load_project_list(project_root)

    line = path + '\n'
    if line in lines:
      lines.remove(line)

    self._save_project_list(project_root, lines)
