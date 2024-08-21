#!/usr/bin/env python3
# Copyright 2023 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Diagnose ChromeOS with custom eval scripts.

This is integrated bisection utility. Given ChromeOS, Chrome, Android source
tree, and necessary parameters, this script can determine which components to
bisect, and hopefully output the culprit CL of regression.

Sometimes the script failed to figure out the final CL for various reasons, it
will cut down the search range as narrow as it can.
"""

from argparse import ArgumentParser
import logging

from bisect_kit import diagnoser_cros
from bisect_kit import errors
from bisect_kit import util


logger = logging.getLogger(__name__)


class DiagnoseCustomEvalCommandLine(diagnoser_cros.DiagnoseCommandLineBase):
    """Diagnose command line interface."""

    def check_options(self, opts):
        super().check_options(opts)

        if not opts.eval_script:
            self.argument_parser.error('argument --eval-script is required')

    def init_hook(self, opts):
        self.states.config.update(
            eval_script=opts.eval_script,
        )

    def create_argument_parser_hook(self, parser_init: ArgumentParser):
        group = parser_init.add_argument_group(title='Options for Custom eval')
        group.add_argument(
            '--eval-script',
            help='Predefined eval script to execute',
            choices=['eval_custom_dut_bootable'],
        )

    def _get_eval_script_file_name(self, eval_script: str):
        if eval_script == 'eval_custom_dut_bootable':
            return 'eval_custom_dut_bootable.py'

        return self.argument_parser.error('argument --eval-script is not valid')

    def _build_cmds(self):
        switch_test_harness_cmd = None
        eval_script = self.config.get('eval_script')
        eval_script_filename = self._get_eval_script_file_name(eval_script)
        common_eval_cmd = [
            f'./{eval_script_filename}',
            '--rich-result',
        ]

        return switch_test_harness_cmd, common_eval_cmd

    def do_run(self):
        diagnoser = diagnoser_cros.CrosDiagnoser(self.states, self.config)

        switch_test_harness_cmd, common_eval_cmd = self._build_cmds()

        diagnoser.diagnose(
            is_autotest=False,
            switch_test_harness_cmd=switch_test_harness_cmd,
            cros_prebuilt_eval_cmd=common_eval_cmd,
            android_prebuilt_eval_cmd=common_eval_cmd,
            chrome_localbuild_eval_cmd=common_eval_cmd,
            should_build_chrome_localbuild_with_tests=False,
            cros_localbuild_eval_cmd=common_eval_cmd,
            is_custom_eval=True,
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
    DiagnoseCustomEvalCommandLine().main()
