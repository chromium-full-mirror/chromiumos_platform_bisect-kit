#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright 2018 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Switcher for chrome on chromeos local internal build bisecting

This script will sync the chrome source tree to the specified version, build,
and deploy.

Typical usage companion with bisect_cr_localbuild_internal:
  $ ./bisect_cr_localbuild_internal.py config switch \
      ./switch_cros_cr_localbuild_internal.py

By default, it will build and deploy chrome. You can specify --target for
alternative binaries.
"""
from __future__ import annotations
from __future__ import print_function

import argparse
import logging
import os
import sys
import typing

import bisect_cr_localbuild_internal
from bisect_kit import bisector_cli
from bisect_kit import cli
from bisect_kit import codechange
from bisect_kit import common
from bisect_kit import configure
from bisect_kit import cr_util
from bisect_kit import cros_lab_util
from bisect_kit import cros_util
from bisect_kit import errors
from bisect_kit import gclient_util
from bisect_kit import lacros_util
from bisect_kit import util
import switch_cros_localbuild


logger = logging.getLogger(__name__)


def create_argument_parser():
    parents = [common.common_argument_parser, common.session_optional_parser]
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
        help='Address of DUT',
    )
    parser.add_argument(
        'rev',
        nargs='?',
        type=cli.argtype_notempty,
        metavar='REV',
        default=configure.get('REV', ''),
        help='ChromeOS version, Chrome version, or Chrome local build intra '
        'version (in format "%s"). For ChromeOS version, the corresponding '
        'Chrome version will be determined by ChromeOS build.'
        % codechange.make_intra_rev('X', 'Y', 3),
    )
    parser.add_argument(
        '--board',
        metavar='BOARD',
        default=configure.get('BOARD'),
        help='ChromeOS board name',
    )
    parser.add_argument(
        '--chrome_root',
        metavar='CHROME_ROOT',
        type=cli.argtype_dir_path,
        default=configure.get('CHROME_ROOT', ''),
        help='Root of Chrome source tree, like ~/chromium',
    )
    parser.add_argument(
        '--chrome_mirror',
        metavar='CHROME_MIRROR',
        type=cli.argtype_dir_path,
        default=configure.get('CHROME_MIRROR', ''),
        help='gclient cache dir',
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
    parser.add_argument(
        '--skip_sync_code',
        action='store_true',
        help='Skip the step syncing source code',
    )
    parser.add_argument(
        '--nobuild',
        action='store_true',
        help='Stop after source code sync; do not build; imply --nodeploy',
    )
    parser.add_argument(
        '--nodeploy', action='store_true', help='Do not deploy after build'
    )

    group = parser.add_argument_group(
        title='Options for building chrome in chromeos sdk chroot'
    )
    group.add_argument(
        '--noimage',
        action='store_true',
        help='Build packages only; do not build image; imply --nodeploy',
    )
    group.add_argument(
        '--chromeos_root',
        type=cli.argtype_dir_path,
        metavar='CHROMEOS_ROOT',
        default=configure.get('CHROMEOS_ROOT'),
        help='ChromeOS tree root; only necessary if deploy_method is '
        'cros_deploy or image',
    )
    switch_cros_localbuild.add_build_and_deploy_arguments(group)
    return parser


def _apply_additional_patch(root_dir, rev):
    # TODO(kcwu): generalize patching mechanism to make it user configurable.
    base_rev, _, _ = codechange.parse_intra_rev(rev)

    # Hack to disable assistant
    # Assistant use custom build rules, which may break build randomly (see
    # b/126633034 and b/127926563).
    # This patch file can support up to 73.0.3669.0, for easier versions, let
    # it go (just rely on retry).
    if cr_util.is_version_lesseq(
        '73.0.3669.0', base_rev
    ) and cr_util.is_version_lesseq(base_rev, '74.0.3699.0'):
        patch_file = os.path.join(
            common.BISECT_KIT_ROOT, 'patching/0001-disable-assistant.patch'
        )
        path = os.path.join(root_dir, 'src')
        util.check_call('git', 'am', patch_file, cwd=path)


def switch_main(args: typing.Optional[tuple[str]]):
    common.init()
    parser = create_argument_parser()
    opts = parser.parse_args(args)
    common.config_logging(opts)

    # --nobuild imply --nodeploy
    if opts.nobuild:
        opts.nodeploy = True

    if opts.skip_sync_code and opts.nobuild:
        raise errors.ArgumentError(
            '--skip_sync_code and --nobuild', 'nothing to do'
        )
    if not opts.dut:
        if not opts.nodeploy:
            raise errors.ArgumentError(
                '--dut', 'DUT can be omitted only if --nodeploy'
            )
        if not opts.board:
            raise errors.ArgumentError(
                '--board', 'board must be specified if no --dut'
            )
    else:
        if cros_lab_util.is_satlab_dut(opts.dut):
            cros_lab_util.write_satlab_ssh_config(opts.dut)
        if not cros_util.is_dut(opts.dut):
            raise errors.BrokenDutException(
                '%r is not a valid DUT address' % opts.dut
            )
    if not opts.board:
        opts.board = cros_util.query_dut_board(opts.dut)
    if opts.deploy_method != 'chrome_deploy':
        if not opts.chromeos_root:
            raise errors.ArgumentError(
                '--chromeos_root',
                'should be specified for --deploy_method=' + opts.deploy_method,
            )

    opts.chrome_src = os.path.join(opts.chrome_root, 'src')
    assert os.path.exists(opts.chrome_src)
    if opts.dut:
        dut_os_version = cros_util.query_dut_short_version(opts.dut)

    opts.rev = bisect_cr_localbuild_internal.guess_chrome_version(
        opts, opts.rev
    )
    config = dict(
        chrome_root=opts.chrome_root, chrome_mirror=opts.chrome_mirror
    )

    cache = gclient_util.GclientCache(opts.chrome_mirror)
    spec_manager = cr_util.ChromeSpecManager(config)
    code_manager = codechange.CodeManager(opts.chrome_root, spec_manager, cache)

    if not opts.skip_sync_code:
        logger.info('switch source code')
        code_manager.switch(opts.rev)
        _apply_additional_patch(opts.chrome_root, opts.rev)

    if opts.nobuild:
        return

    if cr_util.is_chrome_version(opts.rev):
        # Use the default gclient file.
        gclientfile = None
    else:
        # Because the DEPS file referenced in .gclient file is pointing to the DEPS
        # snapshot, runhook may fail due to DEPS file changes in the infra versions
        # (b/129100282). Here we create a temporary gclient file pointing to the
        # live DEPS in the tree. Note this gclient file is used only for runhook,
        # not for code sync.
        gclientfile = '.gclient-for-runhook'
        custom_var_list = [
            'checkout_src_internal=True',
            'cros_boards="arm-generic:amd64-generic"',
            'checkout_lacros_sdk=True',
        ]
        gclient_util.config(
            opts.chrome_root,
            url='https://chromium.googlesource.com/chromium/src.git',
            gclientfile=gclientfile,
            custom_var_list=custom_var_list,
            cache_dir=opts.chrome_mirror,
            target_os='chromeos',
        )
    gclient_util.runhook(opts.chrome_root, gclientfile=gclientfile)

    # TODO(kcwu): support local patch.
    if opts.deploy_method == 'chrome_deploy':
        cr_util.build_and_deploy(
            opts.chrome_src,
            opts.board,
            opts.dut,
            opts.target,
            opts.with_tests,
            nodeploy=opts.nodeploy,
        )
    elif opts.deploy_method == 'image':
        switch_cros_localbuild.build_and_deploy(opts)

    lacros_util.build_and_deploy(opts.chrome_src, opts.dut)

    # Sanity check. The OS version should not change.
    if opts.dut:
        assert dut_os_version == cros_util.query_dut_short_version(
            opts.dut
        ), 'Someone else reflashed the DUT. DUT locking is not respected?'


def main(args: typing.Optional[tuple[str]] = None) -> int:
    return bisector_cli.switch_main_wrapper(switch_main, args)


if __name__ == '__main__':
    sys.exit(main())
