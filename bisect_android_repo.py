#!/usr/bin/env python2
# -*- coding: utf-8 -*-
# Copyright 2018 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Android bisector to bisect local build commits.

Example:
  $ ./bisect_android_repo.py init --old rev1 --new rev2 --android_root ~/android
  $ ./bisect_android_repo.py config switch ./switch_arc_localbuild.py
  $ ./bisect_android_repo.py config eval ./eval-manually.sh
  $ ./bisect_android_repo.py run

When running switcher and evaluator, following environment variables
will be set:
  ANDROID_BRANCH (e.g. git_mnc-dr-arc-dev),
  ANDROID_FLAVOR (e.g. cheets_x86-user),
  ANDROID_ROOT,
  DUT (e.g. samus-dut, if available).
"""

from __future__ import print_function
import logging

from bisect_kit import android_util
from bisect_kit import arc_util
from bisect_kit import cli
from bisect_kit import configure
from bisect_kit import core
from bisect_kit import cros_util
from bisect_kit import repo_util

logger = logging.getLogger(__name__)


def determine_android_build_id(opts, rev):
  """Determine android build id.

  If `rev` is ChromeOS version, query its corresponding Android build id.

  Args:
    opts: parse result of argparse
    rev: Android build id or ChromeOS version

  Returns:
    Android build id
  """
  if cros_util.is_cros_version(rev):
    assert opts.board, 'need to specify BOARD for cros version'
    android_build_id = cros_util.query_android_build_id(opts.board, rev)
    assert android_util.is_android_build_id(android_build_id)
    logger.info('Converted given CrOS version %s to Android build id %s', rev,
                android_build_id)
    rev = android_build_id
  return rev


class AndroidRepoDomain(core.BisectDomain):
  """BisectDomain for Android code changes."""
  # Accepts Android build id or ChromeOS version
  revtype = staticmethod(
      cli.argtype_multiplexer(cros_util.argtype_cros_version,
                              android_util.argtype_android_build_id))
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
        '--android_root',
        metavar='ANDROID_ROOT',
        type=cli.argtype_dir_path,
        required=True,
        default=configure.get('ANDROID_ROOT'),
        help='Android tree root')
    parser.add_argument(
        '--branch',
        metavar='ANDROID_BRANCH',
        help='branch name like "git_mnc-dr-arc-dev"; '
        'default is auto detect via DUT')
    parser.add_argument(
        '--flavor',
        metavar='ANDROID_FLAVOR',
        default=configure.get('ANDROID_FLAVOR'),
        help='example: cheets_x86-user; default is auto detect via DUT')
    parser.add_argument(
        '--board',
        metavar='BOARD',
        default=configure.get('BOARD'),
        help='ChromeOS board name, if ARC++')

  @staticmethod
  def init(opts):
    if opts.dut:
      assert cros_util.is_dut(opts.dut)

    if not opts.flavor:
      assert opts.dut
      opts.flavor = arc_util.query_flavor(opts.dut)

    if not opts.board:
      assert opts.dut
      opts.board = cros_util.query_dut_board(opts.dut)
    if not opts.branch:
      version = cros_util.query_dut_short_version(opts.dut)
      if not cros_util.is_cros_short_localbuild_version(version):
        base = '.'.join(version.split('.')[:-1] + ['0'])
        logger.info('ChromeOS version of DUT (%s) is local build, '
                    'use its base version %s to determine Android branch',
                    version, base)
        version = base
      opts.branch = cros_util.query_android_branch(opts.board, version)
      logger.debug('branch=%s', opts.branch)
      assert opts.branch

    old = determine_android_build_id(opts, opts.old)
    new = determine_android_build_id(opts, opts.new)

    config = dict(
        dut=opts.dut,
        android_root=opts.android_root,
        branch=opts.branch,
        flavor=opts.flavor,
        old=old,
        new=new)

    manifest_manager = android_util.AndroidManifestManager(config)
    dependency_manager = repo_util.DependencyManager(opts.android_root,
                                                     manifest_manager)
    revlist = dependency_manager.get_revlist(old, new)

    return config, revlist

  def __init__(self, config):
    self.config = config

  def setenv(self, env, rev=None):
    env['DUT'] = self.config['dut']
    env['ANDROID_ROOT'] = self.config['android_root']
    env['ANDROID_FLAVOR'] = self.config['flavor']
    env['ANDROID_BRANCH'] = self.config['branch']
    env['INTRA_REV'] = rev

  def view(self, old, new):
    print('old', old)
    print('new', new)

    manifest_manager = android_util.AndroidManifestManager(self.config)
    dependency_manager = repo_util.DependencyManager(
        self.config['android_root'], manifest_manager)
    dependency_manager.view_rev_diff(old, new)


if __name__ == '__main__':
  cli.BisectorCommandLine(AndroidRepoDomain).main()
