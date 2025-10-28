#!/usr/bin/env python3
# Copyright 2018 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Switcher for chromeos localbuild bisecting."""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import sys
import tempfile

from bisect_kit import bisector_cli
from bisect_kit import cli
from bisect_kit import codechange
from bisect_kit import common
from bisect_kit import cros_lab_util
from bisect_kit import cros_util
from bisect_kit import errors
from bisect_kit import gs_util
from bisect_kit import repo_util
from bisect_kit import util
import switch_helper


logger = logging.getLogger(__name__)


def add_build_and_deploy_arguments(parser_group):
    parser_group.add_argument(
        '--goma-chromeos-dir',
        type=cli.argtype_dir_path,
        default=os.environ.get('GOMA_CHROMEOS_DIR'),
        help='Goma-chromeos installed directory to mount into the ChromeOS '
        'chroot',
    )
    parser_group.add_argument(
        '--clobber-stateful',
        action='store_true',
        help='Clobber stateful partition when performing update',
    )
    parser_group.add_argument(
        '--disable-rootfs-verification',
        action=argparse.BooleanOptionalAction,
        default=False,
        help='Whether to disable rootfs verification after update is complete, default is %(default)s',
    )
    parser_group.add_argument(
        '--mark-as-stable',
        action=argparse.BooleanOptionalAction,
        default=True,
        help='Whether to mark repo as stable, default is %(default)s',
    )


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
        type=cli.argtype_notempty,
        metavar='CROS_VERSION',
        default=os.environ.get('CROS_VERSION', ''),
        help='ChromeOS local build version string, in format short version, '
        'full version, or "full,full+N"',
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
        '--chromeos-root',
        type=cli.argtype_dir_path,
        metavar='CHROMEOS_ROOT',
        default=os.environ.get('CHROMEOS_ROOT', ''),
        help='ChromeOS tree root (default: %(default)s)',
    )
    parser.add_argument(
        '--chromeos-mirror',
        type=cli.argtype_dir_path,
        default=os.environ.get('CHROMEOS_MIRROR', ''),
        help='ChromeOS repo mirror path',
    )

    group = parser.add_argument_group(title='Build and deploy options')
    add_build_and_deploy_arguments(group)

    return parser


def mark_as_stable(opts):
    overlays = util.check_output(
        'chromite/bin/cros_list_overlays', '--all', cwd=opts.chromeos_root
    ).splitlines()

    # Get overlays listed in manifest file.
    known_projects = []
    for path in repo_util.list_projects(opts.chromeos_root):
        known_projects.append(
            os.path.realpath(os.path.join(opts.chromeos_root, path))
        )
    logger.debug('known_projects %s', known_projects)

    # Skip recent added overlays.
    # cros_mark_as_stable expects all overlays is recorded in manifest file
    # but we haven't synthesized manifest files for each intra versions yet.
    # TODO(kcwu): synthesize manifest file
    for overlay in list(overlays):
        if 'private-overlays/' in overlay and overlay not in known_projects:
            logger.warning(
                'bisect-kit cannot handle recently added overlay %s yet; ignore',
                overlay,
            )
            overlays.remove(overlay)
            continue

    cmd = [
        'chromite/bin/cros_mark_as_stable',
        '-b',
        opts.board,
        '--list_revisions',
        '--overlays',
        ':'.join(overlays),
        '--debug',
        '--all',
        'commit',
    ]
    util.check_call(*cmd, cwd=opts.chromeos_root)


def cleanup(opts):
    logger.info('clean up leftover in the source tree from the last build')
    repo_util.cleanup_unexpected_files(opts.chromeos_root)

    logger.info('clean up previous result of "mark as stable"')
    repo_util.abandon(opts.chromeos_root, 'stabilizing_branch')


def _build_packages(opts):
    logger.info('build packages')

    # chrome_root is only defined if called from chrome's switcher.
    # This means using chromeos build_packages flow to build chrome during chrome
    # bisection.
    chrome_root = getattr(opts, 'chrome_root', None)

    if not opts.goma_chromeos_dir:
        default_goma_chromeos_dir = os.path.expanduser('~/goma')
        if os.path.exists(default_goma_chromeos_dir):
            logger.debug(
                'found %s available, use goma automatically',
                default_goma_chromeos_dir,
            )
            opts.goma_chromeos_dir = default_goma_chromeos_dir

    cros_util.build_packages(
        opts.chromeos_root,
        opts.board,
        chrome_root=chrome_root,
        goma_dir=opts.goma_chromeos_dir,
        afdo_use=True,
        is_public_build=opts.public_build,
    )


def _get_image_folder(opts):
    cached_name = 'bisect-%s' % util.escape_rev(opts.rev)
    image_folder = os.path.join(
        cros_util.cached_images_dir, opts.board, cached_name
    )
    return image_folder


def _get_image_path(opts):
    image_path = os.path.join(
        _get_image_folder(opts), cros_util.test_image_filename
    )

    return image_path


def _do_build(opts, do_mark_as_stable=True):
    # Used many times in this function, shorten it.
    chromeos_root = opts.chromeos_root

    # create and hotfix chroot if necessary
    cros_util.prepare_chroot(chromeos_root)

    if do_mark_as_stable:
        logger.info('mark as stable')
        mark_as_stable(opts)

    image_folder = _get_image_folder(opts)
    image_path = _get_image_path(opts)

    # If the given version is already built, reuse it.
    chromeos_root_image_folder = os.path.join(chromeos_root, image_folder)
    chromeos_root_image_path = os.path.join(chromeos_root, image_path)
    if not os.path.exists(chromeos_root_image_path):
        _build_packages(opts)
        built_image_folder = cros_util.build_image(
            chromeos_root, opts.board, is_public_build=opts.public_build
        )
        os.makedirs(os.path.dirname(chromeos_root_image_folder), exist_ok=True)
        if os.path.exists(chromeos_root_image_folder):
            os.unlink(chromeos_root_image_folder)
        # Create a relative symlink, so the condition actually only comes in here if
        # the image folder is actually missing.
        os.symlink(
            os.path.join('../../..', built_image_folder),
            chromeos_root_image_folder,
        )

    # Upload the artifacts to Google Storage if the board is VM.
    # GCP VM images can be converted only from the GS location.
    if cros_util.is_vm_board(opts.board):
        with tempfile.TemporaryDirectory() as tmpdir:
            DISK_FILENAME = 'disk.raw'
            tmp_disk_file_path = os.path.join(tmpdir, DISK_FILENAME)
            shutil.copy(chromeos_root_image_path, tmp_disk_file_path)

            try:
                tmp_archive_file_path = os.path.join(
                    tmpdir, cros_util.disk_vm_image_bundle_filename
                )
                util.check_call(
                    'tar',
                    '-zcvf',
                    tmp_archive_file_path,
                    DISK_FILENAME,
                    cwd=tmpdir,
                )
            except Exception as e:
                logger.error(
                    'failed to tar %s to %s: %s',
                    chromeos_root_image_path,
                    tmp_archive_file_path,
                    e,
                )

            gs_path = os.path.join(
                'gs://crosperf-chromeos-builds/',
                opts.board,
                util.escape_rev(opts.rev),
                cros_util.disk_vm_image_bundle_filename,
            )
            logger.info('uploading %s to %s', tmp_archive_file_path, gs_path)
            try:
                gs_util.cp(tmp_archive_file_path, gs_path)
            except Exception as e:
                logger.error('failed to upload %s: %s', gs_path, e)


def build(opts):
    logger.info('build')

    @cros_util.recreate_chroot_and_retry_if_needed(opts.chromeos_root)
    def _build_with_retry(*args, **kwargs):
        _do_build(*args, **kwargs)

    _build_with_retry(opts, do_mark_as_stable=opts.mark_as_stable)


def deploy(opts):
    logger.info('deploy')
    image_info = cros_util.ImageInfo()
    image_info[cros_util.ImageType.DISK_IMAGE] = _get_image_path(opts)

    cros_util.provision_image_with_retry(
        opts.chromeos_root,
        opts.dut,
        opts.board,
        image_info,
        is_public_build=opts.public_build,
        clobber_stateful=opts.clobber_stateful,
        disable_rootfs_verification=opts.disable_rootfs_verification,
        repair_callback=cros_lab_util.repair,
        force_reboot_callback=cros_lab_util.reboot_via_servo,
    )


def switch(opts):
    if not opts.no_sync_code or not opts.no_build:
        cleanup(opts)

    if not opts.no_sync_code:
        logger.info('switch source code')
        config = {
            "board": opts.board,
            "chromeos_root": opts.chromeos_root,
            "chromeos_mirror": opts.chromeos_mirror,
            "is_public_build": opts.public_build,
        }
        spec_manager = cros_util.ChromeOSSpecManager(config)
        cache = repo_util.RepoMirror(opts.chromeos_mirror)
        code_manager = codechange.CodeManager(
            opts.chromeos_root,
            spec_manager,
            cache,
            common.get_session_cache_dir(opts.session),
        )
        code_manager.switch(opts.rev)

    if not opts.no_build:
        build(opts)

    if not opts.no_deploy:
        deploy(opts)


def parse_args(args):
    parser = create_argument_parser()
    return parser.parse_args(args)


def inner_main(opts):
    if opts.dut:
        if cros_lab_util.is_satlab_dut(opts.dut):
            cros_lab_util.write_satlab_ssh_config(opts.dut)
        if not cros_util.is_good_dut(opts.dut):
            logger.fatal('%r is not a good DUT', opts.dut)
            if not cros_lab_util.repair(opts.dut, opts.chromeos_root):
                raise errors.BrokenDutException(
                    '%r is not a good DUT' % opts.dut
                )

    if cros_util.is_cros_short_version(opts.rev):
        opts.rev = cros_util.version_to_full(opts.board, opts.rev)

    cros_util.prepare_chroot(opts.chromeos_root)

    try:
        switch(opts)
    finally:
        if not opts.no_deploy:
            # No matter switching succeeded or not, DUT must be in good state.
            # switch() already tried repairing if possible, no repair here.
            if not cros_util.is_good_dut(opts.dut):
                raise errors.BrokenDutException(
                    '%r is not a good DUT' % opts.dut
                )
    logger.info('done')


def switch_main(args: tuple[str] | None):
    opts = parse_args(args)
    switch_helper.post_init_local_build_flags(opts)
    common.config_logging(opts)
    inner_main(opts)


def action() -> bisector_cli.SwitchAction:
    return bisector_cli.SwitchAction.BUILD_AND_DEPLOY


def main(args: tuple[str] | None = None) -> int:
    return bisector_cli.switch_main_wrapper(switch_main, args)


if __name__ == '__main__':
    sys.exit(main())
