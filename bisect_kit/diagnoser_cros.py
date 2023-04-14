# Copyright 2018 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Diagnose ChromeOS regressions on top of subcomponent bisectors."""

from __future__ import annotations

import argparse
import datetime
import json
import logging
import os
import sys
import time

from bisect_kit import android_util
from bisect_kit import cli
from bisect_kit import common
from bisect_kit import configure
from bisect_kit import core
from bisect_kit import cr_util
from bisect_kit import cros_lab_util
from bisect_kit import cros_util
from bisect_kit import errors
from bisect_kit import util
from bisect_kit import wrapper
import cros_helper
import setup_cros_bisect


logger = logging.getLogger(__name__)


# TODO(b/278835654): Use dataclass instead of this hack.
class AttrDict(dict):
    """A util class whose members can be accessed as dict or properties.

    Caveat: the keys must be valid identifier names.
    """

    def __init__(self, d):
        super().__init__(**d)
        self.__dict__ = d


class DiagnoseStates(core.States):
    """Diagnose states."""

    def init(self, config):
        self.data = dict(config=config, history=[])
        self.init_done()

    @property
    def config(self) -> AttrDict:
        return AttrDict(self.data['config'])

    def add_history(self, event, **kwargs):
        entry = dict(timestamp=time.time(), event=event, **kwargs)
        self.data['history'].append(entry)
        self.save()


class CrosDiagnoser:
    """Integrated bisectors API for ChromeOS regression analysis."""

    def __init__(self, states, path_factory, config, dut):
        self.states = states
        self.path_factory = path_factory
        self.config = config
        # self.noisy will be updated after we gather more information from each
        # bisect session.
        self.noisy = config['noisy']
        self.dut = dut

        self.cros_old = None
        self.cros_new = None
        self.old_info = None
        self.new_info = None

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
        autotest: bool = False,
        common_switch_cmd: list[str] = None,
        cros_prebuilt_extra_switch_cmd: list[str] = None,
        cros_prebuilt_eval_cmd: list[str] = None,
        android_prebuilt_eval_cmd: list[str] = None,
        lacros_prebuilt_eval_cmd: list[str] = None,
        chrome_localbuild_eval_cmd: list[str] = None,
        chrome_localbuild_with_tests: bool = True,
        cros_localbuild_eval_cmd: list[str] = None,
        cros_localbuild_extra_switch_cmd: list[str] = None,
    ):
        # Step 1: Narrows down ChromeOS prebuilt.
        self.narrow_down_chromeos_prebuilt(
            self.config['cros_prebuilt_old'],
            self.config['cros_prebuilt_new'],
            cros_prebuilt_eval_cmd,
            extra_switch_cmd=cros_prebuilt_extra_switch_cmd,
        )

        # Switches CrOS repository and DUT to narrowed down old version.
        self.switch_chromeos_to_old(force=self.config['always_reflash'])
        util.check_call(
            *(common_switch_cmd + [self.cros_old, '--dut', self.dut])
        )
        dut_os_version = cros_util.query_dut_short_version(self.dut)

        # Step 2: Narrows down Android prebuilt. If the error can be reproduced,
        # returns the result and skips other steps.
        try:
            if self.narrow_down_android(android_prebuilt_eval_cmd):
                return
        except errors.DiagnoseContradiction:
            raise
        except Exception:
            self.make_decision(
                'Exception in Android bisector before verification; '
                'assume the culprit is not inside Android and continue'
            )
        # Assume it's ok to leave random version of android prebuilt on DUT.
        # Sanity check. The OS version should not change after android bisect.
        assert dut_os_version == cros_util.query_dut_short_version(
            self.dut
        ), 'Someone else reflashed the DUT. DUT locking is not respected?'

        # Step 3: Narrows down Lacros prebuilt. If the error can be reproduced,
        # returns the result and skips other steps.
        try:
            self.narrow_down_lacros_prebuilt(lacros_prebuilt_eval_cmd)
        except errors.DiagnoseContradiction:
            raise
        except Exception:
            self.make_decision(
                'Exception in Lacros bisector before verification; '
                'assume the culprit is not inside Lacros and continue'
            )
        # Assume it's ok to leave random version of Lacros prebuilt on DUT.
        # Sanity check. The OS version should not change after lacros bisect.
        assert dut_os_version == cros_util.query_dut_short_version(
            self.dut
        ), 'Someone else reflashed the DUT. DUT locking is not respected?'

        # Step 4: Narrows down Chrome local build. If the error can be reproduced,
        # returns the result and skips other steps.
        buildbucket_buildable = cros_util.is_buildbucket_buildable(
            self.config['cros_prebuilt_old']
        )
        if autotest and chrome_localbuild_with_tests:
            # Now, the version of autotest on the DUT is unknown and may be even
            # not installed. Invoke the test once here, so
            #   - make sure autotest-deps is installed, with expected version
            #   - autotest-deps is installed first, so our chrome test binaries
            #     won't be reset to default version during bisection.
            # It's acceptable to spend extra time to run test once because
            #   - only few tests do so
            #   - tests are migrating away from autotest
            util.call(*chrome_localbuild_eval_cmd)
        try:
            if self.narrow_down_chrome(
                chrome_localbuild_eval_cmd,
                buildbucket_build=buildbucket_buildable
                and self.config['enable_buildbucket_chrome'],
                with_tests=chrome_localbuild_with_tests,
            ):
                return
        except errors.DiagnoseContradiction:
            raise
        except Exception:
            self.make_decision(
                'Exception in Chrome bisector before verification; '
                'assume the culprit is not inside Chrome and continue'
            )
        # If chrome is not deployed with cros image, do the sanity check to ensure
        # OS version.
        if (
            not self.config['chrome_deploy_image']
            and not self.config['enable_buildbucket_chrome']
        ):
            assert dut_os_version == cros_util.query_dut_short_version(
                self.dut
            ), 'Someone else reflashed the DUT. DUT locking is not respected?'

        # Step 5: Narrowdown chromeos localbuild.
        self.narrow_down_chromeos_localbuild(
            cros_localbuild_eval_cmd,
            buildbucket_buildable
            and not self.config['disable_buildbucket_chromeos'],
            extra_switch_cmd=cros_localbuild_extra_switch_cmd,
        )

    def narrow_down_chromeos_prebuilt(
        self, old, new, eval_cmd, extra_switch_cmd=None
    ):
        """Bisect with ChromeOS prebuilt.

        Args:
          old: old ChromeOS version
          new: new ChromeOS version
          eval_cmd: command to reproduce the regression
          extra_switch_cmd: additional command to run when switch chromeos version
        """
        if self.config['bisect_chrome']:
            self.make_decision(
                'The ChromeOS prebuilt bisection is bypassed since --bisect_chrome is on'
            )
            self.cros_old = self.cros_new = self.config['base_cros_version']
        elif self.config['bisect_android']:
            self.make_decision(
                'The ChromeOS prebuilt bisection is bypassed since --bisect_android is on'
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
            bisector = wrapper.BisectorWrapper(
                'bisect_cros_version', self.config['session']
            )
            switch_cmd = [
                './switch_cros_prebuilt.py',
                '--rich_result',
                '--session',
                self.config['session'],
                '--rich_result',
            ]
            if not self.config['disable_rootfs_verification']:
                switch_cmd.append('--no_disable_rootfs_verification')

            init_args = ['--dut', self.dut, '--board', self.config['board']]
            if self.config['disable_snapshot']:
                init_args.append('--disable_snapshot')
            bisector.init_if_necessary(
                old,
                new,
                init_args=init_args,
                switch_cmd=switch_cmd,
                eval_cmd=eval_cmd,
                old_value=self.config['old_value'],
                new_value=self.config['new_value'],
                term_old=self.config['term_old'],
                term_new=self.config['term_new'],
                recompute_init_values=self.config['recompute_init_values'],
                noisy=self.noisy,
                endpoint_verification=self.config['endpoint_verification'],
            )
            if extra_switch_cmd:
                bisector.append_switch_command(extra_switch_cmd)

            self.cros_old, self.cros_new, self.noisy = bisector.narrow_down()

        prebuilt_states = core.BisectStates.from_bisector_class(
            'ChromeOSVersionDomain', self.config['session']
        )
        if prebuilt_states.load_states():
            cros_util.SnapshotStore.init_with_state(prebuilt_states)

        # If old and new chrome version not specified, fetches info from metadata.
        if self.config['bisect_chrome']:
            self.old_info = {'cr_version': self.config['chrome_localbuild_old']}
            self.new_info = {'cr_version': self.config['chrome_localbuild_new']}
        elif self.config['bisect_android']:
            base_cros_info = cros_util.version_info(
                self.config['board'], self.config['base_cros_version']
            )
            self.old_info = {
                'android_build_id': self.config['android_prebuilt_old'],
                'android_branch': base_cros_info.get('android_branch'),
            }
            self.new_info = {
                'android_build_id': self.config['android_prebuilt_new'],
                'android_branch': base_cros_info.get('android_branch'),
            }
        else:
            self.old_info = cros_util.version_info(
                self.config['board'], self.cros_old
            )
            self.new_info = cros_util.version_info(
                self.config['board'], self.cros_new
            )

        logger.info(
            'old: cros %s, chrome %s, android %s',
            self.old_info.get('cros_full_version'),
            self.old_info.get('cr_version'),
            self.old_info.get('android_build_id'),
        )
        logger.info(
            'new: cros %s, chrome %s, android %s',
            self.new_info.get('cros_full_version'),
            self.new_info.get('cr_version'),
            self.new_info.get('android_build_id'),
        )

    def switch_chromeos_to_old(self, force=False):
        """Switch ChromeOS version to old prebuilt.

        Args:
          force: always do 'cros flash' no matter the current version number of DUT
        """
        util.check_call(
            './switch_cros_localbuild.py',
            '--rich_result',
            '--chromeos_root',
            self.config['chromeos_root'],
            '--chromeos_mirror',
            self.config['chromeos_mirror'],
            '--board',
            self.config['board'],
            '--nobuild',
            self.cros_old,
        )

        if not force:
            if cros_util.is_cros_full_version(self.cros_old):
                version = cros_util.version_to_short(self.cros_old)
            else:
                version = self.cros_old
            if cros_util.query_dut_prebuilt_version(self.dut) == version:
                return
        cmd = [
            './switch_cros_prebuilt.py',
            '--rich_result',
            '--session',
            self.config['session'],
            '--board',
            self.config['board'],
            '--dut',
            self.dut,
            self.cros_old,
        ]
        if not self.config['disable_rootfs_verification']:
            cmd.append('--no_disable_rootfs_verification')
        util.check_call(*cmd)

    def _need_android_bisect(self, old_info, new_info):
        """Determine whether to do android bisect or not for given two versions.

        Args:
          old_info: old version info from cros_util.version_info()
          new_info: new version info from cros_util.version_info()

        Returns:
          True if android bisect is necessary (ex. version not equal) and possible
          (ex. same branch).
        """
        if self.config['bisect_chrome']:
            self.make_decision(
                (
                    'The Android prebuilt bisection is bypassed since --bisect_chrome'
                    ' is on'
                )
            )
            return False

        if (
            old_info.get('android_build_id') is None
            or new_info.get('android_build_id') is None
        ):
            self.make_decision(
                'At least one version has no Android. skip Android bisect'
            )
            return False

        if (
            old_info.get('android_branch') is None
            or new_info.get('android_branch') is None
        ):
            self.make_decision(
                'At least one chromeOS version has no Android branch '
                'information in the metadata. skip Android bisect'
            )
            return False

        if old_info['android_build_id'] == new_info['android_build_id']:
            self.make_decision(
                'The Android version is identical, no need to bisect Android'
            )
            return False

        if old_info.get('android_branch') != new_info.get('android_branch'):
            self.make_decision(
                'Android branch mismatch: %s vs %s; unable to bisect Android'
                % (
                    old_info.get('android_branch'),
                    new_info.get('android_branch'),
                )
            )
            return False

        return True

    def narrow_down_android(self, eval_cmd, extra_switch_cmd=None):
        """Bisect with Android prebuilt and localbuild.

        It's known that the version of ChromeOS has old behavior.

        Args:
          eval_cmd: command to reproduce the regression
          extra_switch_cmd: additional command to run when switch android version

        Returns:
          True: culprit is inside Android
          False: culprit is not inside Android or unable to bisect
        """
        verified = False
        if not self._need_android_bisect(self.old_info, self.new_info):
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
            android_old = self.old_info['android_build_id']
            android_new = self.new_info['android_build_id']
        else:
            logger.info('bisect android prebuilt')
            self.states.add_history(
                'bisect',
                text='bisect android prebuilt',
                bisector='bisect_android_build_id',
                session=self.config['session'],
            )
            bisector = wrapper.BisectorWrapper(
                'bisect_android_build_id', self.config['session']
            )
            try:
                bisector.init_if_necessary(
                    self.old_info['android_build_id'],
                    self.new_info['android_build_id'],
                    init_args=[
                        '--dut',
                        self.dut,
                        '--branch',
                        self.old_info['android_branch'],
                    ],
                    switch_cmd=['./switch_arc_prebuilt.py', '--rich_result'],
                    eval_cmd=eval_cmd,
                    old_value=self.config['old_value'],
                    new_value=self.config['new_value'],
                    term_old=self.config['term_old'],
                    term_new=self.config['term_new'],
                    recompute_init_values=self.config['recompute_init_values'],
                    noisy=self.noisy,
                    endpoint_verification=self.config['endpoint_verification'],
                )
                if extra_switch_cmd:
                    bisector.append_switch_command(extra_switch_cmd)

                android_old, android_new, self.noisy = bisector.narrow_down()
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
        assert self.old_info['android_branch'].startswith('git_')
        git_branch = self.old_info['android_branch'][4:]
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
            'bisect_android_repo', self.config['session']
        )
        try:
            bisector.init_if_necessary(
                android_old,
                android_new,
                init_args=[
                    '--dut',
                    self.dut,
                    '--android_root',
                    android_root,
                    '--android_mirror',
                    android_mirror,
                ],
                switch_cmd=['./switch_arc_localbuild.py', '--rich_result'],
                eval_cmd=eval_cmd,
                old_value=self.config['old_value'],
                new_value=self.config['new_value'],
                term_old=self.config['term_old'],
                term_new=self.config['term_new'],
                recompute_init_values=self.config['recompute_init_values'],
                noisy=self.noisy,
                endpoint_verification=self.config['endpoint_verification'],
            )
            if extra_switch_cmd:
                bisector.append_switch_command(extra_switch_cmd)

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

    def narrow_down_lacros_prebuilt(self, eval_cmd: str, extra_switch_cmd=None):
        """Bisect with Lacros Prebuilt.

        It's known that the version of ChromeOS has old behavior.

        Args:
          eval_cmd: command to reproduce the regression
          extra_switch_cmd: additional command to run when switch lacros version

        Returns:
          True: culprit is inside Lacros
          False: culprit is not inside Lacros or unable to bisect
        """
        if self.config['bisect_android']:
            self.make_decision(
                (
                    'The bisection with Lacros prebuilt is bypassed since '
                    '--bisect_android is on'
                )
            )
            return False

        if self.config['bypass_lacros_prebuilt']:
            self.make_decision('The bisection with Lacros is bypassed')
            return False

        if self.old_info['cr_version'] == self.new_info['cr_version']:
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
            '--rich_result',
            '--dut',
            self.dut,
            '--chromeos_root',
            self.config['chromeos_root'],
        ]

        bisector = wrapper.BisectorWrapper(
            'bisect_lacros_prebuilt', self.config['session']
        )

        init_args = [
            '--dut',
            self.dut,
            '--board',
            self.config['board'],
            '--chromeos_root',
            self.config['chromeos_root'],
        ]

        try:
            bisector.init_if_necessary(
                self.old_info['cr_version'],
                self.new_info['cr_version'],
                init_args=init_args,
                switch_cmd=switch_cmd,
                eval_cmd=eval_cmd,
                old_value=self.config['old_value'],
                new_value=self.config['new_value'],
                term_old=self.config['term_old'],
                term_new=self.config['term_new'],
                recompute_init_values=self.config['recompute_init_values'],
                noisy=self.noisy,
                endpoint_verification=self.config['endpoint_verification'],
            )
            if extra_switch_cmd:
                bisector.append_switch_command(extra_switch_cmd)

            lacros_old, lacros_new, self.noisy = bisector.narrow_down()

            # If lacros prebuilt successfully narrow down to a range,
            # Run chrome bisection from that range
            self.old_info['cr_version'] = lacros_old
            self.new_info['cr_version'] = lacros_new
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

    def narrow_down_chrome(
        self,
        eval_cmd,
        buildbucket_build=False,
        extra_switch_cmd=None,
        with_tests=True,
    ):
        """Bisect with Chrome localbuild.

        It's known that the version of ChromeOS has old behavior.

        Args:
          eval_cmd: command to reproduce the regression
          buildbucket_build: to build ChromeOS image on buildbucket
          extra_switch_cmd: additional command to run when switch chrome version
          with_tests: build test binaries as well

        Returns:
          True: culprit is inside Chrome
          False: culprit is not inside Chrome or unable to bisect
        """

        if self.config['bisect_android']:
            self.make_decision(
                (
                    'The bisection with Chrome local build is bypassed since '
                    '--bisect_android is on'
                )
            )
            return False

        if self.config['bypass_chrome_build']:
            self.make_decision(
                'The bisection with Chrome local build is bypassed'
            )
            return False

        if self.old_info['cr_version'] == self.new_info['cr_version']:
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
                '--rich_result',
                '--dut',
                self.dut,
                '--chromeos_root',
                self.config['chromeos_root'],
                '--chromeos_mirror',
                self.config['chromeos_mirror'],
                '--chromeos_rev',
                self.cros_old,
            ]
            if not self.config['disable_rootfs_verification']:
                switch_cmd.append('--no_disable_rootfs_verification')
        else:
            switch_cmd = [
                './switch_cros_cr_localbuild_internal.py',
                '--rich_result',
                '--dut',
                self.dut,
            ]
            if not with_tests:
                switch_cmd.append('--without_tests')
            if self.config['chrome_deploy_image']:
                switch_cmd += [
                    '--deploy_method=image',
                    '--chromeos_root',
                    self.config['chromeos_root'],
                ]
                if not self.config['disable_rootfs_verification']:
                    switch_cmd.append('--no_disable_rootfs_verification')

        bisector = wrapper.BisectorWrapper(
            'bisect_cr_localbuild_internal', self.config['session']
        )
        try:
            bisector.init_if_necessary(
                self.old_info['cr_version'],
                self.new_info['cr_version'],
                init_args=[
                    '--chrome_root',
                    self.config['chrome_root'],
                    '--chrome_mirror',
                    self.config['chrome_mirror'],
                ],
                switch_cmd=switch_cmd,
                eval_cmd=eval_cmd,
                old_value=self.config['old_value'],
                new_value=self.config['new_value'],
                term_old=self.config['term_old'],
                term_new=self.config['term_new'],
                recompute_init_values=self.config['recompute_init_values'],
                noisy=self.noisy,
                endpoint_verification=self.config['endpoint_verification'],
            )
            if extra_switch_cmd:
                bisector.append_switch_command(extra_switch_cmd)

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
        self, eval_cmd, buildbucket_build=False, extra_switch_cmd=None
    ):
        """Bisect with ChromeOS localbuild.

        Args:
          eval_cmd: command to reproduce the regression
          buildbucket_build: to build ChromeOS image on buildbucket
          extra_switch_cmd: additional command to run when switch ChromeOS version
        """
        if self.config['bisect_chrome']:
            self.make_decision(
                'Bypass ChromeOS local build since --bisect_chrome is on'
            )
            return
        if self.config['bisect_android']:
            self.make_decision(
                'Bypass ChromeOS local build since --bisect_android is on'
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
            'bisect_cros_repo', self.config['session']
        )
        future_build_cmd = None
        if not buildbucket_build:
            switch_cmd = ['./switch_cros_localbuild.py', '--rich_result']
        else:
            switch_cmd = [
                './switch_cros_localbuild_buildbucket.py',
                'bisect_chromeos',
                '--rich_result',
            ]
            future_build_cmd = [
                './switch_cros_localbuild_buildbucket.py',
                'bisect_chromeos',
                '--rich_result',
                '--nodeploy',
            ]

        if not self.config['disable_rootfs_verification']:
            switch_cmd.append('--no_disable_rootfs_verification')

        bisector.init_if_necessary(
            self.cros_old,
            self.cros_new,
            init_args=[
                '--dut',
                self.dut,
                '--board',
                self.config['board'],
                '--chromeos_root',
                self.config['chromeos_root'],
                '--chromeos_mirror',
                self.config['chromeos_mirror'],
            ],
            switch_cmd=switch_cmd,
            eval_cmd=eval_cmd,
            future_build_cmd=future_build_cmd,
            old_value=self.config['old_value'],
            new_value=self.config['new_value'],
            term_old=self.config['term_old'],
            term_new=self.config['term_new'],
            recompute_init_values=self.config['recompute_init_values'],
            noisy=self.noisy,
            endpoint_verification=self.config['endpoint_verification'],
        )
        if extra_switch_cmd:
            bisector.append_switch_command(extra_switch_cmd)

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
        history = self.states.data['history']
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
                        output = bisector.call(*cmd, capture_output=True)
                        entry['log'] = json.loads(output)
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
        history = self.states.data['history']
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
                    output = bisector.call(*cmd, capture_output=True)
                    result.append(
                        {
                            'bisector': entry['bisector'],
                            'view': json.loads(output),
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
        common.init()
        self.argument_parser = self.create_argument_parser()
        self.states = None

    @property
    def config(self) -> AttrDict:
        return self.states.config

    def check_cros_image_exists(
        self, argument_key: str, board: str, cros_version: str
    ) -> str:
        """Raises error if an image could not be found.

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

    def check_options(self, opts, path_factory):
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
                    '--allocate_dut_satlab_ip',
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
                and not opts.allocate_dut_name
            ):
                self.argument_parser.error(
                    'either --allocate_dut_board, --allocate_dut_model, '
                    '--allocate_dut_sku, or --allocate_dut_name'
                    'need to be specified if DUT is "%s"'
                    % cros_lab_util.LAB_DUT
                )
            # Board name cannot be deduced from auto allocated devices because they
            # may be provisioned with image of unexpected board.
            if not opts.board:
                self.argument_parser.error(
                    '--board need to be specified if DUT is "%s"'
                    % cros_lab_util.LAB_DUT
                )
        else:
            if not opts.board:
                opts.board = cros_util.query_dut_board(opts.dut)

        names = cros_util.list_board_names(opts.chromeos_root)
        if opts.board not in names:
            util.report_similar_candidates('board name', opts.board, names)
            assert 0  # unreachable

        if opts.bisect_chrome and opts.bisect_android:
            self.argument_parser.error(
                '--bisect_chrome and --bisect_android are not allowed together'
            )
        elif opts.bisect_chrome:
            opts.base_cros_version = self.check_cros_image_exists(
                '--base_cros_version', opts.board, opts.base_cros_version
            )
            self.check_chrome_version('--old', opts.old)
            self.check_chrome_version('--new', opts.new)
        elif opts.bisect_android:
            opts.base_cros_version = self.check_cros_image_exists(
                '--base_cros_version', opts.board, opts.base_cros_version
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
                self.argument_parser.error('--old_value is not provided')
            if opts.new_value is None:
                self.argument_parser.error('--new_value is not provided')
            if opts.fail_to_pass:
                self.argument_parser.error(
                    '--fail_to_pass is not for benchmark test (--metric)'
                )
        else:
            if opts.old_value is not None:
                self.argument_parser.error(
                    '--old_value is provided but --metric is not'
                )
            if opts.new_value is not None:
                self.argument_parser.error(
                    '--new_value is provided but --metric is not'
                )
            if opts.recompute_init_values:
                self.argument_parser.error(
                    '--recompute_init_values is provided but --metric is not'
                )

        if (
            opts.term_old is None
            and opts.term_new is not None
            or opts.term_old is not None
            and opts.term_new is None
        ):
            self.argument_parser.error(
                '--term_old and --term_new must be both specified or both blank'
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

    def cmd_init(self, opts):
        path_factory = setup_cros_bisect.DefaultProjectPathFactory(
            opts.mirror_base, opts.work_base, opts.session
        )
        self.check_options(opts, path_factory)

        if opts.dut != cros_lab_util.LAB_DUT and not cros_util.is_good_dut(
            opts.dut
        ):
            if not cros_lab_util.repair(opts.dut):
                raise ValueError('DUT is in bad state')

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

        opts_to_save = vars(opts)
        # Remove opts not to save.
        for k in ['command', 'log_file', 'debug', 'rc', 'func']:
            opts_to_save.pop(k)

        config = opts_to_save | dict(
            cros_prebuilt_old=cros_prebuilt_old,
            cros_prebuilt_new=cros_prebuilt_new,
            chrome_localbuild_old=chrome_localbuild_old,
            chrome_localbuild_new=chrome_localbuild_new,
            android_prebuilt_old=android_prebuilt_old,
            android_prebuilt_new=android_prebuilt_new,
        )

        self.states.init(config)
        self.init_hook(opts)
        self.states.save()

    def cmd_run(self, opts):
        raise NotImplementedError  # implemented by subclass

    def cmd_log(self, opts):
        self.states.load_states()
        path_factory = setup_cros_bisect.DefaultProjectPathFactory(
            self.config['mirror_base'],
            self.config['work_base'],
            self.config['session'],
        )
        diagnoser = CrosDiagnoser(self.states, path_factory, self.config, None)

        diagnoser.cmd_log(opts.json)

    def cmd_view(self, opts):
        self.states.load_states()
        path_factory = setup_cros_bisect.DefaultProjectPathFactory(
            self.config['mirror_base'],
            self.config['work_base'],
            self.config['session'],
        )
        diagnoser = CrosDiagnoser(self.states, path_factory, self.config, None)
        diagnoser.cmd_view(opts.json, opts.verbose)

    def create_argument_parser_hook(self, parser_init):
        pass  # implemented by subclass if necessary

    def create_argument_parser(self):
        parents = [common.create_session_optional_parser()]
        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers(
            dest='command', title='commands', metavar='<command>', required=True
        )

        parser_init = subparsers.add_parser(
            'init', help='Initialize', parents=parents
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
            '--mirror_base',
            metavar='MIRROR_BASE',
            default=configure.get(
                'MIRROR_BASE', setup_cros_bisect.DEFAULT_MIRROR_BASE
            ),
            help='Directory for mirrors (default: %(default)s)',
        )
        group.add_argument(
            '--work_base',
            metavar='WORK_BASE',
            default=configure.get(
                'WORK_BASE', setup_cros_bisect.DEFAULT_WORK_BASE
            ),
            help='Directory for bisection working directories '
            '(default: %(default)s)',
        )
        group.add_argument(
            '--chromeos_root',
            metavar='CHROMEOS_ROOT',
            type=cli.argtype_dir_path,
            default=configure.get('CHROMEOS_ROOT'),
            help='ChromeOS tree root',
        )
        group.add_argument(
            '--chromeos_mirror',
            type=cli.argtype_dir_path,
            default=configure.get('CHROMEOS_MIRROR'),
            help='ChromeOS repo mirror path',
        )
        group.add_argument(
            '--android_root',
            metavar='ANDROID_ROOT',
            type=cli.argtype_dir_path,
            default=configure.get('ANDROID_ROOT'),
            help='Android tree root',
        )
        group.add_argument(
            '--android_mirror',
            type=cli.argtype_dir_path,
            default=configure.get('ANDROID_MIRROR'),
            help='Android repo mirror path',
        )
        group.add_argument(
            '--chrome_root',
            metavar='CHROME_ROOT',
            type=cli.argtype_dir_path,
            default=configure.get('CHROME_ROOT'),
            help='Chrome tree root',
        )
        group.add_argument(
            '--chrome_mirror',
            metavar='CHROME_MIRROR',
            type=cli.argtype_dir_path,
            default=configure.get('CHROME_MIRROR'),
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
            prepend_flags_prefix=True,
        )

        group = parser_init.add_argument_group(title='Essential options')
        group.add_argument(
            '--board',
            metavar='BOARD',
            default=configure.get('BOARD'),
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
            '--term_old', help='Alternative term for "old" state'
        )
        group.add_argument(
            '--term_new', help='Alternative term for "new" state'
        )

        group = parser_init.add_argument_group(
            title='Options for normal autotest tests'
        )
        group.add_argument('--test_name', help='Test name')
        group.add_argument(
            '--fail_to_pass',
            action='store_true',
            help='For functional tests: bisect the CL fixed the regression (when '
            'test became PASS). If not specified, the default is to bisect the CL '
            'which broke the test (when test became FAIL)',
        )
        group.add_argument('--metric', help='Metric name of benchmark test')
        group.add_argument(
            '--old_value',
            type=float,
            help='For benchmark test, old value of metric',
        )
        group.add_argument(
            '--new_value',
            type=float,
            help='For benchmark test, new value of metric',
        )
        group.add_argument(
            '--recompute_init_values',
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
            '--always_reflash',
            action='store_true',
            help='Do not trust ChromeOS version number of DUT and always reflash. '
            'This is usually only needed when resume because previous bisect was '
            'interrupted and the DUT may be in an unexpected state',
        )
        group.add_argument(
            '--reboot_before_test',
            action='store_true',
            help='Reboot before test run',
        )
        group.add_argument(
            '--bypass_chromeos_prebuilt',
            action='store_true',
            help='Bypass chromeos prebuilt image bisect',
        )
        group.add_argument(
            '--bypass_chromeos_build',
            action='store_true',
            help='Bypass chromeos local build bisect',
        )
        group.add_argument(
            '--bypass_lacros_prebuilt',
            action='store_true',
            help='Bypass lacros prebuilt image bisect',
        )
        group.add_argument(
            '--bypass_chrome_build',
            action='store_true',
            help='Bypass chrome local build bisect',
        )
        group.add_argument(
            '--bypass_android_prebuilt',
            action='store_true',
            help='Bypass android prebuilt image bisect',
        )
        group.add_argument(
            '--bypass_android_build',
            action='store_true',
            help='Bypass chromeos local build bisect',
        )
        group.add_argument(
            '--no-disable-rootfs-verification',
            '--no_disable_rootfs_verification',
            dest='disable_rootfs_verification',
            action='store_false',
            help="Don't disable rootfs verification after update is completed",
        )
        group.add_argument(
            '--bisect_chrome',
            action='store_true',
            help='Bisect a chrome version',
        )
        group.add_argument(
            '--bisect_android',
            action='store_true',
            help='Bisect an android version',
        )
        group.add_argument(
            '--base_cros_version',
            type=cros_util.argtype_cros_version,
            help='ChromeOS base version for running chrome bisection',
        )
        group.add_argument(
            '--chrome_deploy_image',
            action='store_true',
            help='Build and deploy ChromeOS image when bisect Chrome',
        )
        group.add_argument(
            '--disable_snapshot',
            action='store_true',
            help='Disable snapshot for bisect chromeos prebuilt',
        )
        group.add_argument(
            '--disable_buildbucket_chromeos',
            action='store_true',
            help='Build ChromeOS on local instead of buildbucket',
        )
        group.add_argument(
            '--enable_buildbucket_chrome',
            action='store_true',
            help='Build Chrome on buildbucket',
        )
        group.add_argument(
            '--extra_test_variables',
            help='Extra variables passed to `test_that --args` or `tast -var`',
            action='append',
            default=[],
        )
        group.add_argument(
            '--endpoint_verification',
            action='store_true',
            help='Enable statistical method to verify endpoints',
        )
        group.add_argument(
            '--consider_test_crash_as_failure',
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

        session_dir = common.determine_session_dir(opts.session)
        session_file = os.path.join(session_dir, self.__class__.__name__)
        self.states = DiagnoseStates(session_file)
        opts.func(opts)
