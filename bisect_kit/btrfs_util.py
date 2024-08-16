# Copyright 2023 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""BTRFS utility."""

import logging
import os
import pathlib
import time

from bisect_kit import common
from bisect_kit import util


PathLike = os.PathLike | str

logger = logging.getLogger(__name__)


def is_btrfs_subvolume(path: PathLike, raise_error=True) -> bool:
    path = pathlib.Path(path).expanduser().absolute()
    if not common.check_dir_existence(path, raise_error=raise_error):
        return False
    if path.is_symlink():
        return False
    if util.check_output('stat', '-f', '--format=%T', path).strip() != 'btrfs':
        return False
    return util.check_output('stat', '--format=%i', path).strip() == '256'


def _make_subvolume(path: PathLike) -> None:
    path = pathlib.Path(path).expanduser().absolute()
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    util.check_call('btrfs', 'subvolume', 'create', path.name, cwd=path.parent)


def makedirs(path: PathLike, use_btrfs: bool) -> None:
    path = pathlib.Path(path).expanduser().absolute()
    if path.exists():
        return

    if use_btrfs:
        _make_subvolume(path)
    else:
        path.mkdir(parents=True, exist_ok=True)


def copy_tree(src: PathLike, dst: PathLike) -> None:
    src = pathlib.Path(src)
    dst = pathlib.Path(dst)
    common.check_dir_existence(src)
    common.check_dir_existence(dst.parent)
    # Make sure dst do not exist, otherwise it becomes "dst/name" (one extra
    # depth) instead of "dst".
    if dst.exists():
        raise FileExistsError(dst)

    if is_btrfs_subvolume(src):
        util.check_call('btrfs', 'subvolume', 'snapshot', src, dst)
    else:
        # -a for recursion and preserve all attributes.
        util.check_call('cp', '-a', src, dst)


def delete_tree(path: PathLike, missing_ok=False) -> None:
    path = pathlib.Path(path).expanduser().absolute()
    if not path.exists():
        if missing_ok:
            return
        raise FileNotFoundError(path)
    if is_btrfs_subvolume(path):
        # btrfs should be mounted with 'user_subvol_rm_allowed' option and thus
        # normal user permission is enough.
        util.check_call('btrfs', 'subvolume', 'delete', path)
        for i in range(10):
            if not common.check_dir_existence(path, raise_error=False):
                return
            logger.debug('waiting for %s to be deleted', path)
            time.sleep(2**i)
        raise TimeoutError('could not delete %s' % path)

    util.check_call('sudo', 'rm', '-rf', path)
