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
import shutil
import subprocess
import textwrap
import time
import urllib2

from bisect_kit import cli
from bisect_kit import codechange
from bisect_kit import common
from bisect_kit import errors
from bisect_kit import gclient_util
from bisect_kit import git_util
from bisect_kit import locking
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
    raise errors.InternalError('Unrecognized commit position: ' + position)

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
    raise errors.InternalError('Unrecognized svn-id: ' + position)

  raise errors.InternalError('Unable to recognize commit position from ' +
                             git_rev)


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
    ValueError: unable to find commits with given commit_position.
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
    except ValueError:
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


def build_and_deploy(chrome_src, board, dut, targets, nodeploy=False):
  """Build and deploy Chrome binaries to ChromeOS DUT

  Args:
    chrome_src: git repo path of chrome/src
    board: ChromeOS board name
    dut: DUT address
    targets: ninja targets. Default to build chrome.
    nodeploy: if True, only build, do not deploy
  """
  if not targets:
    targets = ['chrome', 'chrome_sandbox', 'nacl_helper']

  logger.info('build %s', targets)
  env = os.environ.copy()
  # This work around http://crbug.com/658104, which depot_tool can't find GN
  # path correctly.
  env['CHROMIUM_BUILDTOOLS_PATH'] = os.path.abspath(
      os.path.join(chrome_src, 'buildtools'))

  out_dir = os.path.join('out_%s' % board, 'Release')

  with locking.lock_file(locking.LOCK_FILE_FOR_BUILD):
    cmd = ['./third_party/depot_tools/autoninja', '-C', out_dir] + targets
    try:
      simple_chrome_shell(chrome_src, board, *cmd, internal=True, env=env)
    except subprocess.CalledProcessError:
      logger.warning('build failed, delete out directory and retry again')
      # Some compiler processes are still terminating. Short delay is
      # necessary.
      time.sleep(10)
      shutil.rmtree(os.path.join(chrome_src, out_dir))
      simple_chrome_shell(chrome_src, board, *cmd, internal=True, env=env)

  if nodeploy:
    return

  logger.info('deploy %s', targets)
  env = os.environ.copy()
  env['DUT'] = dut

  deploy_script = os.path.join(common.BISECT_KIT_ROOT,
                               'deploy_chrome_helper.sh')
  cmd = ['sh', '-ex', deploy_script] + targets
  simple_chrome_shell(chrome_src, board, *cmd, internal=True, env=env)


class ChromeSpecManager(codechange.SpecManager):
  """Gclient DEPS related operations.

  This class enumerates DEPS files in chrome's buildspec folder, parses them,
  and sync to disk state according to them.
  """

  def __init__(self, config):
    self.config = config
    self.spec_dir = os.path.join(config['chrome_root'], 'buildspec')
    self.chrome_src = os.path.join(config['chrome_root'], 'src')

  def lookup_build_timestamp(self, rev):
    assert is_chrome_version(rev)

    path = os.path.join('releases', rev, 'DEPS')
    try:
      timestamp = git_util.get_commit_time(self.spec_dir, 'origin/master', path)
    except ValueError:
      raise errors.InternalError('%s does not have %s' % (self.spec_dir, path))
    return timestamp

  def _generate_meta_deps(self, rev):
    """Synthesize branch DEPS with continuous history.

    Although chrome uses DEPS in its tree as source of truth, we need to use
    DEPS in buildspec in order to match the release DEPS files. However,
    branches/xx/DEPS is created during branching and it doesn't contain history
    before branching point (which conceptually refers to chrome master branch).

    This function synthesizes a new DEPS with full history including branch
    DEPS history and earlier history.

    Args:
      rev:

    Returns:
      path to the synthesized repo
    """
    meta_dir = os.path.join(self.config['chrome_root'], 'meta-deps')
    if os.path.exists(meta_dir):
      shutil.rmtree(meta_dir)
    git_util.init(meta_dir)

    # 1. Create initial commit with DEPS file referring to master.
    init_timestamp = '2018-01-01T00:00:00'
    meta_deps = textwrap.dedent('''
    vars = {
      "checkout_src_internal": True,
    }

    deps = {
      "src": "https://chromium.googlesource.com/a/chromium/src.git@master",
      "tools_internal": "https://chrome-internal.googlesource.com/a/chrome/tools/build/internal.DEPS.git@master",
    }

    recursedeps = [
      "src",
      "tools_internal",
    ]
    ''')
    git_util.commit_file(
        meta_dir, 'DEPS', 'initial DEPS', meta_deps, commit_time=init_timestamp)

    # 2. Copy history from buildspec branch DEPS file.
    # TODO(kcwu): support branch of branch
    branch = extract_branch_from_version(rev)
    branch_path = os.path.join('branches', branch, 'DEPS')
    for timestamp, git_hash in git_util.get_history(self.spec_dir, branch_path):
      message = 'copy of %s' % git_hash
      content = git_util.get_file_from_revision(self.spec_dir, git_hash,
                                                branch_path)
      git_util.commit_file(
          meta_dir, 'DEPS', message, content, commit_time=timestamp)

    return meta_dir

  def collect_float_spec(self, old, new):
    logger.info('collect_float_spec')
    old_timestamp = self.lookup_build_timestamp(old)
    new_timestamp = self.lookup_build_timestamp(new)

    # Workaround for racing between DEPS file and other commits.
    # The steps for branching and releasing on a buildbot:
    #  1. sync code
    #  2. commit branch DEPS file
    #  3. flatten the dependency and commit the generated release DEPS file
    # What lookup_build_timestamp() returns is the commit time of step 3. The
    # content of the DEPS file recorded the tree snapshot of step 1. However,
    # there might be some commits arriving between step 1 and step 3. In order
    # to reconstruct complete commit history, we actually need to figure out
    # timestamp of step 1, which is non-trivial.
    # Here, we just assume buildbot can finish these steps in one day. It's
    # harmless to have old_timestamp slightly earlier than the actual time.
    # See crbug.com/900514 for a real case. 16 hours threshold is not enough.
    old_timestamp -= 86400  # 1 day

    meta_dir = self._generate_meta_deps(new)

    result = []
    code_storage = gclient_util.GclientCache(self.config['chrome_mirror'])
    parser = gclient_util.DepsParser(self.config['chrome_root'], code_storage)
    path = os.path.join(meta_dir, 'DEPS')

    logger.info('start enumerate_path_specs')
    for timestamp, path_specs in parser.enumerate_path_specs(
        old_timestamp, new_timestamp, meta_dir):
      spec = codechange.Spec(codechange.SPEC_FLOAT, '(n/a)', timestamp, path)
      spec.entries = path_specs
      result.append(spec)
    return result

  def enumerate_ancestor(self, new):
    releases_dir = os.path.join(self.spec_dir, 'releases')
    result = []
    for rev in sorted(os.listdir(releases_dir), key=util.version_key_func):
      # 74.0.3696.0/DEPS is bad and rejected by gclient (crbug/929544)
      if rev in ['74.0.3696.0']:
        continue
      if not is_chrome_version(rev):
        continue
      if util.is_version_lesseq(rev, new) and util.is_direct_relative_version(
          rev, new):
        result.append(rev)
    return result

  def collect_fixed_spec(self, old, new):
    logger.info('collect_fixed_spec')
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

  def parse_spec(self, spec):
    logging.debug('parse_spec %s', spec.name)

    if spec.spec_type == codechange.SPEC_FLOAT:
      # Already parsed during collect_float_spec().
      # Fully parsing during collect_float_spec() is more efficient than
      # calling parsing here because we lost knowledge of commit ordering here
      # and calling get_rev_by_time() is very slow.
      assert spec.entries
      return

    code_storage = gclient_util.GclientCache(self.config['chrome_mirror'])
    parser = gclient_util.DepsParser(self.config['chrome_root'], code_storage)

    git_rev = git_util.get_rev_by_time(
        self.spec_dir, spec.timestamp, 'origin/master', path=spec.path)
    content = git_util.get_file_from_revision(self.spec_dir, git_rev, spec.path)
    deps = parser.parse_single_deps(content)
    assert not deps.recursedeps

    path_specs = {}
    for path, dep in deps.entries.items():
      path_specs[path] = dep.as_path_spec()
    spec.entries = path_specs

    # Mirror git repos if they do not exist.
    with locking.lock_file(
        os.path.join(self.config['chrome_mirror'],
                     locking.LOCK_FILE_FOR_MIRROR_SYNC)):
      for path_spec in spec.entries.values():
        git_repo = code_storage.cached_git_root(path_spec.repo_url)
        if os.path.exists(git_repo):
          continue
        gclient_util.mirror(code_storage, path_spec.repo_url)

    # Additional dependency fix.
    # DEPS files are maintained in the same tree as code, so releasing DEPS
    # will add one more commit. Because we want to match release DEPS against
    # branch commit history, we need to recover the code state before DEPS
    # commit.
    # To be more specific, chrome-release-bot@ creates and updates new DEPS
    # file to src/ and some internal repos every releases. Here we check the
    # dependency state of all subprojects and use their parent commit if it is
    # committed by release bot.
    for path_spec in spec.entries.values():
      git_repo = code_storage.cached_git_root(path_spec.repo_url)
      while True:
        metadata = git_util.get_commit_metadata(git_repo, path_spec.at)
        if 'parent' not in metadata:
          break
        if 'chrome-release-bot@chromium.org' not in metadata['committer']:
          break

        path_spec.at = metadata['parent'][0]

    assert spec.is_static()

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
        custom_var='checkout_src_internal=True',
        cache_dir=self.config['chrome_mirror'])

    # 'target_os' is mandatory for chromeos build, but 'gclient config' doesn't
    # recognize it. Here add it to .gclient file explicitly.
    with open(os.path.join(self.config['chrome_root'], '.gclient'), 'a') as f:
      f.write('target_os = ["chromeos"]\n')

    gclient_util.sync(
        self.config['chrome_root'],
        with_branch_heads=True,
        with_tags=True,
        ignore_locks=True)

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

    with locking.lock_file(
        os.path.join(self.config['chrome_mirror'],
                     locking.LOCK_FILE_FOR_MIRROR_SYNC)):
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
