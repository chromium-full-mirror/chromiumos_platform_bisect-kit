#!/usr/bin/env python2
# -*- coding: utf-8 -*-
# Copyright 2018 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Android bisector to bisect a range of android build id.

Example:
  $ ./bisect_android_build_id.py init --old rev1 --new rev2 --dut DUT
  $ ./bisect_android_build_id.py config switch ./switch_arc_prebuilt.py
  $ ./bisect_android_build_id.py config eval ./eval-manually.sh
  $ ./bisect_android_build_id.py run

When running switcher and evaluator, following environment variables
will be set:
  ANDROID_BRANCH (e.g. git_mnc-dr-arc-dev),
  ANDROID_FLAVOR (e.g. cheets_x86-user),
  ANDROID_BUILD_ID (e.g. 9876543),
  ANDROID_ROOT (if available), and
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

logger = logging.getLogger(__name__)


class AndroidBuildIDDomain(core.BisectDomain):
  """BisectDomain for Android build IDs."""
  revtype = staticmethod(android_util.argtype_android_build_id)
  help = globals()['__doc__']

  @staticmethod
  def add_init_arguments(parser):
    parser.add_argument(
        '--branch',
        metavar='ANDROID_BRANCH',
        default=configure.get('ANDROID_BRANCH'),
        help='git branch like "git_mnc-dr-arc-dev"')
    parser.add_argument(
        '--flavor',
        metavar='ANDROID_FLAVOR',
        default=configure.get('ANDROID_FLAVOR'),
        help='example: cheets_x86-user')
    parser.add_argument(
        '--only_good_build',
        action='store_true',
        help='Bisect only good builds. '
        'This flag is only needed if weird builds blocked bisection')

    # Only used for Android on ChromeOS.
    parser.add_argument(
        '--dut',
        type=cli.argtype_notempty,
        metavar='DUT',
        default=configure.get('DUT'),
        help='For ChromeOS, address of DUT (Device Under Test)')
    parser.add_argument(
        '--android_root',
        type=cli.argtype_dir_path,
        default=configure.get('ANDROID_ROOT'),
        help='For ChromeOS, default android tree to search push_to_device.py')

  @staticmethod
  def init(opts):
    if opts.dut:
      assert cros_util.is_dut(opts.dut)

    if not opts.flavor:
      assert opts.dut
      opts.flavor = arc_util.query_flavor(opts.dut)

    if not opts.branch:
      assert opts.dut
      board = cros_util.query_dut_board(opts.dut)
      version = cros_util.query_dut_short_version(opts.dut)
      opts.branch = cros_util.query_android_branch(board, version)
      assert opts.branch

    config = dict(
        branch=opts.branch, flavor=opts.flavor, android_root=opts.android_root)
    if opts.dut:
      config['dut'] = opts.dut

    revlist = android_util.get_build_ids_between(opts.branch, opts.old,
                                                 opts.new)

    if opts.only_good_build:
      revlist = [
          bid for bid in revlist
          if android_util.is_good_build(opts.branch, opts.flavor, bid)
      ]

    return config, revlist

  def __init__(self, config):
    self.config = config

  def setenv(self, env, rev):
    env['ANDROID_BRANCH'] = self.config['branch']
    env['ANDROID_FLAVOR'] = self.config['flavor']
    env['ANDROID_BUILD_ID'] = rev
    if self.config.get('dut'):
      env['DUT'] = self.config['dut']
    if self.config.get('android_root'):
      env['ANDROID_ROOT'] = self.config['android_root']

  def view(self, old, new):
    url_template = (
        'https://android-build.googleplex.com/'
        'builds/{new}/branches/{branch}/targets/{flavor}/cls?end={old}')
    print(url_template.format(
        old=old,
        new=new,
        branch=self.config['branch'],
        flavor=self.config['flavor']))
    print('Minus')
    print(url_template.format(
        old=old,
        new=old,
        branch=self.config['branch'],
        flavor=self.config['flavor']))
    print('because this diff viewer is inclusive: '
          'including changes in %s, which is old' % old)


if __name__ == '__main__':
  cli.BisectorCommandLineInterface(AndroidBuildIDDomain).main()
