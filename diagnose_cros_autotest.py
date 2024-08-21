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

import logging
import os

from bisect_kit import cros_util
from bisect_kit import diagnoser_cros
from bisect_kit import errors
from bisect_kit import util


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

    def check_options(self, opts):
        super().check_options(opts)

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
                    'do not specify --test-name, --metric, '
                    + '--extra-test-variables for CTS/GTS tests'
                )
            if not opts.cts_prefix:
                self.argument_parser.error(
                    '--cts-prefix should be specified for CTS/GTS tests'
                )
            if not opts.cts_module:
                self.argument_parser.error(
                    '--cts-module should be specified for CTS/GTS tests'
                )
            opts.test_name = '%s.tradefed-run-test' % opts.cts_prefix
        elif not opts.test_name:
            self.argument_parser.error(
                '--test-name should be specified if not CTS/GTS tests'
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

    def _build_cmds(self):
        # prebuilt version will be specified later.
        switch_test_harness_cmd = [
            './switch_autotest_prebuilt.py',
            '--rich-result',
            '--chromeos-root',
            self.config.get('chromeos_root'),
        ]
        if self.config.get('test_name') and not self.config.get('cts_test'):
            switch_test_harness_cmd += [
                '--test-name',
                self.config.get('test_name'),
            ]

        common_eval_cmd = [
            './eval_cros_autotest.py',
            '--rich-result',
            '--chromeos-root',
            self.config.get('chromeos_root'),
        ]
        if self.config.get('test_name') and not self.config.get('cts_test'):
            common_eval_cmd += ['--test-name', self.config.get('test_name')]
        if self.config.get('metric'):
            common_eval_cmd += [
                '--metric',
                self.config.get('metric'),
            ]
        if self.config.get('fail_to_pass'):
            common_eval_cmd.append('--fail-to-pass')
        if self.config.get('reboot_before_test'):
            common_eval_cmd.append('--reboot-before-test')
        for var in self.config.get('extra_test_variables'):
            common_eval_cmd.append('--args')
            common_eval_cmd.append(var)

        if self.config.get('test_name').startswith('telemetry_'):
            common_eval_cmd += ['--chrome-root', self.config.get('chrome_root')]

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
                    str(self.config.get(arg_name)),
                ]

        if self.config.get('consider_test_crash_as_failure'):
            common_eval_cmd.append('--consider-test-crash-as-failure')

        return switch_test_harness_cmd, common_eval_cmd

    def do_run(self):
        diagnoser = diagnoser_cros.CrosDiagnoser(self.states, self.config)

        switch_test_harness_cmd, common_eval_cmd = self._build_cmds()

        # Do not specify version for autotest prebuilt switching here. The trick
        # is that version number is obtained via bisector's environment variable
        # CROS_VERSION.
        cros_prebuilt_eval_cmd = common_eval_cmd + ['--prebuilt']
        android_prebuilt_eval_cmd = common_eval_cmd + ['--prebuilt']
        chrome_localbuild_eval_cmd = common_eval_cmd + ['--prebuilt']
        should_build_chrome_localbuild_with_tests = bool(
            self.config.get('test_name')
        )  # not CTS/GTS
        cros_buildbucket_build = cros_util.is_buildbucket_buildable(
            self.config.get('cros_prebuilt_old')
        ) and not self.config.get('disable_buildbucket_chromeos')
        if not cros_buildbucket_build:
            cros_localbuild_eval_cmd = common_eval_cmd[:]
        else:
            cros_localbuild_eval_cmd = common_eval_cmd + ['--prebuilt']

        diagnoser.diagnose(
            is_autotest=True,
            switch_test_harness_cmd=switch_test_harness_cmd,
            cros_prebuilt_eval_cmd=cros_prebuilt_eval_cmd,
            android_prebuilt_eval_cmd=android_prebuilt_eval_cmd,
            chrome_localbuild_eval_cmd=chrome_localbuild_eval_cmd,
            should_build_chrome_localbuild_with_tests=should_build_chrome_localbuild_with_tests,
            cros_localbuild_eval_cmd=cros_localbuild_eval_cmd,
        )

    @util.MethodTimer(
        diagnoser_cros.DiagnoseCommandLineBase.write_total_execution_time
    )
    def cmd_run(self, opts):
        del opts  # unused

        self.states.load_states()

        try:
            logger.info('experiments: %s', self.config.get('experiments'))
            self.do_run()
            logger.info('%s done', __file__)
        except Exception as e:
            logger.exception('got exception; stop')
            exception_name = e.__class__.__name__
            self.states.add_history(
                'failed',
                text='%s: %s' % (exception_name, e),
                exception=exception_name,
                error_type=errors.error_type(e),
            )

    def create_argument_parser_hook(self, parser_init):
        group = parser_init.add_argument_group(
            title='Options for CTS/GTS tests'
        )
        group.add_argument('--cts-revision', help='CTS revision, like "9.0_r3"')
        group.add_argument('--cts-abi', choices=['arm', 'x86'])
        group.add_argument(
            '--cts-prefix',
            help='Prefix of autotest test name, '
            'like cheets_CTS_N, cheets_CTS_P, cheets_GTS',
        )
        group.add_argument(
            '--cts-module',
            help='CTS/GTS module name, like "CtsCameraTestCases"',
        )
        group.add_argument(
            '--cts-test',
            help='CTS/GTS test name, like '
            '"android.hardware.cts.CameraTest#testDisplayOrientation"',
        )
        group.add_argument(
            '--cts-timeout', type=float, help='timeout, in seconds'
        )


if __name__ == '__main__':
    DiagnoseAutotestCommandLine().main()
