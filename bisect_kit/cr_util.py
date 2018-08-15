# -*- coding: utf-8 -*-
# Copyright 2017 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Chrome utility."""

from __future__ import print_function
import json
import logging
import os
import re
import subprocess
import urllib2

from bisect_kit import cli
from bisect_kit import codechange
from bisect_kit import gclient_util
from bisect_kit import git_util
from bisect_kit import util

logger = logging.getLogger(__name__)

RE_CHROME_VERSION = r'^\d+\.\d+\.\d+\.\d+$'


def is_chrome_version(s):
  """Is a chrome version string?"""
  return bool(re.match(RE_CHROME_VERSION, s))


def extract_branch_from_version(rev):
  """Extracts branch number from version string

  Args:
    rev: chrome version string

  Returns:
    branch number (str). For example, return '3064' for '59.0.3064.0'.
  """
  return rev.split('.')[2]


def argtype_chrome_version(s):
  if not is_chrome_version(s):
    raise cli.ArgTypeError('invalid chrome version', '59.0.3064.0')
  return s


def query_commit_position(chrome_src, git_rev):
  """Queries chrome commit position by git hash.

  Chrome (excluding dependencies) maintains "Cr-Commit-Position", an
  incremental serial number of commits per branch in commit log.

  p.s. Earlier than Aug 2014, the serial number was just svn version number and
  mirrored to git as "git-svn-id". Although this function recognizes
  "git-svn-id", the rest of bisect-kit is never tested against such old version
  and doesn't guarantee to work.

  Args:
    chrome_src: git repo path of chrome/src
    git_rev: git hash id

  Returns:
    (branch, position):
      branch: branch name, e.g. "master" or "2133". Note, it will
          return "master" for svn's trunk for convenient.
      position: '123'

  Raises:
    ValueError: failed to recognize commit position.
  """
  msg = git_util.get_commit_log(chrome_src, git_rev)
  m = re.search(r'Cr-Commit-Position: (\S+)', msg)
  if m:
    position = m.group(1)
    m = re.match(r'refs/heads/master@\{#(\d+)\}', position)
    if m:
      return 'master', m.group(1)
    m = re.match(r'refs/branch-heads/(\d+)@\{#(\d+)\}', position)
    if m:
      return m.group(1), m.group(2)
    raise ValueError('Unrecognized commit position: ' + position)

  m = re.search(r'git-svn-id: (\S+)', msg)
  if m:
    position = m.group(1)
    m = re.match(r'svn://svn.chromium.org/chrome/trunk/src@(\d+)', position)
    if m:
      return 'master', m.group(1)
    m = re.match(r'svn://svn.chromium.org/chrome/branches/(\d+)/src@(\d+)',
                 position)
    if m:
      return m.group(1), m.group(2)
    raise ValueError('Unrecognized svn-id: ' + position)

  raise ValueError('Unable to recognize commit position from ' + git_rev)


def query_git_rev_by_commit_position(chrome_src, branch, commit_position):
  """Looks up git commit by chrome's commit position.

  This function cannot find early commits which is still using 'git-svn-id'.

  Args:
    chrome_src: git repo path of chrome/src
    branch: branch number or 'master'
    commit_position: chrome's commit position number

  Returns:
    git commit id

  Raises:
    core.ExecutionFatalError: unable to find commits with given
        commit_position.
  """
  if branch == 'master':
    ref_path = 'refs/heads/master'
  else:
    ref_path = 'refs/branch-heads/%s' % branch

  rev = util.check_output(
      'git',
      'rev-list',
      '-1',
      '--all',
      '--grep',
      'Cr-Commit-Position: %s@{#%s}' % (ref_path, commit_position),
      cwd=chrome_src).strip()
  if not rev:
    raise ValueError('unable to find commits with given commit_position')
  return rev


def query_git_rev(chrome_src, rev):
  """Guesses git commit by heuristic.

  Args:
    chrome_src: git repo path of chrome/src
    rev: could be
      chrome version, commit position, or git hash

  Returns:
    git commit hash
  """
  if is_chrome_version(rev):
    try:
      # Query via git tag
      return git_util.get_commit_hash(chrome_src, rev)
    except subprocess.CalledProcessError:
      # Failed due to source code not synced yet? Anyway, query by web api
      url = 'https://omahaproxy.appspot.com/deps.json?version=' + rev
      logger.debug('fetch %s', url)
      content = urllib2.urlopen(url).read()
      obj = json.loads(content)
      rev = obj['chromium_commit']
      if not git_util.is_git_rev(rev):
        raise ValueError(
            'The response of omahaproxy, %s, is not a git commit hash' % rev)

  if git_util.is_git_rev(rev):
    return rev

  # Cr-Commit-Position
  m = re.match(r'^#(\d+)$', rev)
  if m:
    return query_git_rev_by_commit_position(chrome_src, 'master', m.group(1))

  raise ValueError, 'unknown rev format: %s' % rev


def simple_chrome_shell(chrome_src, board, *args, **kwargs):
  """Run commands inside chrome-sdk shell.

  Args:
    chrome_src: git repo path of chrome/src
    board: ChromeOS board name
    *args: command to run
    **kwargs:
      env: (dict) environment variables for the command
  """
  prefix = [
      'cros',
      'chrome-sdk',
      '--board=%s' % board,
  ]
  if kwargs.get('internal'):
    prefix.append('--internal')

  env = kwargs.get('env', [])
  cmd = prefix + ['--'] + list(args)

  util.check_call(*cmd, cwd=chrome_src, env=env)


def build_and_deploy(chrome_src, board, dut, targets):
  """Build and deploy Chrome binaries to ChromeOS DUT

  Args:
    chrome_src: git repo path of chrome/src
    board: ChromeOS board name
    dut: DUT address
    targets: ninja targets. Default to build chrome.
  """
  # The main steps are written in the helper shell script because it is
  # expected to be often local hacked by users.
  dirname = os.path.dirname(os.path.abspath(__file__))
  build_and_deploy_script = os.path.join(dirname, '..',
                                         'build_and_deploy_chrome_helper.sh')
  cmd = ['sh', '-ex', build_and_deploy_script] + targets

  env = os.environ.copy()
  env['DUT'] = dut
  # This work around http://crbug.com/658104, which depot_tool can't find GN
  # path correctly.
  env['CHROMIUM_BUILDTOOLS_PATH'] = os.path.abspath(
      os.path.join(chrome_src, 'buildtools'))

  simple_chrome_shell(chrome_src, board, *cmd, internal=True, env=env)


class ChromeSpecManager(codechange.SpecManager):
  """Gclient DEPS related operations.

  This class enumerates DEPS files in chrome's buildspec folder, parses them,
  and sync to disk state according to them.
  """

  def __init__(self, config):
    self.config = config
    self.spec_dir = os.path.join(config['chrome_root'], 'buildspec')

  def _get_branches(self, old_branch, new_branch):
    """Gets branches between given range.

    Args:
      old_branch: start branch (inclusive)
      new_branch: end branch (inclusive)

    Returns:
      list of branches
    """
    branches_dir = os.path.join(self.spec_dir, 'branches')
    result = []
    for branch in os.listdir(branches_dir):
      if not branch.isdigit():
        continue
      if int(old_branch) <= int(branch) <= int(new_branch):
        result.append(branch)
    return sorted(result, key=int)

  def collect_float_spec(self, old, new):
    old_branch = extract_branch_from_version(old)
    new_branch = extract_branch_from_version(new)

    # commits_by_branch[i][j] is a commit. commits_by_branch[i][*] are commits
    # of the same branch.
    commits_by_branch = []

    # 'git log <path>' is slow for huge repo because git will search back the
    # whole history to find commits. We can use restrict git search range to
    # speed up. For example, we can run `git log --after=last_branch_time
    # branches/3360/DEPS` if we know branches/3359/DEPS is branched at
    # time=last_branch_time since 3360 must be branched after 3359.
    last_branch_time = None

    for branch in self._get_branches(old_branch, new_branch):
      # TODO(kcwu): investigate what happened for branch 3351
      if branch in ['3351']:
        continue
      path = os.path.join('branches', branch, 'DEPS')
      commits = []
      for timestamp, git_rev in git_util.get_history(
          self.spec_dir, path, after=last_branch_time):
        commits.append((timestamp, path, git_rev))
      commits_by_branch.append(commits)
      last_branch_time = commits[0][0]

    # Excludes revisions committed after next branch branched.
    #
    # For example, assume we want to collect spec between 3359 and 3360 branch
    # and the following is the commit history of branches 3359 and 3360. Here
    # 3359#4 means the 4th revision of buildspec/branches/3359/DEPS.
    #
    #   3359#1 -> .. -> 3359#4 -> 3359#5 -> ... > 3359#17 -> ...
    #                     |
    #                     +--> 3360#1 -> 3360#2 -> ...
    #
    # As this graph, 3360 is branched at the time of 3359#4. After 3360 is
    # branched, 3359 is still changing (i.e. updates for beta and stable
    # channels). Commit 3395#5 and its successor should be excluded if we want
    # to get all patches between 3359#1 and 3360#2.
    result = []
    for i, commits in enumerate(commits_by_branch):
      for timestamp, path, git_rev in commits:
        if i + 1 == len(
            commits_by_branch) or timestamp < commits_by_branch[i + 1][0][0]:
          result.append(
              codechange.Spec(codechange.SPEC_FLOAT, git_rev, timestamp, path))

    # Hack to exclude bad DEPS files.
    # TODO(kcwu): handle DEPS syntax error automatically
    bad_spec = set([
        # branches/3058/DEPS syntax error
        '6d5af773ff11573c2e16957dc6d0cc451f4b6ad4',
        # branches/3337/DEPS incorrect condition
        'd29971dc0059c2ad681f43feba84b071b4ae0365',
        '0b87411f83c4e85dd87a11797c1b83ec1f2b9970',
        # branches/3359/DEPS syntax error
        'fd0589643594b7b6807f15ecf3b34bc9694be2e5',
        'a65608f3e9e2cabe649cf6056d9817a0caa0893f',
        # branches/3439/DEPS was reverted manually
        'eb3a002e54d85828a052cce874dd48345db10329',
        '366f5f88f2fe3bea3c2c321098adca5e8e64c381',
    ])
    result = [spec for spec in result if spec.name not in bad_spec]

    return result

  def enumerate_ancestor(self, new):
    releases_dir = os.path.join(self.spec_dir, 'releases')
    result = []
    for rev in sorted(os.listdir(releases_dir), key=util.version_key_func):
      if not is_chrome_version(rev):
        continue
      if util.is_version_lesseq(rev, new) and util.is_direct_relative_version(
          rev, new):
        result.append(rev)
    return result

  def collect_fixed_spec(self, old, new):
    assert util.is_direct_relative_version(old, new)
    result = []
    for rev in self.enumerate_ancestor(new):
      if util.is_version_lesseq(old, rev):
        path = os.path.join('releases', rev, 'DEPS')
        timestamp = git_util.get_commit_time(self.spec_dir, 'HEAD', path)
        result.append(
            codechange.Spec(codechange.SPEC_FIXED, rev, timestamp, path))

    assert result[0].name == old, '%s != %s' % (result[0].name, old)
    assert result[-1].name == new, '%s != %s' % (result[-1].name, new)
    return result

  def _load_deps_content(self, spec):
    if spec.spec_type == codechange.SPEC_FIXED:
      content = git_util.get_file_from_revision(self.spec_dir, 'HEAD',
                                                spec.path)
    else:
      content = git_util.get_file_from_revision(self.spec_dir, spec.name,
                                                spec.path)
    return content

  def _custom_vars(self, spec):
    # build bot has ever defined some custom variables to override default
    # behavior. They are shown as "custom_var override" in release DEPS files.
    # Unfortunately, they are not version controlled by buildspec repository.
    # Here hard coded this dirty history (estimated) in order to replicate
    # snapshots of branch DEPS.
    custom_vars = {}
    if spec.spec_type == codechange.SPEC_FLOAT:
      branch = int(spec.path.split(os.sep)[1])
      if (branch == 3282 and spec.timestamp >= 1517533911 or  # M64
          branch == 3325 and spec.timestamp >= 1517537488 or  # M56
          3337 <= branch <= 3344 and spec.timestamp >= 1517544491 or  # M66
          branch >= 3360):  # M67+
        custom_vars['checkout_src_internal'] = True

    return custom_vars

  def parse_spec(self, spec):
    logging.debug('parse_spec %s', spec.name)
    deps_content = self._load_deps_content(spec)
    custom_vars = self._custom_vars(spec)
    spec.entries = gclient_util.parse_deps(deps_content, custom_vars)
    if spec.spec_type == codechange.SPEC_FIXED:
      assert spec.is_static()

  def _checkout_branch_deps(self, branch, git_rev):
    """Save branch DEPS commit as a file.

    Args:
      branch: (str) chrome branch number
      git_rev: git commit id

    Returns:
      DEPS file path, relative to buildspec/ folder
    """
    deps_file = 'branches/%s/DEPS' % branch
    if git_rev:
      spec_dir = os.path.join(self.config['chrome_root'], 'buildspec')
      content = git_util.get_file_from_revision(spec_dir, git_rev, deps_file)
      tmp_fn = '%s@%s' % (deps_file, git_rev)
      with open(os.path.join(spec_dir, tmp_fn), 'w') as f:
        f.write(content)
      deps_file = tmp_fn
    return deps_file

  def _gclient_sync_depfile(self, deps_file):
    """Gclient sync with given DEPS file.

    Args:
      deps_file: path to gclient DEPS file. If relative, relative to
          chrome_root's buildspec/.
    """
    gclient_util.config(
        self.config['chrome_root'],
        url=
        'https://chrome-internal.googlesource.com/a/chrome/tools/buildspec.git',
        deps_file=deps_file,
        cache_dir=self.config['gclient_cache_dir'])

    # 'target_os' is mandatory for chromeos build, but 'gclient config' doesn't
    # recognize it. Here add it to .gclient file explicitly.
    with open(os.path.join(self.config['chrome_root'], '.gclient'), 'a') as f:
      f.write('target_os = ["chromeos"]\n')

    gclient_util.sync(
        self.config['chrome_root'], with_branch_heads=True, with_tags=True)

  def sync_to_release(self, rev):
    """Switch source tree to given release version.

    Args:
      rev: chrome release version
    """
    release_deps_file = self.get_release_deps(rev)
    path = os.path.join(self.spec_dir, release_deps_file)
    if not os.path.exists(path):
      # Give hint that first time sync is very slow.
      logger.info(
          'gclient sync --with_branch_heads, may need more than half hours')
    self._gclient_sync_depfile(release_deps_file)

  def sync_disk_state(self, rev):
    self.sync_to_release(rev)

  @staticmethod
  def get_release_deps(rev):
    """Get DEPS file path of given release.

    Args:
      rev: chrome release version number

    Returns:
      DEPS file path, relative to buildspec/ folder
    """
    return 'releases/%s/DEPS' % rev

  def sync_to_branch_rev(self, branch, rev):
    """Switch source tree to given branch time.

    Args:
      branch: (str) chrome branch number
      rev: git commit id of branch commit
    """
    branch_deps_file = self._checkout_branch_deps(branch, rev)
    logger.info('sync %s', branch_deps_file)
    self._gclient_sync_depfile(branch_deps_file)
