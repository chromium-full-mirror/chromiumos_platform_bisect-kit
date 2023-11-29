#!/usr/bin/env python3
# Copyright 2023 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Switcher for ChromeOS Borealis DLC bisecting."""

import logging
import os
import sys

from bisect_kit import bisector_cli
from bisect_kit import cli
from bisect_kit import common
from bisect_kit import cros_lab_util
from bisect_kit import cros_util
from bisect_kit import errors
from bisect_kit import repo_util
from bisect_kit.dlc import borealis_util
from bisect_kit.dlc import dlc_util


logger = logging.getLogger(__name__)


def create_argument_parser() -> cli.ArgumentParser:
    parents = [cli.create_session_optional_parser()]
    parser = cli.ArgumentParser(parents=parents)

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
        '--board',
        metavar='BOARD',
        default=os.environ.get('BOARD', ''),
        help='ChromeOS board name',
    )
    parser.add_argument(
        '--chromeos-root',
        type=cli.argtype_dir_path,
        metavar='CHROMEOS_ROOT',
        default=os.environ.get('CHROMEOS_ROOT', ''),
        help='ChromeOS tree root (default: %(default)s)',
    )
    parser.add_argument(
        'version',
        nargs='?',
        type=borealis_util.argtype_borealis_version,
        metavar='BOREALIS_DLC_VERSION',
        default=os.environ.get('BOREALIS_DLC_VERSION', ''),
        help=f'Borealis DLC version number, e.g {borealis_util.VERSION_EXAMPLES}',
    )

    return parser


def switch(chromeos_root: str, board: str, dut: str, version: str) -> None:
    borealis_util.uninstall(dut)
    repo_util.cleanup_unexpected_files(chromeos_root)
    borealis_util.download_rootfs_and_kernel(chromeos_root, version)

    @cros_util.recreate_chroot_and_retry_if_needed(chromeos_root)
    def _cros_setup_board_with_retry(chromeos_root, board):
        return cros_util.cros_setup_board(chromeos_root, board)

    _cros_setup_board_with_retry(chromeos_root, board)

    dlc_name = borealis_util.DLC_NAME
    with cros_util.cros_workon(chromeos_root, board, dlc_name):
        cros_util.cros_emerge(chromeos_root, board, dlc_name)
        dlc_util.fix_missing_files(chromeos_root, board, dut)
        cros_util.cros_deploy(chromeos_root, dut, dlc_name)
    logger.info('done: switched to %s-%s', dlc_name, version)


def inner_main(chromeos_root: str, board: str, dut: str, version: str) -> None:
    if cros_lab_util.is_satlab_dut(dut):
        cros_lab_util.write_satlab_ssh_config(dut)

    if not cros_util.is_good_dut(dut):
        raise errors.BrokenDutException(f'{dut!r} is not a good DUT')

    if not board:
        board = cros_util.query_dut_board(dut)

    switch(chromeos_root, board, dut, version)


def switch_main(args) -> None:
    parser = create_argument_parser()
    opts = parser.parse_args(args)
    common.config_logging(opts)

    inner_main(opts.chromeos_root, opts.board, opts.dut, opts.version)


def action() -> bisector_cli.SwitchAction:
    return bisector_cli.SwitchAction.WITH_DUT


def main(args: tuple[str] | None = None) -> int:
    return bisector_cli.switch_main_wrapper(switch_main, args)


if __name__ == '__main__':
    sys.exit(main())
