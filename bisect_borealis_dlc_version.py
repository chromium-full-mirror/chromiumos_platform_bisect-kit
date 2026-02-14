#!/usr/bin/env python3
# Copyright 2023 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Borealis DLC bisector to bisect prebuilt versions.

Example:
  $ ./bisect_borealis_dlc_version.py init --old rev1 --new rev2 --dut DUT \\
    --chromeos-root ~/chromiumos \\
    --chromeos-mirror $CHROMEOS_MIRROR
  $ ./bisect_borealis_dlc_version.py config switch ./switch_cros_borealis_dlc_prebuilt.py
  $ ./bisect_borealis_dlc_version.py config eval ./eval-manually.sh
  $ ./bisect_borealis_dlc_version.py run

When running switcher and evaluator, following environment variables
will be set:
  BOREALIS_DLC_VERSION (e.g. 2023.02.09.031143), and
  DUT (e.g. samus-dut, if available),
  BOARD (e.g. samus, if available) and
  CHROMEOS_ROOT (e.g. ~/chromiumos).
"""

import argparse
import logging
import os

from bisect_kit import bisector_cli
from bisect_kit import cli
from bisect_kit import core
from bisect_kit import cros_lab_util
from bisect_kit import cros_util
from bisect_kit import errors
from bisect_kit.dlc import borealis_util


logger = logging.getLogger(__name__)


def generate_action_link(action):
    if action['action_type'] == 'commit':
        repo_url = action['repo_url']
        action['link'] = repo_url + '/+/' + action['rev']


class BorealisVersionDomain(core.BisectDomain):
    """BorealisVersionDomain for Borealis DLC versions."""

    revtype = staticmethod(
        cli.argtype_multiplexer(
            borealis_util.argtype_borealis_version,
            cros_util.argtype_cros_version,
        )
    )
    help = __doc__

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

        is_both_cros_version = cros_util.is_cros_or_snapshot_version(
            opts.old
        ) and cros_util.is_cros_or_snapshot_version(opts.new)
        is_both_borealis_version = borealis_util.is_borealis_version(
            opts.old
        ) and borealis_util.is_borealis_version(opts.new)

        if is_both_cros_version:
            if cros_util.is_cros_short_version(opts.old):
                opts.old = cros_util.version_to_full(opts.board, opts.old)
            if cros_util.is_cros_short_version(opts.new):
                opts.new = cros_util.version_to_full(opts.board, opts.new)
            if not cros_util.is_ancestor_version(opts.old, opts.new):
                raise errors.ArgumentError(
                    '--old and --new',
                    '%s is not ancestor of %s' % (opts.old, opts.new),
                )
            if not opts.board:
                raise errors.ArgumentError(
                    '--board',
                    'Board must be specified.',
                )
            spec_manager = cros_util.ChromeOSSpecManager(
                {
                    "board": opts.board,
                    "chromeos_root": opts.chromeos_root,
                    "chromeos_mirror": opts.chromeos_mirror,
                    # DLC is only with internal builds.
                    "is_public_build": False,
                }
            )
            fixed_specs = spec_manager.collect_fixed_spec(opts.old, opts.new)
            old_timestamp = fixed_specs[0].timestamp
            new_timestamp = fixed_specs[-1].timestamp
            revlist, details = borealis_util.build_revlist_from_overlay_history(
                opts.chromeos_root, old_timestamp, new_timestamp
            )

        elif is_both_borealis_version:
            revlist, details = borealis_util.build_revlist_from_gs(
                opts.old, opts.new
            )

        else:
            raise errors.ArgumentError(
                '--old and --new',
                f'must be the same type of version ({opts.old}, {opts.new})',
            )

        logger.info('borealis rev list: %s', revlist)
        config = {
            "dut": opts.dut,
            "board": opts.board,
            "chromeos_root": opts.chromeos_root,
            "chromeos_mirror": opts.chromeos_mirror,
        }

        if revlist:
            config['old'] = revlist[0]
            config['new'] = revlist[-1]
        else:
            raise errors.TooFewRevisionsError(f'Too few revisions: {revlist}')

        for detail in details.values():
            for action in detail.get('actions', []):
                generate_action_link(action)

        return config, {'revlist': revlist, 'details': details}

    def __init__(self, config: core.BisectConfig):
        self.config = config

    def setenv(self, env, rev, rev_details=None):
        if self.config.get('dut'):
            env['DUT'] = self.config.get('dut')
        if self.config.get('board'):
            env['BOARD'] = self.config.get('board')
        env['CHROMEOS_ROOT'] = self.config.get('chromeos_root')
        env['CHROMEOS_MIRROR'] = self.config.get('chromeos_mirror')
        env['BOREALIS_DLC_VERSION'] = rev


if __name__ == '__main__':
    bisector_cli.BisectorCommandLine(BorealisVersionDomain).main()
