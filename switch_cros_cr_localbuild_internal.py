#!/usr/bin/env python3
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

import argparse
import logging
import os
import subprocess
import sys
import tempfile

import bisect_cr_localbuild_internal
from bisect_kit import bisector_cli
from bisect_kit import cache_util
from bisect_kit import cli
from bisect_kit import common
from bisect_kit import cr_util
from bisect_kit import cros_lab_util
from bisect_kit import cros_util
from bisect_kit import errors
from bisect_kit import gclient_util
from bisect_kit import gerrit_util
from bisect_kit import git_util
import switch_cros_localbuild
import switch_helper


logger = logging.getLogger(__name__)

GERRIT_CHROMIUM_PROJECT = "chromium/src"


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
        'rev',
        nargs='?',
        type=git_util.argtype_git_rev,
        metavar='REV',
        default=os.environ.get('REV', ''),
        help='Chrome src git hash.',
    )
    parser.add_argument(
        '--board',
        type=cli.argtype_notempty,
        metavar='BOARD',
        default=os.environ.get('BOARD', ''),
        help='ChromeOS board name',
    )
    parser.add_argument(
        '--board-cpu-arch',
        type=cli.argtype_notempty,
        metavar='BOARD_CPU_ARCH',
        default=os.environ.get('BOARD_CPU_ARCH', ''),
    )
    parser.add_argument(
        '--chrome-root',
        metavar='CHROME_ROOT',
        type=cli.argtype_dir_path,
        default=os.environ.get('CHROME_ROOT', ''),
        help='Root of Chrome source tree, like ~/chromium',
    )
    parser.add_argument(
        '--chrome-mirror',
        metavar='CHROME_MIRROR',
        type=cli.argtype_dir_path,
        default=os.environ.get('CHROME_MIRROR', ''),
        help='gclient cache dir',
    )
    parser.add_argument(
        '--chromium-patch-cl',
        action='append',
        help='A gerrit CL to patch the chromium/src repository',
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

    group = parser.add_argument_group(
        title='Options for building chrome in chromeos sdk chroot'
    )
    group.add_argument(
        '--chromeos-root',
        type=cli.argtype_dir_path,
        metavar='CHROMEOS_ROOT',
        default=os.environ.get('CHROMEOS_ROOT'),
        help='ChromeOS tree root; only necessary if deploy_method is '
        'cros_deploy or image',
    )
    switch_cros_localbuild.add_build_and_deploy_arguments(group)
    return parser


def patch_cls(chrome_src: str, chromium_patch_cls: list[str]):
    base_commit_hash = git_util.get_commit_hash(chrome_src, 'HEAD')

    applied_cls = []
    for cl in chromium_patch_cls:
        cl_info = gerrit_util.get_gerrit_cl_info(cl)
        if not cl_info:
            raise errors.PatchFetchError(
                'Failed to download the chromium CL (%s).' % cl
            )

        if cl_info["project"] != GERRIT_CHROMIUM_PROJECT:
            continue

        revision_info = cl_info["revisions"].get(
            cl_info["current_revision"], {}
        )
        gerrit_repo_url = revision_info["fetch"]["http"]["url"]
        gerrit_revision_ref = revision_info["fetch"]["http"]["ref"]

        git_util.fetch(chrome_src, gerrit_repo_url, gerrit_revision_ref)

        try:
            git_util.cherry_pick(chrome_src, "FETCH_HEAD")
        except subprocess.CalledProcessError as exc:
            error_message = 'Failed to apply the chromium CL (%s)' % cl
            if applied_cls:
                error_message += ' after %s' % '.'.join(applied_cls)
            error_message += ' on %s.' % base_commit_hash

            raise errors.PatchApplyError(error_message) from exc


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
    if opts.dut:
        dut_os_version = cros_util.query_dut_short_version(opts.dut)

    opts.rev = bisect_cr_localbuild_internal.guess_chrome_version(
        opts, opts.rev
    )

    chrome_cache = None
    # If build exists in cache, shortcut it.
    if opts.deploy_method == 'chrome_deploy':
        chrome_cache = cache_util.BuildArtifactsCache(
            cache_util.BuildArtifactsCache.BuildType.CHROME,
            opts.board,
            opts.rev,
            opts.target,
        )
        chrome_cache_hit = chrome_cache.cache_hit()
        if chrome_cache_hit:
            logger.info('Got a chrome cache hit. No need to sync and build.')
            opts.no_sync_code = True
            opts.no_build = True

    # If chrome_src is not a git root, force sync code to have a chrome
    # checkout even if no_sync_code is set.
    if not opts.no_sync_code or not git_util.is_git_root(opts.chrome_src):
        logger.info('switch source code to %s', opts.rev)
        gclient_util.sync(opts.chrome_root, revision=opts.rev)
        bisect_cr_localbuild_internal.workaround_b378019087(opts.chrome_src)

        if opts.chromium_patch_cl:
            logger.info('patching the custom CLs: %s', opts.chromium_patch_cl)
            patch_cls(opts.chrome_src, opts.chromium_patch_cl)

    if not opts.no_build:
        # TODO(kcwu): support local patch.
        if opts.deploy_method == 'chrome_deploy':
            if chrome_cache.cache_hit():
                logger.info('No need to build chrome. Found %s', chrome_cache)
            else:
                targets = opts.target
                if not targets:
                    targets = cr_util.determine_targets_to_build(
                        opts.chrome_src,
                        opts.board,
                        opts.with_tests,
                    )
                out_dir = cr_util.build(
                    opts.chrome_src,
                    opts.board,
                    targets,
                    opts.with_tests,
                )
                if out_dir:
                    with tempfile.TemporaryDirectory() as staging_dir:
                        cr_util.deploy_to_staging_dir(
                            opts.chrome_src,
                            opts.board,
                            targets,
                            out_dir,
                            staging_dir,
                        )
                        chrome_cache.put(staging_dir)
        elif opts.deploy_method == 'image':
            switch_cros_localbuild.build(opts)

    if not opts.no_deploy:
        if opts.deploy_method == 'chrome_deploy':
            if chrome_cache.cache_hit():
                with chrome_cache.get() as out_dir:
                    cr_util.deploy(
                        opts.chrome_src,
                        opts.board,
                        opts.dut,
                        opts.target,
                        opts.with_tests,
                        out_dir=out_dir,
                    )
            else:
                cr_util.deploy(
                    opts.chrome_src,
                    opts.board,
                    opts.dut,
                    opts.target,
                    opts.with_tests,
                )
        elif opts.deploy_method == 'image':
            switch_cros_localbuild.deploy(opts)

    # Sanity check. The OS version should not change.
    if opts.dut:
        cros_util.assert_dut_cros_version(dut_os_version, opts.dut)


def action() -> bisector_cli.SwitchAction:
    return bisector_cli.SwitchAction.BUILD_AND_DEPLOY


def main(args: tuple[str] | None = None) -> int:
    return bisector_cli.switch_main_wrapper(switch_main, args)


if __name__ == '__main__':
    sys.exit(main())
