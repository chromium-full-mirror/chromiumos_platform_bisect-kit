#!/usr/bin/env python2
# -*- coding: utf-8 -*-
# Copyright 2018 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Evaluate ChromeOS autotest.

Note that by default 'test_that' will install dependency packages of autotest
if the package checksum mismatch. If you want to override content of autotest
package, e.g. chrome's test binary, please make sure the autotest version
matches. Otherwise your test binary will be overwritten.
"""
from __future__ import print_function
import argparse
import logging
import os
import re
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
  cli.patching_argparser_exit(parser)
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
      '--chrome_root',
      metavar='CHROME_ROOT',
      type=cli.argtype_dir_path,
      default=configure.get('CHROME_ROOT'),
      help='Chrome tree root; necessary for telemetry tests')
  parser.add_argument(
      '--prebuilt',
      action='store_true',
      help='Run autotest using existing prebuilt package if specified; '
      'otherwise use the default one')
  parser.add_argument(
      '--reinstall',
      action='store_true',
      help='Remove existing autotest folder on the DUT first')
  parser.add_argument(
      '--reboot_before_test',
      action='store_true',
      help='Reboot before test run')

  group = parser.add_argument_group(title='Options for normal autotest tests')
  group.add_argument(
      '--test_name', help='Test name, like "video_VideoDecodeAccelerator.h264"')
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
  group.add_argument(
      '--args',
      help='Extra args passed to "test_that --args"; Overrides the default')
  group.add_argument(
      '--board',
      metavar='BOARD',
      default=configure.get('BOARD'),
      help='ChromeOS board name')

  group = parser.add_argument_group(title='Options for CTS/GTS tests')
  group.add_argument('--cts_revision', help='CTS revision, like "9.0_r3"')
  group.add_argument('--cts_abi', choices=['arm', 'x86'])
  group.add_argument(
      '--cts_prefix',
      help='Prefix of autotest test name, '
      'like cheets_CTS_N, cheets_CTS_P, cheets_GTS')
  group.add_argument(
      '--cts_module', help='CTS/GTS module name, like "CtsCameraTestCases"')
  group.add_argument(
      '--cts_test',
      help='CTS/GTS test name, like '
      '"android.hardware.cts.CameraTest#testDisplayOrientation"')
  group.add_argument('--cts_timeout', type=float, help='timeout, in seconds')
  group.add_argument(
      '--cts_runonce', action='store_true', help='run test only once')

  return parser


def parse_test_report_log(result_log, metric):
  """Parses autotest result log.

  Args:
    result_log: content of test_report.log
    metric: what metric to capture if not None

  Returns:
    passed, values:
      passed: True if test run successfully
      values: captured metric values; None if test failed or metric is None
  """
  m = re.search(r'Total PASS: (\d+)/(\d+)', result_log)
  passed = (m and m.group(1) == m.group(2))

  if not metric:
    return passed, None

  values = []
  for line in result_log.splitlines():
    m = re.match(r'^(\S+)\s+(\w+)(?:\{\d+\})?\s+(\d+\.\d+)$', line)
    if not m:
      continue
    if m.group(2) == metric:
      values.append(float(m.group(3)))
  return passed, values


def get_additional_test_args(test_name):
  """Gets extra arguments to specific test.

  Some tests may require special arguments to run.

  Args:
    test_name: test name

  Returns:
    arguments (str)
  """
  if test_name.startswith('telemetry_'):
    return 'local=True'
  return ''


def prepare_to_run_test(opts):
  # Some versions of ChromeOS SDK is broken and ship bad 'ssh' executable. This
  # works around the issue. See crbug/906289 for detail.
  # TODO(kcwu): remove this workaround once we no longer support bisecting
  # versions earlier than R73-11445.0.0.
  ssh_path = os.path.join(opts.chromeos_root, 'chroot/usr/bin/ssh')
  if os.path.exists(ssh_path):
    if 'file descriptor passing not supported' in open(ssh_path).read():
      cros_util.cros_sdk(opts.chromeos_root, 'sudo', 'emerge',
                         'net-misc/openssh')

  # test_that may use this ssh key and ssh complains its permission is too open.
  # chmod every time just before run test_that because the permission may change
  # after some git operations.
  util.check_call(
      'chmod',
      'o-r,g-r',
      'src/scripts/mod_for_test_scripts/ssh_keys/testing_rsa',
      cwd=opts.chromeos_root)

  if opts.reinstall:
    util.ssh_cmd(opts.dut, 'rm', '-rf', '/usr/local/autotest')

  if opts.reboot_before_test:
    cros_util.reboot(opts.dut)


def run_test(opts):
  """Runs an autotest test.

  Args:
    opts: An argparse.Namespace to hold command line arguments.

  Returns:
    path of test result (outside chroot)
  """
  prebuilt_autotest_dir = os.path.join(cros_util.chromeos_root_inside_chroot,
                                       cros_util.prebuilt_autotest_dir)
  # Set results dir inside source tree, so it's easier to access them outside
  # chroot.
  results_dir = os.path.join(cros_util.chromeos_root_inside_chroot,
                             'tmp/autotest_results_tmp')
  if opts.prebuilt:
    test_that_bin = os.path.join(prebuilt_autotest_dir,
                                 'site_utils/test_that.py')
  else:
    test_that_bin = '/usr/bin/test_that'
  cmd = [
      test_that_bin, opts.dut, opts.test_name, '--debug', '--results_dir',
      results_dir, '--board', opts.board
  ]
  if opts.prebuilt:
    cmd += ['--autotest_dir', prebuilt_autotest_dir]

  args = get_additional_test_args(opts.test_name)
  if opts.args:
    if args:
      logger.info(
          'default test_that args `%s` is overridden by '
          'command line option `%s`', args, opts.args)
    cmd += ['--args', opts.args]
  elif args:
    cmd += ['--args', args]

  try:
    output = cros_util.cros_sdk(
        opts.chromeos_root, *cmd, chrome_root=opts.chrome_root)
  except subprocess.CalledProcessError as e:
    output = e.output

  m = re.search(r'Finished running tests. Results can be found in (\S+)',
                output)
  if not m:
    logger.error('result dir is unknown')
    return None
  assert m.group(1) == results_dir
  return results_dir.replace(cros_util.chromeos_root_inside_chroot,
                             opts.chromeos_root)


def gather_test_result(opts, result_dir):
  result_log_path = os.path.join(result_dir, 'test_report.log')
  result_log = open(result_log_path).read()

  passed, values = parse_test_report_log(result_log, opts.metric)
  if opts.metric and not values:
    values = []
    for root, _, files in os.walk(result_dir):
      for filename in files:
        if filename != 'results-chart.json':
          continue
        full_path = os.path.join(root, filename)
        values = catapult_util.get_benchmark_values(full_path, opts.metric)
        return passed, values

  return passed, values


@cli.fatal_error_handler
def main(args=None):
  common.init()
  parser = create_argument_parser()
  opts = parser.parse_args(args)
  common.config_logging(opts)

  if not cros_util.is_dut(opts.dut):
    logger.error('%r is not a valid DUT address', opts.dut)
    return FATAL
  dut_os_version = cros_util.query_dut_short_version(opts.dut)

  is_cts = (
      opts.cts_revision or opts.cts_abi or opts.cts_prefix or opts.cts_module or
      opts.cts_test or opts.cts_runonce or opts.cts_timeout)
  if is_cts:
    if opts.test_name or opts.metric or opts.args:
      parser.error(
          'do not specify --test_name, --metric, --args for CTS/GTS tests')
    if not opts.cts_prefix:
      parser.error('--cts_prefix should be specified for CTS/GTS tests')
    if not opts.cts_module:
      parser.error('--cts_module should be specified for CTS/GTS tests')
    opts.test_name = '%s.tradefed-run-test' % opts.cts_prefix
    opts.args = 'module=%s test=%s' % (opts.cts_module, opts.cts_test)
    if opts.cts_revision:
      opts.args += ' revision=%s' % opts.cts_revision
    if opts.cts_abi:
      opts.args += ' abi=%s' % opts.cts_abi
    if opts.cts_runonce:
      opts.args += ' max_retry=0'
    if opts.cts_timeout:
      opts.args += ' timeout=%s' % opts.cts_timeout
  else:
    if not opts.test_name:
      parser.error('argument --test_name is required')

  # Verify command line options.
  if opts.metric:
    if opts.fail_to_pass:
      logger.error('--fail_to_pass is not for benchmark test (--metric)')
      return FATAL
  if opts.test_name.startswith('telemetry_'):
    if not opts.chrome_root:
      logger.error('--chrome_root is mandatory for telemetry tests')
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

  # Sanity check. The OS version should not change.
  assert dut_os_version == cros_util.query_dut_short_version(opts.dut), \
            'Someone else reflashed the DUT. ' \
            'DUT locking is not respected? b/126141102'

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
