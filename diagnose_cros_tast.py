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

from bisect_kit import cros_lab_util
from bisect_kit import cros_util
from bisect_kit import diagnoser_cros
from bisect_kit import errors
import setup_cros_bisect


logger = logging.getLogger(__name__)


class DiagnoseTastCommandLine(diagnoser_cros.DiagnoseCommandLineBase):
    """Diagnose command line interface."""

    def check_options(self, opts, path_factory):
        super().check_options(opts, path_factory)
        if not opts.test_name:
            self.argument_parser.error('argument --test_name is required')

    def init_hook(self, opts):
        pass

    def _build_cmds(self):
        # prebuilt version will be specified later.
        common_switch_cmd = [
            './switch_tast_prebuilt.py',
            '--rich_result',
            '--chromeos_root',
            self.config['chromeos_root'],
            '--board',
            self.config['board'],
        ]
        common_eval_cmd = [
            './eval_cros_tast.py',
            '--rich_result',
            '--with_private_bundles',
            '--chromeos_root',
            self.config['chromeos_root'],
            '--test_name',
            self.config['test_name'],
        ]  # yapf: disable
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
                assert cros_util.is_dut(dut)
                if self.config['dut'] == cros_lab_util.LAB_DUT:
                    self.config['allocated_dut'] = dut
                    self.states.save()
                common_eval_cmd.append(dut)

                diagnoser = diagnoser_cros.CrosDiagnoser(
                    self.states, path_factory, self.config, dut
                )

                cros_prebuilt_extra_switch_cmd = common_switch_cmd
                # So far only the `release` images support the `--prebuilt` option
                if cros_util.has_release_prebuilt(
                    self.config['board'], self.config['old']
                ):
                    cros_prebuilt_eval_cmd = common_eval_cmd + ['--prebuilt']
                    lacros_prebuilt_eval_cmd = common_eval_cmd + ['--prebuilt']
                else:
                    cros_prebuilt_eval_cmd = common_eval_cmd + ['--tast_build']
                    lacros_prebuilt_eval_cmd = common_eval_cmd + [
                        '--tast_build'
                    ]

                lacros_prebuilt_eval_cmd = (
                    lacros_prebuilt_eval_cmd
                    + ['--args']
                    + ['lacros.DeployedBinary=/usr/local/lacros-chrome']
                )
                android_prebuilt_eval_cmd = common_eval_cmd
                if self.config['chrome_deploy_image']:
                    chrome_localbuild_eval_cmd = common_eval_cmd + [
                        '--tast_build'
                    ]
                else:
                    chrome_localbuild_eval_cmd = common_eval_cmd + [
                        '--prebuilt'
                    ]

                chrome_localbuild_eval_cmd = (
                    chrome_localbuild_eval_cmd
                    + ['--args']
                    + ['lacros.DeployedBinary=/usr/local/lacros-chrome']
                )

                cros_buildbucket_build = (
                    cros_util.is_buildbucket_buildable(
                        self.config['cros_prebuilt_old']
                    )
                    and not self.config['disable_buildbucket_chromeos']
                )
                if not cros_buildbucket_build:
                    cros_localbuild_eval_cmd = common_eval_cmd + [
                        '--tast_build'
                    ]
                    cros_localbuild_extra_switch_cmd = None
                else:
                    cros_localbuild_eval_cmd = common_eval_cmd + ['--prebuilt']
                    cros_localbuild_extra_switch_cmd = common_switch_cmd

                diagnoser.diagnose(
                    common_switch_cmd=common_switch_cmd,
                    cros_prebuilt_extra_switch_cmd=cros_prebuilt_extra_switch_cmd,
                    cros_prebuilt_eval_cmd=cros_prebuilt_eval_cmd,
                    android_prebuilt_eval_cmd=android_prebuilt_eval_cmd,
                    lacros_prebuilt_eval_cmd=lacros_prebuilt_eval_cmd,
                    chrome_localbuild_eval_cmd=chrome_localbuild_eval_cmd,
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


if __name__ == '__main__':
    DiagnoseTastCommandLine().main()
