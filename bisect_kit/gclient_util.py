# -*- coding: utf-8 -*-
# Copyright 2018 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Gclient utility."""

from __future__ import print_function
import collections
import itertools
import logging
import operator
import os
import pprint
import sys
import urlparse

from bisect_kit import codechange
from bisect_kit import git_util
from bisect_kit import util

logger = logging.getLogger(__name__)


def config(gclient_dir,
           url=None,
           cache_dir=None,
           deps_file=None,
           custom_var=None,
           spec=None):
  """Simply wrapper of `gclient config`.

  Args:
    gclient_dir: root directory of gclient project
    url: URL of gclient configuration files
    cache_dir: gclient's git cache folder
    deps_file: override the default DEPS file name
    custom_var: custom variables
    spec: content of gclient file
  """
  cmd = ['gclient', 'config']
  if deps_file:
    cmd += ['--deps-file', deps_file]
  if cache_dir:
    cmd += ['--cache-dir', cache_dir]
  if custom_var:
    cmd += ['--custom-var', custom_var]
  if spec:
    cmd += ['--spec', spec]
  if url:
    cmd.append(url)

  util.check_call(*cmd, cwd=gclient_dir)


def sync(gclient_dir,
         with_branch_heads=False,
         with_tags=False,
         ignore_locks=False,
         jobs=8):
  """Simply wrapper of `gclient sync`.

  Args:
    gclient_dir: root directory of gclient project
    with_branch_heads: whether to clone git `branch_heads` refspecs
    with_tags: whether to clone git tags
    ignore_locks: bypass gclient's lock
    jobs: how many workers running in parallel
  """
  cmd = ['gclient', 'sync', '--jobs', str(jobs), '--delete_unversioned_trees']
  if with_branch_heads:
    cmd.append('--with_branch_heads')
  if with_tags:
    cmd.append('--with_tags')

  # If 'gclient sync' is interrupted by ctrl-c or terminated with whatever
  # reasons, it will leave annoying lock files on disk and thus unfriendly to
  # bot tasks. In bisect-kit, we will use our own lock mechanism (in caller of
  # this function) and bypass gclient's.
  if ignore_locks:
    cmd.append('--ignore_locks')

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


class Dep(object):
  """Represent one entry of DEPS's deps.

  One Dep object means one subproject inside DEPS file. It recorded what to
  checkout (like git or cipd) content of each subproject.

  Attributes:
    path: subproject path, relative to project root
    variables: the variables of the containing DEPS file; these variables will
        be applied to fields of this object (like 'url' and 'condition') and
        children projects.
    condition: whether to checkout this subproject
    dep_type: 'git' or 'cipd'
    url: if dep_type='git', the url of remote repo and associated branch/commit
    packages: if dep_type='cipd', cipd package version and location
  """

  def __init__(self, path, variables, entry):
    self.path = path
    self.variables = variables

    self.url = None  # only valid for dep_type='git'
    self.packages = None  # only valid for dep_type='cipd'

    if isinstance(entry, str):
      self.dep_type = 'git'
      self.url = entry
      self.condition = None
    else:
      self.dep_type = entry.get('dep_type', 'git')
      self.condition = entry.get('condition')
      if self.dep_type == 'git':
        self.url = entry['url']
      else:
        assert self.dep_type == 'cipd'
        self.packages = entry['packages']

    if self.dep_type == 'git':
      self.url = self.url.format(**self.variables)

  def __eq__(self, rhs):
    return vars(self) == vars(rhs)

  def __ne__(self, rhs):
    return not self.__eq__(rhs)

  def as_path_spec(self):
    assert self.dep_type == 'git'

    if '@' in self.url:
      repo_url, at = self.url.split('@')
    else:
      # If the dependency is not pinned, the default is master branch.
      repo_url, at = self.url, 'master'
    return codechange.PathSpec(self.path, repo_url, at)

  def eval_condition(self):
    """Evaluate condition for DEPS parsing.

    Returns:
      eval result
    """
    if not self.condition:
      return True

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
    vars_dict.update(self.variables)
    # pylint: disable=eval-used
    return eval(self.condition, vars_dict)


class Deps(object):
  """DEPS parsed result.

  Attributes:
    variables: 'vars' dict in DEPS file; these variables will be applied
        recursively to children.
    entries: dict of Dep objects
    recursedeps: list of recursive projects
  """

  def __init__(self):
    self.variables = {}
    self.entries = {}
    self.recursedeps = []


class TimeSeriesTree(object):
  """Data structure for generating snapshots of historical dependency tree.

  This is a tree structure with time information. Each tree node represents not
  only typical tree data and tree children information, but also historical
  value of those tree data and tree children.

  To be more specific in terms of DEPS parsing, one TimeSeriesTree object
  represent a DEPS file. The caller will add_snapshot() to add parsed result of
  historical DEPS instances. After that, the tree root of this class can
  reconstruct the every historical moment of the project dependency state.

  This class is slight abstraction of git_util.get_history_recursively() to
  support more than single git repo and be version control system independent.
  """

  # TODO(kcwu): refactor git_util.get_history_recursively() to reuse this class.

  def __init__(self, parent_deps, name, start_time, end_time):
    """TimeSeriesTree constructor.

    Args:
      parent_deps: parent DEPS of the given period. None if this is tree root.
      name: project name
      start_time: start time
      end_time: end time
    """
    self.parent_deps = parent_deps
    self.name = name
    self.snapshots = {}
    self.start_time = start_time
    self.end_time = end_time

    # Intermediate dict to keep track alive children for the time being.
    # Maintained by add_snapshot() and no_more_snapshot().
    self.alive_children = {}

    # All historical children (TimeSeriesTree object) between start_time and
    # end_time. It's possible that children with the same name appear more than
    # once in this list because they are removed and added back to the DEPS
    # file.
    self.subtrees = []

  def subtree_eq(self, deps_a, deps_b, child_name):
    """Compares subtree of two Deps.

    Args:
      deps_a: Deps object
      deps_b: Deps object
      child_name: the subtree to compare

    Returns:
      True if the said subtree of these two Deps equal
    """
    # Need to compare variables because they may influence subtree parsing
    # behavior
    return (deps_a.entries[child_name] == deps_b.entries[child_name] and
            deps_a.variables == deps_b.variables)

  def add_snapshot(self, timestamp, deps, children_names):
    """Adds parsed DEPS result and children.

    For example, if a given DEPS file has N revisions between start_time and
    end_time, the caller should call this method N times to feed all parsed
    results in order (timestamp increasing).

    Args:
      timestamp: timestamp of `deps`
      deps: Deps object
      children_names: list of names of deps' children
    """
    assert timestamp not in self.snapshots
    self.snapshots[timestamp] = deps

    for child_name in set(self.alive_children.keys() + children_names):
      # `child_name` is added at `timestamp`
      if child_name not in self.alive_children:
        self.alive_children[child_name] = timestamp, deps

      # `child_name` is removed at `timestamp`
      elif child_name not in children_names:
        self.subtrees.append(
            TimeSeriesTree(self.alive_children[child_name][1], child_name,
                           self.alive_children[child_name][0], timestamp))
        del self.alive_children[child_name]

      # `child_name` is alive before and after `timestamp`
      else:
        last_deps = self.alive_children[child_name][1]
        if not self.subtree_eq(last_deps, deps, child_name):
          self.subtrees.append(
              TimeSeriesTree(last_deps, child_name,
                             self.alive_children[child_name][0], timestamp))
          self.alive_children[child_name] = timestamp, deps

  def no_more_snapshot(self, deps):
    """Indicates all snapshots are added.

    add_snapshot() should not be invoked after no_more_snapshot().
    """
    for child_name, (timestamp, deps) in self.alive_children.items():
      if timestamp == self.end_time:
        continue
      self.subtrees.append(
          TimeSeriesTree(deps, child_name, timestamp, self.end_time))
    self.alive_children = None

  def events(self):
    """Gets children added/removed events of this subtree.

    Returns:
      list of (timestamp, deps_name, deps, end_flag):
        timestamp: timestamp of event
        deps_name: name of this subtree
        deps: Deps object of given project
        end_flag: True indicates this is the last event of this deps tree
    """
    assert self.snapshots
    assert self.alive_children is None, ('events() is valid only after '
                                         'no_more_snapshot() is invoked')

    result = []

    last_deps = None
    for timestamp, deps in self.snapshots.items():
      result.append((timestamp, self.name, deps, False))
      last_deps = deps

    assert last_deps
    result.append((self.end_time, self.name, last_deps, True))

    for subtree in self.subtrees:
      for event in subtree.events():
        result.append(event)

    result.sort()

    return result

  def iter_path_specs(self):
    """Iterates snapshots of project dependency state.

    Yields:
      (timestamp, path_specs):
        timestamp: time of snapshot
        path_specs: dict of path_spec entries
    """
    forest = {}
    # Group by timestamp
    for timestamp, events in itertools.groupby(self.events(),
                                               operator.itemgetter(0)):
      # It's possible that one deps is removed and added at the same timestamp,
      # i.e. modification, so use counter to track.
      end_counter = collections.Counter()

      for timestamp, name, deps, end in events:
        forest[name] = deps
        if end:
          end_counter[name] += 1
        else:
          end_counter[name] -= 1

      # Merge Deps at time `timestamp` into single path_specs.
      path_specs = {}
      for deps in forest.values():
        for path, dep in deps.entries.items():
          path_specs[path] = dep.as_path_spec()

      yield timestamp, path_specs

      # Remove deps which are removed at this timestamp.
      for name, count in end_counter.items():
        assert -1 <= count <= 1, (timestamp, name)
        if count == 1:
          del forest[name]


class DepsParser(object):
  """Gclient DEPS file parser."""

  def __init__(self, project_root, code_storage):
    self.project_root = project_root
    self.code_storage = code_storage

  def parse_single_deps(self, content, parent_vars=None, parent_path=''):
    """Parses DEPS file without recursion.

    Args:
      content: file content of DEPS file
      parent_vars: variables inherent from parent DEPS
      parent_path: project path of parent DEPS file

    Returns:
      Deps object
    """

    def var_function(name):
      return '{%s}' % name

    global_scope = dict(Var=var_function)
    local_scope = {}
    try:
      exec (content, global_scope, local_scope)  # pylint: disable=exec-used
    except SyntaxError:
      raise

    deps = Deps()
    local_scope.setdefault('vars', {})
    if parent_vars:
      local_scope['vars'].update(parent_vars)
    deps.variables = local_scope['vars']

    # Warnings for old usages which we don't support.
    for name in deps.variables:
      if name.startswith('RECURSEDEPS_') or name.endswith('_DEPS_file'):
        logger.warning('%s is deprecated and not supported recursion syntax',
                       name)
    if 'deps_os' in local_scope:
      logger.warning('deps_os is no longer supported')

    for path, dep_entry in local_scope['deps'].items():
      if local_scope.get('use_relative_paths', False):
        path = os.path.join(parent_path, path)
      path = path.format(**deps.variables)
      dep = Dep(path, deps.variables, dep_entry)
      if not dep.eval_condition():
        continue

      # TODO(kcwu): support dep_type=cipd http://crbug.com/846564
      if dep.dep_type != 'git':
        logger.warning('dep_type=%s is not supported yet: %s', dep.dep_type,
                       path)
        continue

      deps.entries[path] = dep

    recursedeps = []
    for path in local_scope.get('recursedeps', []):
      assert isinstance(path, str)
      if local_scope.get('use_relative_paths', False):
        path = os.path.join(parent_path, path)
      path = path.format(**deps.variables)
      if path in deps.entries:
        recursedeps.append(path)
    deps.recursedeps = recursedeps

    return deps

  def construct_deps_tree(self,
                          tstree,
                          repo_url,
                          at,
                          after,
                          before,
                          parent_vars=None,
                          parent_path='',
                          deps_file='DEPS'):
    """Processes DEPS recursively of given time period.

    This method parses all commits of DEPS between time `after` and `before`,
    segments recursive dependencies into subtrees if they are changed, and
    processes subtrees recursively.

    The parsed results (multiple revisions of DEPS file) are stored in `tstree`.

    Args:
      tstree: TimeSeriesTree object
      repo_url: remote repo url
      at: branch or git commit id
      after: begin of period
      before: end of period
      parent_vars: DEPS variables inherit from parent DEPS (including
          custom_vars)
      parent_path: the path of parent project of current DEPS file
      deps_file: filename of DEPS file, relative to the git repo, repo_rul
    """
    if '://' in repo_url:
      git_repo = self.code_storage.cached_git_root(repo_url)
    else:
      git_repo = repo_url

    if git_util.is_git_rev(at):
      history = [
          (after, at),
          (before, at),
      ]
    else:
      history = git_util.get_history(
          git_repo,
          deps_file,
          branch=at,
          after=after,
          before=before,
          padding=True)
    assert history

    # If not equal, it means the file was deleted but is still referenced by
    # its parent.
    assert history[-1][0] == before

    # TODO(kcwu): optimization: history[-1] is unused
    for timestamp, git_rev in history[:-1]:
      content = git_util.get_file_from_revision(git_repo, git_rev, deps_file)

      deps = self.parse_single_deps(
          content, parent_vars=parent_vars, parent_path=parent_path)
      tstree.add_snapshot(timestamp, deps, deps.recursedeps)

    tstree.no_more_snapshot(deps)

    for subtree in tstree.subtrees:
      path = subtree.name
      path_spec = subtree.parent_deps.entries[path].as_path_spec()
      self.construct_deps_tree(
          subtree,
          path_spec.repo_url,
          path_spec.at,
          subtree.start_time,
          subtree.end_time,
          parent_vars=subtree.parent_deps.variables,
          parent_path=path)

  def enumerate_path_specs(self, start_time, end_time, path):
    tstree = TimeSeriesTree(None, path, start_time, end_time)
    self.construct_deps_tree(tstree, path, 'master', start_time, end_time)
    return tstree.iter_path_specs()


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
      path, repo_url = map(pprint.pformat, item)
      content += '  %s: %s,\n' % (path, repo_url)
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
