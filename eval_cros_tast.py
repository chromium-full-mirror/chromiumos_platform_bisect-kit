#!/usr/bin/env python2
# -*- coding: utf-8 -*-
# Copyright 2019 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Evaluate ChromeOS tast tests."""
from __future__ import print_function
import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import sys

from bisect_kit import catapult_util
from bisect_kit import cli
from bisect_kit import common
from bisect_kit import configure
from bisect_kit import cros_util
from bisect_kit import util

logger = logging.getLogger(__name__)

OLD = 'old'
NEW = 'new'
SKIP = 'skip'
FATAL = 'fatal'

EXIT_CODE_MAP = {
    OLD: cli.EXIT_CODE_OLD,
    NEW: cli.EXIT_CODE_NEW,
    SKIP: cli.EXIT_CODE_SKIP,
    FATAL: cli.EXIT_CODE_FATAL,
}


def create_argument_parser():
  parser = argparse.ArgumentParser(description=__doc__)
  common.add_common_arguments(parser)
  parser.add_argument(
      'dut',
      nargs='?',
      type=cli.argtype_notempty,
      metavar='DUT',
      default=configure.get('DUT', ''))
  parser.add_argument(
      '--chromeos_root',
      type=cli.argtype_dir_path,
      metavar='CHROMEOS_ROOT',
      default=configure.get('CHROMEOS_ROOT', ''),
      help='ChromeOS tree root')
  parser.add_argument(
      '--tast_build',
      action='store_true',
      help='Build tast test bundle (-build=true) if specified; '
      'default is using prebuilt bundle on the DUT')
  parser.add_argument(
      '--reboot_before_test',
      action='store_true',
      help='Reboot before test run')

  group = parser.add_argument_group(title='Options for normal autotest tests')
  group.add_argument(
      '--test_name',
      required=True,
      help='Test name, like "video_VideoDecodeAccelerator.h264"')
  group.add_argument(
      '--fail_to_pass',
      action='store_true',
      help='For functional tests: old behavior is FAIL and new behavior is '
      'PASS; If not specified, default = old behavior is PASS and new '
      'behavior is FAIL')
  group.add_argument(
      '--metric',
      help=
      'Metric name of performance test; example: "cheets_SystemRawImageSize"')

  return parser


def prepare_to_run_test(opts):
  if opts.reboot_before_test:
    cros_util.reboot(opts.dut)


def list_tast_tests(opts, pattern=None):
  cmd = ['tast', 'list']
  if not opts.tast_build:
    cmd.append('-build=false')
  cmd.append(opts.dut)
  if pattern:
    cmd.append(pattern)
  return cros_util.cros_sdk(opts.chromeos_root, *cmd).splitlines()


def run_test(opts):
  """Runs an autotest test.

  Args:
    opts: An argparse.Namespace to hold command line arguments.

  Returns:
    path of test result (outside chroot)
  """
  # Set results dir inside source tree, so it's easier to access them outside
  # chroot.
  results_dir = os.path.join(cros_util.chromeos_root_inside_chroot,
                             'tmp/tast_results_tmp')
  results_dir_output_chroot = results_dir.replace(
      cros_util.chromeos_root_inside_chroot, opts.chromeos_root)
  # Don't reuse existing results dir, otherwise tast may rename output files.
  if os.path.exists(results_dir_output_chroot):
    shutil.rmtree(results_dir_output_chroot)

  # TODO(kcwu): add -timeout
  cmd = ['tast', '-verbose', 'run']
  if not opts.tast_build:
    cmd.append('-build=false')
  cmd += ['-resultsdir', results_dir, opts.dut, opts.test_name]

  try:
    cros_util.cros_sdk(opts.chromeos_root, *cmd)
  except subprocess.CalledProcessError:
    logger.fatal('failed to run tast, incorrect test name?')
    return None

  return results_dir_output_chroot


def gather_test_result(opts, result_dir):
  error_path = os.path.join(result_dir, 'run_error.txt')
  if os.path.exists(error_path):
    message = open(error_path).read()
    raise Exception('tast global error: %s' % message)

  results_path = os.path.join(result_dir, 'results.json')
  passed = None
  for result in json.load(open(results_path)):
    if result['name'] != opts.test_name:
      logger.warning('unexpected test ran: %s', result['name'])
      continue
    passed = result['errors'] is None
  if passed is None:
    raise Exception('no test result for "%s"?' % opts.test_name)

  values = []
  if opts.metric:
    chart_path = os.path.join(result_dir, 'tests', opts.test_name,
                              'results-chart.json')
    values = catapult_util.get_benchmark_values(chart_path, opts.metric)

  return passed, values


def main(args=None):
  common.init()
  parser = create_argument_parser()
  opts = parser.parse_args(args)
  common.config_logging(opts)

  if not cros_util.is_dut(opts.dut):
    logger.error('%r is not a valid DUT address', opts.dut)
    return FATAL

  # Verify command line options.
  if opts.metric:
    if opts.fail_to_pass:
      logger.error('--fail_to_pass is not for benchmark test (--metric)')
      return FATAL
  # Remove the "tast." prefix prepended by autotest.
  opts.test_name = re.sub(r'^tast\.', '', opts.test_name)

  tests = list_tast_tests(opts, opts.test_name)
  if tests:
    if len(tests) != 1 or tests[0] != opts.test_name:
      # For example, tast in chroot after 12205.0.0 is IPC incompatible with
      # tast on DUT earlier than 12028.0.0 (crbug/932307)
      logger.fatal('"tast list" returns unexpected tests; '
                   'incompatible tast on DUT and in chroot?')
      return FATAL
  else:
    tests = list_tast_tests(opts)
    util.show_similar_candidates('test name', opts.test_name, tests)
    return FATAL

  try:
    prepare_to_run_test(opts)
  except Exception:
    logger.exception('failed when prepare, assume it is temporary; SKIP')
    return SKIP

  result_dir = run_test(opts)
  if not result_dir:
    return FATAL

  try:
    passed, values = gather_test_result(opts, result_dir)
  except Exception:
    logger.exception('failed to parse test result')
    return FATAL

  if opts.metric:
    if not values:
      logger.warning('no values found; SKIP')
      return SKIP

    print('BISECT_RESULT_VALUES=', ' '.join(map(str, values)))
    logger.info('values=%s', values)
    # The exit code doesn't matter.
    return OLD
  else:
    if opts.fail_to_pass:
      if passed:
        logger.info('passed')
        return NEW
      logger.info('failed')
      return OLD
    else:
      if passed:
        logger.info('passed')
        return OLD
      logger.info('failed')
      return NEW


if __name__ == '__main__':
  sys.exit(EXIT_CODE_MAP[main()])
