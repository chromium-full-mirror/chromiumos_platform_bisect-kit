# Copyright 2017 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""ChromeOS utility.

Terminology used in this module.
  short_version: ChromeOS version number without milestone, like "9876.0.0".
  full_version: ChromeOS version number with milestone, like "R62-9876.0.0".
  version: if not specified, it could be in short or full format.
"""

from __future__ import print_function
import errno
import json
import logging
import os
import re
import subprocess
import time

from bisect_kit import cli
from bisect_kit import core
from bisect_kit import cr_util
from bisect_kit import git_util
from bisect_kit import repo_util
from bisect_kit import util

logger = logging.getLogger(__name__)

re_chromeos_full_version = r'^R\d+-\d+\.\d+\.\d+$'
re_chromeos_localbuild_version = r'^\d+\.\d+\.\d{4}_\d\d_\d\d_\d{4}$'
re_chromeos_short_version = r'^\d+\.\d+\.\d+$'

gs_archive_path = 'gs://chromeos-image-archive/{board}-release'
gs_release_path = (
    'gs://chromeos-releases/{channel}-channel/{board}/{short_version}')

# Assume gsutil is in PATH.
gsutil_bin = 'gsutil'

VERSION_KEY_CROS_SHORT_VERSION = 'cros_short_version'
VERSION_KEY_CROS_FULL_VERSION = 'cros_full_version'
VERSION_KEY_MILESTONE = 'milestone'
VERSION_KEY_CR_VERSION = 'cr_version'
VERSION_KEY_ANDROID_BUILD_ID = 'android_build_id'
VERSION_KEY_ANDROID_BRANCH = 'android_branch'


def is_cros_short_version(s):
  """Determines if `s` is chromeos short version.

  This function doesn't accept version number of local build.
  """
  return bool(re.match(re_chromeos_short_version, s))


def is_cros_localbuild_version(s):
  """Determines if `s` is chromeos local build version."""
  return bool(re.match(re_chromeos_localbuild_version, s))


def is_cros_full_version(s):
  """Determines if `s` is chromeos full version.

  This function doesn't accept version number of local build.
  """
  return bool(re.match(re_chromeos_full_version, s))


def is_cros_version(s):
  """Determines if `s` is chromeos version (either short or full)"""
  return is_cros_short_version(s) or is_cros_full_version(s)


def make_cros_full_version(milestone, short_version):
  """Makes full_version from milestone and short_version"""
  assert milestone
  return 'R%s-%s' % (milestone, short_version)


def version_split(full_version):
  """Splits full_version into milestone and short_version"""
  assert is_cros_full_version(full_version)
  milestone, short_version = full_version.split('-')
  return milestone[1:], short_version


def argtype_cros_version(s):
  if not is_cros_version(s):
    msg = 'invalid cros version'
    raise cli.ArgTypeError(msg, '9876.0.0 or R62-9876.0.0')
  return s


def query_dut_lsb_release(host):
  """Query /etc/lsb-release of given DUT

  Args:
    host: the DUT address

  Returns:
    dict for keys and values of /etc/lsb-release.

  Raises:
    core.ExecutionFatalError: cannot connect to host or lsb-release file
        doesn't exist
  """
  try:
    output = util.check_output('ssh', host, 'cat', '/etc/lsb-release')
  except subprocess.CalledProcessError:
    raise core.ExecutionFatalError('cannot connect to DUT or not a DUT')
  return dict(re.findall(r'^(\w+)=(.*)$', output, re.M))


def is_dut(host):
  """Determines whether a host is a chromeos device.

  Args:
    host: the DUT address

  Returns:
    True if the host is a chromeos device.
  """
  return query_dut_lsb_release(host).get('DEVICETYPE') in [
      'CHROMEBASE',
      'CHROMEBIT',
      'CHROMEBOOK',
      'CHROMEBOX',
      'REFERENCE',
  ]


def query_dut_board(host):
  """Query board name of a given DUT"""
  return query_dut_lsb_release(host).get('CHROMEOS_RELEASE_BOARD')


def query_dut_short_version(host):
  """Query short version of a given DUT.

  This function may return version of local build, which
  is_cros_short_version() is false.
  """
  return query_dut_lsb_release(host).get('CHROMEOS_RELEASE_VERSION')


def query_dut_boot_id(host, connect_timeout=None):
  """Query boot id.

  Args:
    host: DUT address
    connect_timeout: connection timeout

  Returns:
    boot uuid
  """
  cmd = ['ssh']
  if connect_timeout:
    cmd += ['-oConnectTimeout=%d' % connect_timeout]
  cmd += [host, 'cat', '/proc/sys/kernel/random/boot_id']
  return util.check_output(*cmd).strip()


def reboot(host):
  """Reboot a DUT and verify"""
  logger.debug('reboot %s', host)
  boot_id = query_dut_boot_id(host)

  # Depends on timing, ssh may return failure due to broken pipe,
  # so don't check ssh return code.
  util.call('ssh', host, 'reboot')
  wait_reboot_done(host, boot_id)


def wait_reboot_done(host, boot_id):
  # For dev-mode test image, the reboot time is roughly at least 16 seconds
  # (dev screen short delay) or more (long delay).
  time.sleep(15)
  for _ in range(100):
    try:
      # During boot, DUT does not response and thus ssh may hang a while. So
      # set a connect timeout. 3 seconds are enough and 2 are not. It's okay to
      # set tight limit because it's inside retry loop.
      assert boot_id != query_dut_boot_id(host, connect_timeout=3)
      return
    except subprocess.CalledProcessError:
      logger.debug('reboot not ready? sleep wait 1 sec')
      time.sleep(1)

  raise core.ExecutionFatalError('reboot failed?')


def gsutil(*args, **kwargs):
  """gsutil command line wrapper.

  Args:
    args: command line arguments passed to gsutil
    kwargs:
      ignore_errors: if true, return '' for failures, for example 'gsutil ls'
          but the path not found.

  Returns:
    stdout of gsutil

  Raises:
    core.ExecutionFatalError: gsutil failed to run
    subprocess.CalledProcessError: command failed
  """
  stderr_lines = []
  try:
    return util.check_output(
        gsutil_bin, *args, stderr_callback=stderr_lines.append)
  except subprocess.CalledProcessError as e:
    stderr = ''.join(stderr_lines)
    if re.search(r'ServiceException:.* does not have .*access', stderr):
      raise core.ExecutionFatalError(
          'gsutil failed due to permission. ' +
          'Run "%s config" and follow its instruction. ' % gsutil_bin +
          'Fill any string if it asks for project-id')
    if kwargs.get('ignore_errors'):
      return ''
    raise
  except OSError as e:
    if e.errno == errno.ENOENT:
      raise core.ExecutionFatalError(
          'Unable to run %s. gsutil is not installed or not in PATH?' %
          gsutil_bin)
    raise


def gsutil_ls(*args, **kwargs):
  """gsutil ls.

  Args:
    args: arguments passed to 'gsutil ls'
    kwargs: extra parameters, where
      ignore_errors: if true, return empty list instead of raising
          exception, ex. path not found.

  Returns:
    list of 'gsutil ls' result. One element for one line of gsutil output.

  Raises:
    subprocess.CalledProcessError: gsutil failed, usually means path not found
  """
  return gsutil('ls', *args, **kwargs).splitlines()


def query_milestone_by_version(board, short_version):
  """Query milestone by ChromeOS version number.

  Args:
    board: ChromeOS board name
    short_version: ChromeOS version number in short format, ex. 9300.0.0

  Returns:
    ChromeOS milestone number (string). For example, '58' for '9300.0.0'.
    None if failed.
  """
  path = gs_archive_path.format(board=board) + '/R*-' + short_version
  for line in gsutil_ls('-d', path, ignore_errors=True):
    m = re.search(r'/R(\d+)-', line)
    if not m:
      continue
    return m.group(1)

  for channel in ['canary', 'dev', 'beta', 'stable']:
    path = gs_release_path.format(
        channel=channel, board=board, short_version=short_version)
    for line in gsutil_ls(path, ignore_errors=True):
      m = re.search(r'\bR(\d+)-' + short_version, line)
      if not m:
        continue
      return m.group(1)

  logger.error('unable to query milestone of %s for %s', short_version, board)
  return None


def recognize_version(board, version):
  """Recognize ChromeOS version.

  Args:
    board: ChromeOS board name
    version: ChromeOS version number in short or full format

  Returns:
    (milestone, version in short format)
  """
  if is_cros_short_version(version):
    milestone = query_milestone_by_version(board, version)
    short_version = version
  else:
    milestone, short_version = version_split(version)
  return milestone, short_version


def version_to_short(version):
  """Convert ChromeOS version number to short format.

  Args:
    version: ChromeOS version number in short or full format

  Returns:
    version number in short format
  """
  if is_cros_short_version(version):
    return version
  _, short_version = version_split(version)
  return short_version


def version_to_full(board, version):
  """Convert ChromeOS version number to full format.

  Args:
    board: ChromeOS board name
    version: ChromeOS version number in short or full format

  Returns:
    version number in full format
  """
  if is_cros_full_version(version):
    return version
  milestone = query_milestone_by_version(board, version)
  assert milestone
  return make_cros_full_version(milestone, version)


def prepare_prebuilt_image(board, version):
  """Prepare chromeos prebuilt image.

  It searches for xbuddy image which "cros flash" can use, or fetch image to
  local disk.

  Args:
    board: ChromeOS board name
    version: ChromeOS version number in short or full format

  Returns:
    xbuddy path or file path (outside chroot)
  """
  assert is_cros_version(version)
  full_version = version_to_full(board, version)
  short_version = version_to_short(full_version)

  image_path = None
  gs_path = gs_archive_path.format(board=board) + '/' + full_version
  if gsutil_ls('-d', gs_path, ignore_errors=True):
    image_path = 'xbuddy://remote/{board}/{full_version}/test'.format(
        board=board, full_version=full_version)
  else:
    tmp_dir = 'tmp/ChromeOS-test-%s-%s' % (full_version, board)
    if not os.path.exists(tmp_dir):
      os.makedirs(tmp_dir)
    # gs://chromeos-releases may have more old images than
    # gs://chromeos-image-archive, but 'cros flash' doesn't support it. We have
    # to fetch the image by ourselves
    for channel in ['canary', 'dev', 'beta', 'stable']:
      fn = 'ChromeOS-test-{full_version}-{board}.tar.xz'.format(
          full_version=full_version, board=board)
      gs_path = gs_release_path.format(
          channel=channel, board=board, short_version=short_version)
      gs_path += '/' + fn
      if gsutil_ls(gs_path, ignore_errors=True):
        # TODO(kcwu): delete tmp
        gsutil('cp', gs_path, tmp_dir)
        util.check_call('tar', 'Jxvf', fn, cwd=tmp_dir)
        image_path = os.path.abspath(
            os.path.join(tmp_dir, 'chromiumos_test_image.bin'))
        break

  assert image_path
  return image_path


def cros_flash(chromeos_root,
               host,
               board,
               image_path,
               version=None,
               clobber_stateful=False,
               disable_rootfs_verification=True,
               run_inside_chroot=False):
  """Flash a DUT with given ChromeOS image.

  This is implemented by 'cros flash' command line.

  Args:
    chromeos_root: use 'cros flash' of which chromeos tree
    host: DUT address
    board: ChromeOS board name
    image_path: chromeos image xbuddy path or file path. If
        run_inside_chroot is True, the file path is relative to src/scrips.
        Otherwise, the file path is relative to chromeos_root.
    version: ChromeOS version in short or full format
    clobber_stateful: Clobber stateful partition when performing update
    disable_rootfs_verification: Disable rootfs verification after update
        is completed
    run_inside_chroot: if True, run 'cros flash' command inside the chroot
  """
  logger.info('cros_flash %s %s %s %s', host, board, version, image_path)

  # Reboot is necessary because sometimes previous 'cros flash' failed and
  # entered a bad state.
  reboot(host)

  args = ['--no-ping', host, image_path]
  if clobber_stateful:
    args.append('--clobber-stateful')
  if disable_rootfs_verification:
    args.append('--disable-rootfs-verification')

  if run_inside_chroot:
    cros_sdk(chromeos_root, 'cros', 'flash', *args)
  else:
    util.check_call('chromite/bin/cros', 'flash', *args, cwd=chromeos_root)

  if version:
    # In the past, cros flash may fail with returncode=0
    # So let's have an extra check.
    short_version = version_to_short(version)
    dut_version = query_dut_short_version(host)
    assert dut_version == short_version


def version_info(board, version):
  """Query subcomponents version info of given version of ChromeOS

  Args:
    board: ChromeOS board name
    version: ChromeOS version number in short or full format

  Returns:
    dict of component and version info, including (if available):
      cros_short_version: ChromeOS version
      cros_full_version: ChromeOS version
      milestone: milestone of ChromeOS
      cr_version: Chrome version
      android_build_id: Android build id
      android_branch: Android branch, in format like 'git_nyc-mr1-arc'
  """
  info = {}
  full_version = version_to_full(board, version)

  # Some boards may have only partial-metadata.json but no metadata.json.
  # e.g. caroline R60-9462.0.0
  # Let's try both.
  metadata = None
  for metadata_filename in ['metadata.json', 'partial-metadata.json']:
    path = gs_archive_path.format(board=board) + '/%s/%s' % (full_version,
                                                             metadata_filename)
    metadata = gsutil('cat', path, ignore_errors=True)
    if metadata:
      o = json.loads(metadata)
      v = o['version']
      board_metadata = o['board-metadata'][board]
      info.update({
          VERSION_KEY_CROS_SHORT_VERSION: v['platform'],
          VERSION_KEY_CROS_FULL_VERSION: v['full'],
          VERSION_KEY_MILESTONE: v['milestone'],
          VERSION_KEY_CR_VERSION: v['chrome'],
      })

      if 'android' in v:
        info[VERSION_KEY_ANDROID_BUILD_ID] = v['android']
      if 'android-branch' in v:  # this appears since R58-9317.0.0
        info[VERSION_KEY_ANDROID_BRANCH] = v['android-branch']
      elif 'android-container-branch' in board_metadata:
        info[VERSION_KEY_ANDROID_BRANCH] = v['android-container-branch']
      break
  else:
    logger.error('Failed to read metadata from gs://chromeos-image-archive')
    logger.error(
        'Note, so far no quick way to look up version info for too old builds')

  return info


def query_chrome_version(board, version):
  """Queries chrome version of chromeos build.

  Args:
    board: ChromeOS board name
    version: ChromeOS version number in short or full format

  Returns:
    Chrome version number
  """
  info = version_info(board, version)
  return info['cr_version']


def query_android_build_id(board, rev):
  info = version_info(board, rev)
  rev = info['android_build_id']
  return rev


def query_android_branch(board, rev):
  info = version_info(board, rev)
  rev = info['android_branch']
  return rev


def guess_chrome_version(board, rev):
  """Guess chrome version number.

  Args:
    board: chromeos board name
    rev: chrome or chromeos version

  Returns:
    chrome version number
  """
  if is_cros_version(rev):
    assert board, 'need to specify BOARD for cros version'
    rev = query_chrome_version(board, rev)
    assert cr_util.is_chrome_version(rev)

  return rev


def is_inside_chroot():
  """Returns True if we are inside chroot."""
  return os.path.exists('/etc/cros_chroot_version')


def cros_sdk(chromeos_root, *args, **kwargs):
  """Run commands inside chromeos chroot.

  Args:
    chromeos_root: chromeos tree root
    *args: command to run
    **kwargs:
      env: (dict) environment variables for the command
      stdin: standard input file handle for the command
  """
  envs = []
  for k, v in kwargs.get('env', {}).items():
    assert re.match(r'^[A-Za-z_][A-Za-z0-9_]*$', k)
    envs.append('%s=%s' % (k, v))

  # Use --no-ns-pid to prevent cros_sdk change our pgid, otherwise subsequent
  # commands would be considered as background process.
  cmd = ['chromite/bin/cros_sdk', '--no-ns-pid'] + envs + ['--'] + list(args)
  return util.check_output(*cmd, cwd=chromeos_root, stdin=kwargs.get('stdin'))


def copy_into_chroot(chromeos_root, src, dst):
  """Copies file into chromeos chroot.

  Args:
    chromeos_root: chromeos tree root
    src: path outside chroot
    dst: path inside chroot
  """
  # chroot may be an image, so we cannot copy to corresponding path
  # directly.
  cros_sdk(chromeos_root, 'sh', '-c', 'cat > %s' % dst, stdin=open(src))


def exists_in_chroot(chromeos_root, path):
  """Determine whether a path exists in the chroot.

  Args:
    chromeos_root: chromeos tree root
    path: path inside chroot, relative to src/scripts

  Returns:
    True if a path exists
  """
  try:
    cros_sdk(chromeos_root, 'test', '-e', path)
  except subprocess.CalledProcessError:
    return False
  return True


def build_image(chromeos_root, board, rev):
  """Build ChromeOS image.

  Args:
    chromeos_root: chromeos tree root
    board: ChromeOS board name
    rev: the version name to build

  Returns:
    Image path
  """

  # If the given version is already built, reuse it.
  image_name = 'bisect-%s' % rev
  image_path = os.path.join('../build/images', board, image_name,
                            'chromiumos_test_image.bin')
  if exists_in_chroot(chromeos_root, image_path):
    logger.info('"%s" already exists, skip build step', image_path)
    return image_path

  dirname = os.path.dirname(os.path.abspath(__file__))
  script_name = 'build_cros_helper.sh'
  copy_into_chroot(chromeos_root, os.path.join(dirname, '..', script_name),
                   script_name)
  cros_sdk(chromeos_root, 'chmod', '+x', script_name)
  cros_sdk(chromeos_root, './%s' % script_name, board, image_name)
  return image_path


class ChromeOSManifestManager(repo_util.ManifestManager):
  """Manifest operations for chromeos repo"""

  def __init__(self, config):
    self.config = config

  def _query_manifest_name(self, rev):
    assert is_cros_full_version(rev)
    milestone, short_version = version_split(rev)
    manifest_name = os.path.join('buildspecs', milestone,
                                 '%s.xml' % short_version)
    return manifest_name

  def sync_disk_state(self, rev):
    manifest_name = self._query_manifest_name(rev)

    # For ChromeOS, mark_as_stable step requires 'repo init -m', which sticks
    # manifest. 'repo sync -m' is not enough
    repo_util.init(
        self.config['repo_dir'],
        'https://chrome-internal.googlesource.com/chromeos/manifest-versions/',
        manifest_name=manifest_name,
        repo_url='https://chromium.googlesource.com/external/repo.git')

    # Note, don't sync with current_branch=True for chromeos. One of its
    # build steps (inside mark_as_stable) executes "git describe" which
    # needs git tag information.
    repo_util.sync(self.config['repo_dir'])

  def fetch_git_repos(self, rev):
    # Early return if necessary git history are already fetched.
    mf = self.fetch_manifest(rev)
    repo_set = repo_util.parse_repo_set(self.config['repo_dir'], mf)
    all_have = True
    for path, git_rev in repo_set.items():
      git_root = os.path.join(self.config['repo_dir'], path)
      if not os.path.exists(git_root):
        all_have = False
        break
      if not git_util.is_containing_commit(git_root, git_rev):
        all_have = False
        break
    if all_have:
      return

    # TODO(kcwu): fetch git history but don't switch current disk state.
    self.sync_disk_state(rev)

  def enumerate_manifest(self, old, new):
    assert is_cros_full_version(old)
    assert is_cros_full_version(new)
    old_milestone, old_short_version = version_split(old)
    new_milestone, new_short_version = version_split(new)

    # TODO(kcwu): fetch manifests but don't switch current disk state.
    self.sync_disk_state(new)

    spec_dir = os.path.join(self.config['repo_dir'], '.repo', 'manifests',
                            'buildspecs')
    result = []
    for root, dirs, files in os.walk(spec_dir):
      dirs[:] = [
          dn for dn in dirs if dn.isdigit() and
          int(old_milestone) <= int(dn) <= int(new_milestone)
      ]

      for fn in files:
        short_version, ext = os.path.splitext(fn)
        if ext != '.xml':
          continue
        milestone = os.path.basename(root)
        if (util.is_version_lesseq(old_short_version, short_version) and
            util.is_version_lesseq(short_version, new_short_version) and
            util.is_direct_relative_version(short_version, new_short_version)):
          result.append(make_cros_full_version(milestone, short_version))

    def version_key_func(full_version):
      _milestone, short_version = version_split(full_version)
      return util.version_key_func(short_version)

    result.sort(key=version_key_func)
    assert result[0] == old
    assert result[-1] == new
    return result

  def fetch_manifest(self, rev):
    # The manifest is already synced, no need to fetch.
    return self._query_manifest_name(rev)
