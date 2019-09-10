#!/usr/bin/env python2
# -*- coding: utf-8 -*-
# Copyright 2017 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""ChromeOS bisector to bisect a range of chromeos versions.

Example:
  $ ./bisect_cros_version.py init --old rev1 --new rev2 --dut DUT
  $ ./bisect_cros_version.py config switch ./switch_cros_prebuilt.py
  $ ./bisect_cros_version.py config eval ./eval-manually.sh
  $ ./bisect_cros_version.py run

When running switcher and evaluator, following environment variables
will be set:
  BOARD (e.g. samus),
  CROS_FULL_VERSION (e.g. R62-9876.0.0),
  CROS_SHORT_VERSION (e.g. 9876.0.0),
  CROS_VERSION (e.g. R62-9876.0.0).
  DUT (e.g. samus-dut), and
  MILESTONE (e.g. 62).
"""

from __future__ import print_function
import logging

from bisect_kit import bisector_cli
from bisect_kit import cli
from bisect_kit import configure
from bisect_kit import core
from bisect_kit import cros_util
from bisect_kit import errors

logger = logging.getLogger(__name__)


def get_revlist(board, old, new, use_snapshot=False):
  logger.info('get_revlist %s %s %s', board, old, new)
  logger.info('use_snapshot: %s', use_snapshot)
  full_versions = cros_util.list_chromeos_prebuilt_versions(
      board, old, new, use_snapshot=use_snapshot)
  short_versions = map(cros_util.version_to_short, full_versions)

  old_idx = short_versions.index(cros_util.version_to_short(old))
  new_idx = short_versions.index(cros_util.version_to_short(new))
  return full_versions[old_idx], full_versions[new_idx], full_versions


class ChromeOSVersionDomain(core.BisectDomain):
  """BisectDomain for chromeos versions."""
  revtype = staticmethod(cros_util.argtype_cros_version)
  help = globals()['__doc__']

  @staticmethod
  def add_init_arguments(parser):
    parser.add_argument(
        '--dut',
        type=cli.argtype_notempty,
        metavar='DUT',
        default=configure.get('DUT'),
        help='Address of DUT (Device Under Test). Either --dut or '
        '--board need to be specified')
    parser.add_argument(
        '--board',
        metavar='BOARD',
        default=configure.get('BOARD'),
        help='ChromeOS board name. Either --dut or --board need '
        'to be specified')
    parser.add_argument(
        '--experimental_use_snapshot',
        action='store_true',
        help='Use snapshot to bisect chromeos prebuild')

  @staticmethod
  def init(opts):
    if not opts.dut and not opts.board:
      raise errors.ArgumentError('--dut and --board', 'Neither is specified')
    if opts.dut:
      assert cros_util.is_dut(opts.dut)
    if not opts.board:
      opts.board = cros_util.query_dut_board(opts.dut)

    old, new, revlist = get_revlist(
        opts.board,
        opts.old,
        opts.new,
        use_snapshot=opts.experimental_use_snapshot)
    config = dict(dut=opts.dut, board=opts.board, old=old, new=new)
    return config, revlist

  def __init__(self, config):
    self.config = config

  def setenv(self, env, rev):
    if self.config['dut']:
      env['DUT'] = self.config['dut']
    env['BOARD'] = self.config['board']

    assert cros_util.is_cros_full_version(
        rev) or cros_util.is_cros_snapshot_version(rev)
    if cros_util.is_cros_snapshot_version(rev):
      milestone, short_version, _ = cros_util.snapshot_version_split(rev)
    else:
      milestone, short_version = cros_util.version_split(rev)

    env['MILESTONE'] = milestone
    env['CROS_SHORT_VERSION'] = short_version
    env['CROS_FULL_VERSION'] = rev
    env['CROS_VERSION'] = rev

  def fill_candidate_summary(self, summary, interesting_indexes):
    url_template = 'https://crosland.corp.google.com/log/%s..%s'
    if 'current_range' in summary:
      old, new = summary['current_range']
      old_short = cros_util.version_to_short(old)
      new_short = cros_util.version_to_short(new)
      summary['links'] = [
          {
              'name': 'change_list',
              'url': url_template % (old_short, new_short),
          },
      ]

    for i in interesting_indexes:
      if i == 0:
        continue
      rev_info = summary['rev_info'][i]
      old_short = cros_util.version_to_short(summary['rev_info'][i - 1]['rev'])
      new_short = cros_util.version_to_short(rev_info['rev'])
      link = url_template % (old_short, new_short)
      rev_info.update({
          'actions': [dict(link=link)],
      })


if __name__ == '__main__':
  bisector_cli.BisectorCommandLine(ChromeOSVersionDomain).main()
