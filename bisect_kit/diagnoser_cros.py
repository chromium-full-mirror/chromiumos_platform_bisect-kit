# -*- coding: utf-8 -*-
# Copyright 2018 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Diagnose ChromeOS regressions on top of subcomponent bisectors."""
from __future__ import print_function
import argparse
import datetime
import json
import logging
import os
import random
import time

from bisect_kit import cli
from bisect_kit import common
from bisect_kit import configure
from bisect_kit import core
from bisect_kit import cros_lab_util
from bisect_kit import cros_util
from bisect_kit import errors
from bisect_kit import util
from bisect_kit import wrapper
import setup_cros_bisect

logger = logging.getLogger(__name__)


def grab_dut(config):
  if config.get('allocated_dut'):
    host_name = cros_lab_util.dut_host_name(config['allocated_dut'])
    logger.info('try to lease the same host (%s) as last run', host_name)
    status = cros_lab_util.skylab_lease_dut(host_name)
    if not status:
      return None
    return host_name

  # TODO(kcwu): support test-specific dependency labels
  dimensions = ['dut_state:ready', 'label-pool:DUT_POOL_QUOTA']
  if config['model']:
    dimensions.append('label-model:' + config['model'])
  if config['sku']:
    dimensions.append('label-hwid_sku:' +
                      cros_lab_util.normalize_sku_name(config['sku']))

  bots = cros_lab_util.swarming_bots_list(dimensions, is_busy=False)
  if not bots:
    bots = cros_lab_util.swarming_bots_list(dimensions)
  if not bots:
    raise errors.NoDutAvailable(
        'no bots satisfy constraints; incorrect model/sku? %s' % dimensions)
  bot = random.choice(bots)
  host = bot['dimensions']['dut_name'][0]
  logger.info('trying to lease %s', host)
  if not cros_lab_util.skylab_lease_dut(host):
    logger.error('all DUTs are busy')
    return None

  logger.info('leased %s (bot_id=%s)', host, bot['bot_id'])
  return host


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
      switch_cmd = ['./switch_cros_prebuilt.py']
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
          noisy=self.noisy)
      if extra_switch_cmd:
        bisector.append_switch_command(extra_switch_cmd)

      self.cros_old, self.cros_new, self.noisy = bisector.narrow_down(
          self.states, 'bisect chromeos prebuilt')

    self.old_info = cros_util.version_info(self.config['board'], self.cros_old)
    self.new_info = cros_util.version_info(self.config['board'], self.cros_new)

    logger.info('old: cros %s, chrome %s, android %s',
                self.old_info.get('cros_full_version'),
                self.old_info.get('cr_version'),
                self.old_info.get('android_build_id'))
    logger.info('new: cros %s, chrome %s, android %s',
                self.new_info.get('cros_full_version'),
                self.new_info.get('cr_version'),
                self.new_info.get('android_build_id'))

  def switch_chromeos_to_old(self, force=False):
    """Switch ChromeOS version to old prebuilt.

    Args:
      force: always do 'cros flash' no matter the current version number of DUT
    """
    util.check_call('./switch_cros_localbuild.py', '--chromeos_root',
                    self.config['chromeos_root'], '--chromeos_mirror',
                    self.config['chromeos_mirror'], '--board',
                    self.config['board'], '--nobuild', self.cros_old)

    if not force:
      short_version = cros_util.version_to_short(self.cros_old)
      if cros_util.query_dut_short_version(self.dut) == short_version:
        return
    cmd = [
        './switch_cros_prebuilt.py', '--board', self.config['board'], '--dut',
        self.dut, self.cros_old
    ]
    if not self.config['disable_rootfs_verification']:
      cmd.append('--no_disable_rootfs_verification')
    util.check_call(*cmd)

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
    if not need_android_bisect(self.old_info, self.new_info):
      return verified

    if (self.config['bypass_android_prebuilt'] and
        self.config['bypass_android_build']):
      logger.info('bypassed android bisect')
      return verified

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
            old_value=self.config['old_value'],
            new_value=self.config['new_value'],
            term_old=self.config['term_old'],
            term_new=self.config['term_new'],
            recompute_init_values=self.config['recompute_init_values'],
            noisy=self.noisy)
        if extra_switch_cmd:
          bisector.append_switch_command(extra_switch_cmd)

        android_old, android_new, self.noisy = bisector.narrow_down(
            self.states, 'bisect android prebuilt')
        verified = True
      except errors.VerifyOldBehaviorFailed:
        logger.exception('expect old android has old behavior but failed')
        raise errors.DiagnoseContradiction(
            '%s that old chromeos has old behavior; '
            'but it became new behavior after deployed old android prebuilt' %
            self._verified_chromeos_prebuilt())
      except errors.VerifyNewBehaviorFailed:
        logger.info('unable to reproduce, culprit is not in android')
        return verified

    if self.config['bypass_android_build']:
      logger.info('bypassed android local build bisect')
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
          'android_mirror does not exist; skip android local build bisect')
      return verified
    if not os.path.exists(android_root):
      logger.info(
          'android_root does not exist; skip android local build bisect')
      return verified

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
          old_value=self.config['old_value'],
          new_value=self.config['new_value'],
          term_old=self.config['term_old'],
          term_new=self.config['term_new'],
          recompute_init_values=self.config['recompute_init_values'],
          noisy=self.noisy)
      if extra_switch_cmd:
        bisector.append_switch_command(extra_switch_cmd)

      android_old, android_new, self.noisy = bisector.narrow_down(
          self.states, 'bisect android local build')
      verified = True
    except errors.VerificationFailed as e:
      if verified:
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
        return verified
    return verified

  def narrow_down_chrome(self, eval_cmd, extra_switch_cmd=None,
                         with_tests=True):
    """Bisect with Chrome localbuild.

    It's known that the version of ChromeOS has old behavior.

    Args:
      eval_cmd: command to reproduce the regression
      extra_switch_cmd: additional command to run when switch chrome version
      with_tests: build test binaries as well

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
    switch_cmd = ['./switch_cros_cr_localbuild_internal.py', '--dut', self.dut]
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
          old_value=self.config['old_value'],
          new_value=self.config['new_value'],
          term_old=self.config['term_old'],
          term_new=self.config['term_new'],
          recompute_init_values=self.config['recompute_init_values'],
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
    switch_cmd = ['./switch_cros_localbuild.py']
    if not self.config['disable_rootfs_verification']:
      switch_cmd.append('--no_disable_rootfs_verification')
    bisector.init_if_necessary(
        self.cros_old,
        self.cros_new,
        init_args=[
            '--dut', self.dut, '--board', self.config['board'],
            '--chromeos_root', self.config['chromeos_root'],
            '--chromeos_mirror', self.config['chromeos_mirror']
        ],
        switch_cmd=switch_cmd,
        eval_cmd=eval_cmd,
        old_value=self.config['old_value'],
        new_value=self.config['new_value'],
        term_old=self.config['term_old'],
        term_new=self.config['term_new'],
        recompute_init_values=self.config['recompute_init_values'],
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


class DiagnoseCommandLineBase(object):
  """Diagnose command line interface."""

  def __init__(self):
    common.init()
    self.argument_parser = self.create_argument_parser()
    self.states = None

  @property
  def config(self):
    return self.states.config

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

    if opts.dut == cros_lab_util.LAB_DUT:
      if not opts.model and not opts.sku:
        self.argument_parser.error(
            'either --model or --sku need to be specified if DUT is "%s"' %
            cros_lab_util.LAB_DUT)
      # Board name cannot be deduced from auto allocated devices because they
      # may be provisioned with image of unexpected board.
      if not opts.board:
        self.argument_parser.error('--board need to be specified if DUT is "%s"'
                                   % cros_lab_util.LAB_DUT)
    else:
      if not opts.board:
        opts.board = cros_util.query_dut_board(opts.dut)

    names = cros_util.list_board_names(opts.chromeos_root)
    if opts.board not in names:
      util.show_similar_candidates('board name', opts.board, names)
      self.argument_parser.error('incorrect board name: %r' % opts.board)

    if cros_util.is_cros_short_version(opts.old):
      opts.old = cros_util.version_to_full(opts.board, opts.old)
    if cros_util.is_cros_short_version(opts.new):
      opts.new = cros_util.version_to_full(opts.board, opts.new)

    if opts.metric:
      if opts.old_value is None:
        self.argument_parser.error('--old_value is not provided')
      if opts.new_value is None:
        self.argument_parser.error('--new_value is not provided')
      if opts.fail_to_pass:
        self.argument_parser.error(
            '--fail_to_pass is not for benchmark test (--metric)')
    else:
      if opts.old_value is not None:
        self.argument_parser.error(
            '--old_value is provided but --metric is not')
      if opts.new_value is not None:
        self.argument_parser.error(
            '--new_value is provided but --metric is not')
      if opts.recompute_init_values:
        self.argument_parser.error(
            '--recompute_init_values is provided but --metric is not')

    if (opts.term_old is None and opts.term_new is not None or
        opts.term_old is not None and opts.term_new is None):
      self.argument_parser.error(
          '--term_old and --term_new must be both specified or both blank')
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
        opts.mirror_base, opts.work_base, opts.session)
    self.check_options(opts, path_factory)

    if (opts.dut != cros_lab_util.LAB_DUT and
        not cros_util.is_good_dut(opts.dut)):
      if not cros_lab_util.repair(opts.dut):
        raise ValueError('DUT is in bad state')

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
        pools=opts.pools,
        model=opts.model,
        sku=opts.sku,
        board=opts.board,
        old=opts.old,
        new=opts.new,
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
        bypass_chrome_build=opts.bypass_chrome_build,
        bypass_android_prebuilt=opts.bypass_android_prebuilt,
        bypass_android_build=opts.bypass_android_build,
        disable_rootfs_verification=opts.disable_rootfs_verification,
        chrome_deploy_image=opts.chrome_deploy_image,
        disable_snapshot=opts.disable_snapshot,
    )

    self.states.init(config)
    self.init_hook(opts)
    self.states.save()

  def cmd_run(self, opts):
    raise NotImplementedError  # implemented by subclass

  def cmd_log(self, opts):
    self.states.load()
    path_factory = setup_cros_bisect.DefaultProjectPathFactory(
        self.config['mirror_base'], self.config['work_base'],
        self.config['session'])
    diagnoser = CrosDiagnoser(self.states, path_factory, self.config, None)

    diagnoser.cmd_log(opts.json)

  def cmd_view(self, opts):
    self.states.load()
    path_factory = setup_cros_bisect.DefaultProjectPathFactory(
        self.config['mirror_base'], self.config['work_base'],
        self.config['session'])
    diagnoser = CrosDiagnoser(self.states, path_factory, self.config, None)
    diagnoser.cmd_view(opts.json, opts.verbose)

  def create_argument_parser_hook(self, parser_init):
    pass  # implemented by subclass if necessary

  def create_argument_parser(self):
    parser = argparse.ArgumentParser()
    common.add_common_arguments(parser)
    parser.add_argument('--session_base', default='bisect.sessions')
    parser.add_argument('--session', help='Session name', required=True)
    subparsers = parser.add_subparsers(
        dest='command', title='commands', metavar='<command>')

    parser_init = subparsers.add_parser('init', help='Initialize')
    group = parser_init.add_argument_group(
        title='Source tree path options',
        description="""
        Specify the paths of chromeos/chrome/android mirror and checkout. They
        have the same default values as setup_cros_bisect.py, so usually you can
        omit them and it just works.
        """)
    group.add_argument(
        '--mirror_base',
        metavar='MIRROR_BASE',
        default=configure.get('MIRROR_BASE',
                              setup_cros_bisect.DEFAULT_MIRROR_BASE),
        help='Directory for mirrors (default: %(default)s)')
    group.add_argument(
        '--work_base',
        metavar='WORK_BASE',
        default=configure.get('WORK_BASE', setup_cros_bisect.DEFAULT_WORK_BASE),
        help='Directory for bisection working directories '
        '(default: %(default)s)')
    group.add_argument(
        '--chromeos_root',
        metavar='CHROMEOS_ROOT',
        type=cli.argtype_dir_path,
        default=configure.get('CHROMEOS_ROOT'),
        help='ChromeOS tree root')
    group.add_argument(
        '--chromeos_mirror',
        type=cli.argtype_dir_path,
        default=configure.get('CHROMEOS_MIRROR'),
        help='ChromeOS repo mirror path')
    group.add_argument(
        '--android_root',
        metavar='ANDROID_ROOT',
        type=cli.argtype_dir_path,
        default=configure.get('ANDROID_ROOT'),
        help='Android tree root')
    group.add_argument(
        '--android_mirror',
        type=cli.argtype_dir_path,
        default=configure.get('ANDROID_MIRROR'),
        help='Android repo mirror path')
    group.add_argument(
        '--chrome_root',
        metavar='CHROME_ROOT',
        type=cli.argtype_dir_path,
        default=configure.get('CHROME_ROOT'),
        help='Chrome tree root')
    group.add_argument(
        '--chrome_mirror',
        metavar='CHROME_MIRROR',
        type=cli.argtype_dir_path,
        default=configure.get('CHROME_MIRROR'),
        help="chrome's gclient cache dir")

    group = parser_init.add_argument_group(title='DUT allocation options')
    group.add_argument(
        '--dut',
        metavar='DUT',
        required=True,
        help='Address of DUT (Device Under Test). If "%s", DUT will be '
        'automatically allocated from the lab' % cros_lab_util.LAB_DUT)
    group.add_argument(
        '--pools',
        type=cli.argtype_notempty,
        default='suites',
        help='Desired pools, comma separated and ordered by preference '
        '(default: %(default)s)')
    group.add_argument(
        '--model',
        metavar='MODEL',
        help='"model" criteria if DUT is auto allocated from the lab')
    group.add_argument(
        '--sku',
        metavar='SKU',
        help='"sku" criteria if DUT is auto allocated from the lab')

    group = parser_init.add_argument_group(title='Essential options')
    group.add_argument(
        '--board',
        metavar='BOARD',
        default=configure.get('BOARD'),
        help='ChromeOS board name; auto detected if DUT is not auto allocated')
    group.add_argument(
        '--old',
        type=cros_util.argtype_cros_version,
        required=True,
        help='ChromeOS version with old behavior')
    group.add_argument(
        '--new',
        type=cros_util.argtype_cros_version,
        required=True,
        help='ChromeOS version with new behavior')
    group.add_argument('--term_old', help='Alternative term for "old" state')
    group.add_argument('--term_new', help='Alternative term for "new" state')

    group = parser_init.add_argument_group(
        title='Options for normal autotest tests')
    group.add_argument('--test_name', help='Test name')
    group.add_argument(
        '--fail_to_pass',
        action='store_true',
        help='For functional tests: bisect the CL fixed the regression (when '
        'test became PASS). If not specified, the default is to bisect the CL '
        'which broke the test (when test became FAIL)')
    group.add_argument('--metric', help='Metric name of benchmark test')
    group.add_argument(
        '--old_value',
        type=float,
        help='For benchmark test, old value of metric')
    group.add_argument(
        '--new_value',
        type=float,
        help='For benchmark test, new value of metric')
    group.add_argument(
        '--recompute_init_values',
        action='store_true',
        help='For performance test, recompute initial values')

    self.create_argument_parser_hook(parser_init)

    group = parser_init.add_argument_group(title='Bisect behavior options')
    group.add_argument(
        '--noisy',
        help='Enable noisy binary search. Example value: "old=1/10,new=2/3"')
    group.add_argument(
        '--always_reflash',
        action='store_true',
        help='Do not trust ChromeOS version number of DUT and always reflash. '
        'This is usually only needed when resume because previous bisect was '
        'interrupted and the DUT may be in an unexpected state')
    group.add_argument(
        '--reboot_before_test',
        action='store_true',
        help='Reboot before test run')
    group.add_argument(
        '--bypass_chromeos_prebuilt',
        action='store_true',
        help='Bypass chromeos prebuilt image bisect')
    group.add_argument(
        '--bypass_chromeos_build',
        action='store_true',
        help='Bypass chromeos local build bisect')
    group.add_argument(
        '--bypass_chrome_build',
        action='store_true',
        help='Bypass chrome local build bisect')
    group.add_argument(
        '--bypass_android_prebuilt',
        action='store_true',
        help='Bypass android prebuilt image bisect')
    group.add_argument(
        '--bypass_android_build',
        action='store_true',
        help='Bypass chromeos local build bisect')
    group.add_argument(
        '--no-disable-rootfs-verification',
        '--no_disable_rootfs_verification',
        dest='disable_rootfs_verification',
        action='store_false',
        help="Don't disable rootfs verification after update is completed")
    group.add_argument(
        '--chrome_deploy_image',
        action='store_true',
        help='Build and deploy ChromeOS image when bisect Chrome')
    group.add_argument(
        '--disable_snapshot',
        action='store_true',
        help='Disable snapshot for bisect chromeos prebuilt')
    parser_init.set_defaults(func=self.cmd_init)

    parser_run = subparsers.add_parser('run', help='Start auto bisection')
    parser_run.set_defaults(func=self.cmd_run)

    parser_log = subparsers.add_parser(
        'log', help='Prints what has been done so far')
    parser_log.add_argument(
        '--json', action='store_true', help='Machine readable output')
    parser_log.set_defaults(func=self.cmd_log)

    parser_view = subparsers.add_parser(
        'view', help='Prints summary of current status')
    parser_view.add_argument('--verbose', '-v', action='store_true')
    parser_view.add_argument(
        '--json', action='store_true', help='Machine readable output')
    parser_view.set_defaults(func=self.cmd_view)

    return parser

  def main(self, args=None):
    opts = self.argument_parser.parse_args(args)
    common.config_logging(opts)
    # It's optional by default since python3.
    if not opts.command:
      self.argument_parser.error('command is missing')

    session_base = configure.get('SESSION_BASE', common.DEFAULT_SESSION_BASE)
    session_file = os.path.join(session_base, opts.session,
                                self.__class__.__name__)
    self.states = DiagnoseStates(session_file)
    opts.func(opts)
