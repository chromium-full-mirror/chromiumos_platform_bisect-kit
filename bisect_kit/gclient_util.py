# -*- coding: utf-8 -*-
# Copyright 2018 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Gclient utility."""

from __future__ import print_function
import logging
import os
import pprint
import sys
import urlparse

from bisect_kit import codechange
from bisect_kit import util

logger = logging.getLogger(__name__)


def config(gclient_dir, url, cache_dir=None, deps_file=None):
  """Simply wrapper of `gclient config`.

  Args:
    gclient_dir: root directory of gclient project
    url: URL of gclient configuration files
    cache_dir: gclient's git cache folder
    deps_file: override the default DEPS file name
  """
  cmd = ['gclient', 'config']
  if deps_file:
    cmd += ['--deps-file', deps_file]
  if cache_dir:
    cmd += ['--cache-dir', cache_dir]
  cmd.append(url)

  util.check_call(*cmd, cwd=gclient_dir)


def sync(gclient_dir, with_branch_heads=False, with_tags=False, jobs=8):
  """Simply wrapper of `gclient sync`.

  Args:
    gclient_dir: root directory of gclient project
    with_branch_heads: whether to clone git `branch_heads` refspecs
    with_tags: whether to clone git tags
    jobs: how many workers running in parallel
  """
  cmd = ['gclient', 'sync', '--jobs', str(jobs)]
  if with_branch_heads:
    cmd.append('--with_branch_heads')
  if with_tags:
    cmd.append('--with_tags')
  util.check_call(*cmd, cwd=gclient_dir)


# Copied from depot_tools' gclient.py
_PLATFORM_MAPPING = {
    'cygwin': 'win',
    'darwin': 'mac',
    'linux2': 'linux',
    'win32': 'win',
    'aix6': 'aix',
}


def _detect_host_os():
  return _PLATFORM_MAPPING[sys.platform]


def _eval_condition(condition, dep_vars, custom_vars):
  """Evaluate condition for DEPS parsing.

  Args:
    condition: python expression
    dep_vars: variables defined in the DEPS file
    custom_vars: custom variables

  Returns:
    eval result
  """
  vars_dict = {
      # default os: linux
      'checkout_android': False,
      'checkout_chromeos': False,
      'checkout_fuchsia': False,
      'checkout_ios': False,
      'checkout_linux': True,
      'checkout_mac': False,
      'checkout_win': False,
      # default cpu: x64
      'checkout_arm64': False,
      'checkout_arm': False,
      'checkout_mips': False,
      'checkout_ppc': False,
      'checkout_s390': False,
      'checkout_x64': True,
      'checkout_x86': False,
      'host_os': _detect_host_os(),
      'False': False,
      'None': None,
      'True': True,
  }
  vars_dict.update(dep_vars)
  vars_dict.update(custom_vars)
  # pylint: disable=eval-used
  return eval(condition, vars_dict)


def _normalize_dep(dep):
  if isinstance(dep, str):
    result = {'url': dep}
  else:
    assert isinstance(dep, dict)
    result = dep.copy()
  result.setdefault('dep_type', 'git')
  return result


def parse_deps(content, custom_vars):
  """Parses DEPS file.

  Args:
    content: file content of DEPS file
    custom_vars: custom variables

  Returns:
    path to PathSpec dict
  """

  def var_function(name):
    return '{%s}' % name

  global_scope = dict(Var=var_function)
  local_scope = {}
  try:
    exec (content, global_scope, local_scope)  # pylint: disable=exec-used
  except SyntaxError:
    raise

  local_scope.setdefault('vars', {})
  for name in local_scope['vars']:
    if name.startswith('RECURSEDEPS_') or name.endswith('_DEPS_file'):
      logger.warning('%s is deprecated and not supported recursion syntax',
                     name)
  if 'recursedeps' in local_scope:
    # TODO(kcwu): support recursedeps
    logger.warning('recursedeps is not supported yet')

  # Merge 'deps_os' deps into 'deps' dict.
  for os_name, os_deps in local_scope.get('deps_os', {}).items():
    os_condition = 'checkout_%s' % (os_name if os_name != 'unix' else 'linux')
    if not _eval_condition(os_condition, local_scope['vars'], custom_vars):
      continue

    for path, os_dep in os_deps.items():
      dep = local_scope['deps'].setdefault(path, {'condition': 'False'})
      os_dep = _normalize_dep(os_dep)
      if os_dep['dep_type'] != 'git' or not os_dep.get('url'):
        logger.error('deps_os[%s][%s] should be git dep: %s', os_name, path,
                     os_dep)
        continue
      if 'url' in dep:
        assert dep['url'] == os_dep['url']
      else:
        dep['url'] = os_dep['url']

      new_condition = '(%s) or (%s)' % (dep.get('condition', 'True'),
                                        os_dep.get('condition', 'True'))
      dep['condition'] = new_condition

  result = {}
  for path, dep in local_scope['deps'].items():
    dep = _normalize_dep(dep)
    condition = dep.get('condition')
    if condition and not _eval_condition(condition, local_scope['vars'],
                                         custom_vars):
      continue
    # TODO(kcwu): support dep_type=cipd http://crbug.com/846564
    assert dep['dep_type'] == 'git'
    url = dep['url']

    url = url.format(**local_scope['vars'])
    repo_url, at = url.split('@')
    result[path] = codechange.PathSpec(path, repo_url, at)
  return result


class GclientCache(codechange.CodeStorage):
  """Gclient git cache."""

  def __init__(self, cache_dir):
    self.cache_dir = cache_dir

  def _url_to_cache_dir(self, url):
    # ref: depot_tools' git_cache.Mirror.UrlToCacheDir
    parsed = urlparse.urlparse(url)
    norm_url = parsed.netloc + parsed.path
    if norm_url.endswith('.git'):
      norm_url = norm_url[:-len('.git')]
    return norm_url.replace('-', '--').replace('/', '-').lower()

  def cached_git_root(self, repo_url):
    cache_path = self._url_to_cache_dir(repo_url)
    return os.path.join(self.cache_dir, cache_path)

  def _load_project_list(self, project_root):
    repo_project_list = os.path.join(project_root, '.gclient_entries')
    scope = {}
    exec open(repo_project_list) in scope  # pylint: disable=exec-used
    return scope.get('entries', {})

  def _save_project_list(self, project_root, projects):
    repo_project_list = os.path.join(project_root, '.gclient_entries')
    content = 'entries = {\n'
    for item in sorted(projects.items()):
      content += '  %s: %s,\n' % map(pprint.pformat, item)
    content += '}\n'
    with open(repo_project_list, 'w') as f:
      f.write(content)

  def add_to_project_list(self, project_root, path, repo_url):
    projects = self._load_project_list(project_root)

    projects[path] = repo_url

    self._save_project_list(project_root, projects)

  def remove_from_project_list(self, project_root, path):
    projects = self._load_project_list(project_root)

    if path in projects:
      del projects[path]

    self._save_project_list(project_root, projects)
