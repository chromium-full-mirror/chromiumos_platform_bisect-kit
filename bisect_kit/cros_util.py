# -*- coding: utf-8 -*-
# Copyright 2017 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""ChromeOS utility.

Terminology used in this module.
  short_version: ChromeOS version number without milestone, like "9876.0.0".
  full_version: ChromeOS version number with milestone, like "R62-9876.0.0".
  snapshot_version: ChromeOS version number with milestone and snapshot id,
                    like "R62-9876.0.0-12345".
  version: if not specified, it could be in short or full format.
"""

from __future__ import print_function
import ast
import calendar
import datetime
import enum
import errno
import glob
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse

from google.protobuf import json_format

from bisect_kit import buildbucket_util
from bisect_kit import cli
from bisect_kit import codechange
from bisect_kit import common
from bisect_kit import cr_util
from bisect_kit import errors
from bisect_kit import git_util
from bisect_kit import locking
from bisect_kit import repo_util
from bisect_kit import util

logger = logging.getLogger(__name__)

re_chromeos_full_version = r'^R\d+-\d+\.\d+\.\d+$'
re_chromeos_short_version = r'^\d+\.\d+\.\d+$'
re_chromeos_snapshot_version = r'^R\d+-\d+\.\d+\.\d+-\d+$'

gs_archive_base = 'gs://chromeos-image-archive/'
gs_archive_path = 'gs://chromeos-image-archive/{board}-release'

# Assume gsutil is in PATH.
gsutil_bin = 'gsutil'

# Since snapshots with version >= 12618.0.0 have android and chrome version
# info.
snapshot_cutover_version = '12618.0.0'

# http://crbug.com/1170601, small snapshot ids should be ignored
# 21000 is R80-12617.0.0
snapshot_cutover_id = 21000

# current earliest buildbucket buildable versions
# picked from https://crrev.com/c/2072618
buildbucket_cutover_versions = [
    '12931.0.0',
    '12871.26.0',  # R81
    '12871.24.2',  # stabilize-12871.24.B
    '12812.10.0',  # factory-excelsior-12812.B
    '12768.14.0',  # firmware-servo-12768.B
    '12739.85.0',  # R80
    '12739.67.1',  # stabilize-excelsior-12739.67.B
    '12692.36.0',  # factory-hatch-12692.B
    '12672.104.0',  # firmware-hatch-12672.B
    '12607.110.0',  # R79
    '12607.83.2',  # stabilize-quickfix-12607.83.B
    '12587.59.0',  # factory-kukui-12587.B
    '12573.78.0',  # firmware-kukui-12573.B
    '12499.96.0',  # R78
    '12422.33.0',  # firmware-mistral-12422.B
    '12371.190.0',  # R77
    '12361.38.0',  # factory-mistral-12361.B
    '12200.65.0',  # firmware-sarien-12200.B
    '12105.128.0',  # R75
    '12033.82.0',  # factory-sarien-12033.B
]

chromeos_root_inside_chroot = '/mnt/host/source'
# relative to chromeos_root
in_tree_autotest_dir = 'src/third_party/autotest/files'
prebuilt_autotest_dir = 'tmp/autotest-prebuilt'
prebuilt_tast_dir = 'tmp/tast-prebuilt'
# Relative to chromeos root. Images are build_images_dir/$board/$image_name.
build_images_dir = 'src/build/images'
cached_images_dir = 'tmp/images'
test_image_filename = 'chromiumos_test_image.bin'
sample_partition_filename = 'full_dev_part_KERN.bin.gz'

VERSION_KEY_CROS_SHORT_VERSION = 'cros_short_version'
VERSION_KEY_CROS_FULL_VERSION = 'cros_full_version'
VERSION_KEY_MILESTONE = 'milestone'
VERSION_KEY_CR_VERSION = 'cr_version'
VERSION_KEY_ANDROID_BUILD_ID = 'android_build_id'
VERSION_KEY_ANDROID_BRANCH = 'android_branch'
CROSLAND_URL_TEMPLATE = 'https://crosland.corp.google.com/log/%s..%s'

autotest_shadow_config = """
[CROS]
enable_ssh_tunnel_for_servo: True
enable_ssh_tunnel_for_chameleon: True
enable_ssh_connection_for_devserver: True
enable_ssh_tunnel_for_moblab: True
"""


class ImageType(enum.Enum):
  """Chrome OS image type

  It describes the image format, not image location.
  """
  # Full disk image like chromiumos_test_image.bin and
  # chromiumos_test_image.tar.xz.
  # Supported by 'cros flash'.
  DISK_IMAGE = enum.auto()
  # Contains files like full_dev_part_KERN.bin.gz.
  # Supported by quick-provision and newer 'cros flash'.
  PARTITION_IMAGE = enum.auto()
  # Contains files like image.zip. We need to unzip first.
  ZIP_FILE = enum.auto()


class ImageInfo(dict):
  """Image info (dict: image type -> path).

  For a given Chrome OS version, there are several image formats available.
  This class describes a collection of images for a certain Chrome OS version.
  cros_flash() or quick_provision() can resolve a compatible image from this
  object. `image type` is an ImageType enum. `path` could be a path on the
  local disk or a remote URI.
  """


class NeedRecreateChrootException(Exception):
  """Failed to build ChromeOS because of chroot mismatch or corruption"""


def is_cros_short_version(s):
  """Determines if `s` is chromeos short version.

  This function doesn't accept version number of local build.
  """
  return bool(re.match(re_chromeos_short_version, s))


def is_cros_full_version(s):
  """Determines if `s` is chromeos full version.

  This function doesn't accept version number of local build.
  """
  return bool(re.match(re_chromeos_full_version, s))


def is_cros_version(s):
  """Determines if `s` is chromeos version (either short or full)"""
  return is_cros_short_version(s) or is_cros_full_version(s)


def is_cros_snapshot_version(s):
  """Determines if `s` is chromeos snapshot version"""
  return bool(re.match(re_chromeos_snapshot_version, s))


def is_cros_version_lesseq(ver1, ver2):
  """Determines if ver1 is less or equal to ver2.

  Args:
    ver1: a Chrome OS version in short, full, or snapshot format.
    ver2: a Chrome OS version in short, full, or snapshot format.

  Returns:
    True if ver1 is less or equal to ver2.
  """
  assert is_cros_version(ver1) or is_cros_snapshot_version(ver1)
  assert is_cros_version(ver2) or is_cros_snapshot_version(ver2)

  # Compare milestone if available.
  m1 = re.match(r'R(\d+)', ver1)
  m2 = re.match(r'R(\d+)', ver2)
  if m1 and m2 and int(m1.group(1)) > int(m2.group(1)):
    return False

  ver1 = [int(x) for x in re.split(r'[.-]', ver1) if not x.startswith('R')]
  ver2 = [int(x) for x in re.split(r'[.-]', ver2) if not x.startswith('R')]
  return ver1 <= ver2


def is_ancestor_version(ver1, ver2):
  """Determines `ver1` version is ancestor of `ver2` version.

  Returns:
    True only if `ver1` is the ancestor of `ver2`. One version is not considered
    as ancestor of itself.
  """
  assert is_cros_version(ver1) or is_cros_snapshot_version(ver1)
  assert is_cros_version(ver2) or is_cros_snapshot_version(ver2)

  if is_cros_version_lesseq(ver2, ver1):  # pylint: disable=arguments-out-of-order
    return False
  if not is_cros_version_lesseq(ver1, ver2):
    return False

  if not util.is_direct_relative_version(
      version_to_short(ver1), version_to_short(ver2)):
    return False

  # Compare snapshot id if available.
  if is_cros_snapshot_version(ver1) and is_cros_snapshot_version(ver2):
    _, short_1, snapshot_1 = snapshot_version_split(ver1)
    _, short_2, snapshot_2 = snapshot_version_split(ver2)
    if short_1 == short_2 and snapshot_1 >= snapshot_2:
      return False

  return True


def is_buildbucket_buildable(version):
  """Determines if a version is buildable on buildbucket."""
  short_version = version_to_short(version)
  # If given version is child of any cutover, then it's buildable
  return any([
      util.is_direct_relative_version(x, short_version) and
      is_cros_version_lesseq(x, version) for x in buildbucket_cutover_versions
  ])


def make_cros_full_version(milestone, short_version):
  """Makes full_version from milestone and short_version"""
  assert milestone
  return 'R%s-%s' % (milestone, short_version)


def make_cros_snapshot_version(milestone, short_version, snapshot_id):
  """Makes snapshot version from milestone, short_version and snapshot id"""
  return 'R%s-%s-%s' % (milestone, short_version, snapshot_id)


def version_split(version):
  """Splits full_version or snapshot_version into milestone and short_version"""
  assert is_cros_full_version(version) or is_cros_snapshot_version(version)
  if is_cros_snapshot_version(version):
    return snapshot_version_split(version)[0:2]
  milestone, short_version = version.split('-')
  return milestone[1:], short_version


def snapshot_version_split(snapshot_version):
  """Splits snapshot_version into milestone, short_version and snapshot_id"""
  assert is_cros_snapshot_version(snapshot_version)
  milestone, short_version, snapshot_id = snapshot_version.split('-')
  return milestone[1:], short_version, snapshot_id


def query_snapshot_buildbucket_id(board, snapshot_version):
  """Query buildbucket id of a snapshot"""
  assert is_cros_snapshot_version(snapshot_version)
  path = ('gs://chromeos-image-archive/{board}-snapshot'
          '/{snapshot_version}-*/image.zip')
  output = gsutil_ls(
      '-d',
      path.format(board=board, snapshot_version=snapshot_version),
      ignore_errors=True)
  for line in output:
    m = re.match(r'.*-snapshot/R\d+-\d+\.\d+\.\d+-\d+-(.+)/image\.zip', line)
    if m:
      return m.group(1)
  return None


def argtype_cros_version(s):
  if (not is_cros_version(s)) and (not is_cros_snapshot_version(s)):
    msg = 'invalid cros version'
    raise cli.ArgTypeError(msg, '9876.0.0, R62-9876.0.0 or R77-12369.0.0-11681')
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
    output = util.ssh_cmd(host, 'cat', '/etc/lsb-release', allow_retry=True)
  except subprocess.CalledProcessError as e:
    raise errors.ExternalError(
        'unable to read /etc/lsb-release; not a DUT') from e
  return dict(re.findall(r'^(\w+)=(.*)$', output, re.M))


def query_dut_os_release(host):
  """Query /etc/os-release of given DUT

  Args:
    host: the DUT address

  Returns:
    dict for keys and values of /etc/os-release.

  Raises:
    errors.SshConnectionError: cannot connect to host
    errors.ExternalError: lsb-release file doesn't exist
  """
  try:
    output = util.ssh_cmd(host, 'cat', '/etc/os-release', allow_retry=True)
  except subprocess.CalledProcessError as e:
    raise errors.ExternalError(
        'unable to read /etc/os-release; not a DUT') from e
  return dict(re.findall(r'^(\w+)=(.*)$', output, re.M))


def is_dut(host):
  """Determines whether a host is a chromeos device.

  Args:
    host: the DUT address

  Returns:
    True if the host is a chromeos device.
  """
  try:
    return query_dut_os_release(host).get('ID') in [
        'chromiumos',
        'chromeos',
    ]
  except (errors.ExternalError, errors.SshConnectionError):
    return False


def is_good_dut(host):
  if not is_dut(host):
    return False

  # Sometimes python is broken after 'cros flash'.
  try:
    util.ssh_cmd(host, 'python', '-c', '1', allow_retry=True)
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


def query_dut_prebuilt_version(host):
  """Return a snapshot version or short version of a given DUT.

  Args:
    host: dut host

  Returns:
    Snapshot version or short version.
  """
  lsb_release = query_dut_lsb_release(host)
  release_version = lsb_release.get('CHROMEOS_RELEASE_VERSION')
  builder_path = lsb_release.get('CHROMEOS_RELEASE_BUILDER_PATH', '')
  match = re.match(r'\S+-(?:snapshot|postsubmit)/(R\d+-\d+\.\d+\.\d+-\d+)-\d+',
                   builder_path)
  if match:
    return match.group(1)
  return release_version


def query_dut_is_by_official_builder(host):
  """Query if given DUT is build by official builder"""
  build_type = query_dut_lsb_release(host).get('CHROMEOS_RELEASE_BUILD_TYPE',
                                               '')
  build_type = build_type.split(' - ')[0]
  assert build_type in ('Official Build', 'Continuous Builder',
                        'Developer Build',
                        'Test Build'), 'unknown build type (%s)' % build_type
  return build_type in ['Official Build', 'Continuous Builder']


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


def reboot(host, force_reboot_callback=None):
  """Reboot a DUT and verify.

  Args:
    host: DUT address
    force_reboot_callback: powerful reboot hook (via servo). This will be
        invoked if normal reboot failed.
  """
  logger.debug('reboot %s', host)
  boot_id = None
  try:
    boot_id = query_dut_boot_id(host)

    try:
      util.ssh_cmd(host, 'reboot')
    except errors.SshConnectionError:
      # Depends on timing, ssh may return failure due to broken pipe, which is
      # working as intended. Ignore such kind of errors.
      pass

    wait_reboot_done(host, boot_id)
  except (errors.SshConnectionError, errors.ExternalError):
    if force_reboot_callback and force_reboot_callback(host):
      wait_reboot_done(host, boot_id)
      return
    raise


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
    errors.ExternalError: gsutil failed to run
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


def gsutil_stat_creation_time(*args, **kwargs):
  """Returns the creation time of a file or multiple files.

  Args:
    args: arguments passed to 'gsutil stat'.
    kwargs: extra parameters for gsutil.

  Returns:
    A integer indicates the creation timestamp.

  Raises:
    subprocess.CalledProcessError: gsutil failed, usually means path not found
    errors.ExternalError: creation time is not found
  """
  result = -1
  # Currently we believe stat always returns a UTC time, and strptime also
  # parses a UTC time by default.
  time_format = '%a, %d %b %Y %H:%M:%S GMT'

  for line in gsutil('stat', *args, **kwargs).splitlines():
    if ':' not in line:
      continue
    key, value = line.split(':', 1)
    key, value = key.strip(), value.strip()
    if key != 'Creation time':
      continue
    dt = datetime.datetime.strptime(value, time_format)
    unixtime = int(calendar.timegm(dt.utctimetuple()))
    result = max(result, unixtime)

  if result == -1:
    raise errors.ExternalError("didn't find creation time")
  return result


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

  logger.debug('unable to query milestone of %s for %s', short_version, board)
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
        with open(name_file) as f:
          name = f.read().strip()

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


def extract_major_version(version):
  """Converts a version to its major version.

  Args:
    version: ChromeOS version number or snapshot version

  Returns:
    major version number in string format
  """
  version = version_to_short(version)
  m = re.match(r'^(\d+)\.\d+\.\d+$', version)
  return m.group(1)


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
  if is_cros_snapshot_version(version):
    milestone, short_version, _ = snapshot_version_split(version)
    return make_cros_full_version(milestone, short_version)
  if is_cros_full_version(version):
    return version
  milestone = query_milestone_by_version(board, version)
  if not milestone:
    raise errors.ExternalError('incorrect board=%s or version=%s ?' %
                               (board, version))
  return make_cros_full_version(milestone, version)


def list_snapshots_from_image_archive(board, major_version):
  """List ChromeOS snapshot image available from gs://chromeos-image-archive.

  Args:
    board: ChromeOS board
    major_version: ChromeOS major version

  Returns:
    list of (version, gs_path):
      version: Chrome OS snapshot version
      gs_path: gs path of test image
  """

  def extract_snapshot_id(result):
    m = re.match(r'^R\d+-\d+\.\d+\.\d+-(\d+)', result[0])
    assert m
    return int(m.group(1))

  short_version = '%s.0.0' % major_version
  milestone = query_milestone_by_version(board, short_version)
  if not milestone:
    milestone = '*'

  path = ('gs://chromeos-image-archive/{board}-snapshot/R{milestone}-'
          '{short_version}-*/image.zip')
  result = []
  output = gsutil_ls(
      path.format(
          board=board, milestone=milestone, short_version=short_version),
      ignore_errors=True)

  for gs_path in sorted(output):
    m = re.match(r'^gs:\S+(R\d+-\d+\.\d+\.\d+-\d+)', gs_path)
    if m:
      snapshot_version = m.group(1)
      # we should skip if there is duplicate snapshot
      if result and result[-1][0] == snapshot_version:
        continue

      _, _, snapshot_id = snapshot_version_split(snapshot_version)

      # crbug/1170601: ignore small snapshot ids
      if int(snapshot_id) <= snapshot_cutover_id:
        continue

      # b/151054108: snapshot version in [29288, 29439] is broken
      if 29288 <= int(snapshot_id) <= 29439:
        continue

      result.append((snapshot_version, gs_path))

  # sort by its snapshot_id
  result.sort(key=extract_snapshot_id)
  return result


def list_prebuilt_from_image_archive(board):
  """Lists ChromeOS prebuilt image available from gs://chromeos-image-archive.

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


def has_test_image(board, version):
  if is_cros_snapshot_version(version):
    return bool(query_snapshot_buildbucket_id(board, version))

  try:
    full_version = version_to_full(board, version)
  except errors.ExternalError:
    # version_to_full() is implemented by checking image, thus its failure
    # means no image.
    return False
  path = (
      gs_archive_path.format(board=board) +
      '/%s/chromiumos_test_image.tar.xz' % full_version)

  if gsutil_ls(path, ignore_errors=True):
    return True
  return False


def list_chromeos_prebuilt_versions(board,
                                    old,
                                    new,
                                    only_good_build=True,
                                    use_snapshot=False):
  """Lists ChromeOS version numbers with prebuilt between given range

  Args:
    board: ChromeOS board name
    old: start version (inclusive)
    new: end version (inclusive)
    only_good_build: only if test image is available
    use_snapshot: return snapshot versions if found

  Returns:
    list of sorted version numbers (in full format) between [old, new] range
    (inclusive).
  """
  old_short = version_to_short(old)
  new_short = version_to_short(new)

  rev_map = {
  }  # dict: short version -> list of (short/full or snapshot version, gs path)
  for full_version, gs_path in list_prebuilt_from_image_archive(board):
    short_version = version_to_short(full_version)
    rev_map[short_version] = [(full_version, gs_path)]

  if use_snapshot:
    for major_version in range(
        int(extract_major_version(old)),
        int(extract_major_version(new)) + 1):
      short_version = '%s.0.0' % major_version
      next_short_version = '%s.0.0' % (major_version + 1)
      # If current version is smaller than cutover, ignore it as it might not
      # contain enough information for continuing android and chrome bisection.
      if not util.is_version_lesseq(snapshot_cutover_version, short_version):
        continue

      # Given the fact that snapshots are images between two release versions.
      # Adding snapshots of 12345.0.0 should be treated as adding commits
      # between [12345.0.0, 12346.0.0).
      # So in the following lines we check two facts:
      # 1) If 12346.0.0(next_short_version) is a version between old and new
      if not util.is_direct_relative_version(next_short_version, old_short):
        continue
      if not util.is_direct_relative_version(next_short_version, new_short):
        continue
      # 2) If 12345.0.0(short_version) is a version between old and new
      if not util.is_direct_relative_version(short_version, old_short):
        continue
      if not util.is_direct_relative_version(short_version, new_short):
        continue

      snapshots = list_snapshots_from_image_archive(board, str(major_version))
      if snapshots:
        # if snapshots found, we can append them after the release version,
        # so the prebuilt image list of this version will be
        # release_image, snapshot1, snapshot2,...
        if short_version not in rev_map:
          rev_map[short_version] = []
        rev_map[short_version] += snapshots

  result = []
  for rev in sorted(rev_map, key=util.version_key_func):
    if not util.is_direct_relative_version(new_short, rev):
      continue
    if not util.is_version_lesseq(old_short, rev):
      continue
    if not util.is_version_lesseq(rev, new_short):
      continue

    for version, gs_path in rev_map[rev]:

      # version_to_full() and gsutil_ls() may take long time if versions are a
      # lot. This is acceptable because we usually bisect only short range.

      if only_good_build and not is_cros_snapshot_version(version):
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

      if is_cros_version_lesseq(old, version) and is_cros_version_lesseq(
          version, new):
        result.append(version)

  return result


def search_snapshot_image(board, snapshot_version):
  """Searches chromeos snapshot image.

  Args:
    board: ChromeOS board name
    snapshot_version: ChromeOS snapshot version number

  Returns:
    ImageInfo object
  """
  assert is_cros_snapshot_version(snapshot_version)
  image_info = ImageInfo()
  gs_path = gs_archive_base + '{board}-snapshot/{snapshot_version}-*'.format(
      board=board, snapshot_version=snapshot_version)
  files = gsutil_ls(
      gs_path + '/' + sample_partition_filename, ignore_errors=True)
  if files:
    image_info[ImageType.PARTITION_IMAGE] = files[0].replace(
        sample_partition_filename, '')
  files = gsutil_ls(gs_path + '/image.zip', ignore_errors=True)
  if files:
    image_info[ImageType.ZIP_FILE] = files[0]
  return image_info


def prepare_image_for_quick_provision(image_info):
  path = image_info.get(ImageType.PARTITION_IMAGE)
  if path and path.startswith(gs_archive_base):
    return urllib.parse.urlparse(path).path[1:]

  logger.warning(
      'image format or location are not supported by quick-provision: %s',
      image_info)
  return None


def _cache_path_for_download(chromeos_root, url):
  cache_folder = os.path.join(chromeos_root, cached_images_dir)
  os.makedirs(cache_folder, exist_ok=True)
  name = urllib.parse.quote(url, safe='')
  return os.path.join(cache_folder, name)


def prepare_image_for_cros_flash(chromeos_root, image_info):
  path = image_info.get(ImageType.DISK_IMAGE)
  if path:
    # local path
    if '://' not in path:
      return path

    m = re.search(gs_archive_base + r'([^/]+)-[a-z]+/([^/]+)/', path)
    if m:
      return 'xbuddy://remote/%s/%s/test' % (m.group(1), m.group(2))
    if path.startswith(gs_archive_base):
      return path.replace('chromiumos_test_image.tar.xz', 'test')

    # 'cros flash' doesn't support other gs bucket, download to local.
    if path.startswith('gs://'):
      cache_path = _cache_path_for_download(chromeos_root, path)
      if os.path.exists(cache_path):
        return cache_path
      gsutil('cp', path, cache_path)
      return cache_path

  path = image_info.get(ImageType.PARTITION_IMAGE)
  if path and path.startswith(gs_archive_base):
    # newer 'cros flash' support partition images
    # TODO(kcwu): update the commit hash after new flash is enabled
    if git_util.is_ancestor_commit(
        os.path.join(chromeos_root, 'chromite'), '153f91684a9a59', 'HEAD'):
      return path

  path = image_info.get(ImageType.ZIP_FILE)
  if path:
    cached_image_full_path = _cache_path_for_download(
        chromeos_root, path + '.' + test_image_filename)
    if os.path.exists(cached_image_full_path):
      return cached_image_full_path

    tmp_dir = tempfile.mkdtemp()
    try:
      if path.startswith('gs://'):
        gsutil('cp', path, tmp_dir)
        path = os.path.join(tmp_dir, os.path.basename(path))
        assert os.path.exists(path)
      util.check_call('unzip', '-j', path, test_image_filename, cwd=tmp_dir)
      shutil.move(
          os.path.join(tmp_dir, test_image_filename), cached_image_full_path)
    finally:
      shutil.rmtree(tmp_dir)

    return cached_image_full_path

  return None


def quick_provision(chromeos_root, host, image_info):
  # TODO(kimjae): Transition to using TLS ProvisionDut for F20.
  logger.debug('quick_provision %s %s', host, image_info)
  build = prepare_image_for_quick_provision(image_info)
  if not build:
    return False

  autotest_path = os.path.join(chromeos_root_inside_chroot,
                               in_tree_autotest_dir)
  quick_provision_cmd = [
      'test_that', '--args',
      "value='%s'" % build, host, 'provision_QuickProvision', '--autotest_dir',
      autotest_path, '--debug'
  ]
  try:
    cros_sdk(chromeos_root, *quick_provision_cmd)
  except subprocess.CalledProcessError as e:
    raise errors.ExternalError('quick-provision failed') from e
  return True


def verify_dut_version(host, board, version):
  if version:
    # In the past, cros flash may fail with returncode=0
    # So let's have an extra check.
    if is_cros_snapshot_version(version):
      builder_path = query_dut_lsb_release(host).get(
          'CHROMEOS_RELEASE_BUILDER_PATH', '')
      expect_prefix = '%s-snapshot/%s-' % (board, version)
      if not builder_path.startswith(expect_prefix):
        raise errors.ExternalError(
            'although provision succeeded, the OS builder path is '
            'unexpected: actual=%s expect=%s' % (builder_path, expect_prefix))
    else:
      expect_version = version_to_short(version)
      dut_version = query_dut_short_version(host)
      if dut_version != expect_version:
        raise errors.ExternalError(
            'although provision succeeded, the OS version is unexpected: '
            'actual=%s expect=%s' % (dut_version, expect_version))

  # "cros flash" may terminate successfully but the DUT starts self-repairing
  # (b/130786578), so it's necessary to do sanity check.
  if not is_good_dut(host):
    raise errors.ExternalError(
        'although provision succeeded, the DUT is in bad state')


def provision_image(chromeos_root,
                    host,
                    board,
                    image_info,
                    version=None,
                    clobber_stateful=False,
                    disable_rootfs_verification=True,
                    force_reboot_callback=None):
  # Try quick_provision first, but fallback to cros flash.
  # TODO(kcwu): only use quick_provision for DUTs in the lab
  try:
    if quick_provision(chromeos_root, host, image_info):
      verify_dut_version(host, board, version)
      return
    logger.debug('quick-provision is not supported; fallback to cros flash')
  except errors.ExternalError as e:
    logger.warning('quick-provision failed; fallback to cros flash: %s', e)

  if not cros_flash(
      chromeos_root,
      host,
      image_info,
      clobber_stateful=clobber_stateful,
      disable_rootfs_verification=disable_rootfs_verification,
      force_reboot_callback=force_reboot_callback):
    raise errors.InternalError('unsupported image: ' + str(image_info))
  verify_dut_version(host, board, version)


def search_prebuilt_image(board, version):
  """Searches chromeos prebuilt image.

  Args:
    chromeos_root: chromeos tree root
    board: ChromeOS board name
    version: ChromeOS version number in short or full format

  Returns:
    ImageInfo object
  """
  assert is_cros_version(version)
  full_version = version_to_full(board, version)

  image_info = ImageInfo()
  gs_path = gs_archive_path.format(board=board) + '/' + full_version
  if gsutil_ls(gs_path + '/chromiumos_test_image.tar.xz', ignore_errors=True):
    image_info[ImageType.DISK_IMAGE] = gs_path + '/chromiumos_test_image.tar.xz'
  if gsutil_ls(gs_path + '/' + sample_partition_filename, ignore_errors=True):
    image_info[ImageType.PARTITION_IMAGE] = gs_path
  return image_info


def search_image(board, version):
  if is_cros_snapshot_version(version):
    return search_snapshot_image(board, version)
  return search_prebuilt_image(board, version)


def cros_flash(chromeos_root,
               host,
               image_info,
               clobber_stateful=False,
               disable_rootfs_verification=True,
               force_reboot_callback=None):
  """Flash a DUT with given ChromeOS image.

  This is implemented by 'cros flash' command line.

  Args:
    chromeos_root: use 'cros flash' of which chromeos tree
    host: DUT address
    board: ChromeOS board name
    image_info: ImageInfo object
    version: ChromeOS version in short or full format
    clobber_stateful: Clobber stateful partition when performing update
    disable_rootfs_verification: Disable rootfs verification after update
        is completed
    force_reboot_callback: powerful reboot hook (via servo)

  Returns:
    False for unsupported images

  Raises:
    errors.ExternalError: cros flash failed
  """
  logger.info('cros_flash %s %s', host, image_info)

  # Reboot is necessary because sometimes previous 'cros flash' failed and
  # entered a bad state.
  reboot(host, force_reboot_callback=force_reboot_callback)

  # Stop service ap-update-manager to prevent rebooting during auto update.
  # The service is used in jetstream boards, but not other CrOS devices.
  if query_dut_os_release(host).get('GOOGLE_CRASH_ID') == 'Jetstream':
    try:
      # Sleep to wait ap-update-manager start, which may take up to 27 seconds.
      # For simplicity, we wait 60 seconds here, which is the timeout value of
      # jetstream_host.
      # https://chromium.googlesource.com/chromiumos/third_party/autotest
      #     /+/HEAD/server/hosts/jetstream_host.py#27
      time.sleep(60)
      util.ssh_cmd(host, 'stop', 'ap-update-manager')
    except subprocess.CalledProcessError:
      pass  # not started; do nothing

  image_path = prepare_image_for_cros_flash(chromeos_root, image_info)
  if not image_path:
    return False

  # Handle relative path.
  if '://' not in image_path and not os.path.isabs(image_path):
    image_path = os.path.join(chromeos_root_inside_chroot, image_path)

  args = [
      '--debug',
      '--no-ping',
      # Speed up for slow network connection.
      '--send-payload-in-parallel',
      host,
      image_path,
  ]
  # TODO(kcwu): remove this check if we don't need to support chromeos versions
  # earlier than Dec 2020.
  if git_util.is_ancestor_commit(
      os.path.join(chromeos_root, 'chromite'), '9ed30bc3ed292b', 'HEAD'):
    # To reduce disk usage on DUT.
    args.append('--no-copy-payloads-to-device')
  if clobber_stateful:
    args.append('--clobber-stateful')
  if disable_rootfs_verification:
    args.append('--disable-rootfs-verification')

  # TODO(kcwu): remove this flag once the new flash code enabled by default
  if git_util.is_ancestor_commit(
      os.path.join(chromeos_root, 'chromite'), '153f91684a9a59', 'HEAD'):
    args.append('--exp-new-flash')

  try:
    cros_sdk(chromeos_root, 'cros', 'flash', *args)
  except subprocess.CalledProcessError as e:
    raise errors.ExternalError('cros flash failed') from e
  return True


def provision_image_with_retry(chromeos_root,
                               host,
                               board,
                               image_info,
                               version=None,
                               clobber_stateful=False,
                               disable_rootfs_verification=True,
                               repair_callback=None,
                               force_reboot_callback=None):
  # 'cros flash' is not 100% reliable, retry if necessary.
  for attempt in range(2):
    if attempt > 0:
      logger.info('will retry 60 seconds later')
      time.sleep(60)

    try:
      provision_image(
          chromeos_root,
          host,
          board,
          image_info,
          version=version,
          clobber_stateful=clobber_stateful,
          disable_rootfs_verification=disable_rootfs_verification,
          force_reboot_callback=force_reboot_callback)
      workaround_b183567529(
          host, board, version, force_reboot_callback=force_reboot_callback)
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
  if is_cros_snapshot_version(version):
    api = buildbucket_util.BuildbucketApi()
    milestone, short_version, _ = snapshot_version_split(version)
    buildbucket_id = query_snapshot_buildbucket_id(board, version)
    data = api.get_build(int(buildbucket_id)).output.properties
    target_versions = json_format.MessageToDict(data['target_versions'])
    return {
        VERSION_KEY_MILESTONE: milestone,
        VERSION_KEY_CROS_FULL_VERSION: version,
        VERSION_KEY_CROS_SHORT_VERSION: short_version,
        VERSION_KEY_CR_VERSION: target_versions.get('chromeVersion'),
        VERSION_KEY_ANDROID_BUILD_ID: target_versions.get('androidVersion'),
        VERSION_KEY_ANDROID_BRANCH: target_versions.get('androidBranchVersion'),
    }
  info = {}
  full_version = version_to_full(board, version)

  # Some boards may have only partial-metadata.json but no metadata.json.
  # e.g. caroline R60-9462.0.0
  # Let's try both.
  metadata = None
  for metadata_filename in ['metadata.json', 'partial-metadata.json']:
    path = gs_archive_path.format(
        board=board) + '/%s/%s' % (full_version, metadata_filename)
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


def convert_path_outside_chroot(chromeos_root, path):
  """Converts path in chroot to outside.

  Args:
    chromeos_root: chromeos tree root
    path: path inside chroot; support starting with '~/'

  Returns:
    The corresponding path outside chroot assuming the chroot is mounted
  """
  if path.startswith('~/'):
    path = path.replace('~', '/home/' + os.environ['USER'])
  assert '~' not in path, 'tilde (~) character is not fully supported'

  assert os.path.isabs(path)
  assert path[0] == os.sep
  return os.path.join(chromeos_root, 'chroot', path[1:])


def cros_sdk(chromeos_root,
             *args,
             chrome_root=None,
             env=None,
             log_stdout=True,
             stdin=None,
             stderr_callback=None,
             goma_dir=None):
  """Run commands inside chromeos chroot.

  Args:
    chromeos_root: chromeos tree root
    *args: command to run
    chrome_root: pass to cros_sdk; mount this path into the SDK chroot
    env: (dict) environment variables for the command
    log_stdout: Whether write the stdout output of the child process to log.
    stdin: standard input file handle for the command
    stderr_callback: Callback function for stderr. Called once per line.
    goma_dir: Goma installed directory to mount into the chroot
  """
  envs = []
  if env:
    for k, v in env.items():
      assert re.match(r'^[A-Za-z_][A-Za-z0-9_]*$', k)
      envs.append('%s=%s' % (k, v))

  # Use --no-ns-pid to prevent cros_sdk change our pgid, otherwise subsequent
  # commands would be considered as background process.
  prefix = ['chromite/bin/cros_sdk', '--no-ns-pid']

  if chrome_root:
    prefix += ['--chrome_root', chrome_root]
  if goma_dir:
    prefix += ['--goma_dir', goma_dir]

  prefix += envs + ['--']

  # In addition to the output of command we are interested, cros_sdk may
  # generate its own messages. For example, chroot creation messages if we run
  # cros_sdk the first time.
  # This is the hack to run dummy command once, so we can get clean output for
  # the command we are interested.
  cmd = prefix + ['true']
  try:
    util.check_call(*cmd, cwd=chromeos_root)
  except subprocess.CalledProcessError:
    logger.exception('cros_sdk init/update failed')
    raise

  cmd = prefix + list(args)
  return util.check_output(
      *cmd,
      cwd=chromeos_root,
      log_stdout=log_stdout,
      stdin=stdin,
      stderr_callback=stderr_callback)


def create_chroot(chromeos_root):
  """Creates ChromeOS chroot if necessary.

  Args:
    chromeos_root: chromeos tree root
  """
  if os.path.exists(os.path.join(chromeos_root, 'chroot')):
    return
  if os.path.exists(os.path.join(chromeos_root, 'chroot.img')):
    return

  util.check_output('chromite/bin/cros_sdk', '--create', cwd=chromeos_root)


def mount_chroot(chromeos_root):
  """Creates ChromeOS chroot if necessary.

  Args:
    chromeos_root: chromeos tree root
  """
  # An arbitrary file must exist in chroot.
  path = convert_path_outside_chroot(chromeos_root, '/bin/ls')

  # Not created or mounted yet.
  if not os.path.exists(path):
    create_chroot(chromeos_root)
    # After this command, the chroot is mounted.
    cros_sdk(chromeos_root, 'true')
    assert os.path.exists(path)


def copy_into_chroot(chromeos_root, src, dst, overwrite=True):
  """Copies file into chromeos chroot.

  The side effect is chroot created and mounted.

  Args:
    chromeos_root: chromeos tree root
    src: path outside chroot
    dst: path inside chroot
    overwrite: overwrite if dst already exists
  """
  mount_chroot(chromeos_root)
  src = os.path.expanduser(src)
  dst_outside = convert_path_outside_chroot(chromeos_root, dst)
  if not overwrite and os.path.exists(dst_outside):
    return

  # Haven't support directory or special files yet.
  assert os.path.isfile(src)
  assert os.path.isfile(dst_outside) or not os.path.exists(dst_outside)

  dirname = os.path.dirname(dst_outside)
  if not os.path.exists(dirname):
    os.makedirs(dirname)
  shutil.copy(src, dst_outside)


def _copy_template_files(src, dst):
  if not os.path.exists(src):
    return

  def copy_if_nonexistent(src, dst):
    if not os.path.exists(dst):
      shutil.copy2(src, dst)

  shutil.copytree(
      src, dst, dirs_exist_ok=True, copy_function=copy_if_nonexistent)


def override_autotest_config(autotest_dir):
  shadow_config_path = os.path.join(autotest_dir, 'shadow_config.ini')
  if not os.path.exists(shadow_config_path):
    with open(shadow_config_path, 'w') as f:
      f.write(autotest_shadow_config)


def prepare_chroot(chromeos_root):
  mount_chroot(chromeos_root)

  # Work around b/149077936:
  # The creds file is copied into the chroot since 12866.0.0.
  # But earlier versions need this file as well because of cipd ACL change.
  creds_path = '~/.config/chrome_infra/auth/creds.json'
  if os.path.exists(os.path.expanduser(creds_path)):
    copy_into_chroot(chromeos_root, creds_path, creds_path, overwrite=False)

  # quick-provision requires special config for autotest.
  override_autotest_config(os.path.join(chromeos_root, in_tree_autotest_dir))

  # Copy optional configure files into the home directory inside chromeos
  # chroot. For example, quick-provision may need special ssh config.
  assert os.environ.get('USER')
  _copy_template_files(
      os.path.join(common.BISECT_KIT_ROOT, 'cros_template_files', 'at_home'),
      os.path.join(chromeos_root, 'chroot', 'home', os.environ['USER']))


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

  # Detect failures due to file collisions.
  # For example, kernel uprev from 3.x to 4.x, they are two separate packages
  # and conflict with each other. Other possible cases are package renaming or
  # refactoring. Let's recreate chroot to work around them.
  if 'Detected file collision' in stdout:
    # Using wildcard between words because the text wraps to the next line
    # depending on length of package name and each line is prefixed with
    # package name.
    # Using ".{,100}" instead of ".*" to prevent regex matching time explodes
    # exponentially. 100 is chosen arbitrarily. It should be longer than any
    # package name (65 now).
    m = re.search(
        r'Package (\S+).{,100}NOT.{,100}merged.{,100}'
        r'due.{,100}to.{,100}file.{,100}collisions', stdout, re.S)
    if m:
      return 'failed to install package due to file collision: ' + m.group(1)

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

  def has_build_package_argument(argument):
    stderr_lines = []
    try:
      util.check_call(
          'src/scripts/build_packages',
          '--help',
          cwd=chromeos_root,
          stderr_callback=stderr_lines.append)
    except subprocess.CalledProcessError:
      help_output = ''.join(stderr_lines)
    return '--[no]%s' % argument in help_output

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

      # `use_any_chrome` flag is default on and will force to use a chrome
      # prebuilt even if the version doesn't match.

      # As this argument is landed in 12681, we should check if the argument
      # exists before adding this.
      if has_build_package_argument('use_any_chrome'):
        cmd.append('--nouse_any_chrome')

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
      raise NeedRecreateChrootException(reason) from e

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
      raise NeedRecreateChrootException(reason) from e

    # For other failures, don't know how to handle. Just bail out.
    raise

  image_symlink = os.path.join(chromeos_root, build_images_dir, board, 'latest')
  assert os.path.exists(image_symlink)
  image_name = os.readlink(image_symlink)
  image_folder = os.path.join(build_images_dir, board, image_name)
  assert os.path.exists(
      os.path.join(chromeos_root, image_folder, test_image_filename))
  return image_folder


def workaround_b183567529(host, board, version=None,
                          force_reboot_callback=None):
  """Workaround for volteer failure.

  See b/183567529#comment8 and b/183020319#comment26 for more details.
  """
  broken_range = [
      ('13836.0.0', '13854.0.0'),
      ('13816.13.0', '13816.19.0'),
  ]
  if board != 'volteer' or not version:
    return

  if is_cros_short_version(version):
    short_version = version
  else:
    _, short_version = version_split(version)
  for old, new in broken_range:
    if util.is_version_lesseq(old, short_version) and util.is_version_lesseq(
        short_version,
        new) and (util.is_direct_relative_version(old, short_version) and
                  util.is_direct_relative_version(short_version, new)):
      logger.info('applying b183567529 cbi patch for volteer')
      cbi_override = os.path.join(
          common.BISECT_KIT_ROOT,
          'patching/b183567529/volteer-cbi-override.conf')
      util.scp_cmd(cbi_override, 'root@%s:/etc/init/' % host)

      patch_script = os.path.join(common.BISECT_KIT_ROOT,
                                  'patching/b183567529/test_cbi_script.sh')
      util.check_output(patch_script, host)
      reboot(host, force_reboot_callback)
      break


class AutotestControlInfo:
  """Parsed content of autotest control file.

  Attributes:
    name: test name
    path: control file path
    variables: dict of top-level control variables. Sample keys: NAME, AUTHOR,
        DOC, ATTRIBUTES, DEPENDENCIES, etc.
  """

  def __init__(self, path, variables):
    assert 'NAME' in variables, 'invalid control file'
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
  with open(path) as f:
    code = ast.parse(f.read())
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

  denylist = ['site-packages', 'venv', 'results', 'logs', 'containers']
  result = []
  for subpath in subpaths:
    path = os.path.join(autotest_dir, subpath)
    for root, dirs, files in os.walk(path):

      for deny in denylist:
        if deny in dirs:
          dirs.remove(deny)

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
    try:
      info = parse_autotest_control_file(control_file)
    except SyntaxError:
      logger.warning('%s is not parsable, ignore', control_file)
      continue

    if info.name == test_name:
      return info
  return None


def _get_overlay_name(overlay):
  path = os.path.join(overlay, 'metadata', 'layout.conf')
  if os.path.exists(path):
    with open(path) as f:
      for line in f:
        m = re.search(r'repo-name\s*=\s*(\S+)', line)
        if m:
          return m.group(1)

  path = os.path.join(overlay, 'profiles', 'repo_name')
  if os.path.exists(path):
    with open(path) as f:
      return f.readline().rstrip()

  return None


def parse_chromeos_overlays(chromeos_root):
  # ref: chromite's lib/portage_util.py ListOverlays().
  overlays = {}
  paths = ['src/overlays', 'src/private-overlays']

  for path in paths:
    path = os.path.join(chromeos_root, path, 'overlay-*')
    for overlay in sorted(glob.glob(path)):
      name = _get_overlay_name(overlay)
      if not name:
        continue
      # Special cases which have variant boards.
      if name in ['auron', 'guado', 'nyan', 'veyron']:
        continue

      path = os.path.join(overlay, 'metadata', 'layout.conf')
      masters = []
      if os.path.exists(path):
        with open(path) as f:
          for line in f:
            m = re.search(r'masters\s*=(.*)', line)
            if m:
              masters = m.group(1).split()
      overlays[name] = masters
  return overlays


def resolve_basic_boards(overlays):

  def normalize(name):
    return name.replace('-private', '')

  def resolve(name):
    result = set()
    for parent in overlays[name]:
      assert parent != name, 'recursive overlays definition?'
      if parent not in overlays:
        continue
      for basic in resolve(parent):
        result.add(basic)
    if not result:
      result.add(name)
    return set(map(normalize, result))

  result = {}
  for name in overlays:
    board = normalize(name)
    basic = resolve(name)
    assert len(basic) == 1
    basic_board = basic.pop()
    result[board] = basic_board
  return result


def detect_branch_level(branch):
  """Given a branch name of manifest-internal, detect it's branch level.

  level1: if ChromeOS version is x.0.0
  level2: if ChromeOS version is x.x.0
  level3: if ChromeOS version is x.x.x
  Where x is an non-zero integer.

  Args:
    branch: branch name or ref name in manifest-internal

  Returns:
    An integer indicates the branch level, or zero if not detectable.
  """
  level1 = r'^(refs\/\S+(\/\S+)?/)?master$'
  level2 = r'^\S+-(\d+)(\.0)?\.B$'
  level3 = r'^\S+-(\d+)\.(\d+)(\.0)?\.B$'

  if re.match(level1, branch):
    return 1
  if re.match(level2, branch):
    return 2
  if re.match(level3, branch):
    return 3
  return 0


def get_crosland_link(old, new):
  """Generates crosland link between two versions.

  Args:
    old: ChromeOS version
    new: ChromeOS version

  Returns:
    A crosland url.
  """

  def version_to_url_parameter(ver):
    if is_cros_snapshot_version(ver):
      return snapshot_version_split(ver)[2]
    return version_to_short(ver)

  old_parameter = version_to_url_parameter(old)
  new_parameter = version_to_url_parameter(new)
  return CROSLAND_URL_TEMPLATE % (old_parameter, new_parameter)


class ChromeOSSpecManager(codechange.SpecManager):
  """Repo manifest related operations.

  This class enumerates chromeos manifest files, parses them,
  and sync to disk state according to them.
  """

  def __init__(self, config):
    self.config = config
    self.manifest_dir = os.path.join(self.config['chromeos_root'], '.repo',
                                     'manifests')
    self.manifest_internal_dir = os.path.join(self.config['chromeos_mirror'],
                                              'manifest-internal.git')
    self.historical_manifest_git_dir = os.path.join(
        self.config['chromeos_mirror'], 'chromeos/manifest-versions.git')
    self.historical_manifest_branch_name = 'refs/heads/master'
    if not os.path.exists(self.historical_manifest_git_dir):
      raise errors.InternalError('Manifest snapshots should be cloned into %s' %
                                 self.historical_manifest_git_dir)

  def lookup_snapshot_manifest_revisions(self, old, new):
    """Get manifest commits between snapshot versions.

    Returns:
      list of (timestamp, commit_id, snapshot_id):
        timestamp: integer unix timestamp
        commit_id: a string indicates commit hash
        snapshot_id: a string indicates snapshot id
    """
    assert is_cros_snapshot_version(old)
    assert is_cros_snapshot_version(new)

    gs_path = (
        'gs://chromeos-image-archive/{board}-snapshot/{version}-*/image.zip')
    # Try to guess the commit time of a snapshot manifest, it is usually a few
    # minutes different between snapshot manifest commit and image.zip
    # generate.
    try:
      old_timestamp = gsutil_stat_creation_time(
          gs_path.format(board=self.config['board'], version=old)) - 86400
    except subprocess.CalledProcessError:
      old_timestamp = None
    try:
      new_timestamp = gsutil_stat_creation_time(
          gs_path.format(board=self.config['board'], version=new)) + 86400
      # 1558657989 is snapshot_id 5982's commit time, this ensures every time
      # we can find snapshot 5982
      # snapshot_id <= 5982 has different commit message format, so we need
      # to identify its id in different ways, see below comment for more info.
      new_timestamp = max(new_timestamp, 1558657989 + 1)
    except subprocess.CalledProcessError:
      new_timestamp = None
    result = []
    _, _, old_snapshot_id = snapshot_version_split(old)
    _, _, new_snapshot_id = snapshot_version_split(new)
    repo = self.manifest_internal_dir
    path = 'snapshot.xml'
    branch = 'snapshot'
    commits = git_util.get_history(
        repo,
        path,
        branch,
        after=old_timestamp,
        before=new_timestamp,
        with_subject=True)

    # Unfortunately, we can not identify snapshot_id <= 5982 from its commit
    # subject, as their subjects are all `Annealing manifest snapshot.`.
    # So instead we count the snapshot_id manually.
    count = 5982
    # There are two snapshot_id = 2633 in commit history, ignore the former
    # one.
    ignore_list = ['95c8526a7f0798d02f692010669dcbd5a152439a']
    # We examine the commits in reverse order as there are some testing
    # commits before snapshot_id=2, this method works fine after
    # snapshot 2, except snapshot 2633
    for commit in reversed(commits):
      msg = commit[2]
      if commit[1] in ignore_list:
        continue

      match = re.match(r'^annealing manifest snapshot (\d+)', msg)
      if match:
        snapshot_id = match.group(1)
      elif 'Annealing manifest snapshot' in msg:
        snapshot_id = str(count)
        count -= 1
      else:
        continue
      # b/151054108: snapshot version in [29288, 29439] is broken
      if 29288 <= int(snapshot_id) <= 29439:
        continue
      if int(old_snapshot_id) <= int(snapshot_id) <= int(new_snapshot_id):
        result.append((commit[0], commit[1], snapshot_id))
    # We find commits in reversed order, now reverse it again to chronological
    # order.
    return list(reversed(result))

  def lookup_build_timestamp(self, rev):
    assert is_cros_full_version(rev) or is_cros_snapshot_version(rev)
    if is_cros_full_version(rev):
      return self.lookup_release_build_timestamp(rev)
    return self.lookup_snapshot_build_timestamp(rev)

  def lookup_snapshot_build_timestamp(self, rev):
    assert is_cros_snapshot_version(rev)
    return int(self.lookup_snapshot_manifest_revisions(rev, rev)[0][0])

  def lookup_release_build_timestamp(self, rev):
    assert is_cros_full_version(rev)
    milestone, short_version = version_split(rev)
    path = os.path.join('buildspecs', milestone, short_version + '.xml')
    try:
      timestamp = git_util.get_commit_time(self.historical_manifest_git_dir,
                                           self.historical_manifest_branch_name,
                                           path)
    except ValueError as e:
      raise errors.InternalError(
          '%s does not have %s' %
          (self.historical_manifest_git_dir, path)) from e
    return timestamp

  def detect_float_spec_branch_level(self, spec):
    results = [
        detect_branch_level(branch) for branch in git_util.get_branches(
            self.manifest_dir, commit=spec.name)
    ]
    results = [x for x in results if x > 0]
    return min(results) if results else 0

  def branch_between_float_specs(self, old_spec, new_spec):
    if old_spec.spec_type != codechange.SPEC_FLOAT:
      return False
    if new_spec.spec_type != codechange.SPEC_FLOAT:
      return False

    level_old = self.detect_float_spec_branch_level(old_spec)
    level_new = self.detect_float_spec_branch_level(new_spec)

    if not level_old or not level_new:
      logger.warning('branch level detect failed, assume not branched')
      return False
    return level_old != level_new

  def _determine_float_branch(self, old, new, fixed_specs):
    # There is no revision tag in snapshot's xml. We know snapshot
    # builds are on master branch.
    master_refname = 'refs/remotes/origin/master'
    if fixed_specs[0].revision:
      old_branches = git_util.get_branches(
          self.manifest_dir, commit=fixed_specs[0].revision, remote=True)
    else:
      old_branches = [master_refname]

    if fixed_specs[-1].revision:
      new_branches = git_util.get_branches(
          self.manifest_dir, commit=fixed_specs[-1].revision, remote=True)
    else:
      new_branches = [master_refname]

    common_branches = list(set(old_branches) & set(new_branches))
    assert common_branches, '%s and %s are not on common branches?' % (old, new)

    if len(common_branches) == 1:
      return common_branches[0]

    # There are more than one common branches, use heuristic to tie breaking.
    # The heuristic is simple: choice the branch with "smallest" number.
    # "Smaller" means the more major branch (not branched) or branched later.
    #
    # Following is the commit graph of manifest-internal repo. It shows many
    # interesting cases.
    #
    #          84/13021.0.0   84/13022.0.0   84/13024.0.0
    # --A--+---X--------------X------B-------X-----------> master
    #       \
    #        \    83/13020.1.0    83/13020.56.0    83/13020.68.0
    #         C---X----D--+-------X-------+--------X-----> release-R83-13020.B
    #                      \               \
    #                       \               E------------> stabilize-13020.67.B
    #                        \      83/13020.55.1
    #                         F-----X--------------------> stabilize-13020.55.B
    #
    # How to read this graph:
    #  - Time goes from left to right. Branch names are on the right side of
    #    arrows.
    #  - Letters A-F are manifest commits.
    #  - Marker X means release image build at that time, the version numbers
    #    are labeled above the X marker.
    # For example,
    #  1) 13021.0.0 release is based on manifest A, which is on all branches
    #     shown on the graph.
    #     We know 13021.0.0 is on master (and R84 branch later, not shown in
    #     this graph), not on 13020* branches.
    #  2) 13020.56.0 release is based on manifest D, which is on 3 branches
    #     (R83-13020.B, 13020.67.B, and 13020.55.B).
    #     We know 13020.56.0 is on R83-13020.B and 13020.67.B, but not
    #     13020.55.B.
    #
    # There is an important property here. Every time a new branch is created,
    # there will always be a commit (like C, E, and F) to fix "revision" field
    # in the manifest file. In other words, xxxxx.1.0 is impossible based on
    # manifest on master branch. xxxxx.yy.1 is impossible based on manifest on
    # xxxxx.B branch.
    #
    # With such property, among the branches containing the given manifest
    # file, the branch with "smallest" number guarantees where the release is.

    def branch_key(s):
      if s == master_refname:
        return 0, 0, 0
      m = re.search(r'-(\d+)\.B$', s)
      if m:
        return int(m.group(1)), 0, 0
      m = re.search(r'-(\d+)\.(\d+)\.B$', s)
      if m:
        return int(m.group(1)), int(m.group(2)), 0
      m = re.search(r'-(\d+)\.(\d+)\.(\d+)\.B$', s)
      if m:
        return int(m.group(1)), int(m.group(2)), int(m.group(3))

      logger.warning('unexpected branch name: %s', s)
      return (sys.maxsize, sys.maxsize, sys.maxsize, s)

    common_branches.sort(key=branch_key)
    return common_branches[0]

  def collect_float_spec(self, old, new, fixed_specs=None):
    assert fixed_specs
    branch = self._determine_float_branch(old, new, fixed_specs)
    logger.debug('float branch=%s', branch)

    old_timestamp = self.lookup_build_timestamp(old)
    new_timestamp = self.lookup_build_timestamp(new)
    # snapshot time is different from commit time
    # usually it's a few minutes different
    # 30 minutes should be safe in most cases
    if is_cros_snapshot_version(old):
      old_timestamp = old_timestamp - 1800
    if is_cros_snapshot_version(new):
      new_timestamp = new_timestamp + 1800

    # TODO(zjchang): add logic to combine symlink target's (full.xml) history
    result = []
    path = 'default.xml'
    parser = repo_util.ManifestParser(self.manifest_dir)
    for timestamp, git_rev in parser.enumerate_manifest_commits(
        old_timestamp, new_timestamp, path, branch=branch):
      result.append(
          codechange.Spec(codechange.SPEC_FLOAT, git_rev, timestamp, path))
    return result

  def collect_fixed_spec(self, old, new):
    assert is_cros_full_version(old) or is_cros_snapshot_version(old)
    assert is_cros_full_version(new) or is_cros_snapshot_version(new)

    # case 1: if both are snapshot, return a list of snapshot
    if is_cros_snapshot_version(old) and is_cros_snapshot_version(new):
      return self.collect_snapshot_specs(old, new)

    # case 2: if both are release version
    #         return a list of release version
    if is_cros_full_version(old) and is_cros_full_version(new):
      return self.collect_release_specs(old, new)

    # case 3: return a list of release version and append a snapshot
    #         before or at the end
    result = self.collect_release_specs(
        version_to_full(self.config['board'], old),
        version_to_full(self.config['board'], new))
    if is_cros_snapshot_version(old):
      result = self.collect_snapshot_specs(old, old) + result[1:]
    elif is_cros_snapshot_version(new):
      result += self.collect_snapshot_specs(new, new)
    return result

  def collect_snapshot_specs(self, old, new):
    assert is_cros_snapshot_version(old)
    assert is_cros_snapshot_version(new)

    def guess_snapshot_version(board, snapshot_id, old, new):
      if old.endswith('-' + snapshot_id):
        return old
      if new.endswith('-' + snapshot_id):
        return new
      gs_path = ('gs://chromeos-image-archive/{board}-snapshot/'
                 'R*-{snapshot_id}-*'.format(
                     board=board, snapshot_id=snapshot_id))
      for line in gsutil_ls(gs_path, ignore_errors=True):
        m = re.match(r'^gs:\S+(R\d+-\d+\.\d+\.\d+-\d+)\S+', line)
        if m:
          return m.group(1)
      return None

    result = []
    path = 'snapshot.xml'
    revisions = self.lookup_snapshot_manifest_revisions(old, new)
    for timestamp, _git_rev, snapshot_id in revisions:
      snapshot_version = guess_snapshot_version(self.config['board'],
                                                snapshot_id, old, new)
      if snapshot_version:
        result.append(
            codechange.Spec(codechange.SPEC_FIXED, snapshot_version, timestamp,
                            path))
      else:
        logger.warning('snapshot id %s is not found, ignore', snapshot_id)
    return result

  def collect_release_specs(self, old, new):
    assert is_cros_full_version(old)
    assert is_cros_full_version(new)
    old_milestone, old_short_version = version_split(old)
    new_milestone, new_short_version = version_split(new)

    result = []
    for milestone in git_util.list_dir_from_revision(
        self.historical_manifest_git_dir, self.historical_manifest_branch_name,
        'buildspecs'):
      if not milestone.isdigit():
        continue
      if not int(old_milestone) <= int(milestone) <= int(new_milestone):
        continue

      files = git_util.list_dir_from_revision(
          self.historical_manifest_git_dir,
          self.historical_manifest_branch_name,
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
          timestamp = git_util.get_commit_time(
              self.historical_manifest_git_dir,
              self.historical_manifest_branch_name, path)
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
    assert is_cros_full_version(rev) or is_cros_snapshot_version(rev)
    if is_cros_full_version(rev):
      milestone, short_version = version_split(rev)
      path = os.path.join('buildspecs', milestone, '%s.xml' % short_version)
      manifest = git_util.get_file_from_revision(
          self.historical_manifest_git_dir,
          self.historical_manifest_branch_name, path)
    else:
      revisions = self.lookup_snapshot_manifest_revisions(rev, rev)
      commit_id = revisions[0][1]
      manifest = git_util.get_file_from_revision(self.manifest_internal_dir,
                                                 commit_id, 'snapshot.xml')
    return manifest

  def get_manifest_file(self, rev):
    assert is_cros_full_version(rev) or is_cros_snapshot_version(rev)
    manifest_name = 'manifest_%s.xml' % rev
    manifest_path = os.path.join(self.manifest_dir, manifest_name)
    with open(manifest_path, 'w') as f:
      f.write(self.get_manifest(rev))

    # workaround for b/150572399
    # for chromeOS version < 12931.0.0, manifests are included from incorrect
    # folder .repo instead of.repo/manifests
    if is_cros_version_lesseq(rev, '12931.0.0'):
      repo_path = os.path.join(self.config['chromeos_root'], '.repo')
      manifest_patch_path = os.path.join(repo_path, manifest_name)
      with open(manifest_patch_path, 'w') as f:
        f.write(self.get_manifest(rev))

    return manifest_name

  def parse_spec(self, spec):
    parser = repo_util.ManifestParser(self.manifest_dir)
    if spec.spec_type == codechange.SPEC_FIXED:
      manifest_name = self.get_manifest_file(spec.name)
      manifest_path = os.path.join(self.manifest_dir, manifest_name)
      with open(manifest_path) as f:
        content = f.read()
      root = parser.parse_single_xml(content, allow_include=False)
    else:
      root = parser.parse_xml_recursive(spec.name, spec.path)

    spec.entries = parser.process_parsed_result(root)
    if spec.spec_type == codechange.SPEC_FIXED:
      if not spec.is_static():
        raise ValueError('fixed spec %r has unexpected floating entries' %
                         spec.name)
    spec.revision = root.get('revision')

  def sync_disk_state(self, rev):
    manifest_name = self.get_manifest_file(rev)

    # For ChromeOS, mark_as_stable step requires 'repo init -m', which sticks
    # manifest. 'repo sync -m' is not enough
    repo_util.init(
        self.config['chromeos_root'],
        'https://chrome-internal.googlesource.com/chromeos/manifest-internal',
        manifest_name=manifest_name,
        repo_url='https://chromium.googlesource.com/external/repo.git',
        reference=self.config['chromeos_mirror'],
        # b/150753074: moblab is in non-default group and causes mark_as_stable
        # fail
        groups='default,moblab,platform-linux',
    )

    # Note, don't sync with current_branch=True for chromeos. One of its
    # build steps (inside mark_as_stable) executes "git describe" which
    # needs git tag information.
    repo_util.sync(self.config['chromeos_root'])
