# -*- coding: utf-8 -*-
# Copyright 2018 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Diagnose ChromeOS regressions on top of subcomponent bisectors."""
from __future__ import print_function
import logging
import os

from bisect_kit import cros_util
from bisect_kit import errors
from bisect_kit import util
from bisect_kit import wrapper

logger = logging.getLogger(__name__)


def need_android_bisect(old_info, new_info):
  """Determine whether to do android bisect or not for given two versions.

  Args:
    old_info: old version info from cros_util.version_info()
    new_info: new version info from cros_util.version_info()

  Returns:
    True if android bisect is necessary (ex. version not equal) and possible
    (ex. same branch).
  """
  if (old_info.get('android_build_id') is None or
      new_info.get('android_build_id') is None):
    logger.info('At least one version has no android. skip android bisect')
    return False

  if old_info['android_build_id'] == new_info['android_build_id']:
    logger.info('android version identical, no need to bisect android')
    return False

  if old_info.get('android_branch') != new_info.get('android_branch'):
    logger.warning(
        'android branch mismatch: %s vs %s; unable to bisect android',
        old_info.get('android_branch'), new_info.get('android_branch'))
    return False

  return True


class CrosDiagnoser(object):
  """Integrated bisectors API for ChromeOS regression analysis."""

  def __init__(self, session, path_factory, chromeos_root, chromeos_mirror,
               android_root, android_mirror, chrome_root, chrome_mirror, board,
               noisy, dut):
    self.session = session
    self.path_factory = path_factory
    self.chromeos_root = chromeos_root
    self.chromeos_mirror = chromeos_mirror
    self.android_root = android_root
    self.android_mirror = android_mirror
    self.chrome_root = chrome_root
    self.chrome_mirror = chrome_mirror
    self.board = board
    self.noisy = noisy
    self.dut = dut

    self.cros_old = None
    self.cros_new = None
    self.old_info = None
    self.new_info = None

  def narrow_down_chromeos_prebuilt(self,
                                    old,
                                    new,
                                    eval_cmd,
                                    extra_switch_cmd=None):
    """Bisect with ChromeOS prebuilt.

    Args:
      old: old ChromeOS version
      new: new ChromeOS version
      eval_cmd: command to reproduce the regression
      extra_switch_cmd: additional command to run when switch chromeos version

    Returns:
      True: bisect done successfully
      False: failed
    """
    bisector = wrapper.BisectorWrapper('bisect_cros_version', self.session)
    bisector.init_if_necessary(
        old,
        new,
        init_args=['--dut', self.dut, '--board', self.board],
        switch_cmd=['./switch_cros_prebuilt.py'],
        eval_cmd=eval_cmd,
        noisy=self.noisy)
    if extra_switch_cmd:
      bisector.append_switch_command(extra_switch_cmd)

    try:
      self.cros_old, self.cros_new, self.noisy = bisector.narrow_down()
      logger.info('result %s %s %s', self.cros_old, self.cros_new, self.noisy)
    except errors.VerificationFailed:
      return False
    except Exception:
      logger.exception('exception in bisect_cros_version')
      return False

    self.old_info = cros_util.version_info(self.board, self.cros_old)
    self.new_info = cros_util.version_info(self.board, self.cros_new)
    logger.info('old: cros %s, chrome %s, android %s',
                self.old_info['cros_full_version'], self.old_info['cr_version'],
                self.old_info.get('android_build_id'))
    logger.info('new: cros %s, chrome %s, android %s',
                self.new_info['cros_full_version'], self.new_info['cr_version'],
                self.new_info.get('android_build_id'))
    return True

  def switch_chromeos_to_old(self, force=False):
    """Switch ChromeOS version to old prebuilt.

    Args:
      force: always do 'cros flash' no matter the current version number of DUT
    """
    if not force:
      short_version = cros_util.version_to_short(self.cros_old)
      if cros_util.query_dut_short_version(self.dut) == short_version:
        return
    util.check_call('./switch_cros_prebuilt.py', '--board', self.board,
                    self.dut, self.cros_old)

  def narrow_down_android(self, eval_cmd, extra_switch_cmd=None):
    """Bisect with Android prebuilt and localbuild.

    Args:
      eval_cmd: command to reproduce the regression
      extra_switch_cmd: additional command to run when switch android version

    Returns:
      True: bisect done successfully
      False: failed
      None: unable to bisect this component
    """
    if not need_android_bisect(self.old_info, self.new_info):
      return None

    # TODO(kcwu): what if the branch name does not have 'git_' prefix
    git_branch = self.old_info['android_branch'].replace('git_', '')
    if not self.android_mirror:
      self.android_mirror = self.path_factory.get_android_mirror(git_branch)
      logger.info('android_mirror = %s', self.android_mirror)
    if not self.android_root:
      self.android_root = self.path_factory.get_android_tree(git_branch)
      logger.info('android_root = %s', self.android_root)

    if not os.path.exists(self.android_mirror):
      logger.info('android_mirror does not exist; skip android bisect')
      return None
    if not os.path.exists(self.android_root):
      logger.info('android_root does not exist; skip android bisect')
      return None

    logger.info('bisect android prebuilt')
    session = '%s/%s' % (self.session, self.cros_old)
    bisector = wrapper.BisectorWrapper('bisect_android_build_id', session)
    try:
      bisector.init_if_necessary(
          self.old_info['android_build_id'],
          self.new_info['android_build_id'],
          init_args=[
              '--dut',
              self.dut,
              '--branch',
              self.old_info['android_branch'],
              '--android_root',
              self.android_root,
          ],
          switch_cmd=['./switch_arc_prebuilt.py'],
          eval_cmd=eval_cmd,
          noisy=self.noisy)
      if extra_switch_cmd:
        bisector.append_switch_command(extra_switch_cmd)

      android_old, android_new, self.noisy = bisector.narrow_down()
      logger.info('result %s %s %s', android_old, android_new, self.noisy)
    except errors.VerifyOldFailed:
      logger.fatal('expect old android has old behavior but failed')
      return False
    except errors.VerifyNewFailed:
      logger.info('unable to reproduce, culprit is not in android')
      return None
    except Exception:
      logger.exception('exception in bisect_android_build_id')
      return False

    logger.info('bisect android local build')
    bisector = wrapper.BisectorWrapper('bisect_android_repo', session)
    try:
      bisector.init_if_necessary(
          android_old,
          android_new,
          init_args=[
              '--dut', self.dut, '--android_root', self.android_root,
              '--android_mirror', self.android_mirror
          ],
          switch_cmd=['./switch_arc_localbuild.py'],
          eval_cmd=eval_cmd,
          noisy=self.noisy)
      if extra_switch_cmd:
        bisector.append_switch_command(extra_switch_cmd)

      android_old, android_new, self.noisy = bisector.narrow_down()
      logger.info('result %s %s %s', android_old, android_new, self.noisy)
    except errors.VerificationFailed:
      return False
    except Exception:
      logger.exception('exception in bisect_android_repo')
      return False
    return True

  def narrow_down_chrome(self,
                         eval_cmd,
                         extra_switch_cmd=None,
                         chrome_binaries=None):
    """Bisect with Chrome localbuild.

    Args:
      eval_cmd: command to reproduce the regression
      extra_switch_cmd: additional command to run when switch chrome version
      chrome_binaries: what chrome binaries to build and deploy. If None,
          default is chrome.

    Returns:
      True: bisect done successfully
      False: failed
      None: unable to bisect this component
    """
    if self.old_info['cr_version'] == self.new_info['cr_version']:
      logger.info('chrome version identical')
      return None

    logger.info('bisect chrome local build')
    session = '%s/%s' % (self.session, self.cros_old)
    switch_cmd = ['./switch_cros_cr_localbuild_internal.py', self.dut]
    if chrome_binaries:
      for target in chrome_binaries:
        switch_cmd += ['--target', target]
    bisector = wrapper.BisectorWrapper('bisect_cr_localbuild_internal', session)
    try:
      bisector.init_if_necessary(
          self.old_info['cr_version'],
          self.new_info['cr_version'],
          init_args=[
              '--chrome_root', self.chrome_root, '--chrome_mirror',
              self.chrome_mirror
          ],
          switch_cmd=switch_cmd,
          eval_cmd=eval_cmd,
          noisy=self.noisy)
      if extra_switch_cmd:
        bisector.append_switch_command(extra_switch_cmd)

      chrome_old, chrome_new, self.noisy = bisector.narrow_down()
      logger.info('result %s %s %s', chrome_old, chrome_new, self.noisy)
    except errors.VerifyOldFailed:
      logger.exception('expect old chrome has old behavior but failed')
      return False
    except errors.VerifyNewFailed:
      logger.info('unable to reproduce, culprit is not in chrome')
      return None
    except Exception:
      logger.exception('exception in bisect_cr_localbuild_internal')
      return False
    return True

  def narrow_down_chromeos_localbuild(self, eval_cmd, extra_switch_cmd=None):
    """Bisect with ChromeOS localbuild.

    Args:
      eval_cmd: command to reproduce the regression
      extra_switch_cmd: additional command to run when switch ChromeOS version

    Returns:
      True: bisect done successfully
      False: failed
      None: unable to bisect this component
    """
    bisector = wrapper.BisectorWrapper('bisect_cros_repo', self.session)
    bisector.init_if_necessary(
        self.cros_old,
        self.cros_new,
        init_args=[
            '--dut', self.dut, '--board', self.board, '--chromeos_root',
            self.chromeos_root, '--chromeos_mirror', self.chromeos_mirror
        ],
        switch_cmd=['./switch_cros_localbuild.py'],
        eval_cmd=eval_cmd,
        noisy=self.noisy)
    if extra_switch_cmd:
      bisector.append_switch_command(extra_switch_cmd)

    try:
      bisector.narrow_down()
    except errors.VerificationFailed:
      return False
    except Exception:
      logger.exception('exception in bisect_cros_repo')
      return False
    return True
