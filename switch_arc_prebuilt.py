#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright 2018 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Switcher for ChromeOS ARC container prebuilt.

Typical usage:
  $ bisect_android_build_id.py config switch ./switch_arc_prebuilt.py

It is implemented by calling Android's push_to_device.py.
"""

from __future__ import annotations
from __future__ import print_function

import argparse
import logging
import sys
import typing

from bisect_kit import android_util
from bisect_kit import arc_util
from bisect_kit import bisector_cli
from bisect_kit import cli
from bisect_kit import common
from bisect_kit import configure
from bisect_kit import cros_lab_util
from bisect_kit import cros_util
from bisect_kit import errors


logger = logging.getLogger(__name__)


def create_argument_parser():
    parents = [common.common_argument_parser, common.session_optional_parser]
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
        default=configure.get('DUT', ''),
    )
    parser.add_argument(
        'build_id',
        nargs='?',
        type=android_util.argtype_android_build_id,
        metavar='ANDROID_BUILD_ID',
        default=configure.get('ANDROID_BUILD_ID', ''),
    )
    parser.add_argument(
        '--flavor',
        metavar='ANDROID_FLAVOR',
        default=configure.get('ANDROID_FLAVOR'),
        help='example: cheets_x86-user',
    )

    return parser


def switch_main(args: typing.Optional[tuple[str]]):
    common.init()
    parser = create_argument_parser()
    opts = parser.parse_args(args)
    common.config_logging(opts)

    if cros_lab_util.is_satlab_dut(opts.dut):
        cros_lab_util.write_satlab_ssh_config(opts.dut)

    if not opts.flavor:
        opts.flavor = arc_util.query_flavor(opts.dut)
        assert opts.flavor
    logger.info('use flavor=%s', opts.flavor)

    if not cros_util.is_good_dut(opts.dut):
        logger.fatal('%r is not a good DUT', opts.dut)
        if not cros_lab_util.repair(opts.dut):
            raise errors.BrokenDutException('%r is not a good DUT' % opts.dut)

    arc_util.push_prebuilt_to_device(opts.dut, opts.flavor, opts.build_id)

    arc_version = cros_util.query_dut_lsb_release(opts.dut).get(
        'CHROMEOS_ARC_VERSION'
    )
    if not arc_version or str(opts.build_id) not in arc_version:
        raise errors.ExecutionFatalError(
            'arc version is expected matching "%s" but actual "%s"'
            % (opts.build_id, arc_version)
        )

    logger.info('updated to "%s", done', arc_version)


def main(args: typing.Optional[tuple[str]] = None) -> int:
    return bisector_cli.switch_main_wrapper(switch_main, args)


if __name__ == '__main__':
    sys.exit(main())
