#!/usr/bin/env python3
# Copyright 2018 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Android bisector to bisect a range of android build id.

Example:
  $ ./bisect_android_build_id.py init --old rev1 --new rev2 --branch BRANCH \\
      --flavor FLAVOR --dut DUT
  $ ./bisect_android_build_id.py config switch ./switch_arc_prebuilt.py
  $ ./bisect_android_build_id.py config eval ./eval-manually.sh
  $ ./bisect_android_build_id.py run

When running switcher and evaluator, following environment variables
will be set:
  ANDROID_BRANCH (e.g. git_mnc-dr-arc-dev),
  ANDROID_FLAVOR (e.g. cheets_x86-user),
  ANDROID_BUILD_ID (e.g. 9876543),
  DUT (e.g. samus-dut, if available).
"""

import argparse
import logging
import os

from bisect_kit import android_util
from bisect_kit import bisector_cli
from bisect_kit import cli
from bisect_kit import core


logger = logging.getLogger(__name__)

diff_url_template = (
    'https://android-build.googleplex.com/'
    'builds/{new}/branches/{branch}/targets/{flavor}/cls?end={old}'
)


class AndroidBuildIDDomain(core.BisectDomain):
    """BisectDomain for Android build IDs."""

    revtype = staticmethod(android_util.argtype_android_build_id)
    help = globals()['__doc__']

    @staticmethod
    def add_init_arguments(parser):
        parser.add_argument(
            '--branch',
            type=cli.argtype_notempty,
            metavar='ANDROID_BRANCH',
            default=os.environ.get('ANDROID_BRANCH', ''),
            help='git branch like "git_mnc-dr-arc-dev"',
        )
        parser.add_argument(
            '--flavor',
            type=cli.argtype_notempty,
            metavar='ANDROID_FLAVOR',
            default=os.environ.get('ANDROID_FLAVOR', ''),
            help='example: cheets_x86-user',
        )
        parser.add_argument(
            '--only-good-build',
            action='store_true',
            help='Bisect only good builds. '
            'This flag is only needed if weird builds blocked bisection',
        )

        # Only used for Android on ChromeOS.
        parser.add_argument(
            '--dut',
            type=cli.argtype_notempty,
            metavar='DUT',
            default=os.environ.get('DUT'),
            help='For ChromeOS, address of DUT (Device Under Test)',
        )

    @staticmethod
    def init(opts: argparse.Namespace) -> tuple[core.BisectConfig, dict]:
        config = {"branch": opts.branch, "flavor": opts.flavor}
        if opts.dut:
            config['dut'] = opts.dut

        revlist = android_util.get_build_ids_between(
            opts.branch, opts.flavor, opts.old, opts.new
        )

        if opts.only_good_build:
            revlist = [
                bid
                for bid in revlist
                if android_util.is_good_build(opts.branch, opts.flavor, bid)
            ]

        details = {}
        for i in range(1, len(revlist)):
            url = diff_url_template.format(
                branch=opts.branch,
                flavor=opts.flavor,
                old=revlist[i - 1],
                new=revlist[i],
            )
            details[revlist[i]] = {'actions': [{'link': url}]}

        return config, {'revlist': revlist, 'details': details}

    def __init__(self, config: core.BisectConfig):
        self.config = config

    def setenv(self, env, rev, rev_details=None):
        env['ANDROID_BRANCH'] = self.config.get('branch')
        env['ANDROID_FLAVOR'] = self.config.get('flavor')
        env['ANDROID_BUILD_ID'] = rev

    def fill_candidate_summary(self, summary):
        if 'current_range' in summary:
            old, new = summary['current_range']
            url = diff_url_template.format(
                branch=self.config.get('branch'),
                flavor=self.config.get('flavor'),
                old=old,
                new=new,
            )
            summary['links'] = [{"name": 'change_list', url: url}]


if __name__ == '__main__':
    bisector_cli.BisectorCommandLine(AndroidBuildIDDomain).main()
