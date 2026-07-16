#!/usr/bin/env python3
# Copyright 2018 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Switcher for ChromeOS ARC container prebuilt.

Typical usage:
  $ bisect_android_build_id.py config switch ./switch_arc_prebuilt.py

It is implemented by calling Android's push_to_device.py.
"""

from __future__ import annotations

import logging
import os
import sys

from bisect_kit import android_util
from bisect_kit import arc_util
from bisect_kit import bisector_cli
from bisect_kit import cli
from bisect_kit import common
from bisect_kit import cros_lab_util
from bisect_kit import cros_util
from bisect_kit import errors


logger = logging.getLogger(__name__)


def create_argument_parser():
    parents = [cli.create_session_optional_parser()]
    parser = cli.ArgumentParser(description=__doc__, parents=parents)
    parser.add_argument(
        '--rich-result',
        action='store_true',
        help='Instead of mere exit code, output detailed information in json',
    )
    parser.add_argument(
        '--dut',
        type=cli.argtype_notempty,
        metavar='DUT',
        default=os.environ.get('DUT', ''),
        help='DUT address',
    )
    parser.add_argument(
        'build_id',
        nargs='?',
        type=android_util.argtype_android_build_id,
        metavar='ANDROID_BUILD_ID',
        default=os.environ.get('ANDROID_BUILD_ID', ''),
    )
    parser.add_argument(
        '--flavor',
        type=cli.argtype_notempty,
        metavar='ANDROID_FLAVOR',
        default=os.environ.get('ANDROID_FLAVOR', ''),
        help='example: cheets_x86-user',
    )
    return parser


def switch_main(args: tuple[str] | None):
    parser = create_argument_parser()
    opts = parser.parse_args(args)
    common.config_logging(opts)

    # opts.dut is guaranteed to be nonempty in the add_argument() definition.
    if cros_lab_util.is_satlab_dut(opts.dut):
        cros_lab_util.write_satlab_ssh_config(opts.dut)
    if not cros_util.is_good_dut(opts.dut):
        logger.fatal('%r is not a good DUT', opts.dut)
        if not cros_lab_util.repair(opts.dut):
            raise errors.BrokenDutException('%r is not a good DUT' % opts.dut)

    logger.info('use flavor=%s', opts.flavor)
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


def action() -> bisector_cli.SwitchAction:
    return bisector_cli.SwitchAction.WITH_DUT


def main(args: tuple[str] | None = None) -> int:
    return bisector_cli.switch_main_wrapper(switch_main, args)


if __name__ == '__main__':
    sys.exit(main())
