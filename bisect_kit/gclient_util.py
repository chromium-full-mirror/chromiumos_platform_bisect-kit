# Copyright 2018 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Gclient utility."""

from __future__ import annotations

import ast
import collections
import itertools
import logging
import operator
import os
import pprint
import queue
import shutil
import subprocess
import sys
import tempfile
import time
import typing
import urllib.parse

from bisect_kit import codechange
from bisect_kit import errors
from bisect_kit import git_util
from bisect_kit import locking
from bisect_kit import util

# from third_party
from depot_tools import gclient_eval


logger = logging.getLogger(__name__)
emitted_warnings = set()

# If the dependency is not pinned in DEPS file, the default branch.
# ref: gclient_scm.py GitWrapper.update default_rev
# TODO(kcwu): follow gclient to change the default branch name
DEFAULT_BRANCH_NAME = 'main'


def config(
    gclient_dir: str,
    url: str | None = None,
    cache_dir: str | None = None,
    deps_file: str | None = None,
    custom_var_list: list[str] | None = None,
    gclientfile: str | None = None,
    managed: bool = True,
    spec: str | None = None,
    target_os: str | None = None,
):
    """Simply wrapper of `gclient config`.

    Args:
      gclient_dir: root directory of gclient project
      url: URL of gclient configuration files
      cache_dir: gclient's git cache folder
      deps_file: override the default DEPS file name
      custom_var_list: A list of custom variables
      gclientfile: alternate .gclient file name
      managed: use managed dependencies
      spec: content of gclient file
      target_os: target OS
    """
    cmd = ['gclient', 'config']
    if deps_file:
        cmd += ['--deps-file', deps_file]
    if cache_dir:
        cmd += ['--cache-dir', cache_dir]
    if custom_var_list:
        for custom_var in custom_var_list:
            cmd += ['--custom-var', custom_var]
    if gclientfile:
        cmd += ['--gclientfile', gclientfile]
    if not managed:
        cmd.append('--unmanaged')
    if spec:
        cmd += ['--spec', spec]
    if url:
        cmd.append(url)

    with git_util.PatchGitConfig() as new_env:
        util.check_call(*cmd, cwd=gclient_dir, env=new_env)

    # 'target_os' is mandatory for chromeos build, but 'gclient config' doesn't
    # recognize it. Here add it to gclient file explicitly.
    if target_os:
        if not gclientfile:
            gclientfile = '.gclient'
        with open(os.path.join(gclient_dir, gclientfile), 'a') as f:
            f.write('target_os = ["%s"]\n' % target_os)


def sync(
    gclient_dir: str,
    with_branch_heads: bool = False,
    with_tags: bool = False,
    revision: str | None = None,
    jobs: int = 8,
    max_attempts: int = 3,
    retry_interval: int = 60,
):
    """Simply wrapper of `gclient sync`.

    Args:
      gclient_dir: root directory of gclient project
      with_branch_heads: whether to clone git `branch_heads` refspecs
      with_tags: whether to clone git tags
      revision: the revision name, e.g. src@100.0.4857.0 or 100.0.4857.0 or git hash
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
    if revision:
        cmd += ['--revision', revision]

    try:
        old_projects = load_gclient_entries(gclient_dir)
    except IOError:
        old_projects = {}

    with git_util.PatchGitConfig() as new_env:
        attempts = 0
        while True:
            attempts += 1
            try:
                util.check_call(*cmd, cwd=gclient_dir, env=new_env)
                break
            except subprocess.CalledProcessError as e:
                if attempts >= max_attempts:
                    raise errors.ExternalError(
                        'gclient sync failed: %s' % cmd
                    ) from e
                logger.warning(
                    'gclient sync failed, will retry %d seconds later: %s',
                    retry_interval,
                    e,
                )
                time.sleep(retry_interval)

    # Remove dead .git folder after sync.
    # Ideally, this should be handled by gclient but sometimes gclient didn't
    # (crbug/930047).
    new_projects = load_gclient_entries(gclient_dir)
    for path in old_projects:
        if path in new_projects:
            continue
        old_git_dir = os.path.join(gclient_dir, path)
        if not os.path.exists(old_git_dir):
            continue

        if git_util.is_git_root(old_git_dir):
            logger.warning(
                '%s was removed from .gclient_entries but %s still exists; remove it',
                path,
                old_git_dir,
            )
            shutil.rmtree(old_git_dir)
        else:
            logger.warning(
                '%s was removed from .gclient_entries but %s still exists;'
                ' keep it because it is not git root',
                path,
                old_git_dir,
            )


def runhook(gclient_dir, jobs=8, gclientfile=None):
    """Simply wrapper of `gclient runhook`.

    Args:
      gclient_dir: root directory of gclient project
      jobs: how many workers running in parallel
      gclientfile: specify an alternate .gclient file
    """
    cmd = ['gclient', 'runhook', '--job', str(jobs)]
    if gclientfile:
        cmd += ['--gclientfile', gclientfile]
    with git_util.PatchGitConfig() as new_env:
        util.check_call(*cmd, cwd=gclient_dir, env=new_env)


def load_gclient_entries(gclient_dir: str):
    """Loads .gclient_entries."""
    repo_project_list = os.path.join(gclient_dir, '.gclient_entries')
    scope: dict[str, typing.Any] = {}
    with open(repo_project_list) as f:
        code = compile(f.read(), repo_project_list, 'exec')
        # pylint: disable=exec-used
        exec(code, scope)
    entries = scope.get('entries', {})

    # normalize path: remove trailing slash
    entries = dict(
        (os.path.normpath(path), url) for path, url in entries.items()
    )

    return entries


def write_gclient_entries(gclient_dir, projects):
    """Writes .gclient_entries."""
    repo_project_list = os.path.join(gclient_dir, '.gclient_entries')
    content = 'entries = {\n'
    for path, repo_url in sorted(projects.items()):
        content += '  %s: %s,\n' % (
            pprint.pformat(path),
            pprint.pformat(repo_url),
        )
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

    with git_util.PatchGitConfig() as new_env:
        util.check_call('git', 'init', '--bare', cwd=tmp_dir, env=new_env)

    # These config parameters are copied from gclient.
    git_util.config(tmp_dir, 'gc.autodetach', '0')
    git_util.config(tmp_dir, 'gc.autopacklimit', '0')
    git_util.config(tmp_dir, 'core.deltaBaseCacheLimit', '2g')
    git_util.config(tmp_dir, 'remote.origin.url', repo_url)
    git_util.config(
        tmp_dir,
        '--replace-all',
        'remote.origin.fetch',
        '+refs/heads/*:refs/heads/*',
        r'\+refs/heads/\*:.*',
    )
    git_util.config(
        tmp_dir,
        '--replace-all',
        'remote.origin.fetch',
        '+refs/tags/*:refs/tags/*',
        r'\+refs/tags/\*:.*',
    )
    git_util.config(
        tmp_dir,
        '--replace-all',
        'remote.origin.fetch',
        '+refs/branch-heads/*:refs/branch-heads/*',
        r'\+refs/branch-heads/\*:.*',
    )

    git_util.fetch(tmp_dir, 'origin', '+refs/heads/*:refs/heads/*')
    git_util.fetch(tmp_dir, 'origin', '+refs/tags/*:refs/tags/*')
    git_util.fetch(
        tmp_dir, 'origin', '+refs/branch-heads/*:refs/branch-heads/*'
    )

    # Rename to correct name atomically.
    os.rename(tmp_dir, git_root)


# Copied from depot_tools' gclient.py
_PLATFORM_MAPPING = {
    'cygwin': 'win',
    'darwin': 'mac',
    'linux2': 'linux',
    'linux': 'linux',
    'win32': 'win',
    'aix6': 'aix',
    'zos': 'zos',
}


def _detect_host_os():
    return _PLATFORM_MAPPING[sys.platform]


def evaluate_condition(condition, variables, referenced_variables=None):
    """Evaluate condition in gclient DEPS.

    This function is copied from gclient_eval.py EvaluateCondition().

    Returns:
      evaluate result
    """
    if not referenced_variables:
        referenced_variables = set()
    _allowed_names = {'None': None, 'True': True, 'False': False}
    main_node = ast.parse(condition, mode='eval')
    if isinstance(main_node, ast.Expression):
        main_node = main_node.body

    def _convert(node, allow_tuple=False):
        if isinstance(node, ast.Str):
            return node.s
        if isinstance(node, ast.Tuple) and allow_tuple:
            return tuple(map(_convert, node.elts))
        if isinstance(node, ast.Name):
            if node.id in referenced_variables:
                raise ValueError(
                    'invalid cyclic reference to %r (inside %r)'
                    % (node.id, condition)
                )
            if node.id in _allowed_names:
                return _allowed_names[node.id]
            if node.id in variables:
                value = variables[node.id]

                # Allow using "native" types, without wrapping everything in strings.
                # Note that schema constraints still apply to variables.
                if not isinstance(value, str):
                    return value

                # Recursively evaluate the variable reference.
                return evaluate_condition(
                    variables[node.id],
                    variables,
                    referenced_variables.union([node.id]),
                )
            # Implicitly convert unrecognized names to strings.
            # If we want to change this, we'll need to explicitly distinguish
            # between arguments for GN to be passed verbatim, and ones to
            # be evaluated.
            return node.id
        if not sys.version_info[:2] < (3, 4) and isinstance(
            node, ast.NameConstant
        ):  # Since Python 3.4
            return node.value
        if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
            bool_values = []
            for value in node.values:
                bool_values.append(_convert(value))
                if not isinstance(bool_values[-1], bool):
                    raise ValueError(
                        'invalid "or" operand %r (inside %r)'
                        % (bool_values[-1], condition)
                    )
            return any(bool_values)
        if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.And):
            bool_values = []
            for value in node.values:
                bool_values.append(_convert(value))
                if not isinstance(bool_values[-1], bool):
                    raise ValueError(
                        'invalid "and" operand %r (inside %r)'
                        % (bool_values[-1], condition)
                    )
            return all(bool_values)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            value = _convert(node.operand)
            if not isinstance(value, bool):
                raise ValueError(
                    'invalid "not" operand %r (inside %r)' % (value, condition)
                )
            return not value
        if isinstance(node, ast.Compare):
            if len(node.ops) != 1:
                raise ValueError(
                    'invalid compare: exactly 1 operator required (inside %r)'
                    % (condition)
                )
            if len(node.comparators) != 1:
                raise ValueError(
                    'invalid compare: exactly 1 comparator required (inside %r)'
                    % (condition)
                )

            left = _convert(node.left)
            right = _convert(
                node.comparators[0], allow_tuple=isinstance(node.ops[0], ast.In)
            )

            if isinstance(node.ops[0], ast.Eq):
                return left == right
            if isinstance(node.ops[0], ast.NotEq):
                return left != right
            if isinstance(node.ops[0], ast.In):
                return left in right

            raise ValueError(
                'unexpected operator: %s %s (inside %r)'
                % (node.ops[0], ast.dump(node), condition)
            )

        raise ValueError(
            'unexpected AST node: %s %s (inside %r)'
            % (node, ast.dump(node), condition)
        )

    return _convert(main_node)


class Dep:
    """Represent one entry of DEPS's deps.

    One Dep object means one subproject inside DEPS file. It recorded what to
    checkout (like git, cipd or gcs) content of each subproject.

    Attributes:
      path: subproject path, relative to project root
      variables: the variables of the containing DEPS file; these variables will
          be applied to fields of this object (like 'url' and 'condition') and
          children projects.
      condition: whether to checkout this subproject
      dep_type: 'git', 'cipd' or 'gcs'
      url: if dep_type='git', the url of remote repo and associated branch/commit
      packages: if dep_type='cipd', cipd package version and location
      bucket:  if dep_type='gcs', gcd bucket location
      objects: if dep_type='gcs', gcs object info(object_name,sha256sum,size_bytes,generation,output_file)
    """

    def __init__(self, path, variables, entry):
        self.path = path
        self.variables = variables

        self.url = None  # only valid for dep_type='git'
        self.packages = None  # only valid for dep_type='cipd'
        self.bucket = None  # only valid for dep_type='gcs'
        self.objects = None  # only valid for dep_type='gcs'

        if isinstance(entry, str):
            self.dep_type = 'git'
            self.url = entry
            self.condition = None
        else:
            self.dep_type = entry.get('dep_type', 'git')
            self.condition = entry.get('condition')
            if self.dep_type == 'git':
                self.url = entry['url']
            elif self.dep_type == 'gcs':
                self.bucket = entry['bucket']
                self.objects = entry['objects']
            else:
                assert self.dep_type == 'cipd', (
                    'unknown dep_type:' + self.dep_type
                )
                self.packages = entry['packages']

        if self.dep_type == 'git':
            # TODO(b/301570818): It should be just
            # self.url.format(vars.variables).
            # Pass unpacked self.variables temporarily due to a bad entry in
            # DEPS.
            self.url = self.url.format(vars=self.variables, **self.variables)

    def __eq__(self, rhs):
        return vars(self) == vars(rhs)

    def __ne__(self, rhs):
        return not self.__eq__(rhs)

    def set_url(self, repo_url, at):
        assert self.dep_type == 'git'
        self.url = '%s@%s' % (repo_url, at)

    def set_revision(self, at):
        assert self.dep_type == 'git'
        repo_url, _ = self.parse_url()
        self.set_url(repo_url, at)

    def parse_url(self):
        assert self.dep_type == 'git'
        assert self.url is not None

        if '@' in self.url:
            repo_url, at = self.url.split('@')
        else:
            # If the dependency is not pinned, the default branch.
            repo_url, at = self.url, DEFAULT_BRANCH_NAME
        return repo_url, at

    def as_path_spec(self):
        repo_url, at = self.parse_url()
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
            'checkout_mips64': False,
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

        return evaluate_condition(self.condition, vars_dict)

    def to_lines(self):
        s = []
        condition_part = (
            ['    "condition": %r,' % self.condition] if self.condition else []
        )
        if self.dep_type == 'cipd':
            s.extend(
                [
                    '  "%s": {' % (self.path.split(':')[0],),
                    '    "packages": [',
                ]
            )
            for p in sorted(self.packages, key=lambda x: x['package']):
                s.extend(
                    [
                        '      {',
                        '        "package": "%s",' % p['package'],
                        '        "version": "%s",' % p['version'],
                        '      },',
                    ]
                )
            s.extend(
                [
                    '    ],',
                    '    "dep_type": "cipd",',
                ]
                + condition_part
                + [
                    '  },',
                    '',
                ]
            )
        elif self.dep_type == 'gcs':
            s.extend(
                [
                    '  "%s": {' % (self.path.split(':')[0],),
                    '    "objects": [',
                ]
            )
            for obj in sorted(self.objects, key=lambda x: x['object_name']):
                s.extend(
                    [
                        '      {',
                        '        "object_name": "%s",' % obj['object_name'],
                        '        "sha256sum": "%s",' % obj['sha256sum'],
                        '        "size_bytes": %s,' % obj['size_bytes'],
                        '        "generation": %s,' % obj['generation'],
                    ]
                )
                if 'output_file' in obj:
                    s.extend(
                        [
                            '        "output_file": "%s",' % obj['output_file'],
                        ]
                    )
            s.extend(
                [
                    '      },',
                    '    ],',
                    '    "dep_type": "gcs",',
                    '    "bucket": "%s",' % self.bucket,
                ]
                + condition_part
                + [
                    '  },',
                    '',
                ]
            )
        else:
            s.extend(
                [
                    '  "%s": {' % (self.path,),
                    '    "url": "%s",' % (self.url,),
                ]
                + condition_part
                + [
                    '  },',
                    '',
                ]
            )
        return s


class Deps:
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
        self.ignored_entries = {}
        self.recursedeps = []
        self.allowed_hosts = set()
        self.gn_args_from = None
        self.gn_args_file = None
        self.gn_args = []
        self.hooks = []
        self.pre_deps_hooks = []
        self.modified = set()

    def _gn_settings_to_lines(self):
        s = []
        if self.gn_args_file:
            s.extend(
                [
                    'gclient_gn_args_file = "%s"' % self.gn_args_file,
                    'gclient_gn_args = %r' % self.gn_args,
                ]
            )
        return s

    def _allowed_hosts_to_lines(self):
        """Converts |allowed_hosts| set to list of lines for output."""
        if not self.allowed_hosts:
            return []
        s = ['allowed_hosts = [']
        for h in sorted(self.allowed_hosts):
            s.append('  "%s",' % h)
        s.extend([']', ''])
        return s

    def _entries_to_lines(self):
        """Converts |entries| dict to list of lines for output."""
        entries = self.ignored_entries
        entries.update(self.entries)
        if not entries:
            return []
        s = ['deps = {']
        for _, dep in sorted(entries.items()):
            s.extend(dep.to_lines())
        s.extend(['}', ''])
        return s

    def _vars_to_lines(self):
        """Converts |variables| dict to list of lines for output."""
        if not self.variables:
            return []
        s = ['vars = {']
        for key, value in sorted(self.variables.items()):
            s.extend(
                [
                    '  "%s": %r,' % (key, value),
                    '',
                ]
            )
        s.extend(['}', ''])
        return s

    def _hooks_to_lines(self, name, hooks):
        """Converts |hooks| list to list of lines for output."""
        if not hooks:
            return []
        hooks.sort(key=lambda x: x.get('name', ''))
        s = ['%s = [' % name]
        for hook in hooks:
            s.extend(
                [
                    '  {',
                ]
            )
            if hook.get('name') is not None:
                s.append('    "name": "%s",' % hook.get('name'))
            if hook.get('pattern') is not None:
                s.append('    "pattern": "%s",' % hook.get('pattern'))
            if hook.get('condition') is not None:
                s.append('    "condition": %r,' % hook.get('condition'))
            # Flattened hooks need to be written relative to the root gclient dir
            cwd = os.path.relpath(os.path.normpath(hook.get('cwd', '.')))
            s.extend(
                ['    "cwd": "%s",' % cwd]
                + ['    "action": [']
                + ['        "%s",' % arg for arg in hook.get('action', [])]
                + ['    ]', '  },', '']
            )
        s.extend([']', ''])
        return s

    def to_string(self):
        """Return flatten DEPS string."""
        return '\n'.join(
            self._gn_settings_to_lines()
            + self._allowed_hosts_to_lines()
            + self._entries_to_lines()
            + self._hooks_to_lines('hooks', self.hooks)
            + self._hooks_to_lines('pre_deps_hooks', self.pre_deps_hooks)
            + self._vars_to_lines()
            + ['']
        )  # Ensure newline at end of file.

    def remove_src(self):
        """Return src_revision for buildbucket use."""
        assert 'src' in self.entries
        _, src_rev = self.entries['src'].parse_url()
        del self.entries['src']
        return src_rev

    def apply_commit(self, path, revision, overwrite=True):
        """Set revision to a project by path.

        Args:
          path: A project's path.
          revision: A git commit id.
          overwrite: Overwrite flag, the project won't change if overwrite=False
                     and it was modified before.
        """
        if path in self.modified and not overwrite:
            return
        self.modified.add(path)

        if path not in self.entries:
            logger.warning('path: %s not found in DEPS', path)
            return
        self.entries[path].set_revision(revision)

    def apply_action_groups(self, action_groups):
        """Apply multiple action groups to DEPS.

        If there are multiple actions in one repo, only last one is applied.

        Args:
          action_groups: A list of action groups.
        """
        # Apply in reversed order with overwrite=False,
        # so each repo is on the state of last action.
        for action_group in reversed(action_groups):
            for action in reversed(action_group.actions):
                if isinstance(action, codechange.GitCheckoutCommit):
                    self.apply_commit(action.path, action.rev, overwrite=False)
                if isinstance(action, codechange.GitAddRepo):
                    self.apply_commit(action.path, action.rev, overwrite=False)
                if isinstance(action, codechange.GitRemoveRepo):
                    assert action.path not in self.entries
                    self.modified.add(action.path)

    def apply_deps(self, deps):
        for path, dep in deps.entries.items():
            if path in self.entries:
                _, rev = dep.parse_url()
                self.apply_commit(path, rev, overwrite=False)

        # hooks, vars, ignored_entries are ignored and should be set by float_spec


class TimeSeriesTree:
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
        self.alive_children: dict[str, tuple[float, dict[str, str]]] | None = {}

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
        return (
            deps_a.entries[path] == deps_b.entries[path]
            and deps_a.variables == deps_b.variables
        )

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
        assert self.alive_children is not None

        for child_entry in set(
            list(self.alive_children.keys()) + children_entries
        ):
            # `child_entry` is added at `timestamp`
            if child_entry not in self.alive_children:
                self.alive_children[child_entry] = timestamp, deps

            # `child_entry` is removed at `timestamp`
            elif child_entry not in children_entries:
                self.subtrees.append(
                    TimeSeriesTree(
                        self.alive_children[child_entry][1],
                        child_entry,
                        self.alive_children[child_entry][0],
                        timestamp,
                    )
                )
                del self.alive_children[child_entry]

            # `child_entry` is alive before and after `timestamp`
            else:
                last_deps = self.alive_children[child_entry][1]
                if not self.subtree_eq(last_deps, deps, child_entry):
                    self.subtrees.append(
                        TimeSeriesTree(
                            last_deps,
                            child_entry,
                            self.alive_children[child_entry][0],
                            timestamp,
                        )
                    )
                    self.alive_children[child_entry] = timestamp, deps

    def no_more_snapshot(self):
        """Indicates all snapshots are added.

        add_snapshot() should not be invoked after no_more_snapshot().
        """
        for child_entry, (timestamp, deps) in self.alive_children.items():
            if timestamp == self.end_time and timestamp != self.start_time:
                continue
            self.subtrees.append(
                TimeSeriesTree(deps, child_entry, timestamp, self.end_time)
            )
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
        if not self.snapshots:
            # This substree is broken (e.g., deps files are removed
            # accidentally but still referenced by its parent).
            return []

        assert self.alive_children is None, (
            'events() is valid only after ' 'no_more_snapshot() is invoked'
        )

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

    def iter_forest(self):
        """Iterates snapshots of project dependency state.

        In terms of DEPS parsing, `forest` is a collection of Deps objects.

        Yields:
          (timestamp, forest):
            timestamp: time of snapshot
            forest: A dict indicates path => deps mapping
        """
        forest = {}
        # Group by timestamp
        for timestamp, events in itertools.groupby(
            self.events(), operator.itemgetter(0)
        ):
            # It's possible that one deps is removed and added at the same timestamp,
            # i.e. modification, so use counter to track.
            end_counter: collections.Counter[str] = collections.Counter()

            for _timestamp, entry, deps, end in events:
                forest[entry] = deps
                if end:
                    end_counter[entry] += 1
                else:
                    end_counter[entry] -= 1

            yield timestamp, forest

            # Remove deps which are removed at this timestamp.
            for entry, count in end_counter.items():
                assert -1 <= count <= 1, (timestamp, entry)
                if count == 1:
                    del forest[entry]

    def iter_path_specs(self):
        """Iterates snapshots of project dependency state.

        In terms of DEPS parsing, `path_specs` is flatten of recursive Deps objects.

        Yields:
          (timestamp, path_specs):
            timestamp: time of snapshot
            path_specs: dict of path_spec entries
        """
        for timestamp, forest in self.iter_forest():
            path_specs = {}
            # Merge Deps at time `timestamp` into single path_specs.
            for deps in forest.values():
                for path, dep in deps.entries.items():
                    path_specs[path] = dep.as_path_spec()
            yield timestamp, path_specs


class DepsParser:
    """Gclient DEPS file parser."""

    def __init__(self, project_root, code_storage):
        self.project_root = project_root
        self.code_storage = code_storage

    def load_single_deps(self, content):
        # var names may not be valid python identifiers, so wrap them in a
        # dictionary (b/301370477).
        def var_function(name):
            return '{vars[%s]}' % name

        def str_function(name):
            return str(name)

        global_scope = {"Var": var_function, "Str": str_function}
        local_scope: dict[str, typing.Any] = {}
        # pylint: disable=exec-used
        exec(content, global_scope, local_scope)
        return local_scope

    def parse_single_deps(
        self, content, parent_vars=None, parent_path='', parent_dep=None
    ):
        """Parses DEPS file without recursion.

        Args:
          content: file content of DEPS file
          parent_vars: variables inherent from parent DEPS
          parent_path: project path of parent DEPS file
          parent_dep: A corresponding Dep object in parent DEPS

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
                logger.warning(
                    '%s is deprecated and not supported recursion syntax', name
                )
        if 'deps_os' in local_scope:
            logger.warning('deps_os is no longer supported')

        if 'allowed_hosts' in local_scope:
            deps.allowed_hosts = set(local_scope.get('allowed_hosts'))
        deps.hooks = local_scope.get('hooks', [])
        deps.pre_deps_hooks = local_scope.get('pre_deps_hooks', [])
        deps.gn_args_from = local_scope.get('gclient_gn_args_from')
        deps.gn_args_file = local_scope.get('gclient_gn_args_file')
        deps.gn_args = local_scope.get('gclient_gn_args', [])

        # recalculate hook path
        use_relative_hooks = local_scope.get('use_relative_hooks', False)
        if use_relative_hooks:
            assert local_scope.get('use_relative_paths', False)
            for hook in deps.hooks:
                hook['cwd'] = os.path.join(parent_path, hook.get('cwd', ''))
            for pre_deps_hook in deps.pre_deps_hooks:
                pre_deps_hook['cwd'] = os.path.join(
                    parent_path, pre_deps_hook.get('cwd', '')
                )

        for path, dep_entry in local_scope.get('deps', {}).items():
            # recalculate path
            path = path.format(vars=deps.variables)
            if local_scope.get('use_relative_paths', False):
                path = os.path.join(parent_path, path)
            path = os.path.normpath(path)

            dep = Dep(path, deps.variables, dep_entry)
            eval_condition = dep.eval_condition()

            # update condition
            if parent_dep and parent_dep.condition:
                tmp_dict = {'condition': dep.condition}
                gclient_eval.UpdateCondition(
                    tmp_dict, 'and', parent_dep.condition
                )
                dep.condition = tmp_dict['condition']

            if not eval_condition:
                deps.ignored_entries[path] = dep
                continue

            # TODO(kcwu): support dep_type=cipd http://crbug.com/846564
            if dep.dep_type != 'git':
                warning_key = ('dep_type', dep.dep_type, path)
                if warning_key not in emitted_warnings:
                    emitted_warnings.add(warning_key)
                    logger.warning(
                        'dep_type=%s is not supported yet: %s',
                        dep.dep_type,
                        path,
                    )
                deps.ignored_entries[path] = dep
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
            path = path.format(vars=deps.variables)
            if path in deps.entries:
                recursedeps.append((path, deps_file))
        deps.recursedeps = recursedeps

        return deps

    def construct_deps_tree(
        self,
        tstree,
        repo_url,
        at,
        after,
        before,
        parent_vars=None,
        parent_path='',
        parent_dep=None,
        deps_file='DEPS',
        allow_floating=True,
    ):
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
          parent_dep: A corresponding Dep object in parent DEPS
          deps_file: filename of DEPS file, relative to the git repo, repo_rul
          allow_floating: True if allow floating commit references
        """
        if '://' in repo_url:
            git_repo = self.code_storage.cached_git_root(repo_url)
            if not os.path.exists(git_repo):
                with locking.lock_file(
                    os.path.join(
                        self.code_storage.cache_dir,
                        locking.LOCK_FILE_FOR_MIRROR_SYNC,
                    )
                ):
                    mirror(self.code_storage, repo_url)
        else:
            git_repo = repo_url

        if git_util.is_git_rev(at):
            history = git_util.Commit.make_commit_list(
                [
                    (after, at),
                    (before, at),
                ]
            )
        else:
            if not allow_floating:
                raise errors.ExternalError(
                    'Reference to floating commit (%s) is not allowed' % at
                )
            history = git_util.get_history(
                git_repo,
                deps_file,
                branch=at,
                after=after,
                before=before,
                padding_begin=True,
                padding_end=True,
            )
        assert history

        # If not equal, it means the file was deleted but is still referenced by
        # its parent.
        assert history[-1].timestamp == before

        # TODO(kcwu): optimization: history[-1] is unused
        for commit in history[:-1]:
            try:
                content = git_util.get_file_from_revision(
                    git_repo, commit.rev, deps_file
                )
            except subprocess.CalledProcessError:
                logger.error(
                    'Failed to get %s:%s from repo %s. Skip this snapshot. '
                    'It usually means somebody messed up the deps file.',
                    commit.rev,
                    deps_file,
                    git_repo,
                )
                continue

            deps = self.parse_single_deps(
                content,
                parent_vars=parent_vars,
                parent_path=parent_path,
                parent_dep=parent_dep,
            )
            tstree.add_snapshot(commit.timestamp, deps, deps.recursedeps)

        tstree.no_more_snapshot()

        for subtree in tstree.subtrees:
            path, deps_file = subtree.entry
            path_spec = subtree.parent_deps.entries[path].as_path_spec()
            self.construct_deps_tree(
                subtree,
                path_spec.repo_url,
                path_spec.rev,
                subtree.start_time,
                subtree.end_time,
                parent_vars=subtree.parent_deps.variables,
                parent_path=path,
                parent_dep=subtree.parent_deps.entries[path],
                deps_file=deps_file,
                allow_floating=allow_floating,
            )

    def enumerate_path_specs(self, start_time, end_time, path, branch=None):
        tstree = TimeSeriesTree(None, path, start_time, end_time)
        if not branch:
            branch = DEFAULT_BRANCH_NAME
        self.construct_deps_tree(
            tstree,
            path,
            branch,
            start_time,
            end_time,
            allow_floating=(start_time != end_time),
        )
        return tstree.iter_path_specs()

    def enumerate_gclient_solutions(self, start_time, end_time, path):
        tstree = TimeSeriesTree(None, path, start_time, end_time)
        self.construct_deps_tree(
            tstree, path, DEFAULT_BRANCH_NAME, start_time, end_time
        )
        return tstree.iter_forest()

    def flatten(self, solutions, entry_point: str):
        """Flatten all given Deps

        Args:
          solutions: A name => Deps dict, name can be either a str or a tuple.
          entry_point: An entry_point name of solutions.

        Returns:
          Deps: A flatten Deps.
        """

        def _add_unvisited_recursedeps(deps_queue, visited, deps):
            for name in deps.recursedeps:
                if name not in visited:
                    visited.add(name)
                    deps_queue.put(name)

        result = solutions[entry_point]
        deps_queue: queue.SimpleQueue[str | tuple[str, ...]] = (
            queue.SimpleQueue()
        )
        visited = set()
        visited.add(entry_point)
        _add_unvisited_recursedeps(deps_queue, visited, solutions[entry_point])

        # BFS to merge `deps` into `result`
        while not deps_queue.empty():
            deps_name = deps_queue.get()
            deps = solutions[deps_name]

            result.allowed_hosts.update(deps.allowed_hosts)
            for key, value in deps.variables.items():
                assert (
                    key not in result.variables or deps.variables[key] == value
                )
                result.variables[key] = value
            result.pre_deps_hooks += deps.pre_deps_hooks
            result.hooks += deps.hooks

            for dep in deps.entries.values():
                assert (
                    dep.path not in result.entries
                    or result.entries.get(dep.path) == dep
                )
                result.entries[dep.path] = dep

            for dep in deps.ignored_entries.values():
                assert (
                    dep.path not in result.ignored_entries
                    or result.ignored_entries.get(dep.path) == dep
                )
                result.ignored_entries[dep.path] = dep

            _add_unvisited_recursedeps(deps_queue, visited, deps)

        # If gn_args_from is set in root DEPS, overwrite gn arguments
        if solutions[entry_point].gn_args_from:
            gn_args_dep = solutions[
                (solutions[entry_point].gn_args_from, 'DEPS')
            ]
            result.gn_args = gn_args_dep.gn_args
            result.gn_args_file = gn_args_dep.gn_args_file

        return result


class GclientCache(codechange.CodeStorage):
    """Gclient git cache."""

    def __init__(self, cache_dir: str):
        self.cache_dir = cache_dir

    def _url_to_cache_dir(self, url: str) -> str:
        # ref: depot_tools' git_cache.Mirror.UrlToCacheDir
        parsed = urllib.parse.urlparse(url)
        norm_url = parsed.netloc + parsed.path
        if norm_url.endswith('.git'):
            norm_url = norm_url[: -len('.git')]
        norm_url = norm_url.replace('googlesource.com/a/', 'googlesource.com/')
        return norm_url.replace('-', '--').replace('/', '-').lower()

    def cached_git_root(self, repo_url: str) -> str:
        cache_path = self._url_to_cache_dir(repo_url)
        return os.path.join(self.cache_dir, cache_path)

    def add_to_project_list(
        self, project_root: str, path: str, repo_url: str
    ) -> None:
        projects = load_gclient_entries(project_root)

        projects[path] = repo_url

        write_gclient_entries(project_root, projects)

    def remove_from_project_list(self, project_root: str, path: str) -> None:
        projects = load_gclient_entries(project_root)

        if path in projects:
            del projects[path]

        write_gclient_entries(project_root, projects)
