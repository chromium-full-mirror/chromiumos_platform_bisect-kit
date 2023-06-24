#!/usr/bin/env python3
# Copyright 2017 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Switcher for chrome on chromeos local build bisecting

This script will sync the chrome source tree to the specified version, build,
and deploy.

Typical usage companion with bisect_cr_localbuild_master:
  $ ./bisect_cr_localbuild_master.py config switch \
      ./switch_cros_cr_localbuild_master.py

By default, it will build and deploy chrome. You can specify --target for
alternative binaries.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import typing

import bisect_cr_localbuild_master
from bisect_kit import bisector_cli
from bisect_kit import cli
from bisect_kit import common
from bisect_kit import cr_util
from bisect_kit import cros_lab_util
from bisect_kit import cros_util
from bisect_kit import errors
from bisect_kit import gclient_util
from bisect_kit import git_util
from bisect_kit import lacros_util
import switch_cros_localbuild
import switch_helper


logger = logging.getLogger(__name__)


def create_argument_parser():
    parents = [
        cli.create_session_optional_parser(),
        switch_helper.common_local_build_flags(),
    ]
    parser = argparse.ArgumentParser(parents=parents)
    cli.patching_argparser_exit(parser)
    parser.add_argument(
        '--rich_result',
        action='store_true',
        help='Instead of mere exit code, output detailed information in json',
    )
    parser.add_argument(
        '--board_cpu_arch',
        type=cli.argtype_notempty,
        metavar='BOARD_CPU_ARCH',
        default=os.environ.get('BOARD_CPU_ARCH', ''),
    )
    parser.add_argument(
        'rev',
        nargs='?',
        type=cli.argtype_notempty,
        metavar='REV',
        default=os.environ.get('REV', ''),
        help='Version string',
    )
    parser.add_argument(
        '--board',
        type=cli.argtype_notempty,
        default=os.environ.get('BOARD', ''),
        help='ChromeOS board name',
    )
    parser.add_argument(
        '--chrome_root',
        metavar='CHROME_ROOT',
        type=cli.argtype_dir_path,
        default=os.environ.get('CHROME_ROOT', ''),
        help='Root of Chrome source tree, like ~/chromium',
    )

    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        '--target',
        action='append',
        help='Binary to build and deploy, like unittest or fuzzers. '
        'Example value: "video_decode_accelerator_unittest". '
        'This option could be specified multiple times.',
    )
    group.add_argument(
        '--without_tests',
        action='store_false',
        dest='with_tests',
        help='(if --target is not specified) '
        'Besides chrome, build test binaries as well.',
    )

    parser.add_argument(
        '--deploy_method',
        choices=['chrome_deploy', 'image'],
        default='chrome_deploy',
        help='Deploy method (default: %(default)s)',
    )

    group = parser.add_argument_group(
        title='Options for building chrome in chromeos sdk chroot'
    )
    group.add_argument(
        '--chromeos_root',
        type=cli.argtype_dir_path,
        metavar='CHROMEOS_ROOT',
        default=os.environ.get('CHROMEOS_ROOT'),
        help='ChromeOS tree root; only necessary if deploy_method is '
        'cros_deploy or image',
    )
    switch_cros_localbuild.add_build_and_deploy_arguments(group)
    return parser


def switch_main(args: typing.Optional[tuple[str]]):
    parser = create_argument_parser()
    opts = parser.parse_args(args)
    switch_helper.post_init_local_build_flags(opts)
    common.config_logging(opts)

    if opts.dut:
        if cros_lab_util.is_satlab_dut(opts.dut):
            cros_lab_util.write_satlab_ssh_config(opts.dut)
        if not cros_util.is_dut(opts.dut):
            raise errors.BrokenDutException(
                '%r is not a valid DUT address' % opts.dut
            )
    if opts.deploy_method != 'chrome_deploy':
        if not opts.chromeos_root:
            raise errors.ArgumentError(
                '--chromeos_root',
                'should be specified for --deploy_method=' + opts.deploy_method,
            )

    opts.chrome_src = os.path.join(opts.chrome_root, 'src')
    assert os.path.exists(opts.chrome_src)

    assert bisect_cr_localbuild_master.verify_gclient_dep(opts.chrome_root)
    rev = bisect_cr_localbuild_master.guess_git_rev(opts, opts.rev)

    if not opts.no_sync_code:
        git_util.checkout_version(opts.chrome_src, rev)
        gclient_util.sync(opts.chrome_root)

    if not opts.no_build:
        # TODO(kcwu): support local patch.
        if opts.deploy_method == 'chrome_deploy':
            cr_util.build(
                opts.chrome_src,
                opts.board,
                opts.target,
                opts.with_tests,
            )
        elif opts.deploy_method == 'image':
            switch_cros_localbuild.build(opts)
        lacros_util.build(opts.chrome_src, opts.board_cpu_arch)

    if not opts.no_deploy:
        # TODO(kcwu): support local patch.
        if opts.deploy_method == 'chrome_deploy':
            cr_util.deploy(
                opts.chrome_src,
                opts.board,
                opts.dut,
                opts.target,
                opts.with_tests,
            )
        elif opts.deploy_method == 'image':
            switch_cros_localbuild.deploy(opts)
        lacros_util.deploy(opts.chrome_src, opts.dut)


def main(args: typing.Optional[tuple[str]] = None) -> int:
    return bisector_cli.switch_main_wrapper(switch_main, args)


if __name__ == '__main__':
    sys.exit(main())
