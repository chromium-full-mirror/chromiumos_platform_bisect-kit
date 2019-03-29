# -*- coding: utf-8 -*-
# Copyright 2018 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Diagnose ChromeOS regressions on top of subcomponent bisectors."""
from __future__ import print_function
import datetime
import json
import logging
import os
import time

from bisect_kit import core
from bisect_kit import cros_util
from bisect_kit import errors
from bisect_kit import util
from bisect_kit import wrapper

logger = logging.getLogger(__name__)


class DiagnoseStates(core.States):
  """Diagnose states."""

  def init(self, config):
    self.set_data(dict(config=config, history=[]))

  @property
  def config(self):
    return self.data['config']

  def add_history(self, event, text, **kwargs):
    entry = dict(timestamp=time.time(), event=event, text=text, **kwargs)
    self.data['history'].append(entry)
    self.save()


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
    else:
      return 'verified'

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
    """
    if self.config['bypass_chromeos_prebuilt']:
      logger.info('bypassed chromeos prebuilt image bisect; '
                  'assume that old chromeos has old behavior and '
                  'new chromeos has new behavior')
      self.cros_old = old
      self.cros_new = new
    else:
      bisector = wrapper.BisectorWrapper('bisect_cros_version',
                                         self.config['session'])
      bisector.init_if_necessary(
          old,
          new,
          init_args=['--dut', self.dut, '--board', self.config['board']],
          switch_cmd=['./switch_cros_prebuilt.py'],
          eval_cmd=eval_cmd,
          noisy=self.noisy)
      if extra_switch_cmd:
        bisector.append_switch_command(extra_switch_cmd)

      self.cros_old, self.cros_new, self.noisy = bisector.narrow_down(
          self.states, 'bisect chromeos prebuilt')

    self.old_info = cros_util.version_info(self.config['board'], self.cros_old)
    self.new_info = cros_util.version_info(self.config['board'], self.cros_new)
    logger.info('old: cros %s, chrome %s, android %s',
                self.old_info['cros_full_version'], self.old_info['cr_version'],
                self.old_info.get('android_build_id'))
    logger.info('new: cros %s, chrome %s, android %s',
                self.new_info['cros_full_version'], self.new_info['cr_version'],
                self.new_info.get('android_build_id'))

  def switch_chromeos_to_old(self, force=False):
    """Switch ChromeOS version to old prebuilt.

    Args:
      force: always do 'cros flash' no matter the current version number of DUT
    """
    if not force:
      short_version = cros_util.version_to_short(self.cros_old)
      if cros_util.query_dut_short_version(self.dut) == short_version:
        return
    util.check_call('./switch_cros_prebuilt.py', '--board',
                    self.config['board'], self.dut, self.cros_old)

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
    if not need_android_bisect(self.old_info, self.new_info):
      return False

    if (self.config['bypass_android_prebuilt'] and
        self.config['bypass_android_build']):
      logger.info('bypassed android bisect')
      return False

    session = '%s/%s' % (self.config['session'], self.cros_old)
    if self.config['bypass_android_prebuilt']:
      logger.info('bypassed android prebuilt bisect')
      android_old = self.old_info['android_build_id']
      android_new = self.new_info['android_build_id']
    else:
      logger.info('bisect android prebuilt')
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
            ],
            switch_cmd=['./switch_arc_prebuilt.py'],
            eval_cmd=eval_cmd,
            noisy=self.noisy)
        if extra_switch_cmd:
          bisector.append_switch_command(extra_switch_cmd)

        android_old, android_new, self.noisy = bisector.narrow_down(
            self.states, 'bisect android prebuilt')
      except errors.VerifyOldBehaviorFailed:
        logger.exception('expect old android has old behavior but failed')
        raise errors.DiagnoseContradiction(
            '%s that old chromeos has old behavior; '
            'but it became new behavior after deployed old android prebuilt' %
            self._verified_chromeos_prebuilt())
      except errors.VerifyNewBehaviorFailed:
        logger.info('unable to reproduce, culprit is not in android')
        return False

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
          'android_mirror does not exist; skip android local build bisect')
      return True
    if not os.path.exists(android_root):
      logger.info(
          'android_root does not exist; skip android local build bisect')
      return True

    logger.info('bisect android local build')
    bisector = wrapper.BisectorWrapper('bisect_android_repo', session)
    try:
      bisector.init_if_necessary(
          android_old,
          android_new,
          init_args=[
              '--dut', self.dut, '--android_root', android_root,
              '--android_mirror', android_mirror
          ],
          switch_cmd=['./switch_arc_localbuild.py'],
          eval_cmd=eval_cmd,
          noisy=self.noisy)
      if extra_switch_cmd:
        bisector.append_switch_command(extra_switch_cmd)

      android_old, android_new, self.noisy = bisector.narrow_down(
          self.states, 'bisect android local build')
    except errors.VerificationFailed as e:
      if not self.config['bypass_android_prebuilt']:
        logger.exception(
            'verified with android prebuilt but failed with local build')
        raise errors.DiagnoseContradiction(
            'verified that the issue could be reproduced with '
            'android prebuilt; but unable to reproduce with local build')
      elif isinstance(e, errors.VerifyOldBehaviorFailed):
        raise errors.DiagnoseContradiction(
            '%s that old chromeos has old behavior; '
            'but it became new behavior after deployed old android prebuilt' %
            self._verified_chromeos_prebuilt())
      else:
        logger.info('unable to reproduce, culprit is not in android')
        return False
    return True

  def narrow_down_chrome(self,
                         eval_cmd,
                         extra_switch_cmd=None,
                         chrome_binaries=None):
    """Bisect with Chrome localbuild.

    It's known that the version of ChromeOS has old behavior.

    Args:
      eval_cmd: command to reproduce the regression
      extra_switch_cmd: additional command to run when switch chrome version
      chrome_binaries: what chrome binaries to build and deploy. If None,
          default is chrome.

    Returns:
      True: culprit is inside Chrome
      False: culprit is not inside Chrome or unable to bisect
    """
    if self.old_info['cr_version'] == self.new_info['cr_version']:
      logger.info('chrome version identical')
      return False

    if self.config['bypass_chrome_build']:
      logger.info('bypassed chrome local build bisect')
      return False

    logger.info('bisect chrome local build')
    session = '%s/%s' % (self.config['session'], self.cros_old)
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
              '--chrome_root', self.config['chrome_root'], '--chrome_mirror',
              self.config['chrome_mirror']
          ],
          switch_cmd=switch_cmd,
          eval_cmd=eval_cmd,
          noisy=self.noisy)
      if extra_switch_cmd:
        bisector.append_switch_command(extra_switch_cmd)

      bisector.narrow_down(self.states, 'bisect chrome local build')
    except errors.VerifyOldBehaviorFailed:
      logger.exception('expect old chrome has old behavior but failed')
      raise errors.DiagnoseContradiction(
          '%s that old chrome os has old behavior; '
          'but it became new behavior after deployed chrome' %
          self._verified_chromeos_prebuilt())
    except errors.VerifyNewBehaviorFailed:
      logger.info('unable to reproduce, culprit is not in chrome')
      return False
    return True

  def narrow_down_chromeos_localbuild(self, eval_cmd, extra_switch_cmd=None):
    """Bisect with ChromeOS localbuild.

    Args:
      eval_cmd: command to reproduce the regression
      extra_switch_cmd: additional command to run when switch ChromeOS version
    """
    if self.config['bypass_chromeos_build']:
      logger.info('bypassed chromeos local build bisect')
      return

    bisector = wrapper.BisectorWrapper('bisect_cros_repo',
                                       self.config['session'])
    bisector.init_if_necessary(
        self.cros_old,
        self.cros_new,
        init_args=[
            '--dut', self.dut, '--board', self.config['board'],
            '--chromeos_root', self.config['chromeos_root'],
            '--chromeos_mirror', self.config['chromeos_mirror']
        ],
        switch_cmd=['./switch_cros_localbuild.py'],
        eval_cmd=eval_cmd,
        noisy=self.noisy)
    if extra_switch_cmd:
      bisector.append_switch_command(extra_switch_cmd)

    try:
      bisector.narrow_down(self.states, 'bisect chromeos local build')
    except errors.VerifyNewBehaviorFailed:
      raise errors.DiagnoseContradiction(
          '%s that the issue could be reproduced with chromeos prebuilt; '
          'but unable to reproduce with local build' %
          self._verified_chromeos_prebuilt())
    except errors.VerifyOldBehaviorFailed:
      raise errors.DiagnoseContradiction(
          '%s that the issue could only be reproduced with new chromeos '
          'prebuilt; but reproduced with both old and new local build' %
          self._verified_chromeos_prebuilt())

  def cmd_log(self, json_output=False):
    result = []
    history = self.states.data['history']
    for i, entry in enumerate(history):
      if json_output:
        entry = entry.copy()
        result.append(entry)
      else:
        entry_time = datetime.datetime.fromtimestamp(int(entry['timestamp']))
        print(entry_time, entry['text'])

      # Special case, combine bisector log nestedly.
      if entry.get('event') == 'bisect':
        bisector = wrapper.BisectorWrapper(entry['bisector'], entry['session'])
        cmd = ['log', '--after', str(entry['timestamp'])]
        if len(history) > i + 1:
          cmd += ['--before', str(history[i + 1]['timestamp'])]

        if json_output:
          cmd.append('--json')
          output = bisector.call(*cmd, capture_output=True)
          entry['log'] = json.loads(output)
        else:
          bisector.call(*cmd)

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
      bisector = wrapper.BisectorWrapper(entry['bisector'], entry['session'])
      cmd = ['view']
      if verbose:
        cmd.append('--verbose')
      if json_output:
        cmd.append('--json')
        output = bisector.call(*cmd, capture_output=True)
        result.append({
            'bisector': entry['bisector'],
            'view': json.loads(output),
        })
      else:
        print('bisector: %s' % entry['bisector'])
        bisector.call(*cmd)
        print('=' * 60)

    if json_output:
      print(json.dumps(result, indent=2))
