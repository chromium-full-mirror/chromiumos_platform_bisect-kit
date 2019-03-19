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
import shutil
import sys
import tempfile
import urlparse

from bisect_kit import codechange
from bisect_kit import git_util
from bisect_kit import locking
from bisect_kit import util

logger = logging.getLogger(__name__)
emitted_warnings = set()


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
  # Work around gclient issue crbug/943430
  # gclient rejected to sync if there are untracked symlink even with --force
  for path in [
      'src/chromeos/assistant/libassistant/src/deps',
      'src/chromeos/assistant/libassistant/src/libassistant',
  ]:
    if os.path.islink(os.path.join(gclient_dir, path)):
      os.unlink(os.path.join(gclient_dir, path))

  cmd = [
      'gclient',
      'sync',
      '--jobs=%d' % jobs,
      '--delete_unversioned_trees',
      # --force is necessary because runhook may generate some untracked files.
      '--force',
  ]
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

  try:
    old_projects = load_gclient_entries(gclient_dir)
  except IOError:
    old_projects = {}

  util.check_call(*cmd, cwd=gclient_dir)

  # Remove dead .git folder after sync.
  # Ideally, this should be handled by gclient but sometimes gclient didn't
  # (crbug/930047).
  new_projects = load_gclient_entries(gclient_dir)
  for path in old_projects:
    if path in new_projects:
      continue
    old_git_dir = os.path.join(gclient_dir, path)
    if os.path.exists(old_git_dir):
      logger.warning(
          '%s was removed from .gclient_entries but %s still exists; remove it',
          path, old_git_dir)
      shutil.rmtree(old_git_dir)


def load_gclient_entries(gclient_dir):
  """Loads .gclient_entries."""
  repo_project_list = os.path.join(gclient_dir, '.gclient_entries')
  scope = {}
  exec open(repo_project_list) in scope  # pylint: disable=exec-used
  entries = scope.get('entries', {})

  # normalize path: remove trailing slash
  entries = dict((os.path.normpath(path), url) for path, url in entries.items())

  return entries


def write_gclient_entries(gclient_dir, projects):
  """Writes .gclient_entries."""
  repo_project_list = os.path.join(gclient_dir, '.gclient_entries')
  content = 'entries = {\n'
  for item in sorted(projects.items()):
    path, repo_url = map(pprint.pformat, item)
    content += '  %s: %s,\n' % (path, repo_url)
  content += '}\n'
  with open(repo_project_list, 'w') as f:
    f.write(content)


def mirror(code_storage, repo_url):
  """Mirror git repo.

  This function mimics the caching behavior of 'gclient sync' with 'cache_dir'.

  Args:
    code_storage: CodeStorage object
    repo_url: remote repo url
  """
  logger.info('mirror %s', repo_url)
  tmp_dir = tempfile.mkdtemp(dir=code_storage.cache_dir)
  git_root = code_storage.cached_git_root(repo_url)
  assert not os.path.exists(git_root)

  util.check_call('git', 'init', '--bare', cwd=tmp_dir)

  # These config parameters are copied from gclient.
  git_util.config(tmp_dir, 'gc.autodetach', '0')
  git_util.config(tmp_dir, 'gc.autopacklimit', '0')
  git_util.config(tmp_dir, 'core.deltaBaseCacheLimit', '2g')
  git_util.config(tmp_dir, 'remote.origin.url', repo_url)
  git_util.config(tmp_dir, '--replace-all', 'remote.origin.fetch',
                  '+refs/heads/*:refs/heads/*', r'\+refs/heads/\*:.*')
  git_util.config(tmp_dir, '--replace-all', 'remote.origin.fetch',
                  '+refs/tags/*:refs/tags/*', r'\+refs/tags/\*:.*')
  git_util.config(tmp_dir, '--replace-all', 'remote.origin.fetch',
                  '+refs/branch-heads/*:refs/branch-heads/*',
                  r'\+refs/branch-heads/\*:.*')

  git_util.fetch(tmp_dir, 'origin', '+refs/heads/*:refs/heads/*')
  git_util.fetch(tmp_dir, 'origin', '+refs/tags/*:refs/tags/*')
  git_util.fetch(tmp_dir, 'origin', '+refs/branch-heads/*:refs/branch-heads/*')

  # Rename to correct name atomically.
  os.rename(tmp_dir, git_root)


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

    # Currently, we only support chromeos as target_os.
    # TODO(kcwu): make it configurable if we need to bisect for other os.
    # We don't specify `target_os_only`, so `unix` will be considered by
    # gclient as well.
    target_os = ['chromeos', 'unix']

    vars_dict = {
        'checkout_android': 'android' in target_os,
        'checkout_chromeos': 'chromeos' in target_os,
        'checkout_fuchsia': 'fuchsia' in target_os,
        'checkout_ios': 'ios' in target_os,
        'checkout_linux': 'unix' in target_os,
        'checkout_mac': 'mac' in target_os,
        'checkout_win': 'win' in target_os,
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

  def __init__(self, parent_deps, entry, start_time, end_time):
    """TimeSeriesTree constructor.

    Args:
      parent_deps: parent DEPS of the given period. None if this is tree root.
      entry: project entry
      start_time: start time
      end_time: end time
    """
    self.parent_deps = parent_deps
    self.entry = entry
    self.snapshots = {}
    self.start_time = start_time
    self.end_time = end_time

    # Intermediate dict to keep track alive children for the time being.
    # Maintained by add_snapshot() and no_more_snapshot().
    self.alive_children = {}

    # All historical children (TimeSeriesTree object) between start_time and
    # end_time. It's possible that children with the same entry appear more than
    # once in this list because they are removed and added back to the DEPS
    # file.
    self.subtrees = []

  def subtree_eq(self, deps_a, deps_b, child_entry):
    """Compares subtree of two Deps.

    Args:
      deps_a: Deps object
      deps_b: Deps object
      child_entry: the subtree to compare

    Returns:
      True if the said subtree of these two Deps equal
    """
    # Need to compare variables because they may influence subtree parsing
    # behavior
    path = child_entry[0]
    return (deps_a.entries[path] == deps_b.entries[path] and
            deps_a.variables == deps_b.variables)

  def add_snapshot(self, timestamp, deps, children_entries):
    """Adds parsed DEPS result and children.

    For example, if a given DEPS file has N revisions between start_time and
    end_time, the caller should call this method N times to feed all parsed
    results in order (timestamp increasing).

    Args:
      timestamp: timestamp of `deps`
      deps: Deps object
      children_entries: list of names of deps' children
    """
    assert timestamp not in self.snapshots
    self.snapshots[timestamp] = deps

    for child_entry in set(self.alive_children.keys() + children_entries):
      # `child_entry` is added at `timestamp`
      if child_entry not in self.alive_children:
        self.alive_children[child_entry] = timestamp, deps

      # `child_entry` is removed at `timestamp`
      elif child_entry not in children_entries:
        self.subtrees.append(
            TimeSeriesTree(self.alive_children[child_entry][1], child_entry,
                           self.alive_children[child_entry][0], timestamp))
        del self.alive_children[child_entry]

      # `child_entry` is alive before and after `timestamp`
      else:
        last_deps = self.alive_children[child_entry][1]
        if not self.subtree_eq(last_deps, deps, child_entry):
          self.subtrees.append(
              TimeSeriesTree(last_deps, child_entry,
                             self.alive_children[child_entry][0], timestamp))
          self.alive_children[child_entry] = timestamp, deps

  def no_more_snapshot(self, deps):
    """Indicates all snapshots are added.

    add_snapshot() should not be invoked after no_more_snapshot().
    """
    for child_entry, (timestamp, deps) in self.alive_children.items():
      if timestamp == self.end_time:
        continue
      self.subtrees.append(
          TimeSeriesTree(deps, child_entry, timestamp, self.end_time))
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
      result.append((timestamp, self.entry, deps, False))
      last_deps = deps

    assert last_deps
    result.append((self.end_time, self.entry, last_deps, True))

    for subtree in self.subtrees:
      for event in subtree.events():
        result.append(event)

    result.sort(key=lambda x: x[0])

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

      for timestamp, entry, deps, end in events:
        forest[entry] = deps
        if end:
          end_counter[entry] += 1
        else:
          end_counter[entry] -= 1

      # Merge Deps at time `timestamp` into single path_specs.
      path_specs = {}
      for deps in forest.values():
        for path, dep in deps.entries.items():
          path_specs[path] = dep.as_path_spec()

      yield timestamp, path_specs

      # Remove deps which are removed at this timestamp.
      for entry, count in end_counter.items():
        assert -1 <= count <= 1, (timestamp, entry)
        if count == 1:
          del forest[entry]


class DepsParser(object):
  """Gclient DEPS file parser."""

  def __init__(self, project_root, code_storage):
    self.project_root = project_root
    self.code_storage = code_storage

  def load_single_deps(self, content):

    def var_function(name):
      return '{%s}' % name

    global_scope = dict(Var=var_function)
    local_scope = {}
    try:
      exec (content, global_scope, local_scope)  # pylint: disable=exec-used
    except SyntaxError:
      raise

    return local_scope

  def parse_single_deps(self, content, parent_vars=None, parent_path=''):
    """Parses DEPS file without recursion.

    Args:
      content: file content of DEPS file
      parent_vars: variables inherent from parent DEPS
      parent_path: project path of parent DEPS file

    Returns:
      Deps object
    """

    local_scope = self.load_single_deps(content)
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
      path = path.format(**deps.variables)
      if local_scope.get('use_relative_paths', False):
        path = os.path.join(parent_path, path)
      path = os.path.normpath(path)
      dep = Dep(path, deps.variables, dep_entry)
      if not dep.eval_condition():
        continue

      # TODO(kcwu): support dep_type=cipd http://crbug.com/846564
      if dep.dep_type != 'git':
        warning_key = ('dep_type', dep.dep_type, path)
        if warning_key not in emitted_warnings:
          emitted_warnings.add(warning_key)
          logger.warning('dep_type=%s is not supported yet: %s', dep.dep_type,
                         path)
        continue

      deps.entries[path] = dep

    recursedeps = []
    for recurse_entry in local_scope.get('recursedeps', []):
      # Normalize entries.
      if isinstance(recurse_entry, tuple):
        path, deps_file = recurse_entry
      else:
        assert isinstance(path, str)
        path, deps_file = recurse_entry, 'DEPS'

      if local_scope.get('use_relative_paths', False):
        path = os.path.join(parent_path, path)
      path = path.format(**deps.variables)
      if path in deps.entries:
        recursedeps.append((path, deps_file))
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
      if not os.path.exists(git_repo):
        with locking.lock_file(
            os.path.join(self.code_storage.cache_dir,
                         locking.LOCK_FILE_FOR_MIRROR_SYNC)):
          mirror(self.code_storage, repo_url)
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
      path, deps_file = subtree.entry
      path_spec = subtree.parent_deps.entries[path].as_path_spec()
      self.construct_deps_tree(
          subtree,
          path_spec.repo_url,
          path_spec.at,
          subtree.start_time,
          subtree.end_time,
          parent_vars=subtree.parent_deps.variables,
          parent_path=path,
          deps_file=deps_file)

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

  def add_to_project_list(self, project_root, path, repo_url):
    projects = load_gclient_entries(project_root)

    projects[path] = repo_url

    write_gclient_entries(project_root, projects)

  def remove_from_project_list(self, project_root, path):
    projects = load_gclient_entries(project_root)

    if path in projects:
      del projects[path]

    write_gclient_entries(project_root, projects)
