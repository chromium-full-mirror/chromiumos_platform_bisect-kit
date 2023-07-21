# Copyright 2023 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Utility functions for CrOS DLCs."""

import pathlib
import tempfile

from bisect_kit import cros_util
from bisect_kit import util


def fix_missing_files(chromeos_root: str, board: str, dut: str) -> None:
    """Copy missing files for the DLC module, see crbug.com/913076"""
    chroot_build_etc = pathlib.Path(
        cros_util.convert_path_outside_chroot(
            chromeos_root, f'/build/{board}/etc/'
        )
    )
    for file_name in ['lsb-release', 'update_engine.conf']:
        if not (chroot_build_etc / file_name).exists():
            with tempfile.TemporaryDirectory() as tmp_folder:
                tmp_folder = pathlib.Path(tmp_folder)
                util.scp_cmd(f'{dut}:/etc/{file_name}', str(tmp_folder))
                util.check_call(
                    'sudo',
                    'cp',
                    str(tmp_folder / file_name),
                    str(chroot_build_etc),
                )
        util.check_call(
            'sudo', 'chmod', 'a+r', str(chroot_build_etc / file_name)
        )
