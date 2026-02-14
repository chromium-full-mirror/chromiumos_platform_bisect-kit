#!/usr/bin/env python3
# Copyright 2017 The ChromiumOS Authors
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

import argparse
import logging
import os

from bisect_kit import bisector_cli
from bisect_kit import cli
from bisect_kit import core
from bisect_kit import cros_lab_util
from bisect_kit import cros_util
from bisect_kit import errors


logger = logging.getLogger(__name__)


def _get_revlist(board, old, new, is_public_build, use_snapshot=False):
    logger.info('get_revlist %s %s %s', board, old, new)
    logger.info('is_public_build: %s', is_public_build)
    logger.info('use_snapshot: %s', use_snapshot)
    full_versions, details = cros_util.list_chromeos_prebuilt_versions(
        board,
        old,
        new,
        use_snapshot=use_snapshot,
        is_public_build=is_public_build,
    )
    logger.info('full_versions: %s', full_versions)
    short_versions = [
        (
            v
            if cros_util.is_cros_snapshot_version(v)
            else cros_util.version_to_short(v)
        )
        for v in full_versions
    ]
    logger.info('short_versions: %s', short_versions)

    if cros_util.is_cros_snapshot_version(old):
        old_idx = full_versions.index(old)
    else:
        old_idx = short_versions.index(cros_util.version_to_short(old))
    if cros_util.is_cros_snapshot_version(new):
        new_idx = full_versions.index(new)
    else:
        new_idx = short_versions.index(cros_util.version_to_short(new))
    logger.info('old_idx=%d, new_idx=%d', old_idx, new_idx)
    return (
        full_versions[old_idx],
        full_versions[new_idx],
        full_versions,
        details,
    )


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
            default=os.environ.get('DUT'),
            help='Address of DUT (Device Under Test). Either --dut or '
            '--board need to be specified',
        )
        parser.add_argument(
            '--board',
            metavar='BOARD',
            default=os.environ.get('BOARD'),
            help='ChromeOS board name. Either --dut or --board need '
            'to be specified',
        )
        parser.add_argument(
            '--public-build',
            action='store_true',
            help='Use public build artifacts instead of internal ones.',
        )
        parser.add_argument(
            '--disable-snapshot',
            action='store_true',
            help='Disable snapshot for bisect chromeos prebuilt',
        )

    @staticmethod
    def init(opts: argparse.Namespace) -> tuple[core.BisectConfig, dict]:
        if not opts.dut and not opts.board:
            raise errors.ArgumentError(
                '--dut and --board', 'Neither is specified'
            )
        if not cros_util.is_ancestor_version(opts.old, opts.new):
            raise errors.ArgumentError(
                '--old and --new',
                '%s is not ancestor of %s' % (opts.old, opts.new),
            )
        if opts.dut:
            if cros_lab_util.is_satlab_dut(opts.dut):
                cros_lab_util.write_satlab_ssh_config(opts.dut)
            assert cros_util.is_dut(opts.dut)
        if not opts.board:
            opts.board = cros_util.query_dut_board(opts.dut)
        if not cros_util.has_test_image(
            opts.board, opts.old, is_public_build=opts.public_build
        ):
            raise errors.ArgumentError(
                '--old',
                '%s has no image for %s (is_public_build: %s)'
                % (opts.board, opts.old, opts.public_build),
            )
        if not cros_util.has_test_image(
            opts.board, opts.new, is_public_build=opts.public_build
        ):
            raise errors.ArgumentError(
                '--new',
                '%s has no image for %s (is_public_build: %s)'
                % (opts.board, opts.new, opts.public_build),
            )

        old, new, revlist, details = _get_revlist(
            opts.board,
            opts.old,
            opts.new,
            is_public_build=opts.public_build,
            use_snapshot=not opts.disable_snapshot,
        )
        config = {
            "dut": opts.dut,
            "board": opts.board,
            "old": old,
            "new": new,
            "is_public_build": opts.public_build,
        }

        for i in range(1, len(revlist)):
            link = cros_util.get_crosland_link(revlist[i - 1], revlist[i])
            details[revlist[i]]['actions'] = [{'link': link}]

        return config, {'revlist': revlist, 'details': details}

    def __init__(self, config: core.BisectConfig):
        self.config = config

    def setenv(self, env, rev, rev_details=None):
        env['BOARD'] = self.config.get('board')

        assert cros_util.is_cros_full_version(
            rev
        ) or cros_util.is_cros_snapshot_version(rev)
        if cros_util.is_cros_snapshot_version(rev):
            milestone, short_version, _ = cros_util.snapshot_version_split(rev)
        else:
            milestone, short_version = cros_util.version_split(rev)

        env['MILESTONE'] = milestone
        env['CROS_SHORT_VERSION'] = short_version
        env['CROS_FULL_VERSION'] = rev
        env['CROS_VERSION'] = rev

    def fill_candidate_summary(self, summary):
        if 'current_range' in summary:
            old, new = summary['current_range']
            summary['links'] = [
                {
                    'name': 'change_list',
                    'url': cros_util.get_crosland_link(old, new),
                },
            ]


if __name__ == '__main__':
    bisector_cli.BisectorCommandLine(ChromeOSVersionDomain).main()
