#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright 2018 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""ChromeOS bisector to bisect local build commits.

Example:
  $ ./bisect_cros_repo.py init --old rev1 --new rev2 \\
      --chromeos_root ~/chromiumos \\
      --chromeos_mirror $CHROMEOS_MIRROR
  $ ./bisect_cros_repo.py config switch ./switch_cros_localbuild.py
  $ ./bisect_cros_repo.py config eval ./eval-manually.sh
  $ ./bisect_cros_repo.py run

When running switcher and evaluator, following environment variables
will be set:
  BOARD (e.g. samus),
  DUT (e.g. samus-dut),
  CROS_VERSION (e.g. 9901.0.0,9902.0.0+3), and
  CHROMEOS_ROOT (e.g. ~/chromiumos).
"""

from __future__ import print_function
import logging

from bisect_kit import bisector_cli
from bisect_kit import cli
from bisect_kit import codechange
from bisect_kit import configure
from bisect_kit import core
from bisect_kit import cros_util
from bisect_kit import errors
from bisect_kit import repo_util

logger = logging.getLogger(__name__)


def generate_action_link(action):
  if action['action_type'] == 'commit':
    repo_url = action['repo_url']
    action['link'] = repo_url + '/+/' + action['rev']


class ChromeOSRepoDomain(core.BisectDomain):
  """BisectDomain for ChromeOS code changes."""
  revtype = staticmethod(cros_util.argtype_cros_version)
  intra_revtype = staticmethod(
      codechange.argtype_intra_rev(cros_util.argtype_cros_version))
  help = globals()['__doc__']

  @staticmethod
  def add_init_arguments(parser):
    parser.add_argument(
        '--dut',
        type=cli.argtype_notempty,
        metavar='DUT',
        default=configure.get('DUT'),
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
    parser.add_argument(
        '--chromeos_mirror',
        type=cli.argtype_dir_path,
        default=configure.get('CHROMEOS_MIRROR', ''),
        help='ChromeOS repo mirror path')

  @staticmethod
  def init(opts):
    if not opts.dut and not opts.board:
      raise errors.ArgumentError('--dut and --board', 'Neither is specified')

    if opts.dut:
      assert cros_util.is_dut(opts.dut)
    else:
      logger.info("Tips: unless you don't need to build, otherwise it's "
                  'recommended to specify --dut in bisector instead of '
                  'switcher and evaluator.')

    if not opts.board:
      opts.board = cros_util.query_dut_board(opts.dut)

    if cros_util.is_cros_short_version(opts.old):
      opts.old = cros_util.version_to_full(opts.board, opts.old)
    if cros_util.is_cros_short_version(opts.new):
      opts.new = cros_util.version_to_full(opts.board, opts.new)

    logger.info('Clean up previous result of "mark as stable"')
    repo_util.abandon(opts.chromeos_root, 'stabilizing_branch')

    config = dict(
        dut=opts.dut,
        board=opts.board,
        chromeos_root=opts.chromeos_root,
        chromeos_mirror=opts.chromeos_mirror)

    spec_manager = cros_util.ChromeOSSpecManager(config)
    cache = repo_util.RepoMirror(opts.chromeos_mirror)
    code_manager = codechange.CodeManager(opts.chromeos_root, spec_manager,
                                          cache)

    # Make sure all repos in between are cached
    _, float_specs, revlist = code_manager.get_specs_and_revlist(
        opts.old, opts.new)
    for spec in reversed(float_specs):
      if cache.are_spec_commits_available(spec):
        continue
      spec_manager.sync_disk_state(spec.name)

    return config, revlist

  def __init__(self, config):
    self.config = config

  def setenv(self, env, rev):
    if self.config['dut']:
      env['DUT'] = self.config['dut']
    if self.config['board']:
      env['BOARD'] = self.config['board']
    env['CHROMEOS_ROOT'] = self.config['chromeos_root']
    env['CHROMEOS_MIRROR'] = self.config['chromeos_mirror']
    env['CROS_VERSION'] = rev

  def fill_candidate_summary(self, summary, interesting_indexes):
    if 'current_range' in summary:
      old, new = summary['current_range']
      old_base, _, _ = codechange.parse_intra_rev(old)
      _, new_next, _ = codechange.parse_intra_rev(new)
      old_short = cros_util.version_to_short(old_base)
      new_short = cros_util.version_to_short(new_next)
      url_template = 'https://crosland.corp.google.com/log/%s..%s'
      summary['links'] = [
          {
              'name': 'change_list',
              'url': url_template % (old_short, new_short),
          },
      ]

    cache = repo_util.RepoMirror(self.config['chromeos_mirror'])
    spec_manager = cros_util.ChromeOSSpecManager(self.config)
    code_manager = codechange.CodeManager(self.config['chromeos_root'],
                                          spec_manager, cache)
    for i in interesting_indexes:
      rev_info = summary['rev_info'][i]
      rev_info.update(code_manager.get_rev_detail(rev_info['rev']))
      for action in rev_info.get('actions', []):
        generate_action_link(action)


if __name__ == '__main__':
  bisector_cli.BisectorCommandLine(ChromeOSRepoDomain).main()
