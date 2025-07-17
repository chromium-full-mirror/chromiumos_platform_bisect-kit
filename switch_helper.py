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


def common_local_build_flags() -> cli.ArgumentParser:
    """Add common flags for local build switch scripts.

    There are commonly three stages:
        1. sync_code
        2. build
        3. deploy
    Each of the stage can be disabled by a "no_<stage>" flag.
    There are 2^3 = 8 possibilities, though some of them are meaningless.

    Another set of mutual exclusive flags are provided for ease of use:
        1. --sync-code-only: shorthand for --no-build --no-deploy.
        2. --build-only: shorthand for --no-sync-code --no-deploy.
        3. --deploy-only: shorthand for --no-sync-codeand --no-build.

    Returns:
        An cli.ArgumentParser object.
    """
    parser = cli.ArgumentParser(add_help=False)
    parser.add_argument(
        '--dut',
        type=cli.argtype_notempty,
        metavar='DUT',
        default=os.environ.get('DUT'),
        help='DUT address',
    )
    parser.add_argument(
        '--no-sync-code',
        action='store_true',
        default=os.environ.get('NO_SYNC_CODE') == 'true',
        help='Do no sync source code',
    )
    parser.add_argument(
        '--no-build',
        action='store_true',
        default=os.environ.get('NO_BUILD') == 'true',
        help='Do no build',
    )
    parser.add_argument(
        '--no-deploy',
        action='store_true',
        default=os.environ.get('NO_DEPLOY') == 'true',
        help='Do no deploy to DUT',
    )
    shortcut_group = parser.add_mutually_exclusive_group()
    shortcut_group.add_argument(
        '--sync-code-only',
        action='store_true',
        default=os.environ.get('SYNC_CODE_ONLY') == 'true',
        help='sync code only. Shorthand for --no-build and --no-deploy',
    )
    shortcut_group.add_argument(
        '--build-only',
        action='store_true',
        default=os.environ.get('BUILD_ONLY') == 'true',
        help='build only. Shorthand for --no-sync-code and --no-deploy',
    )
    shortcut_group.add_argument(
        '--deploy-only',
        action='store_true',
        default=os.environ.get('DEPLOY_ONLY') == 'true',
        help='deploy only. Shorthand for --no-sync-code and --no-build',
    )
    # Allow no operation without raising an exception. It's for testing.
    shortcut_group.add_argument(
        '--allow-no-ops',
        action='store_true',
        help=argparse.SUPPRESS,
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
                '--sync-code-only and --no-sync-code',
                '--sync-code-only implies --no-sync-code = False',
            )
        opts.no_build = True
        opts.no_deploy = True
    # Cleanup the namespace since this flag is only a shortahnd.
    delattr(opts, 'sync_code_only')

    if opts.build_only:
        if opts.no_build:
            raise errors.ArgumentError(
                '--build-only and --no-build',
                '--build-only implies --no-build = False',
            )
        opts.no_sync_code = True
        opts.no_deploy = True
    # Cleanup the namespace since this flag is only a shortahnd.
    delattr(opts, 'build_only')

    if opts.deploy_only:
        if opts.no_deploy:
            raise errors.ArgumentError(
                '--deploy-only and --no-deploy',
                '--deploy-only implies --no-deploy = False',
            )
        opts.no_sync_code = True
        opts.no_build = True
    # Cleanup the namespace since this flag is only a shortahnd.
    delattr(opts, 'deploy_only')

    if (
        opts.no_sync_code
        and opts.no_build
        and opts.no_deploy
        and not opts.allow_no_ops
    ):
        raise errors.ArgumentError(
            '--no-sync-code, --no-build, --no-deploy',
            'All three of them are True, nothing to do?',
        )

    if not opts.dut:
        if not opts.no_deploy:
            raise errors.ArgumentError(
                '--dut', 'DUT can be omitted only if --no-deploy'
            )
