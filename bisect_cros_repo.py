#!/usr/bin/env python3
# Copyright 2018 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""ChromeOS bisector to bisect local build commits.

Example:
  $ ./bisect_cros_repo.py init --old rev1 --new rev2 \\
      --chromeos-root ~/chromiumos \\
      --chromeos-mirror $CHROMEOS_MIRROR \\
      --dut DUT
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

import argparse
import logging
import os

from bisect_kit import bisector_cli
from bisect_kit import cli
from bisect_kit import codechange
from bisect_kit import common
from bisect_kit import core
from bisect_kit import cros_lab_util
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
        codechange.argtype_intra_rev(cros_util.argtype_cros_version)
    )
    help = globals()['__doc__']

    @staticmethod
    def add_init_arguments(parser):
        parser.add_argument(
            '--dut',
            type=cli.argtype_notempty,
            metavar='DUT',
            default=os.environ.get('DUT'),
            help='DUT address',
        )
        parser.add_argument(
            '--board',
            metavar='BOARD',
            default=os.environ.get('BOARD', ''),
            help='ChromeOS board name',
        )
        parser.add_argument(
            '--chromeos-root',
            metavar='CHROMEOS_ROOT',
            type=cli.argtype_dir_path,
            required=True,
            default=os.environ.get('CHROMEOS_ROOT'),
            help='ChromeOS tree root',
        )
        parser.add_argument(
            '--chromeos-mirror',
            type=cli.argtype_dir_path,
            default=os.environ.get('CHROMEOS_MIRROR', ''),
            help='ChromeOS repo mirror path',
        )
        parser.add_argument(
            '--public-build',
            action='store_true',
            help='Use public build artifacts instead of internal ones.',
        )

    @staticmethod
    def init(opts: argparse.Namespace) -> tuple[core.BisectConfig, dict]:
        if not opts.dut and not opts.board:
            raise errors.ArgumentError(
                '--dut and --board', 'Neither is specified'
            )

        if opts.dut:
            if cros_lab_util.is_satlab_dut(opts.dut):
                cros_lab_util.write_satlab_ssh_config(opts.dut)
            assert cros_util.is_dut(opts.dut)

        if not opts.board:
            opts.board = cros_util.query_dut_board(opts.dut)

        if not cros_util.is_ancestor_version(opts.old, opts.new):
            raise errors.ArgumentError(
                '--old and --new',
                '%s is not ancestor of %s' % (opts.old, opts.new),
            )
        if cros_util.is_cros_short_version(opts.old):
            opts.old = cros_util.version_to_full(opts.board, opts.old)
        if cros_util.is_cros_short_version(opts.new):
            opts.new = cros_util.version_to_full(opts.board, opts.new)

        logger.info('Clean up previous result of "mark as stable"')
        repo_util.abandon(opts.chromeos_root, 'stabilizing_branch')

        config = {
            "dut": opts.dut,
            "board": opts.board,
            "chromeos_root": opts.chromeos_root,
            "chromeos_mirror": opts.chromeos_mirror,
            "is_public_build": opts.public_build,
        }

        spec_manager = cros_util.ChromeOSSpecManager(config)
        cache = repo_util.RepoMirror(opts.chromeos_mirror)
        code_manager = codechange.CodeManager(
            opts.chromeos_root,
            spec_manager,
            cache,
            common.get_session_cache_dir(opts.session),
        )
        states = core.BisectStates.from_bisector_class(
            'ChromeOSVersionDomain', opts.session
        )
        if states.load_states():
            cros_util.SnapshotStore.init_with_state(states)

        revlist, details = code_manager.build_revlist(opts.old, opts.new)
        for detail in details.values():
            for action in detail.get('actions', []):
                generate_action_link(action)

        return config, {'revlist': revlist, 'details': details}

    def __init__(self, config: core.BisectConfig):
        self.config = config

    def setenv(self, env, rev, rev_details=None):
        if self.config.get('board'):
            env['BOARD'] = self.config.get('board')
        env['CHROMEOS_ROOT'] = self.config.get('chromeos_root')
        env['CHROMEOS_MIRROR'] = self.config.get('chromeos_mirror')
        env['CROS_VERSION'] = rev

    def fill_candidate_summary(self, summary):
        if 'current_range' in summary:
            old, new = summary['current_range']
            old_base, _, _ = codechange.parse_intra_rev(old)
            _, new_next, _ = codechange.parse_intra_rev(new)
            summary['links'] = [
                {
                    'name': 'change_list',
                    'url': cros_util.get_crosland_link(old_base, new_next),
                },
            ]


if __name__ == '__main__':
    bisector_cli.BisectorCommandLine(ChromeOSRepoDomain).main()
