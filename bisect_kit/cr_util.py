# -*- coding: utf-8 -*-
# Copyright 2017 The ChromiumOS Authors
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
import urllib.request

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

CHROME_BINARIES = ['chrome']
# nacl_helper is not available on arm64
CHROME_BINARIES_OPTIONAL = ['nacl_helper']
# This list is created manually by inspecting
# src/third_party/chromiumos-overlay/chromeos-base/chrome-binary-tests/
# chrome-binary-tests-0.0.1.ebuild
# If you change the list, deploy_chrome_helper.sh may need update as well.
# TODO(kcwu): we can get rid of this table once we migrated to build chrome
#             using chromeos ebuild rules.
CHROME_TEST_BINARIES = [
    'capture_unittests',
    'dawn_end2end_tests',
    'dawn_unittests',
    'decode_test',
    'fake_dmserver',
    'image_processor_test',
    'jpeg_decode_accelerator_unittest',
    'jpeg_encode_accelerator_unittest',
    'ozone_gl_unittests',
    'ozone_integration_tests',
    'sandbox_linux_unittests',
    'v4l2_stateless_decoder',
    'vaapi_unittest',
    'video_decode_accelerator_perf_tests',
    'video_decode_accelerator_tests',
    'video_encode_accelerator_perf_tests',
    'video_encode_accelerator_tests',
    'wayland_client_integration_tests',
    'wayland_client_perftests',

    # Removed binaries. Keep them here for users to bisect old versions.
    'video_decode_accelerator_unittest',
    'video_encode_accelerator_unittest',
]


def is_chrome_version(s):
  """Is a chrome version string?"""
  return bool(re.match(RE_CHROME_VERSION, s))


def _canonical_build_number(s):
  """Returns canonical build number.

  "The BUILD and PATCH numbers together are the canonical representation of
  what code is in a given release."
  ref: https://www.chromium.org/developers/version-numbers

  This format is used for version comparison.

  Args:
    s: chrome version string, in format MAJOR.MINOR.BUILD.PATCH

  Returns:
    BUILD.PATCH
  """
  assert s.count('.') == 3
  return '.'.join(s.split('.')[2:])


def is_version_lesseq(a, b):
  """Compares whether Chrome version `a` is less or equal to version `b`."""
  return util.is_version_lesseq(
      _canonical_build_number(a), _canonical_build_number(b))


def is_direct_relative_version(a, b):
  return util.is_direct_relative_version(
      _canonical_build_number(a), _canonical_build_number(b))


def extract_branch_from_version(rev):
  """Extracts branch number from version string

  Args:
    rev: chrome version string

  Returns:
    branch number (str). For example, return '3064' for '59.0.3064.0'.
  """
  return rev.split('.')[2]


def extract_milestone_from_version(rev: str) -> str:
  """Extracts milestone from version string

  Args:
    rev: chrome version string

  Returns:
    milestone (str). For example, return '59' for '59.0.3064.0'.
  """
  return rev.split('.')[0]


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
    except ValueError as e:
      # Failed due to source code not synced yet? Anyway, query by web api
      url = 'https://omahaproxy.appspot.com/deps.json?version=' + rev
      logger.debug('fetch %s', url)
      content = urllib.request.urlopen(url).read().decode('utf8')
      obj = json.loads(content)
      rev = obj['chromium_commit']
      if not git_util.is_git_rev(rev):
        raise ValueError(
            'The response of omahaproxy, %s, is not a git commit hash' %
            rev) from e

  if git_util.is_git_rev(rev):
    return rev

  # Cr-Commit-Position
  m = re.match(r'^#(\d+)$', rev)
  if m:
    return query_git_rev_by_commit_position(chrome_src, 'master', m.group(1))

  raise ValueError('unknown rev format: %s' % rev)


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

  # This work around http://crbug.com/1072400
  # New goma exists in depot_tools but chromite still uses old way to fetch
  # goma.
  goma_dir = os.path.expanduser('~/depot_tools/.cipd_bin')
  if os.path.exists(os.path.join(goma_dir, 'gomacc')):
    prefix += ['--gomadir', goma_dir]

  lkgm_file = os.path.join(chrome_src, 'chromeos', 'CHROMEOS_LKGM')
  if os.path.exists(lkgm_file):
    with open(lkgm_file) as f:
      lkgm_version = f.read().strip()
      # This work around b/205818006
      if (util.is_version_lesseq('14285.0.0', lkgm_version) and
          util.is_version_lesseq(lkgm_version, '14309.0.0')):
        prefix += ['--version', '14284.0.0']

  env = kwargs.get('env', os.environ).copy()

  # This work around http://crbug.com/658104, which depot_tool can't find GN
  # path correctly.
  env['CHROMIUM_BUILDTOOLS_PATH'] = os.path.abspath(
      os.path.join(chrome_src, 'buildtools'))

  if os.environ.get('SKYLAB_CLOUD_SERVICE_ACCOUNT_JSON'):
    env['GOMA_SERVICE_ACCOUNT_JSON_FILE'] = os.environ.get(
        'SKYLAB_CLOUD_SERVICE_ACCOUNT_JSON')
    env['GOMA_SERVER_HOST'] = 'goma.chromium.org'
    env['GOMA_RPC_EXTRA_PARAMS'] = '?prod'
    env['GOMA_ARBITRARY_TOOLCHAIN_SUPPORT'] = 'true'

  cmd = prefix + ['--'] + list(args)

  return util.check_output(*cmd, cwd=chrome_src, env=env)


def determine_targets_to_build(chrome_src, board, with_tests=True):
  out_dir = os.path.join('out_%s' % board, 'Release')
  available_targets = simple_chrome_shell(
      chrome_src,
      board,
      'buildtools/linux64/gn',
      'ls',
      '--type=executable',
      '--as=output',
      out_dir,
      internal=True).splitlines()

  mandatory = CHROME_BINARIES
  optional = list(CHROME_BINARIES_OPTIONAL)
  if with_tests:
    optional += CHROME_TEST_BINARIES

  result = []
  for binary in mandatory:
    assert binary in available_targets, ('build rule for %r is not found?' %
                                         binary)
    result.append(binary)
  for binary in optional:
    if binary not in available_targets:
      continue
    result.append(binary)
  return result


def build_and_deploy(chrome_src,
                     board,
                     dut,
                     targets,
                     with_tests=True,
                     nodeploy=False):
  """Build and deploy Chrome binaries to ChromeOS DUT

  Args:
    chrome_src: git repo path of chrome/src
    board: ChromeOS board name
    dut: DUT address
    targets: ninja targets. Default to build chrome.
    with_tests: build test binaries as well if targets is not specified
    nodeploy: if True, only build, do not deploy
  """
  if not targets:
    targets = determine_targets_to_build(
        chrome_src, board, with_tests=with_tests)

  logger.info('build %s', targets)
  out_dir = os.path.join('out_%s' % board, 'Release')
  with locking.lock_file(locking.LOCK_FILE_FOR_BUILD):
    cmd = ['./third_party/depot_tools/autoninja', '-C', out_dir] + targets
    try:
      simple_chrome_shell(chrome_src, board, *cmd, internal=True)
    except subprocess.CalledProcessError:
      logger.warning('build failed, delete out directory and retry again')
      # Some compiler processes are still terminating. Short delay is
      # necessary.
      time.sleep(10)
      shutil.rmtree(os.path.join(chrome_src, out_dir))
      simple_chrome_shell(chrome_src, board, *cmd, internal=True)

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

  This class enumerates DEPS files of chrome, parses them, and sync to disk
  state according to them.
  """

  def __init__(self, config):
    self.config = config
    self.spec_dir = os.path.join(config['chrome_root'], 'buildspec')
    self.chrome_src = os.path.join(config['chrome_root'], 'src')

  def lookup_build_timestamp(self, rev):
    assert is_chrome_version(rev)

    return git_util.get_commit_time(self.chrome_src, rev, 'DEPS')

  def generate_meta_deps(self, rev):
    """Generates a root DEPS file.

    This is kind of workaround for the limitation of our DEPS parser. When
    parsing src/, it returns content of src/DEPS but the revision of src/
    itself is lost. It is non-trivial to revise the recursive parser, so I
    created a root DEPS file to include src/ instead.

    Args:
      rev: chrome src/ branch or tag

    Returns:
      path to the synthesized repo
    """
    meta_dir = os.path.join(self.config['chrome_root'], 'meta-deps')
    if os.path.exists(meta_dir):
      shutil.rmtree(meta_dir)
    git_util.init(meta_dir, initial_branch=gclient_util.DEFAULT_BRANCH_NAME)

    # 1. Create initial commit with DEPS file referring to master.
    init_timestamp = '2018-01-01T00:00:00'
    meta_deps = textwrap.dedent("""
    gclient_gn_args_from = "src"

    vars = {
      "checkout_src_internal": True,
    }

    deps = {
      "src": "https://chromium.googlesource.com/chromium/src.git@%s",
    }

    recursedeps = [
      "src",
    ]
    """ % rev)
    git_util.commit_file(
        meta_dir, 'DEPS', 'initial DEPS', meta_deps, commit_time=init_timestamp)

    return meta_dir

  def _hack_trim_libassistant(self, old, _new, spec):
    # Before 74.0.3694.0, assistant use custom hook to sync code.
    # This is bad practice and difficult to deal. Because most of
    # regressions are not related to assistant, we simply remove repo
    # changes inside libassistant to make things easier (b/126633034).
    if is_version_lesseq(old, '74.0.3694.0'):
      for path in spec.entries.keys():
        if path.startswith('src/chromeos/assistant/libassistant/src/'):
          del spec.entries[path]

  def collect_float_spec(self, old, new, fixed_specs=None):
    del fixed_specs  # unused
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

    branch = 'branch-heads/' + extract_branch_from_version(new)
    meta_dir = self.generate_meta_deps(branch)

    result = []
    code_storage = gclient_util.GclientCache(self.config['chrome_mirror'])
    parser = gclient_util.DepsParser(self.config['chrome_root'], code_storage)
    path = os.path.join(meta_dir, 'DEPS')

    logger.info('start enumerate_path_specs')
    for timestamp, path_specs in parser.enumerate_path_specs(
        old_timestamp, new_timestamp, meta_dir):
      spec = codechange.Spec(codechange.SPEC_FLOAT, '(n/a)', timestamp, path)
      spec.entries = path_specs
      self._hack_trim_libassistant(old, new, spec)
      result.append(spec)
    return result

  def enumerate_ancestor(self, new):
    result = []
    for rev in git_util.get_tags(self.chrome_src):
      # 74.0.3696.0/DEPS is bad and rejected by gclient (crbug/929544)
      if rev in ['74.0.3696.0']:
        continue
      if not is_chrome_version(rev):
        continue
      if is_version_lesseq(rev, new) and is_direct_relative_version(rev, new):
        result.append(rev)
    return sorted(result, key=util.version_key_func)

  def collect_fixed_spec(self, old, new):
    logger.info('collect_fixed_spec')
    assert is_direct_relative_version(old, new)
    result = []
    code_storage = gclient_util.GclientCache(self.config['chrome_mirror'])
    parser = gclient_util.DepsParser(self.config['chrome_root'], code_storage)
    for tag in self.enumerate_ancestor(new):
      if not is_version_lesseq(old, tag):
        continue
      tag_rev = git_util.get_commit_hash(self.chrome_src, tag)
      meta_dir = self.generate_meta_deps(tag_rev)
      meta_head_rev = git_util.get_commit_hash(meta_dir, 'HEAD')

      timestamp = git_util.get_commit_time(self.chrome_src, tag, 'DEPS')
      spec = codechange.Spec(codechange.SPEC_FIXED, tag, timestamp, 'DEPS')
      path_specs_result = list(
          parser.enumerate_path_specs(timestamp, timestamp, meta_dir,
                                      meta_head_rev))
      assert len(path_specs_result) == 1
      spec.entries = path_specs_result[0][1]
      self._hack_trim_libassistant(old, new, spec)
      result.append(spec)

    assert result[0].name == old, '%s != %s' % (result[0].name, old)
    assert result[-1].name == new, '%s != %s' % (result[-1].name, new)
    return result

  def parse_spec(self, spec):
    logger.debug('parse_spec %s', spec.name)
    code_storage = gclient_util.GclientCache(self.config['chrome_mirror'])
    assert spec.entries, 'it should already be parsed earlier'

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
      if not path_spec.is_static():  # only surgery for fixed spec
        continue
      while True:
        metadata = git_util.get_commit_metadata(git_repo, path_spec.at)
        if metadata.parent is None:
          break
        bot_email = 'chrome-release-bot@chromium.org'
        if (bot_email not in metadata.committer and
            bot_email not in metadata.author):
          break

        path_spec.at = metadata.parent[0]

  def sync_to_release(self, rev):
    """Switch source tree to given release version.

    Args:
      rev: chrome release version
    """
    assert is_chrome_version(rev)
    with locking.lock_file(
        os.path.join(self.config['chrome_mirror'],
                     locking.LOCK_FILE_FOR_MIRROR_SYNC)):
      git_util.checkout_version(self.chrome_src, rev)
      gclient_util.config(
          self.config['chrome_root'],
          url='https://chromium.googlesource.com/chromium/src.git',
          custom_var='checkout_src_internal=True',
          cache_dir=self.config['chrome_mirror'],
          managed=False,
          target_os='chromeos')

      gclient_util.sync(
          self.config['chrome_root'], with_branch_heads=True, with_tags=True)

  def sync_disk_state(self, rev):
    self.sync_to_release(rev)

  def get_release_deps(self, rev):
    """Get DEPS content of given release.

    Args:
      rev: chrome release version number

    Returns:
      DEPS string
    """
    # TODO(kcwu): flatten recursive DEPS content
    return git_util.get_file_from_revision(self.chrome_src, rev, 'DEPS')
