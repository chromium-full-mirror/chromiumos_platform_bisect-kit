# Copyright 2023 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Borealis related utility functions."""

import os
import pathlib
import re

from bisect_kit import errors
from bisect_kit import gs_util
from bisect_kit import util


PathLike = os.PathLike | str

DLC_NAME = 'borealis-dlc'
GS_PATH_PREFIX = f'gs://chromeos-localmirror-private/borealis/{DLC_NAME}-'
GS_PATH_SUFFIX = '.tar.xz'

CACHE_PATH = 'tmp/images-borealis-dlc'
SRC_PATH = 'src/platform/borealis'

_VERSION_PATTERN = re.compile(r'^(\d+\.)??\d{4}\.\d{2}\.\d{2}(\.\d{6})?$')
VERSION_EXAMPLES = '2023.02.09, 2023.02.09.031143, or 111.2023.02.09.031143'


def is_borealis_version(version) -> bool:
    return bool(_VERSION_PATTERN.match(version))


def argtype_borealis_version(version: str) -> str:
    if is_borealis_version(version):
        return version
    raise errors.ArgTypeError(
        f'Invalid Borealis DLC version ({version})', VERSION_EXAMPLES
    )


def get_borealis_src_path(chromeos_root: PathLike) -> pathlib.Path:
    return pathlib.Path(chromeos_root) / SRC_PATH


def create_package_name(version: str) -> str:
    return f'{DLC_NAME}-{version}{GS_PATH_SUFFIX}'


def create_gs_path(version: str) -> str:
    return f'{GS_PATH_PREFIX}{version}{GS_PATH_SUFFIX}'


def download_rootfs_and_kernel(chromeos_root: PathLike, version: str) -> None:
    file_name = create_package_name(version)
    dlc_cache_folder = pathlib.Path(chromeos_root) / CACHE_PATH
    dlc_cache_folder.mkdir(parents=True, exist_ok=True)

    image_file = dlc_cache_folder / file_name
    if not image_file.exists():
        gs_path = create_gs_path(version)
        gs_util.cp(gs_path, dlc_cache_folder)
    borealis_folder = get_borealis_src_path(chromeos_root)
    util.check_call(
        'tar', 'xf', image_file, '-C', borealis_folder, cwd=dlc_cache_folder
    )
