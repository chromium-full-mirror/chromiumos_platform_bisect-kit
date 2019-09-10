# -*- coding: utf-8 -*-
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
import ast
import errno
import json
import logging
import os
import re
import subprocess
import time

from bisect_kit import cli
from bisect_kit import codechange
from bisect_kit import cr_util
from bisect_kit import errors
from bisect_kit import git_util
from bisect_kit import locking
from bisect_kit import repo_util
from bisect_kit import util

logger = logging.getLogger(__name__)

re_chromeos_full_version = r'^R\d+-\d+\.\d+\.\d+$'
re_chromeos_localbuild_version = r'^\d+\.\d+\.\d{4}_\d\d_\d\d_\d{4}$'
re_chromeos_short_version = r'^\d+\.\d+\.\d+$'

gs_archive_path = 'gs://chromeos-image-archive/{board}-release'
gs_release_path = (
    'gs://chromeos-releases/{channel}-channel/{boardpath}/{short_version}')

# Assume gsutil is in PATH.
gsutil_bin = 'gsutil'

chromeos_root_inside_chroot = '/mnt/host/source'
# relative to chromeos_root
prebuilt_autotest_dir = 'tmp/autotest-prebuilt'
# Relative to chromeos root. Images are cached_images_dir/$board/$image_name.
cached_images_dir = 'src/build/images'
test_image_filename = 'chromiumos_test_image.bin'

VERSION_KEY_CROS_SHORT_VERSION = 'cros_short_version'
VERSION_KEY_CROS_FULL_VERSION = 'cros_full_version'
VERSION_KEY_MILESTONE = 'milestone'
VERSION_KEY_CR_VERSION = 'cr_version'
VERSION_KEY_ANDROID_BUILD_ID = 'android_build_id'
VERSION_KEY_ANDROID_BRANCH = 'android_branch'


class NeedRecreateChrootException(Exception):
  """Failed to build ChromeOS because of chroot mismatch or corruption"""


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
    errors.SshConnectionError: cannot connect to host
    errors.ExternalError: lsb-release file doesn't exist
  """
  try:
    output = util.ssh_cmd(host, 'cat', '/etc/lsb-release')
  except subprocess.CalledProcessError:
    raise errors.ExternalError('unable to read /etc/lsb-release; not a DUT')
  return dict(re.findall(r'^(\w+)=(.*)$', output, re.M))


def is_dut(host):
  """Determines whether a host is a chromeos device.

  Args:
    host: the DUT address

  Returns:
    True if the host is a chromeos device.
  """
  try:
    return query_dut_lsb_release(host).get('DEVICETYPE') in [
        'CHROMEBASE',
        'CHROMEBIT',
        'CHROMEBOOK',
        'CHROMEBOX',
        'REFERENCE',
    ]
  except (errors.ExternalError, errors.SshConnectionError):
    return False


def is_good_dut(host):
  if not is_dut(host):
    return False

  # Sometimes python is broken after 'cros flash'.
  try:
    util.ssh_cmd(host, 'python', '-c', '1')
    return True
  except (subprocess.CalledProcessError, errors.SshConnectionError):
    return False


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
  return util.ssh_cmd(
      host,
      'cat',
      '/proc/sys/kernel/random/boot_id',
      connect_timeout=connect_timeout).strip()


def reboot(host):
  """Reboot a DUT and verify"""
  logger.debug('reboot %s', host)
  boot_id = query_dut_boot_id(host)

  try:
    util.ssh_cmd(host, 'reboot')
  except errors.SshConnectionError:
    # Depends on timing, ssh may return failure due to broken pipe, which is
    # working as intended. Ignore such kind of errors.
    pass
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
    except errors.SshConnectionError:
      logger.debug('reboot not ready? sleep wait 1 sec')
      time.sleep(1)

  raise errors.ExternalError('reboot failed?')


def gs_release_boardpath(board):
  """Normalizes board name for gs://chromeos-releases/

  This follows behavior of PushImage() in chromite/scripts/pushimage.py
  Note, only gs://chromeos-releases/ needs normalization,
  gs://chromeos-image-archive does not.

  Args:
    board: ChromeOS board name

  Returns:
    normalized board name
  """
  return board.replace('_', '-')


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
    errors.InternalError: gsutil failed to run
    subprocess.CalledProcessError: command failed
  """
  stderr_lines = []
  try:
    return util.check_output(
        gsutil_bin, *args, stderr_callback=stderr_lines.append)
  except subprocess.CalledProcessError as e:
    stderr = ''.join(stderr_lines)
    if re.search(r'ServiceException:.* does not have .*access', stderr):
      raise errors.ExternalError(
          'gsutil failed due to permission. ' +
          'Run "%s config" and follow its instruction. ' % gsutil_bin +
          'Fill any string if it asks for project-id')
    if kwargs.get('ignore_errors'):
      return ''
    raise
  except OSError as e:
    if e.errno == errno.ENOENT:
      raise errors.ExternalError(
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
        channel=channel,
        boardpath=gs_release_boardpath(board),
        short_version=short_version)
    for line in gsutil_ls(path, ignore_errors=True):
      m = re.search(r'\bR(\d+)-' + short_version, line)
      if not m:
        continue
      return m.group(1)

  logger.error('unable to query milestone of %s for %s', short_version, board)
  return None


def list_board_names(chromeos_root):
  """List board names.

  Args:
    chromeos_root: chromeos tree root

  Returns:
    list of board names
  """
  # Following logic is simplified from chromite/lib/portage_util.py
  cros_list_overlays = os.path.join(chromeos_root,
                                    'chromite/bin/cros_list_overlays')
  overlays = util.check_output(cros_list_overlays).splitlines()
  result = set()
  for overlay in overlays:
    conf_file = os.path.join(overlay, 'metadata', 'layout.conf')
    name = None
    if os.path.exists(conf_file):
      for line in open(conf_file):
        m = re.match(r'^repo-name\s*=\s*(\S+)\s*$', line)
        if m:
          name = m.group(1)
          break

    if not name:
      name_file = os.path.join(overlay, 'profiles', 'repo_name')
      if os.path.exists(name_file):
        name = open(name_file).read().strip()

    if name:
      name = re.sub(r'-private$', '', name)
      result.add(name)

  return list(result)


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
  assert milestone, 'incorrect board=%s or version=%s ?' % (board, version)
  return make_cros_full_version(milestone, version)


def list_prebuilt_from_image_archive(board):
  """Lists ChromeOS prebuilt image available from gs://chromeos-image-archive.

  gs://chromeos-image-archive contains only recent builds (in two years).
  We prefer this function to list_prebuilt_from_chromeos_releases() because
    - this is what "cros flash" supports directly.
    - the paths have milestone information, so we don't need to do slow query
      by ourselves.

  Args:
    board: ChromeOS board name

  Returns:
    list of (version, gs_path):
      version: Chrome OS version in full format
      gs_path: gs path of test image
  """
  result = []
  for line in gsutil_ls(gs_archive_path.format(board=board)):
    m = re.match(r'^gs:\S+(R\d+-\d+\.\d+\.\d+)', line)
    if m:
      full_version = m.group(1)
      test_image = 'chromiumos_test_image.tar.xz'
      assert line.endswith('/')
      gs_path = line + test_image
      result.append((full_version, gs_path))
  return result


def list_prebuilt_from_chromeos_releases(board):
  """Lists ChromeOS versions available from gs://chromeos-releases.

  gs://chromeos-releases contains more builds. However, 'cros flash' doesn't
  support it.

  Args:
    board: ChromeOS board name

  Returns:
    list of (version, gs_path):
      version: Chrome OS version in short format
      gs_path: gs path of test image (with wildcard)
  """
  result = []
  for line in gsutil_ls(
      gs_release_path.format(
          channel='*', boardpath=gs_release_boardpath(board), short_version=''),
      ignore_errors=True):
    m = re.match(r'gs:\S+/(\d+\.\d+\.\d+)/$', line)
    if m:
      short_version = m.group(1)
      test_image = 'ChromeOS-test-R*-{short_version}-{board}.tar.xz'.format(
          short_version=short_version, board=board)
      gs_path = line + test_image
      result.append((short_version, gs_path))
  return result


def list_chromeos_prebuilt_versions(board,
                                    old,
                                    new,
                                    only_good_build=True,
                                    include_older_build=True):
  """Lists ChromeOS version numbers with prebuilt between given range

  Args:
    board: ChromeOS board name
    old: start version (inclusive)
    new: end version (inclusive)
    only_good_build: only if test image is available
    include_older_build: include prebuilt in gs://chromeos-releases

  Returns:
    list of sorted version numbers (in full format) between [old, new] range
    (inclusive).
  """
  old = version_to_short(old)
  new = version_to_short(new)

  rev_map = {}  # dict: short version -> (short or full version, gs line)
  for full_version, gs_path in list_prebuilt_from_image_archive(board):
    short_version = version_to_short(full_version)
    rev_map[short_version] = full_version, gs_path

  if include_older_build and old not in rev_map:
    for short_version, gs_path in list_prebuilt_from_chromeos_releases(board):
      if short_version not in rev_map:
        rev_map[short_version] = short_version, gs_path

  result = []
  for rev in sorted(rev_map, key=util.version_key_func):
    if not util.is_direct_relative_version(new, rev):
      continue
    if not util.is_version_lesseq(old, rev):
      continue
    if not util.is_version_lesseq(rev, new):
      continue

    version, gs_path = rev_map[rev]

    # version_to_full() and gsutil_ls() may take long time if versions are a
    # lot. This is acceptable because we usually bisect only short range.

    if only_good_build:
      gs_result = gsutil_ls(gs_path, ignore_errors=True)
      if not gs_result:
        logger.warning('%s is not a good build, ignore', version)
        continue
      assert len(gs_result) == 1
      m = re.search(r'(R\d+-\d+\.\d+\.\d+)', gs_result[0])
      if not m:
        logger.warning('format of image path is unexpected: %s', gs_result[0])
        continue
      version = m.group(1)
    elif is_cros_short_version(version):
      version = version_to_full(board, version)

    result.append(version)

  return result


def prepare_prebuilt_image(chromeos_root, board, version):
  """Prepare chromeos prebuilt image.

  It searches for xbuddy image which "cros flash" can use, or fetch image to
  local disk.

  Args:
    chromeos_root: chromeos tree root
    board: ChromeOS board name
    version: ChromeOS version number in short or full format

  Returns:
    xbuddy path or file path (relative to chromeos_root)
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
    tmp_dir = os.path.join(chromeos_root, 'tmp',
                           'ChromeOS-test-%s-%s' % (full_version, board))
    if not os.path.exists(tmp_dir):
      os.makedirs(tmp_dir)
    # gs://chromeos-releases may have more old images than
    # gs://chromeos-image-archive, but 'cros flash' doesn't support it. We have
    # to fetch the image by ourselves
    for channel in ['canary', 'dev', 'beta', 'stable']:
      fn = 'ChromeOS-test-{full_version}-{board}.tar.xz'.format(
          full_version=full_version, board=board)
      gs_path = gs_release_path.format(
          channel=channel,
          boardpath=gs_release_boardpath(board),
          short_version=short_version)
      gs_path += '/' + fn
      if gsutil_ls(gs_path, ignore_errors=True):
        # TODO(kcwu): delete tmp
        gsutil('cp', gs_path, tmp_dir)
        util.check_call('tar', 'Jxvf', fn, cwd=tmp_dir)
        image_path = os.path.relpath(
            os.path.join(tmp_dir, test_image_filename), chromeos_root)
        break

  assert image_path
  return image_path


def cros_flash(chromeos_root,
               host,
               board,
               image_path,
               version=None,
               clobber_stateful=False,
               disable_rootfs_verification=True):
  """Flash a DUT with given ChromeOS image.

  This is implemented by 'cros flash' command line.

  Args:
    chromeos_root: use 'cros flash' of which chromeos tree
    host: DUT address
    board: ChromeOS board name
    image_path: chromeos image xbuddy path or file path. For relative
        path, it should be relative to chromeos_root.
    version: ChromeOS version in short or full format
    clobber_stateful: Clobber stateful partition when performing update
    disable_rootfs_verification: Disable rootfs verification after update
        is completed

  Raises:
    errors.ExternalError: cros flash failed
  """
  logger.info('cros_flash %s %s %s %s', host, board, version, image_path)

  # Reboot is necessary because sometimes previous 'cros flash' failed and
  # entered a bad state.
  reboot(host)

  # Handle relative path.
  if '://' not in image_path and not os.path.isabs(image_path):
    assert os.path.exists(os.path.join(chromeos_root, image_path))
    image_path = os.path.join(chromeos_root_inside_chroot, image_path)

  args = [
      '--debug', '--no-ping', '--send-payload-in-parallel', host, image_path
  ]
  if clobber_stateful:
    args.append('--clobber-stateful')
  if disable_rootfs_verification:
    args.append('--disable-rootfs-verification')

  try:
    cros_sdk(chromeos_root, 'cros', 'flash', *args)
  except subprocess.CalledProcessError:
    raise errors.ExternalError('cros flash failed')

  if version:
    # In the past, cros flash may fail with returncode=0
    # So let's have an extra check.
    short_version = version_to_short(version)
    dut_version = query_dut_short_version(host)
    if dut_version != short_version:
      raise errors.ExternalError(
          'although cros flash succeeded, the OS version is unexpected: '
          'actual=%s expect=%s' % (dut_version, short_version))

  # "cros flash" may terminate sucessfully but the DUT starts self-repairing
  # (b/130786578), so it's necessary to do sanity check.
  if not is_good_dut(host):
    raise errors.ExternalError(
        'although cros flash succeeded, the DUT is in bad state')


def cros_flash_with_retry(chromeos_root,
                          host,
                          board,
                          image_path,
                          version=None,
                          clobber_stateful=False,
                          disable_rootfs_verification=True,
                          repair_callback=None):
  # 'cros flash' is not 100% reliable, retry if necessary.
  for attempt in range(2):
    if attempt > 0:
      logger.info('will retry 60 seconds later')
      time.sleep(60)

    try:
      cros_flash(
          chromeos_root,
          host,
          board,
          image_path,
          version=version,
          clobber_stateful=clobber_stateful,
          disable_rootfs_verification=disable_rootfs_verification)
      return True
    except errors.ExternalError:
      logger.exception('cros flash failed')
      if repair_callback and not repair_callback(host):
        logger.warning('not repaired, assume it is harmless')
      continue
  return False


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
      chrome_root: pass to cros_sdk; mount this path into the SDK chroot
      env: (dict) environment variables for the command
      log_stdout: Whether write the stdout output of the child process to log.
      stdin: standard input file handle for the command
      stderr_callback: Callback function for stderr. Called once per line.
      goma_dir: Goma installed directory to mount into the chroot
  """
  envs = []
  for k, v in kwargs.get('env', {}).items():
    assert re.match(r'^[A-Za-z_][A-Za-z0-9_]*$', k)
    envs.append('%s=%s' % (k, v))

  # Use --no-ns-pid to prevent cros_sdk change our pgid, otherwise subsequent
  # commands would be considered as background process.
  prefix = ['chromite/bin/cros_sdk', '--no-ns-pid']

  if kwargs.get('chrome_root'):
    prefix += ['--chrome_root', kwargs['chrome_root']]
  if kwargs.get('goma_dir'):
    prefix += ['--goma_dir', kwargs['goma_dir']]

  prefix += envs + ['--']

  # In addition to the output of command we are interested, cros_sdk may
  # generate its own messages. For example, chroot creation messages if we run
  # cros_sdk the first time.
  # This is the hack to run dummy command once, so we can get clean output for
  # the command we are interested.
  cmd = prefix + ['true']
  util.check_call(*cmd, cwd=chromeos_root)

  cmd = prefix + list(args)
  return util.check_output(
      *cmd,
      cwd=chromeos_root,
      log_stdout=kwargs.get('log_stdout', True),
      stdin=kwargs.get('stdin'),
      stderr_callback=kwargs.get('stderr_callback'))


def create_chroot(chromeos_root):
  """Creates ChromeOS chroot.

  Args:
    chromeos_root: chromeos tree root
  """
  if os.path.exists(os.path.join(chromeos_root, 'chroot')):
    return
  if os.path.exists(os.path.join(chromeos_root, 'chroot.img')):
    return

  util.check_output('chromite/bin/cros_sdk', '--create', cwd=chromeos_root)


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


def check_if_need_recreate_chroot(stdout, stderr):
  """Analyze build log and determine if chroot should be recreated.

  Args:
    stdout: stdout output of build
    stderr: stderr output of build

  Returns:
    the reason if chroot needs recreated; None otherwise
  """
  if re.search(
      r"The current version of portage supports EAPI '\d+'. "
      'You must upgrade', stderr):
    return 'EAPI version mismatch'

  if 'Chroot is too new. Consider running:' in stderr:
    return 'chroot version is too new'

  # old message before Oct 2018
  if 'Chroot version is too new. Consider running cros_sdk --replace' in stderr:
    return 'chroot version is too new'

  # https://groups.google.com/a/chromium.org/forum/#!msg/chromium-os-dev/uzwT5APspB4/NFakFyCIDwAJ
  if "undefined reference to 'std::__1::basic_string" in stdout:
    return 'might be due to compiler change'

  return None


def build_packages(chromeos_root,
                   board,
                   chrome_root=None,
                   goma_dir=None,
                   afdo_use=False):
  """Build ChromeOS packages.

  Args:
    chromeos_root: chromeos tree root
    board: ChromeOS board name
    chrome_root: Chrome tree root. If specified, build chrome using the
        provided tree
    goma_dir: Goma installed directory to mount into the chroot. If specified,
        build chrome with goma.
    afdo_use: build chrome with AFDO optimization
  """
  common_env = {
      'USE': '-cros-debug chrome_internal',
      'FEATURES': 'separatedebug',
  }
  stderr_lines = []
  try:
    with locking.lock_file(locking.LOCK_FILE_FOR_BUILD):
      env = common_env.copy()
      env['FEATURES'] += ' -separatedebug splitdebug'
      cros_sdk(
          chromeos_root,
          './update_chroot',
          '--toolchain_boards',
          board,
          env=env,
          stderr_callback=stderr_lines.append)

      env = common_env.copy()
      cmd = [
          './build_packages',
          '--board',
          board,
          '--withdev',
          '--noworkon',
          '--skip_chroot_upgrade',
          '--accept_licenses=@CHROMEOS',
      ]
      if goma_dir:
        # Tell build_packages to start and stop goma
        cmd.append('--run_goma')
        env['USE_GOMA'] = 'true'
      if afdo_use:
        env['USE'] += ' afdo_use'
      cros_sdk(
          chromeos_root,
          *cmd,
          env=env,
          chrome_root=chrome_root,
          stderr_callback=stderr_lines.append,
          goma_dir=goma_dir)
  except subprocess.CalledProcessError as e:
    # Detect failures due to incompatibility between chroot and source tree. If
    # so, notify the caller to recreate chroot and retry.
    reason = check_if_need_recreate_chroot(e.output, ''.join(stderr_lines))
    if reason:
      raise NeedRecreateChrootException(reason)

    # For other failures, don't know how to handle. Just bail out.
    raise


def build_image(chromeos_root, board):
  """Build ChromeOS image.

  Args:
    chromeos_root: chromeos tree root
    board: ChromeOS board name

  Returns:
    image folder; relative to chromeos_root
  """
  stderr_lines = []
  try:
    with locking.lock_file(locking.LOCK_FILE_FOR_BUILD):
      cros_sdk(
          chromeos_root,
          './build_image',
          '--board',
          board,
          '--noenable_rootfs_verification',
          'test',
          env={
              'USE': '-cros-debug chrome_internal',
              'FEATURES': 'separatedebug',
          },
          stderr_callback=stderr_lines.append)
  except subprocess.CalledProcessError as e:
    # Detect failures due to incompatibility between chroot and source tree. If
    # so, notify the caller to recreate chroot and retry.
    reason = check_if_need_recreate_chroot(e.output, ''.join(stderr_lines))
    if reason:
      raise NeedRecreateChrootException(reason)

    # For other failures, don't know how to handle. Just bail out.
    raise

  image_symlink = os.path.join(chromeos_root, cached_images_dir, board,
                               'latest')
  assert os.path.exists(image_symlink)
  image_name = os.readlink(image_symlink)
  image_folder = os.path.join(cached_images_dir, board, image_name)
  assert os.path.exists(
      os.path.join(chromeos_root, image_folder, test_image_filename))
  return image_folder


class AutotestControlInfo(object):
  """Parsed content of autotest control file.

  Attributes:
    name: test name
    path: control file path
    variables: dict of top-level control variables. Sample keys: NAME, AUTHOR,
        DOC, ATTRIBUTES, DEPENDENCIES, etc.
  """

  def __init__(self, path, variables):
    self.name = variables['NAME']
    self.path = path
    self.variables = variables


def parse_autotest_control_file(path):
  """Parses autotest control file.

  This only parses simple top-level string assignments.

  Returns:
    AutotestControlInfo object
  """
  variables = {}
  code = ast.parse(open(path).read())
  for stmt in code.body:
    # Skip if not simple "NAME = *" assignment.
    if not (isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and
            isinstance(stmt.targets[0], ast.Name)):
      continue

    # Only support string value.
    if isinstance(stmt.value, ast.Str):
      variables[stmt.targets[0].id] = stmt.value.s

  return AutotestControlInfo(path, variables)


def enumerate_autotest_control_files(autotest_dir):
  """Enumerate autotest control files.

  Args:
    autotest_dir: autotest folder

  Returns:
    list of paths to control files
  """
  # Where to find control files. Relative to autotest_dir.
  subpaths = [
      'server/site_tests',
      'client/site_tests',
      'server/tests',
      'client/tests',
  ]

  blacklist = ['site-packages', 'venv', 'results', 'logs', 'containers']
  result = []
  for subpath in subpaths:
    path = os.path.join(autotest_dir, subpath)
    for root, dirs, files in os.walk(path):

      for black in blacklist:
        if black in dirs:
          dirs.remove(black)

      for filename in files:
        if filename == 'control' or filename.startswith('control.'):
          result.append(os.path.join(root, filename))

  return result


def get_autotest_test_info(autotest_dir, test_name):
  """Get metadata of given test.

  Args:
    autotest_dir: autotest folder
    test_name: test name

  Returns:
    AutotestControlInfo object. None if test not found.
  """
  for control_file in enumerate_autotest_control_files(autotest_dir):
    info = parse_autotest_control_file(control_file)
    if info.name == test_name:
      return info
  return None


class ChromeOSSpecManager(codechange.SpecManager):
  """Repo manifest related operations.

  This class enumerates chromeos manifest files, parses them,
  and sync to disk state according to them.
  """

  def __init__(self, config):
    self.config = config
    self.manifest_dir = os.path.join(self.config['chromeos_root'], '.repo',
                                     'manifests')
    self.historical_manifest_git_dir = os.path.join(
        self.config['chromeos_mirror'], 'chromeos/manifest-versions.git')
    if not os.path.exists(self.historical_manifest_git_dir):
      raise errors.InternalError('Manifest snapshots should be cloned into %s' %
                                 self.historical_manifest_git_dir)

  def lookup_build_timestamp(self, rev):
    assert is_cros_full_version(rev)

    milestone, short_version = version_split(rev)
    path = os.path.join('buildspecs', milestone, short_version + '.xml')
    try:
      timestamp = git_util.get_commit_time(self.historical_manifest_git_dir,
                                           'refs/heads/master', path)
    except ValueError:
      raise errors.InternalError(
          '%s does not have %s' % (self.historical_manifest_git_dir, path))
    return timestamp

  def collect_float_spec(self, old, new):
    old_timestamp = self.lookup_build_timestamp(old)
    new_timestamp = self.lookup_build_timestamp(new)

    path = os.path.join(self.manifest_dir, 'default.xml')
    if not os.path.islink(path) or os.readlink(path) != 'full.xml':
      raise errors.InternalError(
          'default.xml not symlink to full.xml is not supported')

    result = []
    path = 'full.xml'
    parser = repo_util.ManifestParser(self.manifest_dir)
    for timestamp, git_rev in parser.enumerate_manifest_commits(
        old_timestamp, new_timestamp, path):
      result.append(
          codechange.Spec(codechange.SPEC_FLOAT, git_rev, timestamp, path))
    return result

  def collect_fixed_spec(self, old, new):
    assert is_cros_full_version(old)
    assert is_cros_full_version(new)
    old_milestone, old_short_version = version_split(old)
    new_milestone, new_short_version = version_split(new)

    result = []
    for milestone in git_util.list_dir_from_revision(
        self.historical_manifest_git_dir, 'refs/heads/master', 'buildspecs'):
      if not milestone.isdigit():
        continue
      if not int(old_milestone) <= int(milestone) <= int(new_milestone):
        continue

      files = git_util.list_dir_from_revision(
          self.historical_manifest_git_dir, 'refs/heads/master',
          os.path.join('buildspecs', milestone))

      for fn in files:
        path = os.path.join('buildspecs', milestone, fn)
        short_version, ext = os.path.splitext(fn)
        if ext != '.xml':
          continue
        if (util.is_version_lesseq(old_short_version, short_version) and
            util.is_version_lesseq(short_version, new_short_version) and
            util.is_direct_relative_version(short_version, new_short_version)):
          rev = make_cros_full_version(milestone, short_version)
          timestamp = git_util.get_commit_time(self.historical_manifest_git_dir,
                                               'refs/heads/master', path)
          result.append(
              codechange.Spec(codechange.SPEC_FIXED, rev, timestamp, path))

    def version_key_func(spec):
      _milestone, short_version = version_split(spec.name)
      return util.version_key_func(short_version)

    result.sort(key=version_key_func)
    assert result[0].name == old
    assert result[-1].name == new
    return result

  def get_manifest(self, rev):
    assert is_cros_full_version(rev)
    milestone, short_version = version_split(rev)
    path = os.path.join('buildspecs', milestone, '%s.xml' % short_version)
    manifest = git_util.get_file_from_revision(self.historical_manifest_git_dir,
                                               'refs/heads/master', path)

    manifest_name = 'manifest_%s.xml' % rev
    manifest_path = os.path.join(self.manifest_dir, manifest_name)
    with open(manifest_path, 'w') as f:
      f.write(manifest)

    return manifest_name

  def parse_spec(self, spec):
    parser = repo_util.ManifestParser(self.manifest_dir)
    if spec.spec_type == codechange.SPEC_FIXED:
      manifest_name = self.get_manifest(spec.name)
      manifest_path = os.path.join(self.manifest_dir, manifest_name)
      content = open(manifest_path).read()
      root = parser.parse_single_xml(content, allow_include=False)
    else:
      root = parser.parse_xml_recursive(spec.name, spec.path)

    spec.entries = parser.process_parsed_result(root)
    if spec.spec_type == codechange.SPEC_FIXED:
      if not spec.is_static():
        raise ValueError(
            'fixed spec %r has unexpected floating entries' % spec.name)

  def sync_disk_state(self, rev):
    manifest_name = self.get_manifest(rev)

    # For ChromeOS, mark_as_stable step requires 'repo init -m', which sticks
    # manifest. 'repo sync -m' is not enough
    repo_util.init(
        self.config['chromeos_root'],
        'https://chrome-internal.googlesource.com/chromeos/manifest-internal',
        manifest_name=manifest_name,
        repo_url='https://chromium.googlesource.com/external/repo.git',
        reference=self.config['chromeos_mirror'],
    )

    # Note, don't sync with current_branch=True for chromeos. One of its
    # build steps (inside mark_as_stable) executes "git describe" which
    # needs git tag information.
    repo_util.sync(self.config['chromeos_root'])
