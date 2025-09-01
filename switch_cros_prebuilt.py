#!/usr/bin/env python3
# Copyright 2017 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Switcher for ChromeOS prebuilt"""

from __future__ import annotations

import argparse
import logging
import os
import sys

from bisect_kit import bisector_cli
from bisect_kit import cli
from bisect_kit import common
from bisect_kit import core
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
    )
    parser.add_argument(
        'version',
        nargs='?',
        type=cros_util.argtype_cros_version,
        metavar='CROS_VERSION',
        default=os.environ.get('CROS_VERSION', ''),
        help='ChromeOS version number, short (10162.0.0) or full (R64-10162.0.0)',
    )
    parser.add_argument(
        '--board',
        metavar='BOARD',
        default=os.environ.get('BOARD', ''),
        help='ChromeOS board name',
    )
    parser.add_argument(
        '--public-build',
        action='store_true',
        help='Use public build artifacts instead of internal ones.',
    )
    parser.add_argument(
        '--clobber-stateful',
        action='store_true',
        help='Clobber stateful partition when performing update',
    )
    parser.add_argument(
        '--disable-rootfs-verification',
        action=argparse.BooleanOptionalAction,
        default=False,
        help='Whether to disable rootfs verification after update is complete, default is %(default)s',
    )
    parser.add_argument(
        '--default-chromeos-root',
        type=cli.argtype_dir_path,
        default=common.get_default_chromeos_root(),
        help='Default chromeos tree to run "cros flash" (default: %(default)s)',
    )

    return parser


def switch(opts):
    if opts.session:
        states = core.BisectStates.from_bisector_class(
            'ChromeOSVersionDomain', opts.session
        )
        if states.load_states():
            cros_util.SnapshotStore.init_with_state(states)
    # TODO(kcwu): clear cache of cros flash
    image_info = cros_util.search_image(
        opts.board, opts.version, is_public_build=opts.public_build
    )
    if not image_info:
        raise errors.ExecutionFatalError(
            'no images available for %s %s' % (opts.board, opts.version)
        )

    cros_util.provision_image_with_retry(
        opts.default_chromeos_root,
        opts.dut,
        opts.board,
        image_info,
        is_public_build=opts.public_build,
        version=opts.version,
        clobber_stateful=opts.clobber_stateful,
        disable_rootfs_verification=opts.disable_rootfs_verification,
        repair_callback=cros_lab_util.repair,
        force_reboot_callback=cros_lab_util.reboot_via_servo,
    )


def parse_args(args):
    parser = create_argument_parser()
    return parser.parse_args(args)


def inner_main(opts):
    if cros_lab_util.is_satlab_dut(opts.dut):
        cros_lab_util.write_satlab_ssh_config(opts.dut)

    if not cros_util.is_good_dut(opts.dut):
        logger.error('%r is not a good DUT', opts.dut)
        if not cros_lab_util.repair(opts.dut, opts.default_chromeos_root):
            raise errors.BrokenDutException('%r is not a good DUT' % opts.dut)
    if not opts.board:
        opts.board = cros_util.query_dut_board(opts.dut)

    cros_util.prepare_chroot(opts.default_chromeos_root)

    try:
        switch(opts)
    finally:
        # No matter switching succeeded or not, DUT must be in good state.
        # switch() already tried repairing if possible, no repair here.
        if not cros_util.is_good_dut(opts.dut):
            raise errors.BrokenDutException('%r is not a good DUT' % opts.dut)
    logger.info('done')


def switch_main(args: tuple[str] | None):
    opts = parse_args(args)
    common.config_logging(opts)
    inner_main(opts)


def action() -> bisector_cli.SwitchAction:
    return bisector_cli.SwitchAction.WITH_DUT


def main(args: tuple[str] | None = None) -> int:
    return bisector_cli.switch_main_wrapper(switch_main, args)


if __name__ == '__main__':
    sys.exit(main())
