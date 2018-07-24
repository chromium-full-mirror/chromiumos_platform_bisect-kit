#!/usr/bin/env python2
# -*- coding: utf-8 -*-
# Copyright 2018 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Diagnose ChromeOS autotest regressions.

This is integrated bisection utility. Given ChromeOS, Chrome, Android source
tree, and necessary parameters, this script can determine which components to
bisect, and hopefully output the culprit CL of regression.

Sometimes the script failed to figure out the final CL for various reasons, it
will cut down the search range as narrow as it can.
"""
from __future__ import print_function
import argparse
import fnmatch
import glob
import logging
import os

from bisect_kit import cli
from bisect_kit import common
from bisect_kit import configure
from bisect_kit import core
from bisect_kit import cros_lab_util
from bisect_kit import cros_util
from bisect_kit import diagnoser_cros
from bisect_kit import util

logger = logging.getLogger(__name__)

# What chrome binaries to build for given autotest.
# This dict is created manually by inspecting output of
# 'grep -r ChromeBinaryTest autotest/files/client/site_tests'
# If you change this dict, build_and_deploy_chrome_helper.sh may need update
# as well.
CHROME_BINARIES_OF_TEST = {
    'graphics_Chrome.ozone_gl_unittests': ['ozone_gl_unittests'],
    'security_SandboxLinuxUnittests': ['sandbox_linux_unittests'],
    'video_HangoutHardwarePerf*': [
        'video_decode_accelerator_unittest',
        'video_encode_accelerator_unittest',
    ],
    'video_JDAPerf*': ['jpeg_decode_accelerator_unittest'],
    'video_JEAPerf': ['jpeg_encode_accelerator_unittest'],
    'video_JpegDecodeAccelerator': ['jpeg_decode_accelerator_unittest'],
    'video_JpegEncodeAccelerator': ['jpeg_encode_accelerator_unittest'],
    'video_VDAPerf': ['video_decode_accelerator_unittest'],
    'video_VDASanity': ['video_decode_accelerator_unittest'],
    'video_VEAPerf': ['video_encode_accelerator_unittest'],
    'video_VideoDecodeAccelerator*': ['video_decode_accelerator_unittest'],
    'video_VideoEncodeAccelerator*': ['video_encode_accelerator_unittest'],
}


def create_argument_parser():
  parser = argparse.ArgumentParser()
  common.add_common_arguments(parser)
  parser.add_argument('--session_base', default='bisect.sessions')
  parser.add_argument('--session', help='Session name', required=True)

  group = parser.add_argument_group(title='Essential options')
  group.add_argument(
      '--chromeos_root',
      metavar='CHROMEOS_ROOT',
      type=cli.argtype_dir_path,
      required=True,
      default=configure.get('CHROMEOS_ROOT'),
      help='ChromeOS tree root')
  group.add_argument(
      '--android_root',
      metavar='ANDROID_ROOT',
      type=cli.argtype_dir_path,
      required=True,
      default=configure.get('ANDROID_ROOT'),
      help='Android tree root')
  group.add_argument(
      '--android_repo_mirror_dir',
      type=cli.argtype_dir_path,
      required=True,
      default=configure.get('ANDROID_REPO_MIRROR_DIR'),
      help='Android repo mirror path')
  group.add_argument(
      '--chrome_root',
      metavar='CHROME_ROOT',
      type=cli.argtype_dir_path,
      required=True,
      default=configure.get('CHROME_ROOT'),
      help='Chrome tree root')
  group.add_argument(
      '--old',
      type=cros_util.argtype_cros_version,
      required=True,
      help='ChromeOS version with old behavior')
  group.add_argument(
      '--new',
      type=cros_util.argtype_cros_version,
      required=True,
      help='ChromeOS version with new behavior')
  group.add_argument('test_name', help='Test name')

  group = parser.add_argument_group(title='DUT allocation options')
  group.add_argument(
      'dut',
      metavar='DUT',
      help='Address of DUT (Device Under Test). If "%s", DUT will be '
      'automatically allocated from the lab' % cros_lab_util.LAB_DUT)
  group.add_argument(
      '--board',
      metavar='BOARD',
      default=configure.get('BOARD'),
      help='ChromeOS board name')
  group.add_argument(
      '--sku',
      metavar='SKU',
      help='SKU criteria if DUT is auto allocated from the lab')

  group = parser.add_argument_group(title='Options for benchmark test')
  group.add_argument('--metric', help='Metric name of benchmark test')
  group.add_argument(
      '--old_value', type=float, help='For benchmark test, old value of metric')
  group.add_argument(
      '--new_value', type=float, help='For benchmark test, new value of metric')

  group = parser.add_argument_group(title='Options passed to test_that')
  group.add_argument(
      '--args',
      help='Extra args passed to "test_that --args"; Overrides the default')

  group = parser.add_argument_group(title='Bisect behavior options')
  group.add_argument(
      '--noisy',
      help='Enable noisy binary search. Example value: "old=1/10,new=2/3"')
  group.add_argument(
      '--always_reflash',
      action='store_true',
      help='Do not trust ChromeOS version number of DUT and always reflash. '
      'This is usually only needed when resume because previous bisect was '
      'interrupted and the DUT may be in an unexpected state')

  return parser


def grab_dut(opts):
  # Assume "DEPENDENCIES" is identical between the period of
  # `old` and `new` version.
  autotest_dir = os.path.join(opts.chromeos_root,
                              cros_util.prebuilt_autotest_dir)
  info = cros_util.get_autotest_test_info(autotest_dir, opts.test_name)
  assert info

  extra_labels = []
  dependencies = info.variables.get('DEPENDENCIES', '')
  for label in dependencies.split(','):
    label = label.strip()
    # Skip non-machine labels
    if label in ['cleanup-reboot']:
      continue
    extra_labels.append(label)

  reason = 'bisect-kit: %s' % opts.session
  host = cros_lab_util.allocate_host(reason, opts.board, opts.sku, extra_labels)
  if not host:
    logger.error('unable to allocate dut')
    return None

  logger.info('allocated host %s', host)
  return host


def may_depend_on_extra_chrome_binaries(autotest_dir, test_name):
  info = cros_util.get_autotest_test_info(autotest_dir, test_name)
  assert info
  dirpath = os.path.dirname(info.path)
  for pypath in glob.glob(os.path.join(dirpath, '*.py')):
    if 'ChromeBinaryTest' in open(pypath).read():
      return True
  return False


def determine_chrome_binaries(opts):
  chrome_binaries = None
  for name_pattern, binaries in CHROME_BINARIES_OF_TEST.items():
    if fnmatch.fnmatch(opts.test_name, name_pattern):
      chrome_binaries = binaries
      break

  autotest_dir = os.path.join(opts.chromeos_root,
                              cros_util.prebuilt_autotest_dir)
  if chrome_binaries:
    logger.info('This test depends on chrome binary: %s', chrome_binaries)
  elif may_depend_on_extra_chrome_binaries(autotest_dir, opts.test_name):
    logger.warning('%s code used ChromeBinaryTest but the binary is unknown; '
                   'please update CHROME_BINARIES_OF_TEST table',
                   opts.test_name)
  return chrome_binaries


def check_options(opts, parser):
  if opts.dut == cros_lab_util.LAB_DUT:
    session_dir = os.path.join(opts.session_base, opts.session)
    # 1 is log file
    if os.path.exists(session_dir) and len(os.listdir(session_dir)) > 1:
      parser.error('Do not resume if the DUT is dynamically allocated; '
                   '%s is not empty' % session_dir)

  if not opts.board:
    if opts.dut == cros_lab_util.LAB_DUT:
      parser.error(
          '--board need to be specified if DUT is "%s"' % cros_lab_util.LAB_DUT)
    else:
      opts.board = cros_util.query_dut_board(opts.dut)

  if cros_util.is_cros_short_version(opts.old):
    opts.old = cros_util.version_to_full(opts.board, opts.old)
  if cros_util.is_cros_short_version(opts.new):
    opts.new = cros_util.version_to_full(opts.board, opts.new)

  if opts.metric:
    if opts.old_value is None:
      parser.error('--old_value is not provided')
    if opts.new_value is None:
      parser.error('--new_value is not provided')
  else:
    if opts.old_value is not None:
      parser.error('--old_value is provided but --metric is not')
    if opts.new_value is not None:
      parser.error('--new_value is provided but --metric is not')


def main(args=None):
  common.init()
  parser = create_argument_parser()
  opts = parser.parse_args(args)
  common.config_logging(opts)
  check_options(opts, parser)

  # prebuilt version will be specified later.
  common_switch_cmd = [
      './switch_autotest_prebuilt.py',
      '--chromeos_root', opts.chromeos_root,
      '--test_name', opts.test_name,
      '--board', opts.board,
  ]  # yapf: disable

  common_eval_cmd = [
      './eval_cros_autotest.py',
      '--chromeos_root', opts.chromeos_root,
      '--test_name', opts.test_name,
  ]  # yapf: disable
  if opts.metric:
    common_eval_cmd += [
        '--metric', opts.metric,
        '--old_value', str(opts.old_value),
        '--new_value', str(opts.new_value),
    ]  # yapf: disable
  if opts.args:
    common_eval_cmd += ['--args', opts.args]

  # Unpack old autotest prebuilt, assume following information don't change
  # between versions:
  #  - what chrome binaries to run
  #  - dependency labels for DUT allocation
  util.check_call(*(common_switch_cmd + [opts.old]))

  chrome_binaries = determine_chrome_binaries(opts)

  with cros_lab_util.dut_manager(opts.dut, lambda: grab_dut(opts)) as dut:
    if not dut:
      raise core.ExecutionFatalError('unable to allocate DUT')
    assert cros_util.is_dut(dut)
    opts.dut = dut
    common_eval_cmd.append(opts.dut)

    diagnoser = diagnoser_cros.CrosDiagnoser(
        opts.session, opts.chromeos_root, opts.android_root,
        opts.android_repo_mirror_dir, opts.chrome_root, opts.board, opts.noisy,
        opts.dut)

    eval_cmd = common_eval_cmd + ['--prebuilt', '--reinstall']
    # Do not specify version for autotest prebuilt switching here. The trick is
    # that version number is obtained via bisector's environment variable
    # CROS_VERSION.
    extra_switch_cmd = common_switch_cmd
    if not diagnoser.narrow_down_chromeos_prebuilt(
        opts.old, opts.new, eval_cmd, extra_switch_cmd=extra_switch_cmd):
      return

    diagnoser.switch_chromeos_to_old(force=opts.always_reflash)
    util.check_call(*(common_switch_cmd + [diagnoser.cros_old]))
    util.check_call('ssh', opts.dut, 'rm', '-rf', '/usr/local/autotest')

    if diagnoser.narrow_down_android(eval_cmd) is not None:
      return
    # Assume it's ok to leave random version of android prebuilt on DUT.

    # Don't --reinstall to keep chrome binaries override.
    eval_cmd = common_eval_cmd + ['--prebuilt']
    if diagnoser.narrow_down_chrome(
        eval_cmd, chrome_binaries=chrome_binaries) is not None:
      return

    eval_cmd = common_eval_cmd + ['--reinstall']
    if diagnoser.narrow_down_chromeos_localbuild(eval_cmd):
      logger.info('%s done', __file__)


if __name__ == '__main__':
  main()
