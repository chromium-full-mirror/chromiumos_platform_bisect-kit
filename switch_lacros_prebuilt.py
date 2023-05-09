#!/usr/bin/env python3
# Copyright 2022 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Switcher for lacros prebuilt on chromeos

Typical usage:
  $ bisect_lacros_prebuilt.py config switch ./switch_lacros_prebuilt.py

It fetches specified lacros version and deploy to a DUT using deploy_chrome
  script.
"""

from __future__ import annotations

import argparse
import logging
import sys
import typing

from bisect_kit import bisector_cli
from bisect_kit import cli
from bisect_kit import common
from bisect_kit import configure
from bisect_kit import cr_util
from bisect_kit import cros_lab_util
from bisect_kit import cros_util
from bisect_kit import errors
from bisect_kit import lacros_util


logger = logging.getLogger(__name__)


def create_argument_parser():
    parents = [common.create_session_optional_parser()]
    parser = argparse.ArgumentParser(description=__doc__, parents=parents)
    cli.patching_argparser_exit(parser)
    parser.add_argument(
        '--rich_result',
        action='store_true',
        help='Instead of mere exit code, output detailed information in json',
    )
    parser.add_argument(
        '--dut',
        type=cli.argtype_notempty,
        metavar='DUT',
        default=configure.get('DUT'),
    )
    parser.add_argument(
        '--board_cpu_arch',
        type=cli.argtype_notempty,
        metavar='BOARD_CPU_ARCH',
        default=configure.get('BOARD_CPU_ARCH', ''),
    )
    parser.add_argument(
        '--chromeos_root',
        metavar='CHROMEOS_ROOT',
        type=cli.argtype_dir_path,
        default=configure.get('CHROMEOS_ROOT', ''),
        help='Root of ChromeOS source tree, like ~/chromiumOS',
    )
    parser.add_argument(
        'version',
        nargs='?',
        type=cr_util.argtype_chrome_version,
        metavar='LACROS_VERSION',
        default=configure.get('LACROS_VERSION', ''),
        help='Lacros version number, e.g 105.0.5118.0',
    )
    parser.add_argument(
        '--nodeploy', action='store_true', help='Do not deploy after build'
    )

    return parser


def switch_main(args: typing.Optional[tuple[str]]):
    parser = create_argument_parser()
    opts = parser.parse_args(args)
    common.config_logging(opts)

    if not lacros_util.is_prebuilt_version_available(
        opts.version, opts.board_cpu_arch
    ):
        raise errors.ArgumentError(
            'version',
            'Lacros version %s is not available for board_cpu_arch %s'
            % (opts.version, opts.board_cpu_arch),
        )

    if opts.nodeploy:
        return

    if not opts.dut:
        raise errors.ArgumentError(
            '--dut', 'dut can be omitted only if --nodeploy'
        )
    if cros_lab_util.is_satlab_dut(opts.dut):
        cros_lab_util.write_satlab_ssh_config(opts.dut)
    if not cros_util.is_good_dut(opts.dut):
        logger.fatal('%r is not a good DUT', opts.dut)
        if not cros_lab_util.repair(opts.dut):
            raise errors.BrokenDutException('%r is not a good DUT' % opts.dut)

    lacros_util.fetch_and_deploy_lacros_prebuilt_to_dut(
        opts.version, opts.board_cpu_arch, opts.dut, opts.chromeos_root
    )

    # TODO(njrafi): Check if deployed lacros version match with opts.version

    logger.info(
        'Updated lacros version to %s on DUT %s', opts.version, opts.dut
    )


def main(args: typing.Optional[tuple[str]] = None) -> int:
    return bisector_cli.switch_main_wrapper(switch_main, args)


if __name__ == '__main__':
    sys.exit(main())
