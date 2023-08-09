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
from bisect_kit.dlc import dlc_util


PathLike = os.PathLike | str

DLC_NAME = 'borealis-dlc'
GS_PATH_PREFIX = f'gs://chromeos-localmirror-private/borealis/{DLC_NAME}-'
GS_PATH_SUFFIX = '.tar.xz'
GS_PATH_LS_PATTERN = f'{GS_PATH_PREFIX}*{GS_PATH_SUFFIX}'

CACHE_PATH = 'tmp/images-borealis-dlc'
SRC_PATH = 'src/platform/borealis'
OVERLAY_REPO_PATH = 'src/private-overlays/chromeos-partner-overlay'

_VERSION_PATTERN = re.compile(r'^(\d+\.)??\d{4}\.\d{2}\.\d{2}(\.\d{6})?$')
VERSION_EXAMPLES = '2023.02.09, 2023.02.09.031143, or 111.2023.02.09.031143'
VERSION_PIN_SUMMARY_PREFIX_LIST = [
    'borealis-dlc: Automatic uprev to ',
    'VERSION-PIN: updating version pin borealis-dlc to ',
]


def is_borealis_version(version) -> bool:
    return bool(_VERSION_PATTERN.match(version))


def is_installable(dut: str) -> bool:
    return dlc_util.is_installable(DLC_NAME, dut)


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


def install(dut: str) -> bool:
    return dlc_util.install(DLC_NAME, dut)


def uninstall(dut: str) -> bool:
    return dlc_util.uninstall(DLC_NAME, dut)


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


def build_revlist_from_gs(old: str, new: str) -> tuple[list[str], dict]:
    gs_list = gs_util.ls(GS_PATH_LS_PATTERN)
    revlist = [
        gs_path.removeprefix(GS_PATH_PREFIX).removesuffix(GS_PATH_SUFFIX)
        for gs_path in gs_list
    ]
    revlist = [
        version
        for version in revlist
        if old <= version <= new or version.startswith(new)
    ]
    return revlist, {}


def extract_version_from_commit_summary(summary: str) -> str | None:
    summary = summary.strip().rstrip('.')
    for prefix in VERSION_PIN_SUMMARY_PREFIX_LIST:
        summary = summary.removeprefix(prefix)
    rev = summary.split('-', 1)[0]  # remove suffix like '-r2'
    if not is_borealis_version(rev):
        return None
    return rev


def extract_revlist(details) -> tuple[list[str], dict]:
    rev_set, details_from_repo = set(), {}
    for _, rev_info in details.items():
        for action in rev_info['actions']:
            if action['action_type'] != 'commit':
                continue
            if action['path'] != OVERLAY_REPO_PATH:
                continue
            summary = action['commit_summary'].strip()
            rev = extract_version_from_commit_summary(summary)
            if rev is None:
                continue
            rev_set.add(rev)
            details_from_repo[rev] = rev_info
    revlist_from_gs, details_from_gs = build_revlist_from_gs(
        min(rev_set), max(rev_set)
    )
    merged_details = {
        rev: details_from_repo.get(rev) or details_from_gs.get(rev)
        for rev in revlist_from_gs
    }

    return revlist_from_gs, merged_details
