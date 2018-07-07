# -*- coding: utf-8 -*-
# Copyright 2018 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Wrapper of bisector scripts"""
from __future__ import print_function
import importlib
import logging
import subprocess

from bisect_kit import cli
from bisect_kit import core

logger = logging.getLogger(__name__)


class BisectorWrapper(object):
  """Wrapper of bisector scripts as python module."""

  def __init__(self, name, session):
    self.name = name
    self.module = importlib.import_module(name)
    self.session = session
    self.args = ['--session', session]
    self.switch_cmds = []

    self.domain_cls = None
    for x in vars(self.module).values():
      if isinstance(x, type) and issubclass(x, core.BisectDomain):
        self.domain_cls = x
        break
    assert self.domain_cls

  def call(self, *args):
    bisector = cli.BisectorCommandLine(self.domain_cls)
    full_args = self.args + list(args)
    logger.debug('call %s: %s', self.name, full_args)
    bisector.main(*full_args, prog=self.name)
    return bisector

  def current_status(self):
    bisector = cli.BisectorCommandLine(self.domain_cls)
    return bisector.current_status(session=self.session)

  def init_if_necessary(self,
                        old,
                        new,
                        init_args,
                        switch_cmd,
                        eval_cmd,
                        noisy=None):
    status = self.current_status()
    if not status['inited']:
      common_init_args = ['--old', old, '--new', new]
      if noisy:
        common_init_args += ['--noisy', noisy]
      self.call('init', *(common_init_args + init_args))

    self.switch_cmds = [switch_cmd]
    self.call('config', 'switch', *switch_cmd)
    self.call('config', 'eval', *eval_cmd)

  def _set_switch_commands(self):
    cmdline = ' && '.join(map(subprocess.list2cmdline, self.switch_cmds))
    self.call('config', 'switch', 'sh', '-c', cmdline)

  def append_switch_command(self, cmd):
    self.switch_cmds.append(cmd)
    self._set_switch_commands()

  def narrow_down(self):
    status = self.current_status()
    logger.info('%s old=%s, new=%s, done=%s', self.name, status['old'],
                status['new'], status['done'])
    self.call('run')
    status = self.current_status()
    logger.info('%s old=%s, new=%s, done=%s', self.name, status['old'],
                status['new'], status['done'])
    self.call('view')

    return status['old'], status['new'], status['estimated_noise']
