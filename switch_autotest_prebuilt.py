#!/usr/bin/env python3
# Copyright 2018 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Switcher for ChromeOS autotest prebuilt

It unpacks autotest prebuilt (both client and server packages) into
$CHROMEOS_ROOT/tmp/autotest-prebuilt (that is, autotest_dir).
Later, you can run tests using "eval_cros_autotest.py --prebuilt" or
"test_that --autotest_dir".
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
import tempfile

from bisect_kit import bisector_cli
from bisect_kit import cli
from bisect_kit import common
from bisect_kit import cros_lab_util
from bisect_kit import cros_util
from bisect_kit import errors
from bisect_kit import gs_util
from bisect_kit import util


logger = logging.getLogger(__name__)

AUTOTEST_CLIENT_TARBALL = 'autotest_packages.tar'
AUTOTEST_SERVER_TARBALL = 'autotest_server_package.tar.bz2'
GS_GCP_BUCKET_INTERNAL = 'chromeos-image-archive'
GS_GCP_BUCKET_PUBLIC = 'chromiumos-image-archive'
GS_BUILD_PATH = 'gs://{gcp_bucket}/{board}-release/{full_version}'
GS_AUTOTEST_CLIENT_PATH = GS_BUILD_PATH + '/' + AUTOTEST_CLIENT_TARBALL
GS_AUTOTEST_SERVER_PATH = GS_BUILD_PATH + '/' + AUTOTEST_SERVER_TARBALL

GS_BUILDBUCKET_BUILD_PATH = 'gs://{gcp_bucket}/{path}'
GS_BUILDBUCKET_BUILD_AUTOTEST_CLIENT_PATH = (
    GS_BUILDBUCKET_BUILD_PATH + '/' + AUTOTEST_CLIENT_TARBALL
)
GS_BUILDBUCKET_BUILD_AUTOTEST_SERVER_PATH = (
    GS_BUILDBUCKET_BUILD_PATH + '/' + AUTOTEST_SERVER_TARBALL
)


def create_argument_parser():
    parents = [cli.create_session_optional_parser()]
    parser = cli.ArgumentParser(description=__doc__, parents=parents)
    parser.add_argument(
        '--rich-result',
        action='store_true',
        help='Instead of mere exit code, output detailed information in json',
    )
    parser.add_argument(
        '--chromeos-root',
        type=cli.argtype_dir_path,
        default=os.environ.get('CHROMEOS_ROOT', ''),
        help='ChromeOS tree root',
    )
    parser.add_argument(
        '--test-name',
        help='Client test name, like "video_VideoDecodeAccelerator.h264"',
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
        '--dut',
        type=cli.argtype_notempty,
        metavar='DUT',
        default=os.environ.get('DUT'),
        help='Address of DUT',
    )
    parser.add_argument(
        'version',
        nargs='?',
        type=cli.argtype_notempty,
        metavar='CROS_VERSION',
        default=os.environ.get('CROS_VERSION', ''),
        help='ChromeOS local build version string, in format short version, '
        'full version, or "full,full+N"',
    )

    return parser


def _switch(autotest_dir, board, version, test_name, dut, is_public_build):
    logger.info('Unpack autotest packages for %s %s', board, version)
    if cros_util.is_cros_version(version):
        full_version = cros_util.version_to_full(board, version)
        autotest_client_path = GS_AUTOTEST_CLIENT_PATH.format(
            gcp_bucket=(
                GS_GCP_BUCKET_PUBLIC
                if is_public_build
                else GS_GCP_BUCKET_INTERNAL
            ),
            board=board,
            full_version=full_version,
        )
        autotest_server_path = GS_AUTOTEST_SERVER_PATH.format(
            gcp_bucket=(
                GS_GCP_BUCKET_PUBLIC
                if is_public_build
                else GS_GCP_BUCKET_INTERNAL
            ),
            board=board,
            full_version=full_version,
        )
    else:
        gs_path = cros_util.query_dut_lsb_release(dut).get(
            'CHROMEOS_RELEASE_BUILDER_PATH'
        )
        autotest_client_path = GS_BUILDBUCKET_BUILD_AUTOTEST_CLIENT_PATH.format(
            gcp_bucket=(
                GS_GCP_BUCKET_PUBLIC
                if is_public_build
                else GS_GCP_BUCKET_INTERNAL
            ),
            path=gs_path,
        )
        autotest_server_path = GS_BUILDBUCKET_BUILD_AUTOTEST_SERVER_PATH.format(
            gcp_bucket=(
                GS_GCP_BUCKET_PUBLIC
                if is_public_build
                else GS_GCP_BUCKET_INTERNAL
            ),
            path=gs_path,
        )

    logger.info('autotest_client_path %s', autotest_client_path)
    logger.info('autotest_server_path %s', autotest_server_path)

    # TODO(kcwu): cache downloaded tarballs
    tmp_dir = tempfile.mkdtemp()
    if os.path.exists(autotest_dir):
        shutil.rmtree(autotest_dir)
    os.makedirs(autotest_dir)

    gs_util.cp(autotest_client_path, tmp_dir)
    tarball = os.path.join(tmp_dir, AUTOTEST_CLIENT_TARBALL)
    util.check_call(
        'tar',
        'xvf',
        tarball,
        '--strip-components=1',  # strip 'autotest/'
        'autotest',  # only extract autotest/
        cwd=autotest_dir,
    )
    gs_util.cp(autotest_server_path, tmp_dir)
    tarball = os.path.join(tmp_dir, AUTOTEST_SERVER_TARBALL)
    util.check_call(
        'tar',
        'xf',
        tarball,
        '--strip-components=1',  # strip 'autotest/'
        'autotest',  # only extract autotest/
        cwd=autotest_dir,
    )

    # Need to extract the control file if the target is a client site test.
    if test_name:
        test_name = test_name.split('.')[0]
        client_tarball = os.path.abspath(
            os.path.join(
                autotest_dir, 'packages', 'test-%s.tar.bz2' % test_name
            )
        )
        if os.path.exists(client_tarball):
            client_test_dir = os.path.join(
                autotest_dir, 'client', 'site_tests', test_name
            )
            if not os.path.exists(client_test_dir):
                os.makedirs(client_test_dir)
                util.check_call(
                    'tar', 'xvf', client_tarball, cwd=client_test_dir
                )

    shutil.rmtree(tmp_dir)


def switch_main(args: tuple[str] | None):
    parser = create_argument_parser()
    opts = parser.parse_args(args)
    common.config_logging(opts)

    autotest_dir = os.path.join(
        opts.chromeos_root, cros_util.prebuilt_autotest_dir
    )
    if cros_lab_util.is_satlab_dut(opts.dut):
        cros_lab_util.write_satlab_ssh_config(opts.dut)
    if not cros_util.is_dut(opts.dut):
        raise errors.BrokenDutException(
            '%r is not a valid DUT address' % opts.dut
        )

    if opts.test_name:
        opts.test_name = cros_util.normalize_test_name(opts.test_name)
    _switch(
        autotest_dir,
        opts.board,
        opts.version,
        opts.test_name,
        opts.dut,
        opts.public_build,
    )

    # Verify test control file exists.
    if opts.test_name:
        found = cros_util.get_autotest_test_info(autotest_dir, opts.test_name)
        if not found:
            names = []
            for control_file in cros_util.enumerate_autotest_control_files(
                autotest_dir
            ):
                try:
                    info = cros_util.parse_autotest_control_file(control_file)
                except SyntaxError:
                    logger.warning('%s is not parsable, ignore', control_file)
                    continue
                names.append(info.name)
            util.report_similar_candidates('test_name', opts.test_name, names)
            assert 0  # unreachable


def action() -> bisector_cli.SwitchAction:
    return bisector_cli.SwitchAction.WITH_DUT


def main(args: tuple[str] | None = None) -> int:
    return bisector_cli.switch_main_wrapper(switch_main, args)


if __name__ == '__main__':
    sys.exit(main())
