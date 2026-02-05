#!/usr/bin/env python3
# Copyright 2019 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Diagnose ChromeOS tast regressions.

This is integrated bisection utility. Given ChromeOS, Chrome, Android source
tree, and necessary parameters, this script can determine which components to
bisect, and hopefully output the culprit CL of regression.

Sometimes the script failed to figure out the final CL for various reasons, it
will cut down the search range as narrow as it can.
"""

import logging

from bisect_kit import cros_util
from bisect_kit import diagnoser_cros
from bisect_kit import errors
from bisect_kit import util


logger = logging.getLogger(__name__)


class DiagnoseTastCommandLine(diagnoser_cros.DiagnoseCommandLineBase):
    """Diagnose command line interface."""

    def check_options(self, opts):
        super().check_options(opts)
        if not opts.test_name:
            self.argument_parser.error('argument --test-name is required')

    def init_hook(self, opts):
        pass

    def _build_cmds(self):
        # prebuilt version will be specified later.
        switch_test_harness_cmd: list[str] | None = [
            './switch_tast_prebuilt.py',
            '--rich-result',
            '--chromeos-root',
            self.config.get('chromeos_root'),
        ]
        common_eval_cmd = [
            './eval_cros_tast.py',
            '--rich-result',
            '--chromeos-root',
            self.config.get('chromeos_root'),
            '--test-name',
            self.config.get('test_name'),
        ]
        if not self.config.get('is_public_build'):
            common_eval_cmd.append('--with-private-bundles')
        if self.config.get('metric'):
            common_eval_cmd += [
                '--metric',
                self.config.get('metric'),
            ]
        if self.config.get('tast_patch_cls'):
            for patch_cl in self.config.get('tast_patch_cls'):
                common_eval_cmd += ['--tast-patch-cl', patch_cl]
            # With tast_patch_cls switch tast harness cmd is redundant
            switch_test_harness_cmd = None
        if self.config.get('tast_revision'):
            common_eval_cmd += [
                '--tast-revision',
                self.config.get('tast_revision'),
            ]
            switch_test_harness_cmd = None
        if self.config.get('tast_private_revision'):
            common_eval_cmd += [
                '--tast-private-revision',
                self.config.get('tast_private_revision'),
            ]
            switch_test_harness_cmd = None
        if self.config.get('tast_runtime_revision'):
            common_eval_cmd += [
                '--tast-runtime-revision',
                self.config.get('tast_runtime_revision'),
            ]
            switch_test_harness_cmd = None
        if self.config.get('fail_to_pass'):
            common_eval_cmd.append('--fail-to-pass')
        if self.config.get('reboot_before_test'):
            common_eval_cmd.append('--reboot-before-test')
        for var in self.config.get('extra_test_variables'):
            common_eval_cmd.append('--args')
            common_eval_cmd.append(var)
        if self.config.get('consider_test_crash_as_failure'):
            common_eval_cmd.append('--consider-test-crash-as-failure')

        return switch_test_harness_cmd, common_eval_cmd

    def do_run(self):
        diagnoser = diagnoser_cros.CrosDiagnoser(self.states, self.config)

        switch_test_harness_cmd, common_eval_cmd = self._build_cmds()

        cros_prebuilt_eval_cmd = common_eval_cmd[:]
        android_prebuilt_eval_cmd = common_eval_cmd
        if self.config.get('chrome_deploy_image'):
            chrome_localbuild_eval_cmd = common_eval_cmd + ['--tast-build']
        else:
            chrome_localbuild_eval_cmd = common_eval_cmd + ['--prebuilt']

        cros_buildbucket_build = cros_util.is_buildbucket_buildable(
            self.config.get('cros_prebuilt_old')
        ) and not self.config.get('disable_buildbucket_chromeos')
        if not cros_buildbucket_build:
            cros_localbuild_eval_cmd = common_eval_cmd + ['--tast-build']
        else:
            cros_localbuild_eval_cmd = common_eval_cmd + ['--prebuilt']

        diagnoser.diagnose(
            is_autotest=False,
            switch_test_harness_cmd=switch_test_harness_cmd,
            cros_prebuilt_eval_cmd=cros_prebuilt_eval_cmd,
            android_prebuilt_eval_cmd=android_prebuilt_eval_cmd,
            chrome_localbuild_eval_cmd=chrome_localbuild_eval_cmd,
            should_build_chrome_localbuild_with_tests=True,
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


if __name__ == '__main__':
    DiagnoseTastCommandLine().main()
