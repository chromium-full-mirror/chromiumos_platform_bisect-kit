#!/usr/bin/env python3
# Copyright 2020 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Switcher for ChromeOS tast prebuilt

It unpacks tast server side prebuilt into $CHROMEOS_ROOT/tmp/tast-prebuilt
Later, you can run tests using "eval_cros_tast.py --prebuilt" or
"$CHROMEOS_ROOT/tmp/tast-prebuilt/run_tast.sh".
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
from bisect_kit import gs_util
from bisect_kit import util


logger = logging.getLogger(__name__)

AUTOTEST_SERVER_TARBALL = 'autotest_server_package.tar.bz2'
GS_GCP_BUCKET_INTERNAL = 'chromeos-image-archive'
GS_GCP_BUCKET_PUBLIC = 'chromiumos-image-archive'
GS_BUILDBUCKET_BUILD_PATH = 'gs://{gcp_bucket}/{path}'


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
        '--board',
        metavar='BOARD',
        default=os.environ.get('BOARD', ''),
        help='ChromeOS board name',
    )
    parser.add_argument(
        '--dut',
        type=cli.argtype_notempty,
        metavar='DUT',
        default=os.environ.get('DUT', ''),
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
    parser.add_argument(
        '--public-build',
        action='store_true',
        help='Use public build artifacts instead of internal ones.',
    )

    return parser


def switch(opts, board, version, dut):
    logger.info('Unpack autotest packages for %s %s', board, version)
    if cros_util.is_cros_version(version):
        gs_build_path = cros_util.query_prebuilt_gs_path(
            board, version, is_public_build=opts.public_build
        )
    else:
        gs_path = cros_util.query_dut_lsb_release(dut).get(
            'CHROMEOS_RELEASE_BUILDER_PATH'
        )
        gs_build_path = GS_BUILDBUCKET_BUILD_PATH.format(
            gcp_bucket=(
                GS_GCP_BUCKET_PUBLIC
                if opts.public_build
                else GS_GCP_BUCKET_INTERNAL
            ),
            path=gs_path,
        )
    autotest_server_path = f'{gs_build_path}/{AUTOTEST_SERVER_TARBALL}'

    logger.info('autotest_server_path %s', autotest_server_path)

    # TODO(kcwu): cache downloaded tarballs
    tmp_dir = tempfile.mkdtemp()
    tast_dir = os.path.join(opts.chromeos_root, cros_util.prebuilt_tast_dir)
    if os.path.exists(tast_dir):
        shutil.rmtree(tast_dir)
    os.makedirs(tast_dir)

    gs_util.cp(autotest_server_path, tmp_dir)
    tarball = os.path.join(tmp_dir, AUTOTEST_SERVER_TARBALL)
    util.check_call(
        'tar', 'xf', tarball, '--strip-components=1', 'tast', cwd=tast_dir
    )

    shutil.rmtree(tmp_dir)


def switch_main(args: tuple[str] | None):
    parser = create_argument_parser()
    opts = parser.parse_args(args)
    common.config_logging(opts)

    if cros_lab_util.is_satlab_dut(opts.dut):
        cros_lab_util.write_satlab_ssh_config(opts.dut)

    switch(opts, opts.board, opts.version, opts.dut)


def action() -> bisector_cli.SwitchAction:
    return bisector_cli.SwitchAction.WITH_DUT


def main(args: tuple[str] | None = None) -> int:
    return bisector_cli.switch_main_wrapper(switch_main, args)


if __name__ == '__main__':
    sys.exit(main())
