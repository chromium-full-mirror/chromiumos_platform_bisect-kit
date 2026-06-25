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
import switch_cros_localbuild
import switch_helper


logger = logging.getLogger(__name__)


def create_argument_parser():
    parents = [
        cli.create_session_optional_parser(),
        switch_helper.common_local_build_flags(),
    ]
    parser = cli.ArgumentParser(parents=parents)
    parser.add_argument(
        '--rich-result',
        action='store_true',
        help='Instead of mere exit code, output detailed information in json',
    )
    parser.add_argument(
        '--board-cpu-arch',
        type=cli.argtype_notempty,
        metavar='BOARD_CPU_ARCH',
        default=os.environ.get('BOARD_CPU_ARCH', ''),
    )
    parser.add_argument(
        '--chrome-rev',
        type=cli.argtype_notempty,
        metavar='REV',
        help='Version string',
    )
    parser.add_argument(
        '--board',
        type=cli.argtype_notempty,
        required=True,
        help='ChromeOS board name',
    )
    parser.add_argument(
        '--public-build',
        action='store_true',
        help='Use public build artifacts instead of internal ones.',
    )
    parser.add_argument(
        '--gn-extra-args',
        action='store',
        type=str,
        help='Enable DCHECK on building Chrome/Chromium.',
    )
    parser.add_argument(
        '--chrome-root',
        metavar='CHROME_ROOT',
        type=cli.argtype_dir_path,
        required=True,
        help='Root of Chrome source tree, like ~/chromium',
    )

    exclusive_group = parser.add_mutually_exclusive_group()
    exclusive_group.add_argument(
        '--target',
        action='append',
        help='Binary to build and deploy, like unittest or fuzzers. '
        'Example value: "video_decode_accelerator_unittest". '
        'This option could be specified multiple times.',
    )
    exclusive_group.add_argument(
        '--with-tests',
        action=argparse.BooleanOptionalAction,
        default=True,
        help='(if --target is not specified) '
        'Whether to build test binaries besides chrome as well, default is %(default)s',
    )

    parser.add_argument(
        '--deploy-method',
        choices=['chrome_deploy', 'image'],
        default='chrome_deploy',
        help='Deploy method (default: %(default)s)',
    )

    build_group = parser.add_argument_group(
        title='Options for building chrome in chromeos sdk chroot'
    )
    build_group.add_argument(
        '--chromeos-root',
        type=cli.argtype_dir_path,
        metavar='CHROMEOS_ROOT',
        default=os.environ.get('CHROMEOS_ROOT'),
        help='ChromeOS tree root; only necessary if deploy_method is '
        'cros_deploy or image',
    )
    switch_cros_localbuild.add_build_and_deploy_arguments(build_group)
    return parser


def switch_main(args: tuple[str] | None):
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
                '--chromeos-root',
                'should be specified for --deploy-method=' + opts.deploy_method,
            )

    opts.chrome_src = os.path.join(opts.chrome_root, 'src')
    assert os.path.exists(opts.chrome_src)

    assert bisect_cr_localbuild_master.verify_gclient_dep(opts.chrome_root)
    rev = bisect_cr_localbuild_master.guess_git_rev(opts, opts.chrome_rev)

    if not opts.no_sync_code:
        git_util.checkout_version(opts.chrome_src, rev)
        gclient_util.sync(opts.chrome_root)

    if not opts.no_build:
        # TODO(kcwu): support local patch.
        if opts.deploy_method == 'chrome_deploy':
            targets = opts.target
            if not targets:
                targets = cr_util.determine_targets_to_build(
                    opts.chrome_src,
                    opts.board,
                    with_tests=opts.with_tests,
                    is_public_build=opts.public_build,
                )

            cr_util.build(
                opts.chrome_src,
                opts.board,
                targets,
                is_public_build=opts.public_build,
                gn_extra_args=opts.gn_extra_args,
            )
        elif opts.deploy_method == 'image':
            switch_cros_localbuild.build(opts)

    if not opts.no_deploy:
        # TODO(kcwu): support local patch.
        if opts.deploy_method == 'chrome_deploy':
            cr_util.deploy(
                opts.chrome_src,
                opts.board,
                opts.dut,
                opts.target,
                with_tests=opts.with_tests,
            )
        elif opts.deploy_method == 'image':
            switch_cros_localbuild.deploy(opts)


def action() -> bisector_cli.SwitchAction:
    return bisector_cli.SwitchAction.BUILD_AND_DEPLOY


def main(args: tuple[str] | None = None) -> int:
    return bisector_cli.switch_main_wrapper(switch_main, args)


if __name__ == '__main__':
    sys.exit(main())
