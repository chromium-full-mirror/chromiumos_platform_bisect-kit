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
from bisect_kit import dut_manager as dut_manager_module
from bisect_kit import util
import experiment
import setup_cros_bisect


logger = logging.getLogger(__name__)


class DiagnoseCustomEvalCommandLine(diagnoser_cros.DiagnoseCommandLineBase):
    """Diagnose command line interface."""

    def check_options(self, opts, path_factory):
        super().check_options(opts, path_factory)

        if not opts.eval_script:
            self.argument_parser.error('argument --eval_script is required')

    def init_hook(self, opts):
        self.states.config.update(
            eval_script=opts.eval_script,
        )

    def create_argument_parser_hook(self, parser_init: ArgumentParser):
        group = parser_init.add_argument_group(title='Options for Custom eval')
        group.add_argument(
            '--eval_script',
            help='Predefined eval script to execute',
            choices=['eval_custom_dut_bootable'],
        )

    def _get_eval_script_file_name(self, eval_script: str):
        if eval_script == 'eval_custom_dut_bootable':
            return 'eval_custom_dut_bootable.py'

        return self.argument_parser.error('argument --eval_script is not valid')

    def _build_cmds(self):
        common_switch_cmd = []
        eval_script = self.config['eval_script']
        eval_script_filename = self._get_eval_script_file_name(eval_script)
        common_eval_cmd = [
            f'./{eval_script_filename}',
            '--rich_result',
        ]

        return common_switch_cmd, common_eval_cmd

    def do_run(self, dut):
        path_factory = setup_cros_bisect.DefaultProjectPathFactory(
            self.config['mirror_base'],
            self.config['work_base'],
            self.config['session'],
        )
        diagnoser = diagnoser_cros.CrosDiagnoser(
            self.states, path_factory, self.config, dut
        )

        common_switch_cmd, common_eval_cmd = self._build_cmds()

        diagnoser.diagnose(
            is_autotest=False,
            common_switch_cmd=common_switch_cmd,
            cros_prebuilt_extra_switch_cmd=common_switch_cmd,
            cros_prebuilt_eval_cmd=common_eval_cmd,
            android_prebuilt_eval_cmd=common_eval_cmd,
            lacros_prebuilt_eval_cmd=common_eval_cmd,
            chrome_localbuild_eval_cmd=common_eval_cmd,
            should_build_chrome_localbuild_with_tests=False,
            cros_localbuild_eval_cmd=common_eval_cmd,
            cros_localbuild_extra_switch_cmd=common_switch_cmd,
            is_custom_eval=True,
        )

    @util.MethodTimer(
        diagnoser_cros.DiagnoseCommandLineBase.write_total_execution_time
    )
    def cmd_run(self, opts):
        del opts  # unused

        self.states.load_states()

        try:
            # Allocate a DUT globally only if not performing a stateless bisect.
            logger.info('experiments: %s', self.config['experiments'])
            should_auto_allocate = (
                not experiment.ID.STATELESS in self.config['experiments']
            )
            dut_manager = dut_manager_module.DutManager(
                'Global',
                self.states,
                self.config['dut'],
                should_auto_allocate,
                should_force_monitoring=True,
            )
            with dut_manager.provision() as dut:
                self.do_run(dut)
            logger.info('%s done', __file__)
        except Exception as e:
            logger.exception('got exception; stop')
            exception_name = e.__class__.__name__
            self.states.add_history(
                'failed',
                text='%s: %s' % (exception_name, e),
                exception=exception_name,
            )


if __name__ == '__main__':
    DiagnoseCustomEvalCommandLine().main()
