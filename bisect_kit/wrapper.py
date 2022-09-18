# -*- coding: utf-8 -*-
# Copyright 2018 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Wrapper of bisector scripts"""
from __future__ import print_function

import importlib
import io
import logging
import subprocess
import sys

from bisect_kit import bisector_cli
from bisect_kit import core

logger = logging.getLogger(__name__)


class BisectorWrapper:
  """Wrapper of bisector scripts as python module."""

  def __init__(self, name, session):
    self.name = name
    self.module = importlib.import_module(name)
    self.session = session
    self.switch_cmds = []

    self.domain_cls = None
    for x in vars(self.module).values():
      if isinstance(x, type) and issubclass(x, core.BisectDomain):
        self.domain_cls = x
        break
    assert self.domain_cls

  def call(self, cmd, *args, **kwargs):
    bisector = bisector_cli.BisectorCommandLine(self.domain_cls)
    full_args = [cmd, '--session', self.session] + list(args)
    logger.debug('call %s: %s', self.name, full_args)
    stdout_bak = sys.stdout
    try:
      if kwargs.get('capture_output'):
        sys.stdout = fake_stdout = io.StringIO()
      bisector.main(*full_args, prog=self.name)
    finally:
      sys.stdout = stdout_bak
    if kwargs.get('capture_output'):
      return fake_stdout.getvalue()
    return None

  def current_status(self):
    bisector = bisector_cli.BisectorCommandLine(self.domain_cls)
    return bisector.current_status(session=self.session)

  def init_if_necessary(self,
                        old,
                        new,
                        init_args,
                        switch_cmd,
                        eval_cmd,
                        old_value=None,
                        new_value=None,
                        term_old=None,
                        term_new=None,
                        recompute_init_values=False,
                        noisy=None,
                        endpoint_verification=False):
    status = self.current_status()
    if not status['inited']:
      common_init_args = ['--old', old, '--new', new]
      if term_old:
        common_init_args += ['--term_old', term_old]
      if term_new:
        common_init_args += ['--term_new', term_new]
      if noisy:
        common_init_args += ['--noisy', noisy]
      if old_value is not None:
        common_init_args += ['--old_value', str(old_value)]
      if new_value is not None:
        common_init_args += ['--new_value', str(new_value)]
      if recompute_init_values:
        common_init_args.append('--recompute_init_values')
      if endpoint_verification:
        common_init_args.append('--endpoint_verification')
      # Note that, if 'init' failed, no session file for this bisector is
      # created. In other words, the error event is only recorded in
      # diagnoser's log.
      self.call('init', *(common_init_args + init_args))

    self.switch_cmds = [switch_cmd]
    self.call('config', 'switch', '--mode=bisectkit', *switch_cmd)
    self.call('config', 'eval', '--mode=bisectkit', *eval_cmd)

  def _set_switch_commands(self):
    cmdline = ' && '.join(
        subprocess.list2cmdline(cmd) for cmd in self.switch_cmds)
    self.call('config', 'switch', '--mode=bisectkit', 'sh', '-c', cmdline)

  def append_switch_command(self, cmd):
    self.switch_cmds.append(cmd)
    self._set_switch_commands()

  def narrow_down(self):
    status = self.current_status()
    logger.info('%s old=%s, new=%s, done=%s', self.name, status['old'],
                status['new'], status['done'])
    try:
      self.call('run')
    except Exception:
      # If bisector failed after bisect range verified, we can continue with
      # knowledge of the partial results. For example,
      #  - ChromeOS prebuilt bisector may cut down half the bisect range and
      #    failed. Although it didn't find the narrowest range, we can still
      #    continue to bisect Android and Chrome using the new range.
      #  - If Android prebuilt bisection failed after verification, although
      #    the range may not shrink, it indicates the culprit is inside Android
      #    and we should continue bisect Android localbuild.
      status = self.current_status()
      if status['verified']:
        logger.exception(
            'got exception; still can continue with partial results')
      else:
        raise
    else:
      status = self.current_status()

    logger.info('%s result old=%s, new=%s, noisy=%s, done=%s', self.name,
                status['old'], status['new'], status['estimated_noise'],
                status['done'])
    self.call('view')
    return status['old'], status['new'], status['estimated_noise']
