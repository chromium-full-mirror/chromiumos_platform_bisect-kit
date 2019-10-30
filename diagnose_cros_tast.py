#!/usr/bin/env python2
# -*- coding: utf-8 -*-
# Copyright 2019 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Diagnose ChromeOS tast regressions.

This is integrated bisection utility. Given ChromeOS, Chrome, Android source
tree, and necessary parameters, this script can determine which components to
bisect, and hopefully output the culprit CL of regression.

Sometimes the script failed to figure out the final CL for various reasons, it
will cut down the search range as narrow as it can.
"""
from __future__ import print_function
import fnmatch
import logging

from bisect_kit import cros_lab_util
from bisect_kit import cros_util
from bisect_kit import diagnoser_cros
from bisect_kit import errors
import setup_cros_bisect

logger = logging.getLogger(__name__)

# What chrome binaries to build for given tast test.
# This list is created manually by inspecting output of
# 'grep -r chrome.BinTestDir src/platform/tast-tests'
# If you change them, deploy_chrome_helper.sh may need update
# as well.
# TODO(kcwu): we can get rid of this table once we migrated to build chrome
#             using chromeos ebuild rules.
CHROME_BINARIES_OF_TEST = [
    ('security.SandboxLinuxUnittests', ['sandbox_linux_unittests']),
    ('camera.CaptureUnittests', ['capture_unittests']),
    ('camera.DecodeAccelJPEG*', ['jpeg_decode_accelerator_unittest']),
    ('camera.EncodeAccelJPEG*', ['jpeg_encode_accelerator_unittest']),
    # Following three tests are renamed to camera.*, keep them for older
    # chromeos version.
    ('video.CaptureUnittests', ['capture_unittests']),
    ('video.DecodeAccelJPEG*', ['jpeg_decode_accelerator_unittest']),
    ('video.EncodeAccelJPEG*', ['jpeg_encode_accelerator_unittest']),
    # TODO(kcwu): need special handling if video_decode_accelerator_unittest
    #             is removed (crbug/933034)
    ('video.DecodeAccel*', [
        'video_decode_accelerator_tests',
        'video_decode_accelerator_perf_tests',
        'video_decode_accelerator_unittest',
    ]),
    ('video.EncodeAccel*', ['video_encode_accelerator_unittest']),
]


def determine_chrome_binaries(test_name):
  for name_pattern, binaries in CHROME_BINARIES_OF_TEST:
    if fnmatch.fnmatch(test_name, name_pattern):
      logger.info('This test depends on chrome binary: %s', binaries)
      return binaries
  return None


class DiagnoseTastCommandLine(diagnoser_cros.DiagnoseCommandLineBase):
  """Diagnose command line interface."""

  def check_options(self, opts, path_factory):
    super(DiagnoseTastCommandLine, self).check_options(opts, path_factory)
    if not opts.test_name:
      self.argument_parser.error('argument --test_name is required')

  def init_hook(self, opts):
    pass

  def _build_cmds(self):
    common_eval_cmd = [
        './eval_cros_tast.py',
        '--with_private_bundles',
        '--chromeos_root', self.config['chromeos_root'],
        '--test_name', self.config['test_name'],
    ]  # yapf: disable
    if self.config['metric']:
      common_eval_cmd += [
          '--metric', self.config['metric'],
      ]  # yapf: disable
    if self.config['fail_to_pass']:
      common_eval_cmd.append('--fail_to_pass')
    if self.config['reboot_before_test']:
      common_eval_cmd.append('--reboot_before_test')

    return common_eval_cmd

  def cmd_run(self, opts):
    del opts  # unused

    self.states.load()

    try:
      path_factory = setup_cros_bisect.DefaultProjectPathFactory(
          self.config['mirror_base'], self.config['work_base'],
          self.config['session'])
      common_eval_cmd = self._build_cmds()

      chrome_binaries = determine_chrome_binaries(self.config['test_name'])

      with cros_lab_util.dut_manager(
          self.config['dut'],
          lambda: diagnoser_cros.grab_dut(self.config)) as dut:
        if not dut:
          raise errors.NoDutAvailable('unable to allocate DUT')
        if not cros_util.is_good_dut(dut):
          if not cros_lab_util.repair(dut):
            raise errors.ExternalError('Not a good DUT and unable to repair')
        assert cros_util.is_dut(dut)
        if self.config['dut'] == cros_lab_util.LAB_DUT:
          self.config['allocated_dut'] = dut
          self.states.save()
        common_eval_cmd.append(dut)

        diagnoser = diagnoser_cros.CrosDiagnoser(self.states, path_factory,
                                                 self.config, dut)

        diagnoser.narrow_down_chromeos_prebuilt(
            self.config['old'], self.config['new'], common_eval_cmd)

        diagnoser.switch_chromeos_to_old(force=self.config['always_reflash'])
        dut_os_version = cros_util.query_dut_short_version(dut)

        try:
          if diagnoser.narrow_down_android(common_eval_cmd):
            return
        except errors.DiagnoseContradiction:
          raise
        except Exception:
          logger.exception('exception in android bisector before verification; '
                           'assume culprit is not inside android and continue')
        # Assume it's ok to leave random version of android prebuilt on DUT.

        # Sanity check. The OS version should not change after android bisect.
        assert dut_os_version == cros_util.query_dut_short_version(dut), \
            'Someone else reflashed the DUT. ' \
            'DUT locking is not respected? b/126141102'

        try:
          if self.config['chrome_deploy_image']:
            eval_cmd = common_eval_cmd + ['--tast_build']
          else:
            eval_cmd = common_eval_cmd
          if diagnoser.narrow_down_chrome(
              eval_cmd, chrome_binaries=chrome_binaries):
            return
        except errors.DiagnoseContradiction:
          raise
        except Exception:
          logger.exception('exception in chrome bisector before verification; '
                           'assume culprit is not inside chrome and continue')

        if not self.config['chrome_deploy_image']:
          # Sanity check. The OS version should not change after chrome bisect.
          assert dut_os_version == cros_util.query_dut_short_version(dut), \
              'Someone else reflashed the DUT. ' \
              'DUT locking is not respected? b/126141102'

        eval_cmd = common_eval_cmd + ['--tast_build']
        diagnoser.narrow_down_chromeos_localbuild(eval_cmd)
        logger.info('%s done', __file__)
    except Exception as e:
      logger.exception('got exception; stop')
      exception_name = e.__class__.__name__
      self.states.add_history(
          'failed', '%s: %s' % (exception_name, e), exception=exception_name)


if __name__ == '__main__':
  DiagnoseTastCommandLine().main()
