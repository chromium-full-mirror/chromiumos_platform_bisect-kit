#!/usr/bin/env python3
# Copyright 2018 The ChromiumOS Authors
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

import logging
import os

from bisect_kit import cros_lab_util
from bisect_kit import cros_util
from bisect_kit import diagnoser_cros
from bisect_kit import errors
from bisect_kit import util
import setup_cros_bisect


logger = logging.getLogger(__name__)


def get_test_dependency_labels(config):
    # Assume "DEPENDENCIES" is identical between the period of
    # `old` and `new` version.
    autotest_dir = os.path.join(
        config['chromeos_root'], cros_util.prebuilt_autotest_dir
    )
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


class DiagnoseAutotestCommandLine(diagnoser_cros.DiagnoseCommandLineBase):
    """Diagnose command line interface."""

    def check_options(self, opts, path_factory):
        super().check_options(opts, path_factory)

        is_cts = (
            opts.cts_revision
            or opts.cts_abi
            or opts.cts_prefix
            or opts.cts_module
            or opts.cts_test
            or opts.cts_timeout
        )
        if is_cts:
            if opts.test_name or opts.metric or opts.extra_test_variables:
                self.argument_parser.error(
                    'do not specify --test_name, --metric, '
                    + '--extra_test_variables for CTS/GTS tests'
                )
            if not opts.cts_prefix:
                self.argument_parser.error(
                    '--cts_prefix should be specified for CTS/GTS tests'
                )
            if not opts.cts_module:
                self.argument_parser.error(
                    '--cts_module should be specified for CTS/GTS tests'
                )
            opts.test_name = '%s.tradefed-run-test' % opts.cts_prefix
        elif not opts.test_name:
            self.argument_parser.error(
                '--test_name should be specified if not CTS/GTS tests'
            )

    def init_hook(self, opts):
        self.states.config.update(
            cts_revision=opts.cts_revision,
            cts_abi=opts.cts_abi,
            cts_prefix=opts.cts_prefix,
            cts_module=opts.cts_module,
            cts_test=opts.cts_test,
            cts_timeout=opts.cts_timeout,
        )

        # Unpack old autotest prebuilt, assume following information don't change
        # between versions:
        #  - what chrome binaries to run
        #  - dependency labels for DUT allocation
        common_switch_cmd, _common_eval_cmd = self._build_cmds()
        util.check_call(
            *(
                common_switch_cmd
                + [self.config['cros_prebuilt_old'], '--dut', opts.dut]
            )
        )

    def _build_cmds(self):
        # prebuilt version will be specified later.
        common_switch_cmd = [
            './switch_autotest_prebuilt.py',
            '--rich_result',
            '--chromeos_root',
            self.config['chromeos_root'],
            '--board',
            self.config['board'],
        ]
        if self.config['test_name'] and not self.config['cts_test']:
            common_switch_cmd += ['--test_name', self.config['test_name']]

        common_eval_cmd = [
            './eval_cros_autotest.py',
            '--rich_result',
            '--chromeos_root',
            self.config['chromeos_root'],
        ]  # yapf: disable
        if self.config['test_name'] and not self.config['cts_test']:
            common_eval_cmd += ['--test_name', self.config['test_name']]
        if self.config['metric']:
            common_eval_cmd += [
                '--metric',
                self.config['metric'],
            ]  # yapf: disable
        if self.config['fail_to_pass']:
            common_eval_cmd.append('--fail_to_pass')
        if self.config['reboot_before_test']:
            common_eval_cmd.append('--reboot_before_test')
        for var in self.config['extra_test_variables']:
            common_eval_cmd.append('--args')
            common_eval_cmd.append(var)

        if self.config['test_name'].startswith('telemetry_'):
            common_eval_cmd += ['--chrome_root', self.config['chrome_root']]

        for arg_name in [
            'cts_revision',
            'cts_abi',
            'cts_prefix',
            'cts_module',
            'cts_test',
            'cts_timeout',
        ]:
            if self.config.get(arg_name) is not None:
                common_eval_cmd += [
                    '--%s' % arg_name,
                    str(self.config[arg_name]),
                ]

        if self.config['consider_test_crash_as_failure']:
            common_eval_cmd.append('--consider_test_crash_as_failure')

        return common_switch_cmd, common_eval_cmd

    def cmd_run(self, opts):
        del opts  # unused

        self.states.load_states()

        try:
            path_factory = setup_cros_bisect.DefaultProjectPathFactory(
                self.config['mirror_base'],
                self.config['work_base'],
                self.config['session'],
            )
            common_switch_cmd, common_eval_cmd = self._build_cmds()

            lease_reason = cros_lab_util.make_lease_reason(
                self.config['session']
            )
            with cros_lab_util.dut_manager(
                self.config['dut'],
                lease_reason,
                lambda: diagnoser_cros.grab_dut(self.config),
            ) as dut:
                if not dut:
                    raise errors.NoDutAvailable('unable to allocate DUT')

                if not cros_util.is_good_dut(dut):
                    if not cros_lab_util.repair(dut):
                        raise errors.ExternalError(
                            'Not a good DUT and unable to repair'
                        )
                if self.config['dut'] == cros_lab_util.LAB_DUT:
                    self.config['allocated_dut'] = dut
                    self.states.save()
                common_eval_cmd.append(dut)

                util.check_call(
                    './switch_cros_localbuild.py',
                    '--nobuild',
                    '--chromeos_root',
                    self.config['chromeos_root'],
                    '--chromeos_mirror',
                    self.config['chromeos_mirror'],
                    '--board',
                    self.config['board'],
                    self.config['cros_prebuilt_new'],
                )

                diagnoser = diagnoser_cros.CrosDiagnoser(
                    self.states, path_factory, self.config, dut
                )

                # Do not specify version for autotest prebuilt switching here. The trick
                # is that version number is obtained via bisector's environment variable
                # CROS_VERSION.
                cros_prebuilt_eval_cmd = common_eval_cmd + ['--prebuilt']
                cros_prebuilt_extra_switch_cmd = common_switch_cmd
                android_prebuilt_eval_cmd = common_eval_cmd + ['--prebuilt']
                lacros_prebuilt_eval_cmd = common_eval_cmd + ['--prebuilt']
                chrome_localbuild_eval_cmd = common_eval_cmd + ['--prebuilt']
                chrome_with_tests = bool(
                    self.config['test_name']
                )  # not CTS/GTS
                cros_buildbucket_build = (
                    cros_util.is_buildbucket_buildable(
                        self.config['cros_prebuilt_old']
                    )
                    and not self.config['disable_buildbucket_chromeos']
                )
                if not cros_buildbucket_build:
                    cros_localbuild_eval_cmd = common_eval_cmd
                    cros_localbuild_extra_switch_cmd = None
                else:
                    cros_localbuild_eval_cmd = common_eval_cmd + ['--prebuilt']
                    cros_localbuild_extra_switch_cmd = common_switch_cmd

                diagnoser.diagnose(
                    autotest=True,
                    common_switch_cmd=common_switch_cmd,
                    cros_prebuilt_extra_switch_cmd=cros_prebuilt_extra_switch_cmd,
                    cros_prebuilt_eval_cmd=cros_prebuilt_eval_cmd,
                    android_prebuilt_eval_cmd=android_prebuilt_eval_cmd,
                    lacros_prebuilt_eval_cmd=lacros_prebuilt_eval_cmd,
                    chrome_localbuild_eval_cmd=chrome_localbuild_eval_cmd,
                    chrome_localbuild_with_tests=chrome_with_tests,
                    cros_localbuild_eval_cmd=cros_localbuild_eval_cmd,
                    cros_localbuild_extra_switch_cmd=cros_localbuild_extra_switch_cmd,
                )
                logger.info('%s done', __file__)
        except Exception as e:
            logger.exception('got exception; stop')
            exception_name = e.__class__.__name__
            self.states.add_history(
                'failed',
                text='%s: %s' % (exception_name, e),
                exception=exception_name,
            )

    def create_argument_parser_hook(self, parser_init):
        group = parser_init.add_argument_group(
            title='Options for CTS/GTS tests'
        )
        group.add_argument('--cts_revision', help='CTS revision, like "9.0_r3"')
        group.add_argument('--cts_abi', choices=['arm', 'x86'])
        group.add_argument(
            '--cts_prefix',
            help='Prefix of autotest test name, '
            'like cheets_CTS_N, cheets_CTS_P, cheets_GTS',
        )
        group.add_argument(
            '--cts_module',
            help='CTS/GTS module name, like "CtsCameraTestCases"',
        )
        group.add_argument(
            '--cts_test',
            help='CTS/GTS test name, like '
            '"android.hardware.cts.CameraTest#testDisplayOrientation"',
        )
        group.add_argument(
            '--cts_timeout', type=float, help='timeout, in seconds'
        )


if __name__ == '__main__':
    DiagnoseAutotestCommandLine().main()
