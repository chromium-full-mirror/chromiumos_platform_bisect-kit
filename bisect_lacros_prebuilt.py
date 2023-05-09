#!/usr/bin/env python3
# Copyright 2022 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Lacros bisector to bisect a range of Lacros prebuilt version.

Example:
  $ ./bisect_lacros_prebuilt.py init --old rev1 --new rev2 --dut DUT \\
        --chromeos_root ~/chromiumos
  $ ./bisect_lacros_prebuilt.py config switch ./switch_lacros_prebuilt.py
  $ ./bisect_lacros_prebuilt.py config eval ./eval-manually.sh
  $ ./bisect_lacros_prebuilt.py run

When running switcher and evaluator, following environment variables
will be set:
  LACROS_VERSION (e.g. 105.0.5118.0),
  DUT (e.g. samus-dut, if available),
  BOARD (e.g. samus, if available) and
  CHROMEOS_ROOT (e.g. ~/chromiumos).
"""

import logging

from bisect_kit import bisector_cli
from bisect_kit import cli
from bisect_kit import configure
from bisect_kit import core
from bisect_kit import cr_util
from bisect_kit import errors
from bisect_kit import lacros_util


logger = logging.getLogger(__name__)

diff_url_template = (
    'https://chromium.googlesource.com/chromium/src/+log/{old}..{new}'
    '?pretty=fuller&n=10000'
)


class LacrosVersionDomain(core.BisectDomain):
    """BisectDomain for lacros versions."""

    revtype = staticmethod(cr_util.argtype_chrome_version)
    help = globals()['__doc__']

    @staticmethod
    def add_init_arguments(parser):
        parser.add_argument(
            '--dut',
            type=cli.argtype_notempty,
            metavar='DUT',
            default=configure.get('DUT'),
            help='Address of DUT (Device Under Test)',
        )
        parser.add_argument(
            '--chromeos_root',
            metavar='CHROMEOS_ROOT',
            type=cli.argtype_dir_path,
            default=configure.get('CHROMEOS_ROOT', ''),
            help='Root of chromeOS source tree, like ~/chromiumos',
        )
        parser.add_argument(
            '--board',
            type=cli.argtype_notempty,
            metavar='BOARD',
            default=configure.get('BOARD', ''),
            help='ChromeOS board name',
        )
        parser.add_argument(
            '--board_cpu_arch',
            type=cli.argtype_notempty,
            metavar='BOARD_CPU_ARCH',
            default=configure.get('BOARD_CPU_ARCH', ''),
        )

    @staticmethod
    def init(opts):
        lacros_folder_name = lacros_util.get_lacros_folder_name(
            opts.board_cpu_arch
        )

        if not cr_util.is_direct_relative_version(opts.old, opts.new):
            logger.warning(
                'old=%s is not ancestor of new=%s', opts.old, opts.new
            )
            lowest_common_ancestor = lacros_util.get_lca_lacros_prebuilt(
                opts.old, opts.new, lacros_folder_name
            )
            logger.warning(
                'Assume their lowest common ancestor, %s,'
                'still have expected old behavior as %s',
                lowest_common_ancestor,
                opts.old,
            )
            opts.old = lowest_common_ancestor

        if not lacros_util.is_prebuilt_version_available(
            opts.old, opts.board_cpu_arch
        ):
            raise errors.ArgumentError(
                '--old',
                'Lacros version %s is not available for board_cpu_arch %s'
                % (opts.old, opts.board_cpu_arch),
            )

        if not lacros_util.is_prebuilt_version_available(
            opts.new, opts.board_cpu_arch
        ):
            raise errors.ArgumentError(
                '--new',
                'Lacros version %s is not available for board_cpu_arch %s'
                % (opts.new, opts.board_cpu_arch),
            )

        revlist = lacros_util.get_lacros_prebuilt_versions_between(
            opts.old, opts.new, lacros_folder_name
        )
        config = dict(
            dut=opts.dut,
            board=opts.board,
            old=opts.old,
            new=opts.new,
            chromeos_root=opts.chromeos_root,
            board_cpu_arch=opts.board_cpu_arch,
        )

        details = {}
        for i in range(1, len(revlist)):
            url = diff_url_template.format(old=revlist[i - 1], new=revlist[i])
            details[revlist[i]] = {'actions': [{'link': url}]}

        return config, {'revlist': revlist, 'details': details}

    def __init__(self, config):
        self.config = config

    def setenv(self, env, rev):
        if self.config['dut']:
            env['DUT'] = self.config['dut']
        env['CHROMEOS_ROOT'] = self.config['chromeos_root']
        env['BOARD'] = self.config['board']
        env['BOARD_CPU_ARCH'] = self.config['board_cpu_arch']
        env['LACROS_VERSION'] = rev

    def fill_candidate_summary(self, summary):
        if 'current_range' in summary:
            old, new = summary['current_range']
            url = diff_url_template.format(old=old, new=new)
            summary['links'] = [dict(name='change_list', url=url)]


if __name__ == '__main__':
    bisector_cli.BisectorCommandLine(LacrosVersionDomain).main()
