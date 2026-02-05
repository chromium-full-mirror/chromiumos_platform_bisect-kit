# Copyright 2023 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Utility functions for CrOS DLCs."""

import pathlib
import subprocess
import tempfile

from bisect_kit import cros_util
from bisect_kit import errors
from bisect_kit import util


DLC_OPT_PATH = pathlib.Path('/opt/google/dlc')


def is_installable(dlc_name: str, dut: str) -> bool:
    """Check if the DLC package is installable supported on the DUT."""
    try:
        ls_output = util.ssh_cmd(
            dut,
            'dlc_metadata_util',
            '--get',
            f'--id={dlc_name}',
            '||',
            'ls',
            str(DLC_OPT_PATH / dlc_name),
            max_attempts=5,
        )
        return ls_output.strip() != ''
    except subprocess.CalledProcessError:
        return False


def install(dlc_name: str, dut: str) -> bool:
    """Install DLC package on the DUT."""
    try:
        util.ssh_cmd(
            dut,
            'dlcservice_util',
            '--install',
            f'--id={dlc_name}',
            max_attempts=5,
        )
        return True
    except (subprocess.CalledProcessError, errors.SshConnectionError):
        return False


def uninstall(dlc_name: str, dut: str) -> bool:
    """Uninstall DLC package on the DUT."""
    try:
        util.ssh_cmd(
            dut,
            'dlcservice_util',
            '--uninstall',
            f'--id={dlc_name}',
            max_attempts=5,
        )
        return True
    except (subprocess.CalledProcessError, errors.SshConnectionError):
        return False


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
                tmp_path = pathlib.Path(tmp_folder)
                util.scp_cmd(f'{dut}:/etc/{file_name}', str(tmp_path))
                util.check_call(
                    'sudo',
                    'cp',
                    str(tmp_path / file_name),
                    str(chroot_build_etc),
                )
        util.check_call(
            'sudo', 'chmod', 'a+r', str(chroot_build_etc / file_name)
        )
