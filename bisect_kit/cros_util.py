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
from bisect_kit import util

logger = logging.getLogger(__name__)

re_chromeos_short_version = r'^\d+\.\d+\.\d+$'
re_chromeos_full_version = r'^R\d+-\d+\.\d+\.\d+$'

gs_archive_path = 'gs://chromeos-image-archive/{board}-release'
gs_release_path = (
    'gs://chromeos-releases/{channel}-channel/{board}/{short_version}')

# Assume gsutil is in PATH.
gsutil_bin = 'gsutil'

VERSION_KEY_CROS_SHORT_VERSION = 'cros_short_version'
VERSION_KEY_CROS_FULL_VERSION = 'cros_full_version'
VERSION_KEY_MILESTONE = 'milestone'
VERSION_KEY_CR_VERSION = 'cr_version'
VERSION_KEY_ANDROID_BID = 'android_bid'
VERSION_KEY_ANDROID_BRANCH = 'android_branch'


def is_cros_short_version(s):
  """Determines if `s` is chromeos short version"""
  return bool(re.match(re_chromeos_short_version, s))


def is_cros_full_version(s):
  """Determines if `s` is chromeos full version"""
  return bool(re.match(re_chromeos_full_version, s))


def is_cros_version(s):
  """Determines if `s` is chromeos version (either short or full)"""
  return is_cros_short_version(s) or is_cros_full_version(s)


def make_cros_full_version(milestone, short_version):
  """Makes full_version from milestone and short_version"""
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
    dict for keys and values of /etc/lsb-release. Return empty dict if failed.
  """
  try:
    output = util.check_output('ssh', host, 'cat', '/etc/lsb-release')
  except subprocess.CalledProcessError:
    return {}
  return dict(re.findall(r'^(\w+)=(.*)$', output, re.M))


def is_dut(host):
  """Determines whether a host is a chromeos device

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
  """Query short version of a given DUT"""
  return query_dut_lsb_release(host).get('CHROMEOS_RELEASE_VERSION')


def query_dut_boot_id(host, connect_timeout=None):
  """Query boot id

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

  # For dev-mode test image, the reboot time is roughtly at least 16 seconds
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
  """gsutil command line wrapper

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
  """gsutil ls

  Args:
    args: arguments passed to 'gsutil ls'
    kwargs: extra parameters, where
      ignore_errors: if true, return empty list instead of rasing
          exception, ex. path not found.

  Returns:
    list of 'gsutil ls' result. One element for one line of gsutil output.

  Raises:
    subprocess.CalledProcessError: gsutil failed, usually means path not found
  """
  return gsutil('ls', *args, **kwargs).splitlines()


def query_milestone_by_version(board, short_version):
  """Query milestone by ChromeOS version number

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
  """Recognize ChromeOS version

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
  """Convert ChromeOS version number to short format

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
  """Convert ChromeOS version number to full format

  Args:
    board: ChromeOS board name
    version: ChromeOS version number in short or full format

  Returns:
    version number in full format
  """
  if is_cros_full_version(version):
    return version
  milestone = query_milestone_by_version(board, version)
  return make_cros_full_version(milestone, version)


def cros_flash(chromeos_root,
               host,
               board,
               version,
               clobber_stateful=False,
               disable_rootfs_verification=True):
  """Flash a DUT with ChromeOS image of given version

  This is implemented by 'cros flash' command line.
  This supports more old versions than 'cros flash'.

  Args:
    chromeos_root: use 'cros flash' of which chromeos tree
    host: DUT address
    board: ChromeOS board name
    version: ChromeOS version in short or full format
    clobber_stateful: Clobber stateful partition when performing update
    disable_rootfs_verification: Disable rootfs verification after update
                                 is completed
  """
  logger.info('cros_flash %s %s %s', host, board, version)
  assert is_cros_version(version)

  # Reboot is necessary because sometimes previous 'cros flash' failed and
  # entered a bad state.
  reboot(host)

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
    # gs://chromeos-image-archive, but 'cros flash' dones't support it. We have
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

  args = ['--no-ping', host, image_path]
  if clobber_stateful:
    args.append('--clobber-stateful')
  if disable_rootfs_verification:
    args.append('--disable-rootfs-verification')

  # TODO(kcwu): clear cache of cros flash
  util.check_call('chromite/bin/cros', 'flash', *args, cwd=chromeos_root)

  # In the past, cros flash may fail with returncode=0
  # So let's have an extra check.
  assert query_dut_short_version(host) == short_version


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
      android_bid: Android build id
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
        info[VERSION_KEY_ANDROID_BID] = v['android']
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
  """Queries chrome version of chromeos build

  Args:
    board: ChromeOS board name
    version: ChromeOS version number in short or full format

  Returns:
    Chrome version number
  """
  info = version_info(board, version)
  return info['cr_version']
