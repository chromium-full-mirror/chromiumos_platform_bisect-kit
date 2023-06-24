#!/usr/bin/env python3
# Copyright 2023 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

import argparse
import logging
import os

from bisect_kit import cli
from bisect_kit import errors


logger = logging.getLogger(__name__)


def common_local_build_flags() -> argparse.ArgumentParser:
    """Add common flags for local build switch scripts.

    There are commonly three stages:
        1. sync_code
        2. build
        3. deploy
    Each of the stage can be disabled by a "no_<stage>" flag.
    There are 2^3 = 8 possibilities, though some of them are meaningless.

    Another set of mutual exclusive flags are provided for ease of use:
        1. --sync_code_only: shorthand for --no_build --no_deploy.
        2. --build_only: shorthand for --no_sync_code --no_deploy.
        3. --deploy_only: shorthand for --no_sync_codeand --no_build.

    Returns:
        An argparse.ArgumentParser object.
    """
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument(
        '--dut',
        type=cli.argtype_notempty,
        metavar='DUT',
        default=os.environ.get('DUT'),
        help='DUT address',
    )
    parser.add_argument(
        '--no_sync_code', action='store_true', help='Do no sync source code'
    )
    parser.add_argument('--no_build', action='store_true', help='Do no build')
    parser.add_argument(
        '--no_deploy', action='store_true', help='Do no deploy to DUT'
    )
    shortcut_group = parser.add_mutually_exclusive_group()
    shortcut_group.add_argument(
        '--sync_code_only',
        action='store_true',
        help='sync code only. Shorthand for --no_build and --no_deploy',
    )
    shortcut_group.add_argument(
        '--build_only',
        action='store_true',
        help='build only. Shorthand for --no_sync_code and --no_deploy',
    )
    shortcut_group.add_argument(
        '--deploy_only',
        action='store_true',
        help='deploy only. Shorthand for --no_sync_code and --no_build',
    )
    return parser


def post_init_local_build_flags(opts: argparse.Namespace):
    """Post init local build flags covered by common_local_build_flags().

    Checks whether the flags has conflicts and sets flags by implications.
    After this method is called, refer to
    opts.no_sync_code, opts.no_build, and opts.no_deploy instead of
    opts.sync_code_only, opts.build_only, and opts.deploy_only in code.

    Args:
        opts: parsed flags.

    Raises:
        errors.ArgumentError if the flags are invalud.
    """
    if opts.sync_code_only:
        if opts.no_sync_code:
            raise errors.ArgumentError(
                '--sync_code_only and --no_sync_code',
                '--sync_code_only implies --no_sync_code = False',
            )
        opts.no_build = True
        opts.no_deploy = True
    # Cleanup the namespace since this flag is only a shortahnd.
    delattr(opts, 'sync_code_only')

    if opts.build_only:
        if opts.no_build:
            raise errors.ArgumentError(
                '--build_only and --no_build',
                '--build_only implies --no_build = False',
            )
        opts.no_sync_code = True
        opts.no_deploy = True
    # Cleanup the namespace since this flag is only a shortahnd.
    delattr(opts, 'build_only')

    if opts.deploy_only:
        if opts.no_deploy:
            raise errors.ArgumentError(
                '--deploy_only and --no_deploy',
                '--deploy_only implies --no_deploy = False',
            )
        opts.no_sync_code = True
        opts.no_build = True
    # Cleanup the namespace since this flag is only a shortahnd.
    delattr(opts, 'deploy_only')

    if opts.no_sync_code and opts.no_build and opts.no_deploy:
        raise errors.ArgumentError(
            '--no_sync_code, --no_build, --no_deploy',
            'All three of them are True, nothing to do?',
        )

    if not opts.dut:
        if not opts.no_deploy:
            raise errors.ArgumentError(
                '--dut', 'DUT can be omitted only if --no_deploy'
            )
