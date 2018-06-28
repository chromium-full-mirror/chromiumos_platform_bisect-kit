#!/usr/bin/env python2
# -*- coding: utf-8 -*-
# Copyright 2018 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""ChromeOS bisector to bisect local build commits.

Example:
  $ ./bisect_cros_repo.py init --old rev1 --new rev2 \
      --chromeos_root ~/chromiumos
  $ ./bisect_cros_repo.py config switch ./switch_cros_localbuild.py
  $ ./bisect_cros_repo.py config eval ./eval-manually.sh
  $ ./bisect_cros_repo.py run

When running switcher and evaluator, following environment variables
will be set:
  BOARD (e.g. samus),
  DUT (e.g. samus-dut),
  INTRA_REV (e.g. 9901.0.0,9902.0.0+3), and
  CHROMEOS_ROOT (e.g. ~/chromiumos).
"""

from __future__ import print_function
import logging

from bisect_kit import cli
from bisect_kit import cros_util
from bisect_kit import repo_util
from bisect_kit import configure
from bisect_kit import core

logger = logging.getLogger(__name__)


class ChromeOSRepoDomain(core.BisectDomain):
  """BisectDomain for ChromeOS code changes."""
  revtype = staticmethod(cros_util.argtype_cros_version)
  help = globals()['__doc__']

  @staticmethod
  def add_init_arguments(parser):
    parser.add_argument(
        '--dut',
        type=cli.argtype_notempty,
        metavar='DUT',
        default=configure.get('DUT', ''),
        help='DUT address')
    parser.add_argument(
        '--board',
        metavar='BOARD',
        default=configure.get('BOARD', ''),
        help='ChromeOS board name')
    parser.add_argument(
        '--chromeos_root',
        metavar='CHROMEOS_ROOT',
        type=cli.argtype_dir_path,
        required=True,
        default=configure.get('CHROMEOS_ROOT'),
        help='ChromeOS tree root')

  @staticmethod
  def init(opts):
    if not opts.dut and not opts.board:
      raise core.ExecutionFatalError('Neither --dut nor --board is specified')

    if opts.dut:
      assert cros_util.is_dut(opts.dut)
    else:
      logger.info("Tips: unless you don't need to build, otherwise it's "
                  "recommended to specify --dut in bisector instead of "
                  "switcher and evaluator.")

    if not opts.board:
      opts.board = cros_util.query_dut_board(opts.dut)

    if cros_util.is_cros_short_version(opts.old):
      opts.old = cros_util.version_to_full(opts.board, opts.old)
    if cros_util.is_cros_short_version(opts.new):
      opts.new = cros_util.version_to_full(opts.board, opts.new)

    logger.info('Clean up previous result of "mark as stable"')
    repo_util.abandon(opts.chromeos_root, 'stabilizing_branch')

    config = dict(
        dut=opts.dut, board=opts.board, chromeos_root=opts.chromeos_root)

    manifest_manager = cros_util.ChromeOSManifestManager(config)
    dependency_manager = repo_util.DependencyManager(opts.chromeos_root,
                                                     manifest_manager)
    revlist = dependency_manager.get_revlist(opts.old, opts.new)
    return config, revlist

  def __init__(self, config):
    self.config = config

  def setenv(self, env, rev):
    if self.config['dut']:
      env['DUT'] = self.config['dut']
    if self.config['board']:
      env['BOARD'] = self.config['board']
    env['CHROMEOS_ROOT'] = self.config['chromeos_root']
    env['INTRA_REV'] = rev

  def view(self, old, new):
    print('old', old)
    print('new', new)

    manifest_manager = cros_util.ChromeOSManifestManager(self.config)
    dependency_manager = repo_util.DependencyManager(
        self.config['chromeos_root'], manifest_manager)
    dependency_manager.view_rev_diff(old, new)


if __name__ == '__main__':
  cli.BisectorCommandLine(ChromeOSRepoDomain).main()
