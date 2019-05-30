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
import fnmatch
import glob
import logging
import os

from bisect_kit import cros_lab_util
from bisect_kit import cros_util
from bisect_kit import diagnoser_cros
from bisect_kit import errors
from bisect_kit import util
import setup_cros_bisect

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
    'video_VDAPerf*': ['video_decode_accelerator_unittest'],
    'video_VDASanity': ['video_decode_accelerator_unittest'],
    'video_VEAPerf*': ['video_encode_accelerator_unittest'],
    'video_VideoDecodeAccelerator*': ['video_decode_accelerator_unittest'],
    'video_VideoEncodeAccelerator*': ['video_encode_accelerator_unittest'],
}


def get_test_dependency_labels(config):
  # Assume "DEPENDENCIES" is identical between the period of
  # `old` and `new` version.
  autotest_dir = os.path.join(config['chromeos_root'],
                              cros_util.prebuilt_autotest_dir)
  info = cros_util.get_autotest_test_info(autotest_dir, config['test_name'])
  assert info, 'incorrect test name? %s' % config['test_name']

  extra_labels = []
  dependencies = info.variables.get('DEPENDENCIES', '')
  for label in dependencies.split(','):
    label = label.strip()
    # Skip non-machine labels
    if not label or label in ['cleanup-reboot']:
      continue
    extra_labels.append(label)

  return extra_labels


def grab_dut(config):
  reason = cros_lab_util.make_lock_reason(config['session'])
  if config.get('allocated_dut'):
    host_name = cros_lab_util.dut_host_name(config['allocated_dut'])
    logger.info('try to lock the same host (%s) as last run', host_name)
    candidates = cros_lab_util.list_host(host=host_name).values()
  else:
    extra_labels = get_test_dependency_labels(config)
    candidates = cros_lab_util.seek_host(
        pools=config.get('pools', '').split(','),
        model=config['model'],
        sku=config['sku'],
        extra_labels=extra_labels)

  if (not candidates or candidates[0]['Status'] != 'Ready' or
      candidates[0]['Locked']):
    logger.error('unable to allocate dut')
    return None

  host_name = candidates[0]['Host']
  info = cros_lab_util.lock_host(host_name, reason)
  if info['Status'] != 'Ready':
    cros_lab_util.unlock_host(host_name)
    raise Exception(
        'unexpected host status=%s, a race condition?' % info['Status'])

  logger.info('allocated host %s', host_name)
  return host_name


def may_depend_on_extra_chrome_binaries(autotest_dir, test_name):
  info = cros_util.get_autotest_test_info(autotest_dir, test_name)
  assert info, 'incorrect test name? %s' % test_name
  dirpath = os.path.dirname(info.path)
  for pypath in glob.glob(os.path.join(dirpath, '*.py')):
    if 'ChromeBinaryTest' in open(pypath).read():
      return True
  return False


def determine_chrome_binaries(chromeos_root, test_name):
  chrome_binaries = None
  for name_pattern, binaries in CHROME_BINARIES_OF_TEST.items():
    if fnmatch.fnmatch(test_name, name_pattern):
      chrome_binaries = binaries
      break

  autotest_dir = os.path.join(chromeos_root, cros_util.prebuilt_autotest_dir)
  if chrome_binaries:
    logger.info('This test depends on chrome binary: %s', chrome_binaries)
  elif may_depend_on_extra_chrome_binaries(autotest_dir, test_name):
    raise errors.InternalError(
        '%s code used ChromeBinaryTest but the binary is unknown; '
        'please update CHROME_BINARIES_OF_TEST table' % test_name)
  return chrome_binaries


class DiagnoseAutotestCommandLine(diagnoser_cros.DiagnoseCommandLineBase):
  """Diagnose command line interface."""

  def check_options(self, opts, path_factory):
    super(DiagnoseAutotestCommandLine, self).check_options(opts, path_factory)

    is_cts = (
        opts.cts_revision or opts.cts_abi or opts.cts_prefix or
        opts.cts_module or opts.cts_test or opts.cts_timeout)
    if is_cts:
      if opts.test_name or opts.metric or opts.args:
        self.argument_parser.error(
            'do not specify --test_name, --metric, --args for CTS/GTS tests')
      if not opts.cts_prefix:
        self.argument_parser.error(
            '--cts_prefix should be specified for CTS/GTS tests')
      if not opts.cts_module:
        self.argument_parser.error(
            '--cts_module should be specified for CTS/GTS tests')
      opts.test_name = '%s.tradefed-run-test' % opts.cts_prefix
    elif not opts.test_name:
      self.argument_parser.error(
          '--test_name should be specified if not CTS/GTS tests')

  def init_hook(self, opts):
    self.states.config.update(
        cts_revision=opts.cts_revision,
        cts_abi=opts.cts_abi,
        cts_prefix=opts.cts_prefix,
        cts_module=opts.cts_module,
        cts_test=opts.cts_test,
        cts_timeout=opts.cts_timeout,
        test_that_args=opts.args,
    )

    # Unpack old autotest prebuilt, assume following information don't change
    # between versions:
    #  - what chrome binaries to run
    #  - dependency labels for DUT allocation
    common_switch_cmd, _common_eval_cmd = self._build_cmds()
    util.check_call(*(common_switch_cmd + [self.config['old']]))

  def _build_cmds(self):
    # prebuilt version will be specified later.
    common_switch_cmd = [
        './switch_autotest_prebuilt.py',
        '--chromeos_root', self.config['chromeos_root'],
        '--board', self.config['board'],
    ]  # yapf: disable
    if self.config['test_name'] and not self.config['cts_test']:
      common_switch_cmd += ['--test_name', self.config['test_name']]

    common_eval_cmd = [
        './eval_cros_autotest.py',
        '--chromeos_root', self.config['chromeos_root'],
    ]  # yapf: disable
    if self.config['test_name'] and not self.config['cts_test']:
      common_eval_cmd += ['--test_name', self.config['test_name']]
    if self.config['metric']:
      common_eval_cmd += [
          '--metric', self.config['metric'],
      ]  # yapf: disable
    if self.config['fail_to_pass']:
      common_eval_cmd.append('--fail_to_pass')
    if self.config['reboot_before_test']:
      common_eval_cmd.append('--reboot_before_test')
    if self.config['test_that_args']:
      common_eval_cmd += ['--args', self.config['test_that_args']]
    if self.config['test_name'].startswith('telemetry_'):
      common_eval_cmd += ['--chrome_root', self.config['chrome_root']]

    for arg_name in [
        'cts_revision', 'cts_abi', 'cts_prefix', 'cts_module', 'cts_test',
        'cts_timeout'
    ]:
      if self.config.get(arg_name) is not None:
        common_eval_cmd += ['--%s' % arg_name, str(self.config[arg_name])]
    if self.config.get('cts_runonce'):
      common_eval_cmd.append('--cts_runonce')

    return common_switch_cmd, common_eval_cmd

  def cmd_run(self, opts):
    del opts  # unused

    self.states.load()

    try:
      path_factory = setup_cros_bisect.DefaultProjectPathFactory(
          self.config['mirror_base'], self.config['work_base'],
          self.config['session'])
      common_switch_cmd, common_eval_cmd = self._build_cmds()

      if self.config['test_name']:
        chrome_binaries = determine_chrome_binaries(
            self.config['chromeos_root'], self.config['test_name'])
      else:
        chrome_binaries = None

      with cros_lab_util.dut_manager(self.config['dut'],
                                     lambda: grab_dut(self.config)) as dut:
        if not dut:
          raise errors.NoDutAvailable('unable to allocate DUT')
        assert cros_util.is_dut(dut)
        if self.config['dut'] == cros_lab_util.LAB_DUT:
          self.config['allocated_dut'] = dut
          self.states.save()
        common_eval_cmd.append(dut)

        diagnoser = diagnoser_cros.CrosDiagnoser(self.states, path_factory,
                                                 self.config, dut)

        eval_cmd = common_eval_cmd + ['--prebuilt', '--reinstall']
        # Do not specify version for autotest prebuilt switching here. The trick
        # is that version number is obtained via bisector's environment variable
        # CROS_VERSION.
        extra_switch_cmd = common_switch_cmd
        diagnoser.narrow_down_chromeos_prebuilt(
            self.config['old'],
            self.config['new'],
            eval_cmd,
            extra_switch_cmd=extra_switch_cmd)

        diagnoser.switch_chromeos_to_old(force=self.config['always_reflash'])
        util.check_call(*(common_switch_cmd + [diagnoser.cros_old]))
        # Now, the version of autotest on the DUT is unknown. This is harmless
        # to the final diagnose result. Even if the culprit is inside autotest
        # itself, the verification step of chrome bisection will know that
        # chrome is not culprit and go on.
        # Do not delete existing autotest folders on the DUT, so chrome custom
        # binary tests can reuse them.

        try:
          if diagnoser.narrow_down_android(eval_cmd):
            return
        except errors.DiagnoseContradiction:
          raise
        except Exception:
          logger.exception('exception in android bisector before verification; '
                           'assume culprit is not inside android and continue')
        # Assume it's ok to leave random version of android prebuilt on DUT.

        # Don't --reinstall to keep chrome binaries override.
        eval_cmd = common_eval_cmd + ['--prebuilt']
        try:
          if diagnoser.narrow_down_chrome(
              eval_cmd, chrome_binaries=chrome_binaries):
            return
        except errors.DiagnoseContradiction:
          raise
        except Exception:
          logger.exception('exception in chrome bisector before verification; '
                           'assume culprit is not inside chrome and continue')

        eval_cmd = common_eval_cmd + ['--reinstall']
        diagnoser.narrow_down_chromeos_localbuild(eval_cmd)
        logger.info('%s done', __file__)
    except Exception as e:
      logger.exception('got exception; stop')
      exception_name = e.__class__.__name__
      self.states.add_history(
          'failed', '%s: %s' % (exception_name, e), exception=exception_name)

  def create_argument_parser_hook(self, parser_init):
    group = parser_init.add_argument_group(title='Options for CTS/GTS tests')
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

    group = parser_init.add_argument_group(title='Options passed to test_that')
    group.add_argument(
        '--args',
        help='Extra args passed to "test_that --args"; Overrides the default')


if __name__ == '__main__':
  DiagnoseAutotestCommandLine().main()
