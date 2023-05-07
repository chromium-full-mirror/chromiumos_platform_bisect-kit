#!/usr/bin/env python3
# Copyright 2018 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Switcher for Android container localbuild for ChromeOS bisecting."""

from __future__ import annotations

import argparse
import logging
import sys
import typing

from bisect_kit import android_util
from bisect_kit import arc_util
from bisect_kit import bisector_cli
from bisect_kit import cli
from bisect_kit import codechange
from bisect_kit import common
from bisect_kit import configure
from bisect_kit import cros_lab_util
from bisect_kit import cros_util
from bisect_kit import errors
from bisect_kit import locking
from bisect_kit import repo_util


logger = logging.getLogger(__name__)


def create_argument_parser():
    parents = [common.create_session_optional_parser()]
    parser = argparse.ArgumentParser(parents=parents)
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
        help='DUT address',
    )
    parser.add_argument(
        'rev',
        nargs='?',
        type=cli.argtype_notempty,
        metavar='INTRA_REV',
        default=configure.get('INTRA_REV', ''),
        help='Android build id, or bisecting intra version number',
    )
    parser.add_argument(
        '--android_root',
        type=cli.argtype_dir_path,
        metavar='ANDROID_ROOT',
        default=configure.get('ANDROID_ROOT', ''),
        help='Android tree root',
    )
    parser.add_argument(
        '--android_mirror',
        type=cli.argtype_dir_path,
        default=configure.get('ANDROID_MIRROR', ''),
        help='Android repo mirror path',
    )
    parser.add_argument(
        '--flavor',
        type=cli.argtype_notempty,
        metavar='ANDROID_FLAVOR',
        default=configure.get('ANDROID_FLAVOR', ''),
        help='example: cheets_x86-user',
    )
    parser.add_argument(
        '--nobuild',
        action='store_true',
        help='Sync source code only; do not build; imply --nodeploy',
    )
    parser.add_argument(
        '--nodeploy', action='store_true', help='Do not deploy after build'
    )
    return parser


def switch(opts):
    config = dict(
        android_root=opts.android_root,
        flavor=opts.flavor,
        android_mirror=opts.android_mirror,
    )
    spec_manager = android_util.AndroidSpecManager(config)
    cache = repo_util.RepoMirror(opts.android_mirror)
    code_manager = codechange.CodeManager(
        opts.android_root, spec_manager, cache
    )

    logger.info('switch source code')
    code_manager.switch(opts.rev)

    if opts.nobuild:
        return

    logger.info('build')
    # TODO(kcwu): use lower job count for non distributed compilation.
    with locking.lock_file(locking.LOCK_FILE_FOR_BUILD):
        android_util.lunch(opts.android_root, opts.flavor, 'm', '-j1000')

    if not opts.nodeploy:
        logger.info('deploy')
        arc_util.push_localbuild_to_device(
            opts.android_root, opts.dut, opts.flavor
        )


def switch_main(args: typing.Optional[tuple[str]]):
    parser = create_argument_parser()
    opts = parser.parse_args(args)
    common.config_logging(opts)

    if not opts.dut:
        if not opts.nodeploy:
            raise errors.ArgumentError(
                '--dut', 'DUT can be omitted only if --nodeploy'
            )
    else:
        if cros_lab_util.is_satlab_dut(opts.dut):
            cros_lab_util.write_satlab_ssh_config(opts.dut)
        if not cros_util.is_dut(opts.dut):
            raise errors.BrokenDutException(
                '%r is not a valid DUT address' % opts.dut
            )

    logger.info('use flavor=%s', opts.flavor)
    switch(opts)


def main(args: typing.Optional[tuple[str]] = None) -> int:
    return bisector_cli.switch_main_wrapper(switch_main, args)


if __name__ == '__main__':
    sys.exit(main())
