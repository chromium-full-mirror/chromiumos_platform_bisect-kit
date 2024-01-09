# Copyright 2018 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Diagnose ChromeOS regressions on top of subcomponent bisectors."""

from __future__ import annotations

import argparse
import datetime
import io
import json
import logging
import os
import sys
import typing

from bisect_kit import android_util
from bisect_kit import bisector_cli
from bisect_kit import cli
from bisect_kit import common
from bisect_kit import core
from bisect_kit import cr_util
from bisect_kit import cros_lab_util
from bisect_kit import cros_util
from bisect_kit import dut_manager as dut_manager_module
from bisect_kit import errors
from bisect_kit import util
from bisect_kit import wrapper
from bisect_kit.dlc import borealis_util
import cros_helper
import experiment


logger = logging.getLogger(__name__)


def run_switch_or_eval_cmd(cmd):
    """Runs a single switch or eval step and handles exceptions."""

    result = bisector_cli.execute_bisectkit_step_command(cmd).data
    if result['status'] in ['skip', 'fatal']:
        # reconstruct_from_string() return None if the exception is not
        # a custom error defined in our own errors module.
        e = errors.reconstruct_from_string(
            result.get('exception'), result.get('reason')
        )
        if e:
            raise e
        raise errors.ExecutionFatalError(
            'failed to run %s (%s): %s'
            % (cmd[0], result.get('exception'), result.get('reason'))
        )


class InitOnce:
    """A helper class to run an init func only once."""

    def __init__(self, name: str, init_func: typing.Callable[[], None]):
        self._name = name
        self._init_func = init_func
        self._initialized = False

    def run(self):
        if self._initialized:
            logger.info('%s has been run, skipped', self._name)
            return
        logger.info('running init func %s', self._name)
        self._init_func()
        logger.info('%s completed', self._name)
        self._initialized = True


class CrosDiagnoser:
    """Integrated bisectors API for ChromeOS regression analysis."""

    def __init__(
        self, states: core.DiagnoseStates, config: dict, dut: str | None
    ):
        """Initializer.

        Args:
          states: a core.DiagnoseStates objejct.
          config: a config from a core.DiagnoseStates object.
          dut: a pre-allocated DUT address, or the string ":lab:" to indicate
              a DUT is to be auto-allocated.
        """
        self.states = states
        self.config = config
        self.path_factory = common.ProjectPathFactory(
            self.config['session'],
            self.config['work_base'],
            self.config['mirror_base'],
        )
        # self.noisy will be updated after we gather more information from each
        # bisect session.
        self.noisy = config['noisy']
        self.pre_allocated_dut = dut

        self.cros_old: str = ''
        self.cros_new: str = ''
        self.old_info: cros_util.VersionInfo = cros_util.VersionInfo()
        self.new_info: cros_util.VersionInfo = cros_util.VersionInfo()

        self.is_vm_board = cros_util.is_vm_board(self.config.get('board'))
        self.is_stateless = (
            experiment.is_in_experiment(
                self.config.get('experiments'), experiment.ID.STATELESS
            )
            or self.is_vm_board
        )
        self._switch_vm_cmd: list[str] = ['./switch_cros_vm.py']

    def _verified_chromeos_prebuilt(self):
        if self.config['bypass_chromeos_prebuilt']:
            return 'assume'
        if self.config['bisect_chrome']:
            return 'assume'
        if self.config['bisect_android']:
            return 'assume'
        return 'verified'

    def make_decision(self, text):
        if sys.exc_info() != (None, None, None):
            logger.exception('decision: %s', text)
        else:
            logger.info('decision: %s', text)
        self.states.add_history('decision', text=text)

    def diagnose(
        self,
        is_autotest: bool,
        switch_test_harness_cmd: list[str] | None,
        cros_prebuilt_eval_cmd: list[str],
        android_prebuilt_eval_cmd: list[str],
        lacros_prebuilt_eval_cmd: list[str],
        chrome_localbuild_eval_cmd: list[str],
        should_build_chrome_localbuild_with_tests: bool,
        cros_localbuild_eval_cmd: list[str],
        is_custom_eval: bool = False,
    ):
        def init_dut_to_old_cros(
            dut_manager: dut_manager_module.DutManager,
        ):
            logger.info('init dut %s to old cros', dut_manager.dut)
            self._switch_chromeos_to_old(
                dut_manager.dut,
                should_always_reflash=self.config['always_reflash'],
            )

        def switch_test_harness_to_old(dut):
            logger.info('switch test harness to old for %s', dut)
            run_switch_or_eval_cmd(
                switch_test_harness_cmd
                + [
                    self.cros_old,
                    '--session',
                    self.config['session'],
                    '--dut',
                    dut,
                ]
            )

        def init_autotest_deps_workaround(dut):
            """A workaround initialization for autotest.

            The version of autotest on the DUT is unknown and may be even
            not installed. Invoke the test once here, so
                - make sure autotest-deps is installed, with expected version
                - autotest-deps is installed first, so our chrome test binaries
                    won't be reset to default version during bisection.
            It's acceptable to spend extra time to run test once because
                - only few tests do so
                - tests are migrating away from autotest
            """
            if is_autotest and should_build_chrome_localbuild_with_tests:
                logger.info('init autotest deps for %s', dut)
                run_switch_or_eval_cmd(chrome_localbuild_eval_cmd + [dut])
            else:
                logger.info('no need to init autotest deps')

        # Indicate that a new round of bisection starts, possibly a retry.
        self.states.add_history('start')
        self.states.save()

        # For VM bisection, if vm_cros_version is defined in __init__ phase,
        # the VM version should be consistent in whole
        # BisectorWrapper.narrow_down() life time, and
        # BisectorCommandLine._switch_and_eval() should not pass any arguments
        # to change VM's version.
        # This case is for bisecting ChromeOS version non-specific domains like
        # Chrome, ARC, DLC or LaCrOS.

        # OTOH, if vm_cros_version is not defined in __init__, the VM version
        # decision will be deffered to BisectorCommandLine._switch_and_eval()
        # level. BisectorCommandLine._switch_and_eval() must pass
        # vm_cros_version argument when calling provision() each time.
        # This case is for bisecting ChromeOS version specific domains like
        # ChromeOS prebuilt and localbuild.
        dut_manager = dut_manager_module.DutManager(
            'InitLease',
            self.states,
            self.pre_allocated_dut,
            self.is_stateless,
            vm_cros_version=self.config['old'] if self.is_vm_board else None,
        )
        if not self.config.get('board') or not self.config.get(
            'board_cpu_arch'
        ):
            logger.info('querying DUT related info')
            # 1. For stateful bisection, a pre-allocated DUT should already exist.
            # The following code would not lease DUT but only query
            # 'board_cpu_arch' from the pre-allocated DUT. On the other hand,
            # 'board' should already exist in self.config.
            # 2. For stateless bisection, a DUT would be auto-leased by the
            # following code. 'board' would be updated in
            # dut_maaner.provision().
            with dut_manager.provision() as dut:
                # Query DUT dependent properties.
                self.config['board_cpu_arch'] = cros_util.query_dut_cpu_arch(
                    dut
                )
                self.states.save()

        assert self.config.get('board'), '"board" is undetermined'
        assert self.config.get(
            'board_cpu_arch'
        ), '"board_cpu_arch" is undetermined'

        # Fix commands which depend on "board".
        if switch_test_harness_cmd:
            switch_test_harness_cmd = switch_test_harness_cmd + [
                '--board',
                self.config['board'],
            ]
        if not is_custom_eval and not is_autotest:
            # So far only the `release` images support the `--prebuilt` option
            if cros_util.has_release_prebuilt(
                self.config['board'], self.config['old']
            ):
                cros_prebuilt_eval_cmd = cros_prebuilt_eval_cmd + ['--prebuilt']
                lacros_prebuilt_eval_cmd = lacros_prebuilt_eval_cmd + [
                    '--prebuilt'
                ]
            else:
                cros_prebuilt_eval_cmd = cros_prebuilt_eval_cmd + [
                    '--tast-build'
                ]
                lacros_prebuilt_eval_cmd = lacros_prebuilt_eval_cmd + [
                    '--tast-build'
                ]

        # Step 1: Narrows down ChromeOS prebuilt.
        dut_manager = dut_manager_module.DutManager(
            'PrebuiltChromeOS',
            self.states,
            self.pre_allocated_dut,
            self.is_stateless,
        )
        self.narrow_down_chromeos_prebuilt(
            dut_manager,
            self.config['cros_prebuilt_old'],
            self.config['cros_prebuilt_new'],
            cros_prebuilt_eval_cmd,
            switch_test_harness_cmd=switch_test_harness_cmd,
        )

        old_cros_version = cros_util.version_to_short(self.cros_old)
        logger.debug('old_cros_version, %s', old_cros_version)

        # Switches CrOS repository and DUT to narrowed down old version.
        # This is time consuming and only needs to be done once for domains
        # which are based on old CrOS image.
        switch_cros_repo_to_old = InitOnce(
            'switch_cros_repo_to_old',
            lambda: run_switch_or_eval_cmd(
                [
                    './switch_cros_localbuild.py',
                    '--rich-result',
                    '--chromeos-root',
                    self.config['chromeos_root'],
                    '--chromeos-mirror',
                    self.config['chromeos_mirror'],
                    '--board',
                    self.config['board'],
                    '--sync-code-only',
                    self.cros_old,
                    '--session',
                    self.config['session'],
                ]
            ),
        )

        def bisect_prebuilt_on_old_cros_dut_init_func(
            dut: str,
        ):
            if not self.is_vm_board:
                init_dut_to_old_cros(dut)
            if switch_test_harness_cmd:
                switch_test_harness_to_old(dut)

        def bisect_prebuilt_on_old_cros_dut_leave_context_check_func(
            dut: str,
        ):
            logger.info('checking cros version on dut %s', dut)
            assert old_cros_version == cros_util.query_dut_short_version(
                dut
            ), 'Someone else reflashed the DUT. DUT locking is not respected?'

        dut_manager = dut_manager_module.DutManager(
            'OldCros',
            self.states,
            self.pre_allocated_dut,
            self.is_stateless,
            bisect_prebuilt_on_old_cros_dut_init_func,
            bisect_prebuilt_on_old_cros_dut_leave_context_check_func,
            vm_cros_version=self.cros_old if self.is_vm_board else None,
        )

        # Step 2: Narrows down Android prebuilt. If the error can be reproduced,
        # returns the result and skips other steps.
        try:
            if self.narrow_down_android(
                dut_manager,
                android_prebuilt_eval_cmd,
                init_once=switch_cros_repo_to_old,
            ):
                return
        except errors.DiagnoseContradiction:
            raise
        except Exception as e:
            if self.is_stateless and isinstance(e, errors.BisectRetriableError):
                raise
            self.make_decision(
                'Exception in Android bisector before verification; '
                'assume the culprit is not inside Android and continue'
            )

        # Step 3: Narrows down Lacros prebuilt. If the error can be reproduced,
        # Run the Chrome local build step with the narrowed down range.
        # In case of failure, run the Chrome local build step with the previous range.
        try:
            self.narrow_down_lacros_prebuilt(
                dut_manager,
                lacros_prebuilt_eval_cmd,
                init_once=switch_cros_repo_to_old,
            )
        except errors.DiagnoseContradiction:
            raise
        except Exception as e:
            if self.is_stateless and isinstance(e, errors.BisectRetriableError):
                raise
            self.make_decision(
                'Exception in Lacros bisector before verification; '
                'assume the culprit is not inside Lacros and continue'
            )

        # Step 4: Narrows down DLC prebuilt.
        try:
            if self.narrow_down_dlc_prebuilt(
                dut_manager,
                self.config['old'],
                self.config['new'],
                cros_prebuilt_eval_cmd,
                init_once=switch_cros_repo_to_old,
            ):
                return
        except errors.DiagnoseContradiction:
            raise
        except Exception as e:
            if self.is_stateless and isinstance(e, errors.BisectRetriableError):
                raise
            self.make_decision(
                'Exception in DLC bisector before verification; '
                'assume the culprit is not inside DLC and continue'
            )

        # Step 5: Narrows down Chrome local build. If the error can be reproduced,
        # returns the result and skips other steps.

        def bisect_local_chrome_dut_init_func(
            dut: str,
        ):
            if not self.is_vm_board:
                init_dut_to_old_cros(dut)
            if switch_test_harness_cmd:
                switch_test_harness_to_old(dut)
            init_autotest_deps_workaround(dut)

        def bisect_local_chrome_dut_leave_context_check_func(
            dut: str,
        ):
            logger.info('checking cros version on dut %s', dut)
            # If chrome is not deployed with cros image, do the sanity check to ensure
            # OS version.
            if (
                not self.config['chrome_deploy_image']
                and not self.config['enable_buildbucket_chrome']
            ):
                assert old_cros_version == cros_util.query_dut_short_version(
                    dut
                ), 'Someone else reflashed the DUT. DUT locking is not respected?'

        dut_manager = dut_manager_module.DutManager(
            'LocalChrome',
            self.states,
            self.pre_allocated_dut,
            self.is_stateless,
            bisect_local_chrome_dut_init_func,
            bisect_local_chrome_dut_leave_context_check_func,
            vm_cros_version=self.cros_old if self.is_vm_board else None,
        )
        buildbucket_buildable = cros_util.is_buildbucket_buildable(
            self.config['cros_prebuilt_old']
        )
        try:
            if self.narrow_down_chrome(
                dut_manager,
                chrome_localbuild_eval_cmd,
                init_once=switch_cros_repo_to_old,
                buildbucket_build=buildbucket_buildable
                and self.config['enable_buildbucket_chrome'],
                should_build_with_tests=should_build_chrome_localbuild_with_tests,
            ):
                return
        except errors.DiagnoseContradiction:
            raise
        except Exception as e:
            if self.is_stateless and isinstance(e, errors.BisectRetriableError):
                raise
            self.make_decision(
                'Exception in Chrome bisector before verification; '
                'assume the culprit is not inside Chrome and continue'
            )

        # Step 6: Narrowdown chromeos localbuild.
        dut_manager = dut_manager_module.DutManager(
            'LocalChromeOS',
            self.states,
            self.pre_allocated_dut,
            self.is_stateless,
        )
        self.narrow_down_chromeos_localbuild(
            dut_manager,
            cros_localbuild_eval_cmd,
            buildbucket_buildable
            and not self.config['disable_buildbucket_chromeos'],
            switch_test_harness_cmd=switch_test_harness_cmd,
        )

    def narrow_down_chromeos_prebuilt(
        self,
        dut_manager: dut_manager_module.DutManager,
        old: str,
        new: str,
        eval_cmd: list[str],
        switch_test_harness_cmd: list[str] | None = None,
    ) -> None:
        """Bisect with ChromeOS prebuilt.

        Args:
          dut_manager: a DutManager instance
          old: old ChromeOS version
          new: new ChromeOS version
          eval_cmd: command to reproduce the regression
          switch_test_harness_cmd: command to switch test harness
        """
        if self.config['bisect_chrome']:
            self.make_decision(
                'The ChromeOS prebuilt bisection is bypassed since --bisect-chrome is on'
            )
            self.cros_old = self.cros_new = self.config['base_cros_version']
        elif self.config['bisect_android']:
            self.make_decision(
                'The ChromeOS prebuilt bisection is bypassed since --bisect-android is on'
            )
            self.cros_old = self.cros_new = self.config['base_cros_version']
        elif self.config['bypass_chromeos_prebuilt']:
            self.make_decision(
                'The bisection with ChromeOS prebuilt image is bypassed; '
                'assume that old ChromeOS has old behavior and '
                'new ChromeOS has new behavior'
            )
            self.cros_old = old
            self.cros_new = new
        else:
            self.states.add_history(
                'bisect',
                text='bisect chromeos prebuilt',
                bisector='bisect_cros_version',
                session=self.config['session'],
            )
            switch_cmd = [
                './switch_cros_prebuilt.py',
                '--rich-result',
            ]
            if self.config['disable_rootfs_verification']:
                switch_cmd.append('--disable-rootfs-verification')
            else:
                switch_cmd.append('--no-disable-rootfs-verification')

            init_args = ['--board', self.config['board']]
            if self.config['disable_snapshot']:
                init_args.append('--disable-snapshot')

            if dut_manager.is_vm:
                switch_cmds = [
                    self._switch_vm_cmd,
                    switch_test_harness_cmd,
                ]
            else:
                switch_cmds = [
                    switch_cmd,
                    switch_test_harness_cmd,
                ]

            bisector = wrapper.BisectorWrapper(
                'bisect_cros_version',
                self.config['session'],
                dut_manager,
                self.is_stateless,
            )
            bisector.init_if_necessary(
                old,
                new,
                init_args=init_args,
                switch_cmds=switch_cmds,
                eval_cmd=eval_cmd,
                old_value=self.config['old_value'],
                new_value=self.config['new_value'],
                term_old=self.config['term_old'],
                term_new=self.config['term_new'],
                recompute_init_values=self.config['recompute_init_values'],
                noisy=self.noisy,
                endpoint_verification=self.config['endpoint_verification'],
            )

            self.cros_old, self.cros_new, self.noisy = bisector.narrow_down()

        prebuilt_states = core.BisectStates.from_bisector_class(
            'ChromeOSVersionDomain', self.config['session']
        )
        if prebuilt_states.load_states():
            cros_util.SnapshotStore.init_with_state(prebuilt_states)

        # If old and new chrome version not specified, fetches info from metadata.
        if self.config['bisect_chrome']:
            self.old_info = cros_util.VersionInfo(
                cr_version=self.config['chrome_localbuild_old']
            )
            self.new_info = cros_util.VersionInfo(
                cr_version=self.config['chrome_localbuild_new']
            )
        elif self.config['bisect_android']:
            base_cros_info = cros_util.query_version_info(
                self.config['board'], self.config['base_cros_version']
            )
            self.old_info = cros_util.VersionInfo(
                android_build_id=self.config['android_prebuilt_old'],
                android_branch=base_cros_info.android_branch,
                android_target=base_cros_info.android_target,
            )
            self.new_info = cros_util.VersionInfo(
                android_build_id=self.config['android_prebuilt_new'],
                android_branch=base_cros_info.android_branch,
                android_target=base_cros_info.android_target,
            )
        else:
            self.old_info = cros_util.query_version_info(
                self.config['board'], self.cros_old
            )
            self.new_info = cros_util.query_version_info(
                self.config['board'], self.cros_new
            )

        logger.info(
            'old: cros %s, chrome %s, android %s',
            self.old_info.cros_full_version,
            self.old_info.cr_version,
            self.old_info.android_build_id,
        )
        logger.info(
            'new: cros %s, chrome %s, android %s',
            self.new_info.cros_full_version,
            self.new_info.cr_version,
            self.new_info.android_build_id,
        )

    def _switch_chromeos_to_old(
        self,
        dut: str,
        should_always_reflash: bool = False,
    ) -> None:
        """Switch ChromeOS version to old prebuilt.

        Args:
          dut: the DUT.
          should_always_reflash: always do 'cros flash' no matter the current version number of DUT
        """
        if not should_always_reflash:
            if cros_util.is_cros_full_version(self.cros_old):
                version = cros_util.version_to_short(self.cros_old)
            else:
                version = self.cros_old
            if cros_util.query_dut_prebuilt_version(dut) == version:
                return
        cmd = [
            './switch_cros_prebuilt.py',
            '--rich-result',
            '--session',
            self.config['session'],
            '--board',
            self.config['board'],
            '--dut',
            dut,
            self.cros_old,
        ]
        if self.config['disable_rootfs_verification']:
            cmd.append('--disable-rootfs-verification')
        else:
            cmd.append('--no-disable-rootfs-verification')
        run_switch_or_eval_cmd(cmd)

    def _need_android_bisect(
        self, old_info: cros_util.VersionInfo, new_info: cros_util.VersionInfo
    ) -> bool:
        """Determine whether to do android bisect or not for given two versions.

        Args:
          old_info: old version info from cros_util.query_version_info()
          new_info: new version info from cros_util.query_version_info()

        Returns:
          True if android bisect is necessary (ex. version not equal) and possible
          (ex. same branch).
        """
        if self.config['bisect_chrome']:
            self.make_decision(
                'The Android prebuilt bisection is bypassed since --bisect-chrome is on'
            )
            return False

        if (
            old_info.android_build_id is None
            or new_info.android_build_id is None
        ):
            self.make_decision(
                'At least one version has no Android. skip Android bisect'
            )
            return False

        if old_info.android_branch is None or new_info.android_branch is None:
            self.make_decision(
                'At least one chromeOS version has no Android branch '
                'information in the metadata. skip Android bisect'
            )
            return False

        if old_info.android_build_id == new_info.android_build_id:
            self.make_decision(
                'The Android version is identical, no need to bisect Android'
            )
            return False

        if old_info.android_branch != new_info.android_branch:
            self.make_decision(
                'Android branch mismatch: %s vs %s; unable to bisect Android'
                % (old_info.android_branch, new_info.android_branch)
            )
            return False

        if old_info.android_target != new_info.android_target:
            self.make_decision(
                'Android target mismatch: %s vs %s; unable to bisect Android'
                % (old_info.android_target, new_info.android_target)
            )
            return False

        return True

    def narrow_down_android(
        self,
        dut_manager: dut_manager_module.DutManager,
        eval_cmd: list[str],
        init_once: InitOnce,
    ) -> bool:
        """Bisect with Android prebuilt and localbuild.

        It's known that the version of ChromeOS has old behavior.

        Args:
          dut_manager: a DutManager instance.
          eval_cmd: command to reproduce the regression
          init_once: init func which should be called once.

        Returns:
          True: culprit is inside Android
          False: culprit is not inside Android or unable to bisect
        """
        verified = False
        if not self._need_android_bisect(self.old_info, self.new_info):
            return verified

        android_flavor = android_util.get_flavor(
            self.old_info.android_target, self.config['board_cpu_arch']
        )
        if android_flavor is None:
            self.make_decision(
                'Android flavor not found for cpu arch (%s). Unable to bisect Android'
                % self.config['board_cpu_arch'],
            )
            return verified

        if (
            self.config['bypass_android_prebuilt']
            and self.config['bypass_android_build']
        ):
            self.make_decision('The bisection with Android is bypassed')
            return verified

        if self.config['bypass_android_prebuilt']:
            self.make_decision(
                'The bisection with Android prebuilt is bypassed'
            )
            android_old = self.old_info.android_build_id
            android_new = self.new_info.android_build_id
        else:
            logger.info('bisect android prebuilt')
            self.states.add_history(
                'bisect',
                text='bisect android prebuilt',
                bisector='bisect_android_build_id',
                session=self.config['session'],
            )
            bisector = wrapper.BisectorWrapper(
                'bisect_android_build_id',
                self.config['session'],
                dut_manager,
                self.is_stateless,
            )
            try:
                if bisector.check_done():
                    return True

                bisector.init_if_necessary(
                    self.old_info.android_build_id,
                    self.new_info.android_build_id,
                    init_args=[
                        '--branch',
                        self.old_info.android_branch,
                        '--flavor',
                        android_flavor,
                    ],
                    switch_cmds=[
                        [
                            './switch_arc_prebuilt.py',
                            '--rich-result',
                        ],
                    ],
                    eval_cmd=eval_cmd,
                    old_value=self.config['old_value'],
                    new_value=self.config['new_value'],
                    term_old=self.config['term_old'],
                    term_new=self.config['term_new'],
                    recompute_init_values=self.config['recompute_init_values'],
                    noisy=self.noisy,
                    endpoint_verification=self.config['endpoint_verification'],
                )
                init_once.run()
                with dut_manager.provision():
                    (
                        android_old,
                        android_new,
                        self.noisy,
                    ) = bisector.narrow_down()
                verified = True
            except errors.VerifyOldBehaviorFailed as e:
                self.make_decision(
                    'Expect old Android has old behavior (%s) but failed'
                    % self.config['term_old']
                )
                raise errors.DiagnoseContradiction(
                    '%s that old chromeos has old behavior (%s); '
                    'but it became new behavior (%s) '
                    'after deployed old android prebuilt'
                    % (
                        self._verified_chromeos_prebuilt(),
                        self.config['term_old'],
                        self.config['term_new'],
                    )
                ) from e
            except errors.VerifyNewBehaviorFailed:
                self.make_decision(
                    'Unable to reproduce, the culprit is not in Android'
                )
                return verified

        if self.config['bypass_android_build']:
            self.make_decision(
                'The bisection with Android local build is bypassed'
            )
            return verified

        # TODO(kcwu): what if the branch name does not have 'git_' prefix
        assert self.old_info.android_branch.startswith('git_')
        git_branch = self.old_info.android_branch[4:]
        android_mirror = self.config['android_mirror']
        if not android_mirror:
            android_mirror = self.path_factory.get_android_mirror(git_branch)
            logger.info('android_mirror = %s', android_mirror)
        android_root = self.config['android_root']
        if not android_root:
            android_root = self.path_factory.get_android_tree(git_branch)
            logger.info('android_root = %s', android_root)

        if not os.path.exists(android_mirror):
            logger.info(
                'android_mirror does not exist; skip android local build bisect'
            )
            return verified
        if not os.path.exists(android_root):
            logger.info(
                'android_root does not exist; skip android local build bisect'
            )
            return verified

        logger.info('bisect android local build')
        self.states.add_history(
            'bisect',
            text='bisect android local build',
            bisector='bisect_android_repo',
            session=self.config['session'],
        )
        bisector = wrapper.BisectorWrapper(
            'bisect_android_repo',
            self.config['session'],
            dut_manager,
            self.is_stateless,
        )
        try:
            if bisector.check_done():
                return True
            bisector.init_if_necessary(
                android_old,
                android_new,
                init_args=[
                    '--android-root',
                    android_root,
                    '--android-mirror',
                    android_mirror,
                    '--branch',
                    self.old_info['android_branch'],
                    '--flavor',
                    android_flavor,
                    '--board',
                    self.config['board'],
                ],
                switch_cmds=[
                    [
                        './switch_arc_localbuild.py',
                        '--rich-result',
                    ],
                ],
                eval_cmd=eval_cmd,
                old_value=self.config['old_value'],
                new_value=self.config['new_value'],
                term_old=self.config['term_old'],
                term_new=self.config['term_new'],
                recompute_init_values=self.config['recompute_init_values'],
                noisy=self.noisy,
                endpoint_verification=self.config['endpoint_verification'],
            )

            init_once.run()
            with dut_manager.provision():
                android_old, android_new, self.noisy = bisector.narrow_down()
            verified = True
        except errors.VerificationFailed as e:
            if verified:
                self.make_decision(
                    'Verified with Android prebuilt but failed with local build'
                )
                raise errors.DiagnoseContradiction(
                    'verified that the issue could be reproduced with '
                    'android prebuilt; but unable to reproduce with local build'
                )
            if isinstance(e, errors.VerifyOldBehaviorFailed):
                raise errors.DiagnoseContradiction(
                    '%s that old chromeos has old behavior (%s); '
                    'but it became new behavior (%s) '
                    'after deployed old android prebuilt'
                    % (
                        self._verified_chromeos_prebuilt(),
                        self.config['term_old'],
                        self.config['term_new'],
                    )
                ) from e
            self.make_decision(
                'Unable to reproduce, the culprit is not in Android'
            )
            return verified
        return verified

    def narrow_down_lacros_prebuilt(
        self,
        dut_manager: dut_manager_module.DutManager,
        eval_cmd: list[str],
        init_once: InitOnce,
    ) -> bool:
        """Bisect with Lacros Prebuilt.

        It's known that the version of ChromeOS has old behavior.

        Args:
          dut_manager: a DutManager instance.
          eval_cmd: command to reproduce the regression
          init_once: init func which should be called once.

        Returns:
          True: culprit is inside Lacros
          False: culprit is not inside Lacros or unable to bisect
        """
        if self.config['bisect_android']:
            self.make_decision(
                'The bisection with Lacros prebuilt is bypassed since '
                '--bisect-android is on'
            )
            return False

        if self.config['bypass_lacros_prebuilt']:
            self.make_decision('The bisection with Lacros is bypassed')
            return False

        if self.old_info.cr_version == self.new_info.cr_version:
            self.make_decision(
                'The Lacros version is identical, no need to bisect Lacros'
            )
            return False

        logger.info('bisect lacros prebuilt')
        self.states.add_history(
            'bisect',
            text='bisect lacros prebuilt',
            bisector='bisect_lacros_prebuilt',
            session=self.config['session'],
        )

        switch_cmd = [
            './switch_lacros_prebuilt.py',
            '--rich-result',
            '--chromeos-root',
            self.config['chromeos_root'],
        ]

        bisector = wrapper.BisectorWrapper(
            'bisect_lacros_prebuilt',
            self.config['session'],
            dut_manager,
            self.is_stateless,
        )

        init_args = [
            '--chromeos-root',
            self.config['chromeos_root'],
            '--board',
            self.config['board'],
            '--board-cpu-arch',
            self.config['board_cpu_arch'],
        ]

        try:
            if bisector.check_done():
                return True
            bisector.init_if_necessary(
                self.old_info.cr_version,
                self.new_info.cr_version,
                init_args=init_args,
                switch_cmds=[switch_cmd],
                eval_cmd=eval_cmd,
                old_value=self.config['old_value'],
                new_value=self.config['new_value'],
                term_old=self.config['term_old'],
                term_new=self.config['term_new'],
                recompute_init_values=self.config['recompute_init_values'],
                noisy=self.noisy,
                endpoint_verification=self.config['endpoint_verification'],
            )

            init_once.run()
            with dut_manager.provision():
                lacros_old, lacros_new, self.noisy = bisector.narrow_down()

            # If lacros prebuilt successfully narrow down to a range,
            # Run chrome bisection from that range
            self.old_info.cr_version = lacros_old
            self.new_info.cr_version = lacros_new
            logger.info(
                'Lacros prebuilt narrowerd down to %s -> %s',
                lacros_old,
                lacros_new,
            )
        except errors.VerifyOldBehaviorFailed:
            self.make_decision(
                'Expect old Lacros has old behavior (%s) but failed. Unable to reproduce, the culprit is not in Lacros.'
                % self.config['term_old']
            )
            return False
        except errors.VerifyNewBehaviorFailed:
            self.make_decision(
                'Unable to reproduce, the culprit is not in Lacros'
            )
            return False
        return True

    def narrow_down_dlc_prebuilt(
        self,
        dut_manager: dut_manager_module.DutManager,
        old: str,
        new: str,
        eval_cmd: list[str],
        init_once: InitOnce,
    ) -> bool:
        """Bisect ChromeOS DLC prebuilt.

        Args:
          dut_manager: a DutManager object.
          old: the old version of the prebuilt.
          new: the new version of the prebuilt.
          eval_cmd: command to reproduce the regression.
          init_once: init func which should be called once.

        Returns:
          True if the bisection is successful.
        """

        if self.config['bisect_android']:
            self.make_decision(
                'The bisection with DLC is bypassed since --bisect-android is on'
            )
            return False

        if self.config['bisect_chrome']:
            self.make_decision(
                'The bisection with DLC is bypassed since --bisect-chrome is on'
            )
            return False

        if self.config['bypass_dlc_prebuilt']:
            self.make_decision(
                'The bisection with DLC is bypassed since --bypass-dlc-prebuilt is on'
            )
            return False

        logger.info('bisect dlc prebuilt')
        self.states.add_history(
            'bisect',
            text='bisect borealis dlc prebuilt',
            bisector='bisect_borealis_dlc_version',
            session=self.config['session'],
        )
        switch_cmd = [
            './switch_cros_borealis_dlc_prebuilt.py',
            '--rich-result',
            '--chromeos-root',
            self.config['chromeos_root'],
        ]
        init_args = [
            '--board',
            self.config['board'],
            '--chromeos-root',
            self.config['chromeos_root'],
            '--chromeos-mirror',
            self.config['chromeos_mirror'],
        ]

        bisector = wrapper.BisectorWrapper(
            'bisect_borealis_dlc_version',
            self.config['session'],
            dut_manager,
            self.is_stateless,
        )
        bisector.init_if_necessary(
            old,
            new,
            init_args=init_args,
            switch_cmds=[switch_cmd],
            eval_cmd=eval_cmd,
            old_value=self.config['old_value'],
            new_value=self.config['new_value'],
            term_old=self.config['term_old'],
            term_new=self.config['term_new'],
            recompute_init_values=self.config['recompute_init_values'],
            noisy=self.noisy,
            endpoint_verification=self.config['endpoint_verification'],
        )

        try:
            if bisector.check_done():
                return True
            init_once.run()
            with dut_manager.provision() as dut:
                if not borealis_util.is_installable(dut):
                    self.make_decision(
                        'The bisection with borealis DLC is bypassed because '
                        'it is not supported by the board/model/version of the DUT.'
                    )
                    return False
                bisector.narrow_down()
        except errors.VerifyOldBehaviorFailed:
            self.make_decision(
                f'Unable to reproduce the old behavior ({self.config["term_old"]}). '
                f'The culprit is not in borealis-dlc.'
            )
            return False
        except errors.VerifyNewBehaviorFailed:
            self.make_decision(
                f'Unable to reproduce the new behavior ({self.config["term_new"]}). '
                f'The culprit is not in borealis-dlc.'
            )
            return False
        return True

    def narrow_down_chrome(
        self,
        dut_manager: dut_manager_module.DutManager,
        eval_cmd: list[str],
        init_once: InitOnce,
        buildbucket_build: bool = False,
        should_build_with_tests: bool = True,
    ) -> bool:
        """Bisect with Chrome localbuild.

        It's known that the version of ChromeOS has old behavior.

        Args:
          dut_manager: a DutManager instance
          eval_cmd: command to reproduce the regression
          buildbucket_build: to build Chrome image on buildbucket
          should_build_with_tests: build test binaries as well
          init_once: init func which should be called once.

        Returns:
          True: culprit is inside Chrome
          False: culprit is not inside Chrome or unable to bisect
        """

        if self.config['bisect_android']:
            self.make_decision(
                'The bisection with Chrome local build is bypassed since --bisect-android is on'
            )
            return False

        if self.config['bypass_chrome_build']:
            self.make_decision(
                'The bisection with Chrome local build is bypassed'
            )
            return False

        if self.old_info.cr_version == self.new_info.cr_version:
            self.make_decision(
                'The Chrome version is identical, no need to bisect Chrome'
            )
            return False

        logger.info('bisect chrome local build')

        self.states.add_history(
            'bisect',
            text='bisect chrome local build',
            bisector='bisect_cr_localbuild_internal',
            session=self.config['session'],
        )
        if buildbucket_build:
            switch_cmd = [
                './switch_cros_localbuild_buildbucket.py',
                'bisect_chrome',
                '--rich-result',
                '--chromeos-root',
                self.config['chromeos_root'],
                '--chromeos-mirror',
                self.config['chromeos_mirror'],
                '--chromeos-rev',
                self.cros_old,
                '--board',
                self.config['board'],
            ]
            if self.config['disable_rootfs_verification']:
                switch_cmd.append('--disable-rootfs-verification')
            else:
                switch_cmd.append('--no-disable-rootfs-verification')
        else:
            switch_cmd = [
                './switch_cros_cr_localbuild_internal.py',
                '--rich-result',
                '--board',
                self.config['board'],
                '--board-cpu-arch',
                self.config['board_cpu_arch'],
            ]
            if not should_build_with_tests:
                switch_cmd.append('--no-with-tests')
            if self.config['chrome_deploy_image']:
                switch_cmd += [
                    '--deploy-method=image',
                    '--chromeos-root',
                    self.config['chromeos_root'],
                ]
                if self.config['disable_rootfs_verification']:
                    switch_cmd.append('--disable-rootfs-verification')
                else:
                    switch_cmd.append('--no-disable-rootfs-verification')

        bisector = wrapper.BisectorWrapper(
            'bisect_cr_localbuild_internal',
            self.config['session'],
            dut_manager,
            self.is_stateless,
        )
        try:
            if bisector.check_done():
                return True
            bisector.init_if_necessary(
                self.old_info.cr_version,
                self.new_info.cr_version,
                init_args=[
                    '--chrome-root',
                    self.config['chrome_root'],
                    '--chrome-mirror',
                    self.config['chrome_mirror'],
                ],
                switch_cmds=[switch_cmd],
                eval_cmd=eval_cmd,
                old_value=self.config['old_value'],
                new_value=self.config['new_value'],
                term_old=self.config['term_old'],
                term_new=self.config['term_new'],
                recompute_init_values=self.config['recompute_init_values'],
                noisy=self.noisy,
                endpoint_verification=self.config['endpoint_verification'],
            )

            init_once.run()
            bisector.narrow_down()
        except errors.VerifyOldBehaviorFailed as e:
            self.make_decision(
                'Expect old Chrome has old behavior (%s) but failed'
                % self.config['term_old']
            )
            raise errors.DiagnoseContradiction(
                '%s that old ChromeOS has old behavior (%s); '
                'but it became new behavior (%s) after deployed chrome'
                % (
                    self._verified_chromeos_prebuilt(),
                    self.config['term_old'],
                    self.config['term_new'],
                )
            ) from e
        except errors.VerifyNewBehaviorFailed:
            self.make_decision(
                'Unable to reproduce, the culprit is not in Chrome'
            )
            return False
        return True

    def narrow_down_chromeos_localbuild(
        self,
        dut_manager: dut_manager_module.DutManager,
        eval_cmd: list[str],
        buildbucket_build: bool = False,
        switch_test_harness_cmd: list[str] | None = None,
    ) -> None:
        """Bisect with ChromeOS localbuild.

        Args:
          dut_manager: a DutManager instance
          eval_cmd: command to reproduce the regression
          buildbucket_build: to build ChromeOS image on buildbucket
          switch_test_harness_cmd: command to switch test harness
        """
        if self.config['bisect_chrome']:
            self.make_decision(
                'Bypass ChromeOS local build since --bisect-chrome is on'
            )
            return
        if self.config['bisect_android']:
            self.make_decision(
                'Bypass ChromeOS local build since --bisect-android is on'
            )
            return
        if self.config['bypass_chromeos_build']:
            self.make_decision(
                'The bisection with ChromeOS local build is bypassed'
            )
            return

        self.states.add_history(
            'bisect',
            text='bisect chromeos local build',
            bisector='bisect_cros_repo',
            session=self.config['session'],
        )
        bisector = wrapper.BisectorWrapper(
            'bisect_cros_repo',
            self.config['session'],
            dut_manager,
            self.is_stateless,
        )
        future_build_cmd = None
        if not buildbucket_build:
            switch_cmd = [
                './switch_cros_localbuild.py',
                '--rich-result',
                '--session',
                self.config['session'],
            ]
        else:
            switch_cmd = [
                './switch_cros_localbuild_buildbucket.py',
                'bisect_chromeos',
                '--rich-result',
                '--board',
                self.config['board'],
            ]
            future_build_cmd = [
                './switch_cros_localbuild_buildbucket.py',
                'bisect_chromeos',
                '--rich-result',
                '--no-deploy',
                '--no-wait-for-build-completion',
                '--board',
                self.config['board'],
                '--session',
                self.config['session'],
            ]

        if self.config['disable_rootfs_verification']:
            switch_cmd.append('--disable-rootfs-verification')
        else:
            switch_cmd.append('--no-disable-rootfs-verification')

        switch_cmds = [switch_cmd]
        if dut_manager.is_vm:
            switch_cmds.append(self._switch_vm_cmd)
        if buildbucket_build:
            switch_cmds.append(switch_test_harness_cmd)

        bisector.init_if_necessary(
            self.cros_old,
            self.cros_new,
            init_args=[
                '--board',
                self.config['board'],
                '--chromeos-root',
                self.config['chromeos_root'],
                '--chromeos-mirror',
                self.config['chromeos_mirror'],
            ],
            switch_cmds=switch_cmds,
            eval_cmd=eval_cmd,
            future_build_cmd=future_build_cmd,
            # If using build bucket, we still need to setup test harness on the
            # bisect runner.
            old_value=self.config['old_value'],
            new_value=self.config['new_value'],
            term_old=self.config['term_old'],
            term_new=self.config['term_new'],
            recompute_init_values=self.config['recompute_init_values'],
            noisy=self.noisy,
            endpoint_verification=self.config['endpoint_verification'],
        )

        try:
            bisector.narrow_down()
        except errors.VerifyNewBehaviorFailed as e:
            raise errors.DiagnoseContradiction(
                '%s that the issue could be reproduced with chromeos prebuilt; '
                'but unable to reproduce with local build'
                % self._verified_chromeos_prebuilt()
            ) from e
        except errors.VerifyOldBehaviorFailed as e:
            raise errors.DiagnoseContradiction(
                '%s that the issue could only be reproduced with new chromeos '
                'prebuilt; but reproduced with both old and new local build'
                % self._verified_chromeos_prebuilt()
            ) from e

    def cmd_log(self, json_output=False):
        result = []
        history = self.states.history
        for i, entry in enumerate(history):
            if json_output:
                entry = entry.copy()
                result.append(entry)
            else:
                entry_time = datetime.datetime.fromtimestamp(
                    int(entry['timestamp'])
                )
                print(entry_time, entry['text'])

            # Special case, combine bisector log nestedly.
            if entry.get('event') == 'bisect':
                bisector = wrapper.BisectorWrapper(
                    entry['bisector'], entry['session']
                )
                try:
                    cmd = ['log', '--after', str(entry['timestamp'])]
                    if len(history) > i + 1:
                        cmd += ['--before', str(history[i + 1]['timestamp'])]

                    if json_output:
                        cmd.append('--json')
                        stdout = io.StringIO()
                        bisector.call(*cmd, stdout=stdout)
                        entry['log'] = json.loads(stdout.getvalue())
                    else:
                        bisector.call(*cmd)
                except errors.Uninitialized:
                    # if 'init' failed, skip
                    continue

        if json_output:
            print(json.dumps(result, indent=2))

    def cmd_view(self, json_output=False, verbose=False):
        result = []

        # Analyze log to determine what to show
        bisects = []
        history = self.states.history
        seen = set()
        for entry in history:
            if entry.get('event') != 'bisect':
                continue
            bisector = entry['bisector']
            if bisector in seen:
                continue
            seen.add(bisector)
            bisects.append(entry)

        for entry in bisects:
            bisector = wrapper.BisectorWrapper(
                entry['bisector'], entry['session']
            )
            try:
                cmd = ['view']
                if verbose:
                    cmd.append('--verbose')
                if json_output:
                    cmd.append('--json')
                    stdout = io.StringIO()
                    bisector.call(*cmd, stdout=stdout)
                    result.append(
                        {
                            'bisector': entry['bisector'],
                            'view': json.loads(stdout.getvalue()),
                        }
                    )
                else:
                    print('bisector: %s' % entry['bisector'])
                    bisector.call(*cmd)
                    print('=' * 60)
            except errors.Uninitialized:
                # if 'init' failed, skip
                continue

        if json_output:
            print(json.dumps(result, indent=2))


class DiagnoseCommandLineBase:
    """Diagnose command line interface."""

    def __init__(self):
        self.argument_parser = self.create_argument_parser()
        self.states = None

    @property
    def config(self):
        return self.states.config

    def check_cros_image_exists(
        self, argument_key: str, board: str, cros_version: str
    ) -> str:
        """Raises error if an image could not be found.

        If board does not present, the check always passes.

        Args:
          board: ChromeOS board name.
          cros_version: ChromeOS version.
          argument_key: The argument key name, will be displayed in error message
            if an image couldn't be found.

        Returns:
          ChromeOS full version.
        """
        cros_util.argtype_cros_version(cros_version)
        if cros_util.is_cros_short_version(cros_version):
            cros_version = cros_util.version_to_full(board, cros_version)

        if not board:
            return cros_version

        if not cros_util.has_test_image(board, cros_version):
            self.argument_parser.error(
                '%s: %s has no image for %s'
                % (argument_key, board, cros_version)
            )
        return cros_version

    def check_chrome_version(self, argument_key: str, chrome_version: str):
        if not cr_util.is_chrome_version(chrome_version):
            self.argument_parser.error(
                '%s: %s is not a valid Chrome version'
                % (argument_key, chrome_version)
            )

    def check_android_version(self, argument_key: str, android_version: str):
        if not android_util.is_android_build_id(android_version):
            self.argument_parser.error(
                '%s: %s is not a valid Android version'
                % (argument_key, android_version)
            )

    def check_options(self, opts):
        path_factory = common.ProjectPathFactory(
            opts.session, opts.work_base, opts.mirror_base
        )
        if not opts.chromeos_mirror:
            opts.chromeos_mirror = path_factory.get_chromeos_mirror()
            logger.info('chromeos_mirror = %s', opts.chromeos_mirror)
        if not opts.chromeos_root:
            opts.chromeos_root = path_factory.get_chromeos_tree()
            logger.info('chromeos_root = %s', opts.chromeos_root)
        if not opts.chrome_mirror:
            opts.chrome_mirror = path_factory.get_chrome_cache()
            logger.info('chrome_mirror = %s', opts.chrome_mirror)
        if not opts.chrome_root:
            opts.chrome_root = path_factory.get_chrome_tree()
            logger.info('chrome_root = %s', opts.chrome_root)

        if opts.bisect_chrome:
            if not cr_util.is_version_lesseq(
                opts.old, opts.new
            ) or not cr_util.is_direct_relative_version(opts.old, opts.new):
                self.argument_parser.error(
                    '%s is not ancestor of %s' % (opts.old, opts.new)
                )
        elif opts.bisect_android:
            if int(opts.old) >= int(opts.new):
                self.argument_parser.error(
                    '%s is not ancestor of %s' % (opts.old, opts.new)
                )
        elif not cros_util.is_ancestor_version(opts.old, opts.new):
            self.argument_parser.error(
                '%s is not ancestor of %s' % (opts.old, opts.new)
            )

        if opts.allocate_dut_satlab_ip:
            if not cros_lab_util.is_satlab_dut(opts.dut):
                raise errors.ArgumentError(
                    '--allocate-dut-satlab-ip',
                    'Should be only provided with a satlab dut',
                )
            cros_lab_util.write_satlab_ssh_config(
                opts.dut, opts.allocate_dut_satlab_ip
            )
        elif cros_lab_util.is_satlab_dut(opts.dut):
            cros_lab_util.write_satlab_ssh_config(opts.dut)

        if opts.dut == cros_lab_util.LAB_DUT:
            if (
                not opts.allocate_dut_board
                and not opts.allocate_dut_model
                and not opts.allocate_dut_sku
                and not opts.allocate_dut_dut_name
            ):
                self.argument_parser.error(
                    'either --allocate-dut-board, --allocate-dut-model, '
                    '--allocate-dut-sku, or --allocate-dut-dut-name '
                    'need to be specified if DUT is missing or "%s"'
                    % cros_lab_util.LAB_DUT
                )
        else:
            if not opts.board:
                opts.board = cros_util.query_dut_board(opts.dut)

        names = cros_util.list_board_names(opts.chromeos_root)
        if opts.board and opts.board not in names:
            util.report_similar_candidates('board name', opts.board, names)
            assert 0  # unreachable

        if opts.bisect_chrome and opts.bisect_android:
            self.argument_parser.error(
                '--bisect-chrome and --bisect-android are not allowed together'
            )
        elif opts.bisect_chrome:
            opts.base_cros_version = self.check_cros_image_exists(
                '--base-cros-version', opts.board, opts.base_cros_version
            )
            self.check_chrome_version('--old', opts.old)
            self.check_chrome_version('--new', opts.new)
        elif opts.bisect_android:
            opts.base_cros_version = self.check_cros_image_exists(
                '--base-cros-version', opts.board, opts.base_cros_version
            )
            self.check_android_version('--old', opts.old)
            self.check_android_version('--new', opts.new)
        else:
            opts.old = self.check_cros_image_exists(
                '--old', opts.board, opts.old
            )
            opts.new = self.check_cros_image_exists(
                '--new', opts.board, opts.new
            )

        if opts.metric:
            if opts.old_value is None:
                self.argument_parser.error('--old-value is not provided')
            if opts.new_value is None:
                self.argument_parser.error('--new-value is not provided')
            if opts.fail_to_pass:
                self.argument_parser.error(
                    '--fail-to-pass is not for benchmark test (--metric)'
                )
        else:
            if opts.old_value is not None:
                self.argument_parser.error(
                    '--old-value is provided but --metric is not'
                )
            if opts.new_value is not None:
                self.argument_parser.error(
                    '--new-value is provided but --metric is not'
                )
            if opts.recompute_init_values:
                self.argument_parser.error(
                    '--recompute-init-values is provided but --metric is not'
                )

        if (
            opts.term_old is None
            and opts.term_new is not None
            or opts.term_old is not None
            and opts.term_new is None
        ):
            self.argument_parser.error(
                '--term-old and --term-new must be both specified or both blank'
            )
        if not opts.term_old:
            if opts.metric:
                if opts.old_value < opts.new_value:
                    opts.term_old = 'LOW'
                    opts.term_new = 'HIGH'
                else:
                    opts.term_old = 'HIGH'
                    opts.term_new = 'LOW'
            else:
                if opts.fail_to_pass:
                    opts.term_old = 'FAIL'
                    opts.term_new = 'PASS'
                else:
                    opts.term_old = 'PASS'
                    opts.term_new = 'FAIL'

    def init_hook(self, opts):
        pass  # implemented by subclass if necessary

    @staticmethod
    def _dut_allocate_spec_from_args(
        opts: argparse.Namespace,
    ) -> core.DutAllocateSpec:
        """Creates a DutAllocateSpec from command line args."""
        return core.DutAllocateSpec(
            pool=opts.allocate_dut_pool,
            dimensions=opts.allocate_dut_dimensions,
            board=opts.allocate_dut_board,
            model=opts.allocate_dut_model,
            sku=opts.allocate_dut_sku,
            dut_name=opts.allocate_dut_dut_name,
            satlab_ip=opts.allocate_dut_satlab_ip,
            version_hint=opts.allocate_dut_version_hint,
            builder_hint=opts.allocate_dut_builder_hint,
            time_limit=opts.allocate_dut_time_limit,
            duration=opts.allocate_dut_duration,
            parallel=opts.allocate_dut_parallel,
            chromeos_root=opts.chromeos_root,
            session=opts.session,
        )

    def write_total_execution_time(self, start_timestamp, end_timestamp):
        statistics = self.states.statistics
        statistics.start_timestamp = start_timestamp
        statistics.end_timestamp = end_timestamp
        statistics.duration_secs = end_timestamp - start_timestamp

        self.states.save()

    def cmd_init(self, opts):
        self.check_options(opts)

        if opts.dut != cros_lab_util.LAB_DUT and not cros_util.is_good_dut(
            opts.dut
        ):
            if not cros_lab_util.repair(opts.dut, opts.chromeos_root):
                raise errors.BrokenDutException(
                    '%r is not a good DUT' % opts.dut
                )

        cros_prebuilt_old = opts.base_cros_version
        cros_prebuilt_new = opts.base_cros_version
        chrome_localbuild_old, chrome_localbuild_new = None, None
        android_prebuilt_old, android_prebuilt_new = None, None

        # Determines CrOS, Chrome, Android, Lacros versions from argument.
        if opts.bisect_chrome:
            chrome_localbuild_old, chrome_localbuild_new = opts.old, opts.new
        elif opts.bisect_android:
            android_prebuilt_old, android_prebuilt_new = opts.old, opts.new
        else:
            cros_prebuilt_old, cros_prebuilt_new = opts.old, opts.new

        config = dict(
            session=opts.session,
            mirror_base=opts.mirror_base,
            work_base=opts.work_base,
            chromeos_root=opts.chromeos_root,
            chromeos_mirror=opts.chromeos_mirror,
            chrome_root=opts.chrome_root,
            chrome_mirror=opts.chrome_mirror,
            android_root=opts.android_root,
            android_mirror=opts.android_mirror,
            dut=opts.dut,
            board=opts.board,
            old=opts.old,  # TODO(zjchang): Remove ambiguous term old and new
            new=opts.new,
            bisect_chrome=opts.bisect_chrome,
            bisect_android=opts.bisect_android,
            base_cros_version=opts.base_cros_version,
            cros_prebuilt_old=cros_prebuilt_old,
            cros_prebuilt_new=cros_prebuilt_new,
            chrome_localbuild_old=chrome_localbuild_old,
            chrome_localbuild_new=chrome_localbuild_new,
            android_prebuilt_old=android_prebuilt_old,
            android_prebuilt_new=android_prebuilt_new,
            term_old=opts.term_old,
            term_new=opts.term_new,
            test_name=opts.test_name,
            fail_to_pass=opts.fail_to_pass,
            metric=opts.metric,
            old_value=opts.old_value,
            new_value=opts.new_value,
            recompute_init_values=opts.recompute_init_values,
            noisy=opts.noisy,
            always_reflash=opts.always_reflash,
            reboot_before_test=opts.reboot_before_test,
            bypass_chromeos_prebuilt=opts.bypass_chromeos_prebuilt,
            bypass_chromeos_build=opts.bypass_chromeos_build,
            bypass_lacros_prebuilt=opts.bypass_lacros_prebuilt,
            bypass_chrome_build=opts.bypass_chrome_build,
            bypass_android_prebuilt=opts.bypass_android_prebuilt,
            bypass_android_build=opts.bypass_android_build,
            bypass_dlc_prebuilt=opts.bypass_dlc_prebuilt,
            disable_rootfs_verification=opts.disable_rootfs_verification,
            chrome_deploy_image=opts.chrome_deploy_image,
            disable_snapshot=opts.disable_snapshot,
            disable_buildbucket_chromeos=opts.disable_buildbucket_chromeos,
            enable_buildbucket_chrome=opts.enable_buildbucket_chrome,
            extra_test_variables=opts.extra_test_variables,
            endpoint_verification=opts.endpoint_verification,
            consider_test_crash_as_failure=opts.consider_test_crash_as_failure,
            experiments=opts.experiments,
        )

        self.states.init_states(config, self._dut_allocate_spec_from_args(opts))
        self.init_hook(opts)
        self.states.save()

    def cmd_run(self, opts):
        raise NotImplementedError  # implemented by subclass

    def cmd_log(self, opts):
        self.states.load_states()
        diagnoser = CrosDiagnoser(self.states, self.config, None)
        diagnoser.cmd_log(opts.json)

    def cmd_view(self, opts):
        self.states.load_states()
        diagnoser = CrosDiagnoser(self.states, self.config, None)
        diagnoser.cmd_view(opts.json, opts.verbose)

    def create_argument_parser_hook(self, parser_init):
        pass  # implemented by subclass if necessary

    def create_argument_parser(self):
        parents = [cli.create_session_optional_parser()]
        parser = cli.ArgumentParser(raise_bad_status=False)
        subparsers = parser.add_subparsers(
            dest='command', title='commands', metavar='<command>', required=True
        )

        parser_init = subparsers.add_parser(
            'init',
            help='Initialize',
            parents=parents + [experiment.common_flags()],
        )
        group = parser_init.add_argument_group(
            title='Source tree path options',
            description="""
        Specify the paths of chromeos/chrome/android mirror and checkout. They
        have the same default values as setup_cros_bisect.py, so usually you can
        omit them and it just works.
        """,
        )
        group.add_argument(
            '--mirror-base',
            metavar='MIRROR_BASE',
            default=os.environ.get('MIRROR_BASE', common.DEFAULT_MIRROR_BASE),
            help='Directory for mirrors (default: %(default)s)',
        )
        group.add_argument(
            '--work-base',
            metavar='WORK_BASE',
            default=os.environ.get('WORK_BASE', common.DEFAULT_WORK_BASE),
            help='Directory for bisection working directories '
            '(default: %(default)s)',
        )
        group.add_argument(
            '--chromeos-root',
            metavar='CHROMEOS_ROOT',
            type=cli.argtype_dir_path,
            default=os.environ.get('CHROMEOS_ROOT'),
            help='ChromeOS tree root',
        )
        group.add_argument(
            '--chromeos-mirror',
            type=cli.argtype_dir_path,
            default=os.environ.get('CHROMEOS_MIRROR'),
            help='ChromeOS repo mirror path',
        )
        group.add_argument(
            '--android-root',
            metavar='ANDROID_ROOT',
            type=cli.argtype_dir_path,
            default=os.environ.get('ANDROID_ROOT'),
            help='Android tree root',
        )
        group.add_argument(
            '--android-mirror',
            type=cli.argtype_dir_path,
            default=os.environ.get('ANDROID_MIRROR'),
            help='Android repo mirror path',
        )
        group.add_argument(
            '--chrome-root',
            metavar='CHROME_ROOT',
            type=cli.argtype_dir_path,
            default=os.environ.get('CHROME_ROOT'),
            help='Chrome tree root',
        )
        group.add_argument(
            '--chrome-mirror',
            metavar='CHROME_MIRROR',
            type=cli.argtype_dir_path,
            default=os.environ.get('CHROME_MIRROR'),
            help="chrome's gclient cache dir",
        )

        group = parser_init.add_argument_group(title='DUT allocation options')
        # Either a specific dut or a set of auto DUT selection options must be
        # given.
        group.add_argument(
            '--dut',
            metavar='DUT',
            default=cros_lab_util.LAB_DUT,
            help=(
                'Address of DUT (Device Under Test). If omitted, DUT will be '
                'automatically allocated from the lab. If present, all other'
                'options in the argument gruop are ignored.'
            ),
        )
        cros_helper.add_allocate_dut_arguments(
            group,
            allow_required=False,
            flags_prefix='allocate-dut',
        )

        group = parser_init.add_argument_group(title='Essential options')
        group.add_argument(
            '--board',
            metavar='BOARD',
            default=os.environ.get('BOARD'),
            help='ChromeOS board name; auto detected if DUT is not auto allocated',
        )
        group.add_argument(
            '--old',
            type=str,
            required=True,
            help='ChromeOS, Chrome or Android version with old behavior',
        )
        group.add_argument(
            '--new',
            type=str,
            required=True,
            help='ChromeOS, Chrome or Android version with new behavior',
        )
        group.add_argument(
            '--term-old', help='Alternative term for "old" state'
        )
        group.add_argument(
            '--term-new', help='Alternative term for "new" state'
        )

        group = parser_init.add_argument_group(
            title='Options for normal autotest tests'
        )
        group.add_argument('--test-name', help='Test name')
        group.add_argument(
            '--fail-to-pass',
            action='store_true',
            help='For functional tests: bisect the CL fixed the regression (when '
            'test became PASS). If not specified, the default is to bisect the CL '
            'which broke the test (when test became FAIL)',
        )
        group.add_argument('--metric', help='Metric name of benchmark test')
        group.add_argument(
            '--old-value',
            type=float,
            help='For benchmark test, old value of metric',
        )
        group.add_argument(
            '--new-value',
            type=float,
            help='For benchmark test, new value of metric',
        )
        group.add_argument(
            '--recompute-init-values',
            action='store_true',
            help='For performance test, recompute initial values',
        )

        self.create_argument_parser_hook(parser_init)

        group = parser_init.add_argument_group(title='Bisect behavior options')
        group.add_argument(
            '--noisy',
            help='Enable noisy binary search. Example value: "old=1/10,new=2/3"',
        )
        group.add_argument(
            '--always-reflash',
            action='store_true',
            help='Do not trust ChromeOS version number of DUT and always reflash. '
            'This is usually only needed when resume because previous bisect was '
            'interrupted and the DUT may be in an unexpected state',
        )
        group.add_argument(
            '--reboot-before-test',
            action='store_true',
            help='Reboot before test run',
        )
        group.add_argument(
            '--bypass-chromeos-prebuilt',
            action='store_true',
            help='Bypass chromeos prebuilt image bisect',
        )
        group.add_argument(
            '--bypass-chromeos-build',
            action='store_true',
            help='Bypass chromeos local build bisect',
        )
        group.add_argument(
            '--bypass-lacros-prebuilt',
            action='store_true',
            help='Bypass lacros prebuilt image bisect',
        )
        group.add_argument(
            '--bypass-chrome-build',
            action='store_true',
            help='Bypass chrome local build bisect',
        )
        group.add_argument(
            '--bypass-android-prebuilt',
            action='store_true',
            help='Bypass android prebuilt image bisect',
        )
        group.add_argument(
            '--bypass-android-build',
            action='store_true',
            help='Bypass chromeos local build bisect',
        )
        group.add_argument(
            '--bypass-dlc-prebuilt',
            action='store_true',
            help='Bypass DLC prebuilt',
        )
        group.add_argument(
            '--disable-rootfs-verification',
            action=argparse.BooleanOptionalAction,
            default=False,
            help='Whether to disable rootfs verification after update is complete, default is %(default)s',
        )
        group.add_argument(
            '--bisect-chrome',
            action='store_true',
            help='Bisect a chrome version',
        )
        group.add_argument(
            '--bisect-android',
            action='store_true',
            help='Bisect an android version',
        )
        group.add_argument(
            '--base-cros-version',
            type=cros_util.argtype_cros_version,
            help='ChromeOS base version for running chrome bisection',
        )
        group.add_argument(
            '--chrome-deploy-image',
            action='store_true',
            help='Build and deploy ChromeOS image when bisect Chrome',
        )
        group.add_argument(
            '--disable-snapshot',
            action='store_true',
            help='Disable snapshot for bisect chromeos prebuilt',
        )
        group.add_argument(
            '--disable-buildbucket-chromeos',
            action='store_true',
            help='Build ChromeOS on local instead of buildbucket',
        )
        group.add_argument(
            '--enable-buildbucket-chrome',
            action='store_true',
            help='Build Chrome on buildbucket',
        )
        group.add_argument(
            '--extra-test-variables',
            help='Extra variables passed to `test_that --args` or `tast -var`',
            action='append',
            default=[],
        )
        group.add_argument(
            '--endpoint-verification',
            action='store_true',
            help='Enable statistical method to verify endpoints',
        )
        group.add_argument(
            '--consider-test-crash-as-failure',
            action='store_true',
            help='Consider test crashes as failure instead of skip',
        )
        parser_init.set_defaults(func=self.cmd_init)

        parser_run = subparsers.add_parser(
            'run', help='Start auto bisection', parents=parents
        )
        parser_run.set_defaults(func=self.cmd_run)

        parser_log = subparsers.add_parser(
            'log', help='Prints what has been done so far', parents=parents
        )
        parser_log.add_argument(
            '--json', action='store_true', help='Machine readable output'
        )
        parser_log.set_defaults(func=self.cmd_log)

        parser_view = subparsers.add_parser(
            'view', help='Prints summary of current status', parents=parents
        )
        parser_view.add_argument('--verbose', '-v', action='store_true')
        parser_view.add_argument(
            '--json', action='store_true', help='Machine readable output'
        )
        parser_view.set_defaults(func=self.cmd_view)

        return parser

    def main(self, args=None):
        opts = self.argument_parser.parse_args(args)
        common.config_logging(opts)

        session_file = common.get_session_log_path(
            opts.session, self.__class__.__name__
        )
        self.states = core.DiagnoseStates(session_file)
        opts.func(opts)
