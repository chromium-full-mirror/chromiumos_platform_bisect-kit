# Copyright 2017 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""ChromeOS utility.

Terminology used in this module.
  short_version: ChromeOS version number without milestone, like "9876.0.0".
  full_version: ChromeOS version number with milestone, like "R62-9876.0.0".
  snapshot_version: ChromeOS version number with milestone and snapshot id,
                    like "R62-9876.0.0-12345".
  version: if not specified, it could be in short or full format.
"""

from __future__ import annotations

import ast
import contextlib
import dataclasses
import enum
import glob
import ipaddress
import json
import logging
import multiprocessing
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import textwrap
import time
import typing
import urllib.parse

import bisect_kit
from bisect_kit import buildbucket_util
from bisect_kit import chromite_util
from bisect_kit import codechange
from bisect_kit import common
from bisect_kit import core
from bisect_kit import errors
from bisect_kit import git_util
from bisect_kit import gs_util
from bisect_kit import locking
from bisect_kit import plugin_util
from bisect_kit import repo_util
from bisect_kit import util
from google.protobuf import json_format


logger = logging.getLogger(__name__)

RE_NZ_NUM = r'(0|[1-9][0-9]*)'  # non-zero leading number
RE_CROS_SHORT_VERSION = rf'{RE_NZ_NUM}\.{RE_NZ_NUM}\.{RE_NZ_NUM}'
RE_CROS_FULL_VERSION = rf'R{RE_NZ_NUM}-{RE_CROS_SHORT_VERSION}'
RE_CROS_SNAPSHOT_VERSION = rf'{RE_CROS_FULL_VERSION}-{RE_NZ_NUM}'

PATTERN_CROS_SHORT_VERSION = re.compile(rf'^{RE_CROS_SHORT_VERSION}$')
PATTERN_CROS_FULL_VERSION = re.compile(rf'^{RE_CROS_FULL_VERSION}$')
PATTERN_CROS_SNAPSHOT_VERSION = re.compile(rf'^{RE_CROS_SNAPSHOT_VERSION}$')

GCP_BUCKET_INTERNAL = 'chromeos-image-archive'
GCP_BUCKET_PUBLIC = 'chromiumos-image-archive'
GS_ARCHIVE_BASE = 'gs://{gcp_bucket}/'
GS_ARCHIVE_PATH = 'gs://{gcp_bucket}/{board}-{bucket}/{version}'
RELEASE_BUCKET = 'release'
FACTORY_BUCKET = 'factory'
SNAPSHOT_BUCKET = 'snapshot'
PUBLIC_BUCKET = 'public'
PUBLIC_SNAPSHOT_BUCKET = 'public-snapshot'

# only fetches manifest from gs when milestone >= 92
gs_manifest_cutoff_milestone = 92
gs_manifest_base = 'gs://chromeos-manifest-versions/'
gs_manifest_path = (
    'gs://chromeos-manifest-versions/buildspecs/{milestone}'
    '/{short_version}.xml'
)

PUBLIC_MANIFEST_REPO_URL = (
    'https://chromium.googlesource.com/chromiumos/manifest'
)
INTERNAL_MANIFEST_REPO_URL = (
    'https://chrome-internal.googlesource.com/chromeos/manifest-internal'
)

# Since snapshots with version >= 12618.0.0 have android and chrome version
# info.
snapshot_cutover_version = '12618.0.0'

# http://crbug.com/1170601, small snapshot ids should be ignored
# 21000 is R80-12617.0.0
snapshot_cutover_id = 21000

# current earliest buildbucket buildable versions
# picked from https://crrev.com/c/2072618
buildbucket_cutover_versions = [
    '12931.0.0',
    '12871.26.0',  # R81
    '12871.24.2',  # stabilize-12871.24.B
    '12812.10.0',  # factory-excelsior-12812.B
    '12768.14.0',  # firmware-servo-12768.B
    '12739.85.0',  # R80
    '12739.67.1',  # stabilize-excelsior-12739.67.B
    '12692.36.0',  # factory-hatch-12692.B
    '12672.104.0',  # firmware-hatch-12672.B
    '12607.110.0',  # R79
    '12607.83.2',  # stabilize-quickfix-12607.83.B
    '12587.59.0',  # factory-kukui-12587.B
    '12573.78.0',  # firmware-kukui-12573.B
    '12499.96.0',  # R78
    '12422.33.0',  # firmware-mistral-12422.B
    '12371.190.0',  # R77
    '12361.38.0',  # factory-mistral-12361.B
    '12200.65.0',  # firmware-sarien-12200.B
    '12105.128.0',  # R75
    '12033.82.0',  # factory-sarien-12033.B
]

chromeos_root_inside_chroot = '/mnt/host/source'
# relative to chromeos_root
in_tree_autotest_dir = 'src/third_party/autotest/files'
prebuilt_autotest_dir = 'tmp/autotest-prebuilt'
prebuilt_tast_dir = 'tmp/tast-prebuilt'
# Relative to chromeos root. Images are build_images_dir/$board/$image_name.
build_images_dir = 'src/build/images'
cached_images_dir = 'tmp/images'
disk_image_bundle_filename = 'chromiumos_test_image.tar.xz'
disk_vm_image_bundle_filename = 'chromiumos_test_image.tar.gz'
test_image_filename = 'chromiumos_test_image.bin'
test_image_zip_filename = 'image.zip'
sample_partition_filename = 'full_dev_part_KERN.bin.gz'
vm_image_filename = 'chromiumos_test_image_gce.tar.gz'

CROSLAND_URL_TEMPLATE = 'https://crosland.corp.google.com/log/%s..%s'
FORWARDED_DUT_HOST = 'localhost'
FORWARDED_DUT_PORT = 2222

DEFAULT_SSH_ATTEMPTS = 10
DEFAULT_QUERY_LSB_RELEASE_ATTEMPTS = DEFAULT_SSH_ATTEMPTS
DEFAULT_QUERY_OS_RELEASE_ATTEMPTS = DEFAULT_SSH_ATTEMPTS
DEFAULT_QUERY_CPU_ARCH_ATTEMPTS = DEFAULT_SSH_ATTEMPTS
DEFAULT_QUERY_ROOT_PARTITION_ATTPEMPTS = DEFAULT_SSH_ATTEMPTS
DEFAULT_CHECK_KERNEL_READY_ATTPEMPTS = DEFAULT_SSH_ATTEMPTS
DEFAULT_CHECK_GOOD_DUT_ATTEMPTS = DEFAULT_SSH_ATTEMPTS
DEFAULT_QUERY_BOOT_ID_ATTEMPTS = DEFAULT_SSH_ATTEMPTS
DEFAULT_QUERY_TEST_RUNNER_ATTEMPTS = DEFAULT_SSH_ATTEMPTS
DEFAULT_REPAIR_DUT_ATTEMPTS = 3

VM_BOARDS = ['amd64-generic', 'betty-arc-r', 'betty-arc-t', 'reven-vmtest']


@dataclasses.dataclass
class ChromeOSVersion:
    """A data class to store ChromeOS Version info parsed.

    The version keys are defined in:
    https://chromium.googlesource.com/chromiumos/overlays/chromiumos-overlay/+/0357a0ff14d1de4fb99976a3edf453063cde8130/chromeos/config/chromeos_version.sh
    """

    chrome_branch: int
    chromeos_build: int
    chromeos_branch: int
    chromeos_patch: int


@dataclasses.dataclass
class VersionInfo:
    """A data class represents version info."""

    cros_short_version: str | None = None
    cros_full_version: str | None = None
    milestone: str | None = None
    cr_version: str | None = None
    android_build_id: str | None = None
    android_branch: str | None = None  # in format like 'git_nyc-mr1-arc'
    android_target: str | None = None
    buildbucket_id: int | None = None

    def has_conflict(self, other) -> bool:
        for field in dataclasses.fields(other):
            my_value = getattr(self, field.name)
            other_value = getattr(other, field.name)
            if (
                my_value is not None
                and other_value is not None
                and my_value != other_value
            ):
                return False
        return True

    def update(self, other):
        for field in dataclasses.fields(other):
            other_value = getattr(other, field.name)
            if other_value:
                setattr(self, field.name, other_value)
        return self


@dataclasses.dataclass
class PartitionInfo:
    """A data class indicates partition info.

    Example:
        disk: /dev/sda
        active_kernel_partition: 2 (/dev/sda2)
        active_root_partition: 3 (/dev/sda3)
    """

    active_kernel_partition: int
    active_root_partition: int
    disk: str


class ImageType(enum.Enum):
    """ChromeOS image type

    It describes the image format, not image location.
    """

    # Full disk image like chromiumos_test_image.bin and
    # chromiumos_test_image.tar.xz.
    # Supported by 'cros flash'.
    DISK_IMAGE = enum.auto()
    # Contains files like full_dev_part_KERN.bin.gz.
    # Supported by quick-provision and newer 'cros flash'.
    PARTITION_IMAGE = enum.auto()
    # Contains files like image.zip. We need to unzip first.
    ZIP_FILE = enum.auto()
    # The image file used to bootup gce image.
    VM_IMAGE = enum.auto()

    @classmethod
    def determine(cls, path: str) -> ImageType | None:
        """Determine a filetype."""
        filename = path.split('/')[-1]
        if filename == sample_partition_filename:
            return cls.PARTITION_IMAGE
        if filename == test_image_zip_filename:
            return cls.ZIP_FILE
        if filename in [disk_image_bundle_filename, test_image_filename]:
            return cls.DISK_IMAGE
        if filename in [vm_image_filename]:
            return cls.VM_IMAGE
        return None


class ImageInfo(dict):
    """Image info (dict: image type -> path).

    For a given ChromeOS version, there are several image formats available.
    This class describes a collection of images for a certain ChromeOS version.
    cros_flash() or quick_provision() can resolve a compatible image from this
    object. `image type` is an ImageType enum. `path` could be a path on the
    local disk or a remote URI.
    """


class NeedRecreateChrootException(Exception):
    """Failed to build ChromeOS because of chroot mismatch or corruption"""


class SnapshotStore:
    """Stores image version of snapshots."""

    images: dict[str, ImageInfo] = {}

    @classmethod
    def init_with_state(cls, states: core.BisectStates):
        """Read snapshot information from ChromeOSVersionDomain state object.

        This is a optional step to speedup and reuse the image list fetched in
        bisect_cros_version. If the state file is not given, SnapshotStore may try
        to fetch images from gs bucket directly.

        Args:
          states: A ChromeOSVersionDomain state.
        """
        if not states.data:
            raise errors.InternalError('BisectStates should be loaded')

        for version, detail in states.details.items():
            cls.images[version] = ImageInfo()
            for img_type_str, path in detail['images'].items():
                cls.images[version][ImageType[img_type_str]] = path

    @classmethod
    def query_snapshot_image(
        cls,
        board: str,
        snapshot_version: str,
        is_public_build: bool,
    ) -> str | None:
        """Query a snapshot image path from SnapshotStore or gs bucket.

        Return a zip image if exists, else return a partition image.

        Args:
          board: CrOS board name
          snapshot_version: CrOS snapshot version
          is_public_build: Whether to use public build buckets.
              If None, use both public and internal buckets.

        Returns:
          A gs path for specific version or None if not exists.
        """

        def __check_file_on_dir(gcp_bucket, dir_pattern, query_files):
            image_info = ImageInfo()
            found = False
            buckets = _generate_possible_bucket_list(
                is_public_build=is_public_build, is_snapshot=True
            )
            for bucket in buckets:
                for filename in query_files:
                    path = (
                        f'gs://{gcp_bucket}/{board}-{bucket}'
                        f'/{dir_pattern}/{filename}'
                    )
                    output = gs_util.ls(path, ignore_errors=True)
                    if not output:
                        continue
                    found = True
                    img_type = ImageType.determine(output[0])
                    image_info[img_type] = output[0]
            return image_info if found else None

        if snapshot_version not in cls.images:
            if is_vm_board(board):
                query_files = [vm_image_filename]
            else:
                query_files = [
                    test_image_zip_filename,
                    sample_partition_filename,
                ]
            logger.warning(
                '%s not found in SnapshotStore, try to fetch from the archive',
                snapshot_version,
            )

            gcp_bucket = (
                GCP_BUCKET_PUBLIC if is_public_build else GCP_BUCKET_INTERNAL
            )

            # First: Assuiming the suffix number is a snapshot sequence number.
            image_info = __check_file_on_dir(
                gcp_bucket, f'{snapshot_version}-*', query_files
            )

            # Fall back: Assuiming the suffix number is a snapshot identifier.
            if not image_info:
                _, _, snapshot_id = snapshot_version_split(snapshot_version)
                lastest_file_content = gs_util.cat(
                    f'gs://{gcp_bucket}/{board}-snapshot'
                    f'/LATEST-SNAPSHOT-{snapshot_id}',
                    ignore_errors=True,
                )
                if lastest_file_content:
                    image_info = __check_file_on_dir(
                        gcp_bucket, lastest_file_content.strip(), query_files
                    )

            cls.images[snapshot_version] = (
                image_info if image_info else ImageInfo()
            )

        if is_vm_board(board):
            return cls.images[snapshot_version].get(ImageType.VM_IMAGE)
        zip_image = cls.images[snapshot_version].get(ImageType.ZIP_FILE)
        partition_image = cls.images[snapshot_version].get(
            ImageType.PARTITION_IMAGE
        )
        return zip_image if zip_image else partition_image

    @classmethod
    def query_snapshot_buildbucket_id(
        cls, board: str, snapshot_version: str, is_public_build: bool
    ) -> int | None:
        """Query buildbucket id of a snapshot"""
        if not is_cros_snapshot_version(snapshot_version):
            return None
        path = cls.query_snapshot_image(
            board, snapshot_version, is_public_build
        )
        if not path:
            raise errors.ExternalError(
                'board=%s snapshot=%s not found' % (board, snapshot_version)
            )
        buildbucket_id = cls.parse_snapshot_path(path).get('buildbucket_id')
        if buildbucket_id is None:
            return None
        return int(buildbucket_id)

    @classmethod
    def query_snapshot_version_by_id(
        cls, board: str, snapshot_id: str, is_public_build: bool
    ) -> str | None:
        """Given a snapshot id, return its corresponding snapshot version."""
        for snapshot_version in cls.images:
            gs_path = cls.query_snapshot_image(
                board, snapshot_version, is_public_build
            )
            if (
                gs_path
                and cls.parse_snapshot_path(gs_path).get('snapshot_id')
                == snapshot_id
            ):
                return snapshot_version

        buckets = _generate_possible_bucket_list(
            is_public_build=is_public_build, is_snapshot=True
        )

        # No existing BisectStates or version not found in the state file,
        # fetch snapshots from cloud storage
        logger.warning(
            'snapshot_id %s not found in SnapshotStore, fetch from buckets %s',
            snapshot_id,
            buckets,
        )
        for bucket in buckets:
            gs_path = GS_ARCHIVE_PATH.format(
                gcp_bucket=(
                    GCP_BUCKET_PUBLIC
                    if is_public_build
                    else GCP_BUCKET_INTERNAL
                ),
                board=board,
                bucket=bucket,
                snapshot_id=snapshot_id,
                version='R*-{snapshot_id}-*',
            )
            for line in gs_util.ls(gs_path, ignore_errors=True):
                info = cls.parse_snapshot_path(line)
                if info.get('snapshot_id') == snapshot_id:
                    return info.get('snapshot_version')

        return None

    @classmethod
    def search_snapshot_image(
        cls, board: str, snapshot_version: str, is_public_build: bool
    ) -> ImageInfo:
        """Searches chromeos snapshot image.

        Args:
          board: ChromeOS board name
          snapshot_version: ChromeOS snapshot version number
          is_public_build: Search from the public build bucket or internal one.

        Returns:
          ImageInfo object
        """
        assert is_cros_snapshot_version(snapshot_version)
        image_info = ImageInfo()

        # No existing BisectStates or version not found in the state file
        if not cls.images.get(snapshot_version):
            cls.query_snapshot_image(board, snapshot_version, is_public_build)

        for img_type, gs_path in cls.images[snapshot_version].items():
            if img_type == ImageType.PARTITION_IMAGE:
                gs_path = gs_path.replace('/' + sample_partition_filename, '')
            image_info[img_type] = gs_path
        return image_info

    @classmethod
    def parse_snapshot_path(cls, path: str) -> dict[str, str]:
        """Parse information in snapshot path."""
        possible_buckets = _generate_possible_bucket_list(
            is_public_build=None, is_snapshot=None
        )
        bucket_pattern = '|'.join(possible_buckets)
        m = re.match(
            rf"""
                .*/ # gs://.../
                (?P<board>[^/]+?)-  # eg. eve, amd64-generic, ...
                (?P<bucket>{bucket_pattern})/ # release, public-snapshot, ...
                (?P<snapshot_version>R\d+-\d+\.\d+\.\d+-(?P<snapshot_id>\d+))
                -(?P<buildbucket_id>.+)/ # R91-12345.0.0-67890-123456/
                (?P<filename>[^/]+) # image.zip
            """,
            path,
            re.VERBOSE,
        )
        return m.groupdict() if m else {}

    @classmethod
    def query_snapshot_info(
        cls, board: str, version: str, is_public_build: bool
    ) -> dict[str, str]:
        """Query snapshot information by board and version."""
        path = cls.query_snapshot_image(board, version, is_public_build)
        if not path:
            raise errors.ExternalError(
                'board=%s snapshot=%s not found' % (board, version)
            )
        return cls.parse_snapshot_path(path)


def is_cros_short_version(version: str) -> bool:
    """Determines if `version` is chromeos short version.

    This function doesn't accept version number of local build.
    """
    return bool(PATTERN_CROS_SHORT_VERSION.match(version))


def is_cros_full_version(version: str) -> bool:
    """Determines if `version` is chromeos full version.

    This function doesn't accept version number of local build.
    """
    return bool(PATTERN_CROS_FULL_VERSION.match(version))


def is_cros_version(version: str) -> bool:
    """Determines if `version` is chromeos version (either short or full)"""
    return is_cros_short_version(version) or is_cros_full_version(version)


def is_cros_snapshot_version(version: str) -> bool:
    """Determines if `version` is chromeos snapshot version"""
    return bool(PATTERN_CROS_SNAPSHOT_VERSION.match(version))


def is_cros_or_snapshot_version(version: str) -> bool:
    """Determines if `version` is chromeos or snapshot version"""
    return is_cros_version(version) or is_cros_snapshot_version(version)


def is_cros_version_lesseq(ver1, ver2):
    """Determines if ver1 is less or equal to ver2.

    Args:
      ver1: a ChromeOS version in short, full, or snapshot format.
      ver2: a ChromeOS version in short, full, or snapshot format.

    Returns:
      True if ver1 is less or equal to ver2.
    """
    assert is_cros_version(ver1) or is_cros_snapshot_version(ver1)
    assert is_cros_version(ver2) or is_cros_snapshot_version(ver2)

    # In rare cases, a later Chrome milestone number doesn't imply a later
    # ChromeOS version. So we are not using the milestone numbers in the
    # comparison to avoid the issue. Please see b/260532030.

    ver1 = [int(x) for x in re.split(r'[.-]', ver1) if not x.startswith('R')]
    ver2 = [int(x) for x in re.split(r'[.-]', ver2) if not x.startswith('R')]
    return ver1 <= ver2


def is_ancestor_version(ver1, ver2):
    """Determines `ver1` version is ancestor of `ver2` version.

    Returns:
      True only if `ver1` is the ancestor of `ver2`. One version is not considered
      as ancestor of itself.
    """
    assert is_cros_version(ver1) or is_cros_snapshot_version(ver1)
    assert is_cros_version(ver2) or is_cros_snapshot_version(ver2)

    if is_cros_version_lesseq(  # pylint: disable=arguments-out-of-order
        ver2, ver1
    ):
        return False

    if not util.is_direct_relative_version(
        version_to_short(ver1), version_to_short(ver2)
    ):
        return False

    # Compare snapshot id if available.
    if is_cros_snapshot_version(ver1) and is_cros_snapshot_version(ver2):
        _, short_1, snapshot_1 = snapshot_version_split(ver1)
        _, short_2, snapshot_2 = snapshot_version_split(ver2)
        if short_1 == short_2 and snapshot_1 >= snapshot_2:
            return False

    return True


def is_buildbucket_buildable(version):
    """Determines if a version is buildable on buildbucket."""
    short_version = version_to_short(version)
    # If given version is child of any cutover, then it's buildable
    return any(
        util.is_direct_relative_version(x, short_version)
        and is_cros_version_lesseq(x, version)
        for x in buildbucket_cutover_versions
    )


def make_cros_full_version(milestone, short_version):
    """Makes full_version from milestone and short_version"""
    assert milestone
    return 'R%s-%s' % (milestone, short_version)


def make_cros_snapshot_version(milestone, short_version, snapshot_id):
    """Makes snapshot version from milestone, short_version and snapshot id"""
    return 'R%s-%s-%s' % (milestone, short_version, snapshot_id)


def version_split(version):
    """Splits full_version or snapshot_version into milestone and short_version"""
    assert is_cros_full_version(version) or is_cros_snapshot_version(version)
    if is_cros_snapshot_version(version):
        return snapshot_version_split(version)[0:2]
    milestone, short_version = version.split('-')
    return milestone[1:], short_version


def snapshot_version_split(snapshot_version):
    """Splits snapshot_version into milestone, short_version and snapshot_id"""
    assert is_cros_snapshot_version(snapshot_version)
    milestone, short_version, snapshot_id = snapshot_version.split('-')
    return milestone[1:], short_version, snapshot_id


def argtype_cros_version(s):
    if (not is_cros_version(s)) and (not is_cros_snapshot_version(s)):
        msg = 'invalid cros version'
        raise errors.ArgTypeError(
            msg, '9876.0.0, R62-9876.0.0 or R77-12369.0.0-11681'
        )
    return s


def query_dut_lsb_release(
    host: str, max_attempts: int = DEFAULT_QUERY_LSB_RELEASE_ATTEMPTS
) -> dict[str, str]:
    """Query /etc/lsb-release of given DUT

    Args:
      host: the DUT address
      max_attempts: the number of attempts to query the DUT

    Returns:
      dict for keys and values of /etc/lsb-release.

    Raises:
      errors.SshConnectionError: cannot connect to host
      errors.ExternalError: lsb-release file doesn't exist
    """
    try:
        output = util.ssh_cmd(
            host, 'cat', '/etc/lsb-release', max_attempts=max_attempts
        )
    except subprocess.CalledProcessError as e:
        raise errors.ExternalError(
            'unable to read /etc/lsb-release; not a DUT'
        ) from e
    return dict(re.findall(r'^(\w+)=(.*)$', output, re.M))


def query_dut_os_release(
    host: str, max_attempts: int = DEFAULT_QUERY_OS_RELEASE_ATTEMPTS
) -> dict[str, str]:
    """Query /etc/os-release of given DUT

    Args:
      host: the DUT address
      max_attempts: the number of attempts to query the DUT

    Returns:
      dict for keys and values of /etc/os-release.

    Raises:
      errors.SshConnectionError: cannot connect to host
      errors.ExternalError: lsb-release file doesn't exist
    """
    try:
        output = util.ssh_cmd(
            host, 'cat', '/etc/os-release', max_attempts=max_attempts
        )
    except subprocess.CalledProcessError as e:
        raise errors.ExternalError(
            'unable to read /etc/os-release; not a DUT'
        ) from e
    return dict(re.findall(r'^(\w+)=(.*)$', output, re.M))


def query_dut_cpu_arch(
    host: str,
    builder: str = '',
    max_attempts: int = DEFAULT_QUERY_CPU_ARCH_ATTEMPTS,
) -> str | None:
    """Query CPU architecture of the given DUT.

    Args:
        host: the DUT address.
        builder: Builder of the target CrOS image.
        max_attempts: the number of attempts to query the DUT

    Returns:
      CPU architecture in string or None if not found.

    Raise:
      errors.SshConnectionError: cannot connect to host.
      errors.ExternalError: failed to run 'hexdump /sbin/init' on the given DUT.
    """
    # Some boards(e.g. jacuzzi,kukui) have moved to 64 bit userspace, however some experimental
    # builder variantions of them(e.g. jacuzzi64, kukui64) are moved to 32 bit temporarily.
    # The experimental builders will be shut down soon and we can remove these checks then.
    # See b/338497470#comment13 for more context.
    if builder in ['kukui', 'jacuzzi', 'cherry', 'asurada']:
        return 'arm64'

    if builder in ['kukui64', 'jacuzzi64', 'cherry64', 'asurada64']:
        return 'arm'

    try:
        # The 18th and 19th bytes of an ELF file indicates the target
        # architecture.
        # See https://en.wikipedia.org/wiki/Executable_and_Linkable_Format
        output = util.ssh_cmd(
            host,
            'hexdump',
            '-e',
            '\'"%x"\'',  # Print the bytes as a hexadecimal number.
            '-s',
            '18',  # Skip 18 bytes.
            '-n',
            '2',  # Dump 2 bytes.
            '/sbin/init',
            max_attempts=max_attempts,
        ).strip()

        if output == 'b7':
            return "arm64"
        if output == '28':
            return "arm"
        if output == '3e':
            return "x86_64"
    except subprocess.CalledProcessError as e:
        raise errors.ExternalError(
            'unable to get DUT architecture by "hexdump /sbin/init"'
        ) from e
    logger.debug(
        "query_dut_cpu_arch failed to match the architecture with any of the arm, arm64, or x86_64."
    )
    return None


def query_dut_root_partition(
    host: str, max_attempts: int = DEFAULT_QUERY_ROOT_PARTITION_ATTPEMPTS
) -> PartitionInfo:
    """Query partition of the given DUT.

    Args:
        host: the DUT address.
        max_attempts: the number of attempts to query the DUT

    Returns:
        A PartitionInfo object indicates the partition used on the DUT.

    Raise:
        errors.ExternalError: SSH connection error, or failed to parse partition info.
    """
    # https://chromium.googlesource.com/chromiumos/docs/+/HEAD/disk_format.md#drive-partitions
    partition_kernel_a = 2
    partition_root_a = 3
    partition_kernel_b = 4
    partition_root_b = 5

    try:
        # Example1: "/dev/mmcblk0"
        # Example2: "/dev/sdb"
        root_disk = util.ssh_cmd(
            host, 'rootdev', '-s', '-d', max_attempts=max_attempts
        ).strip()
        # Example1: "/dev/mmcblk0p5"
        # Example2: "/dev/sdb2"
        cur_root_partition = util.ssh_cmd(
            host, 'rootdev', '-s', max_attempts=max_attempts
        ).strip()
        # Example1: "/dev/mmcblk0p5" => 5
        # Example2: "/dev/sdb2" => 2
        match = re.match(r'.*([0-9]+)', cur_root_partition)
        if not match:
            raise errors.ExternalError(
                f'failed to match partition number from {cur_root_partition}'
            )
        partition_number = int(match.group(1))
        if partition_number == partition_root_a:
            return PartitionInfo(
                disk=root_disk,
                active_root_partition=partition_root_a,
                active_kernel_partition=partition_kernel_a,
            )
        if partition_number == partition_root_b:
            return PartitionInfo(
                disk=root_disk,
                active_root_partition=partition_root_b,
                active_kernel_partition=partition_kernel_b,
            )
        raise errors.ExternalError(
            f'Unknown partition number {partition_number} from {cur_root_partition}'
        )
    except subprocess.CalledProcessError as e:
        raise errors.ExternalError('unable to query dut root partition') from e


def query_dut_test_runner_availability(
    host: str, max_attempts: int = DEFAULT_QUERY_TEST_RUNNER_ATTEMPTS
) -> bool:
    """Query the availability of /usr/local/bin/local_test_runner

    The /usr/local/bin/local_test_runner can occasionally disappear after running a test.

    Args:
      host: the DUT address
      max_attempts: the number of attempts to query the DUT

    Returns:
        A boolean, indicates if the local_test_runner is available.

    Raise:
        errors.ExternalError: SSH connection error.
    """
    test_runner_path = '/usr/local/bin/local_test_runner'
    try:
        output = util.ssh_cmd(
            host, 'ls', test_runner_path, max_attempts=max_attempts
        ).strip()
        if output == test_runner_path:
            return True
    except subprocess.CalledProcessError as e:
        logger.warning('query_dut_test_runner_availability failed: %s', e)
    return False


def is_dut_kernel_ready(
    host: str, max_attempts: int = DEFAULT_CHECK_KERNEL_READY_ATTPEMPTS
) -> bool:
    """Query if the kernel partition is ready on the given DUT.

    Args:
        host: the DUT address.
        max_attempts: the number of attempts to query the DUT

    Returns:
        A boolean, indicates if the dut partition is ready.

    Raise:
        errors.ExternalError: SSH connection error, or failed to parse partition info.
    """
    info = query_dut_root_partition(host)
    try:
        # Returns '1' or '0', indicates the successful flag of a partition.
        # https://chromium.googlesource.com/chromiumos/platform/vboot_reference/+/6d1c48e3179056d965edfdd630a9dd6cda12f14b/cgpt/cgpt_common.c#835
        ready = util.ssh_cmd(
            host,
            'cgpt',
            'show',
            '-S',
            '-i',
            str(info.active_kernel_partition),
            info.disk,
            max_attempts=max_attempts,
        ).strip()
    except subprocess.CalledProcessError as e:
        raise errors.ExternalError(
            'unable to query if dut kernel is ready'
        ) from e
    return ready == '1'


def wait_kernel_ready(host: str):
    """Wait until kernel ready on given DUT."""
    # timeout: 120 seconds, query every 2 seconds
    for _ in range(60):
        if is_dut_kernel_ready(host):
            logger.debug('host %s kernel partition is ready', host)
            return
        time.sleep(2)
    raise errors.ExternalError('kernel partition not ready')


def is_dut(host):
    """Determines whether a host is a chromeos device.

    Args:
      host: the DUT address

    Returns:
      True if the host is a chromeos device.
    """
    try:
        return query_dut_os_release(host).get('ID') in [
            'chromiumos',
            'chromeos',
        ]
    except (errors.ExternalError, errors.SshConnectionError):
        return False


def has_python(
    host: str, max_attempts: int = DEFAULT_CHECK_GOOD_DUT_ATTEMPTS
) -> bool:
    """Returns whether python is present on the DUT."""
    # b/378126412: Temporary workaround to address the bad symlink
    # /usr/bin/python in some CrOS builds. It can be reverted to checking
    # 'python' only after R133-16095.0.0
    for python in ['python', 'python3.8', 'python3.11']:
        try:
            util.ssh_cmd(host, python, '-c', '1', max_attempts=max_attempts)
            return True
        except (subprocess.CalledProcessError, errors.SshConnectionError) as e:
            logger.warning('python checking failed: %s', e)
    return False


def is_good_dut(
    host: str, max_attempts: int = DEFAULT_CHECK_GOOD_DUT_ATTEMPTS
) -> bool:
    """Checks if the given DUT is a chromeos device with working python.

    Args:
      host: the DUT address
      max_attempts: the number of attempts to query the DUT

    Returns:
      True if the host is a chromeos device with working python.
    """
    if not is_dut(host):
        return False

    # Sometimes python is broken after 'cros flash'.
    if not has_python(host, max_attempts):
        return False
    try:
        return query_dut_test_runner_availability(
            host, max_attempts=max_attempts
        )
    except (subprocess.CalledProcessError, errors.SshConnectionError):
        return False


def is_vm_board(board: str) -> bool:
    """Returns if the board is a VM board.

    Args:
      board: The ChromeOS board name.

    Returns:
      True if the board is a vm board.
    """
    return board in VM_BOARDS


def query_dut_board(host):
    """Query board name of a given DUT"""
    return query_dut_lsb_release(host).get('CHROMEOS_RELEASE_BOARD')


def query_dut_short_version(host):
    """Query short version of a given DUT.

    This function may return version of local build, which
    is_cros_short_version() is false.
    """
    return version_to_short(
        query_dut_lsb_release(host).get('CHROMEOS_RELEASE_VERSION')
    )


def query_dut_prebuilt_version(host):
    """Return a snapshot version or short version of a given DUT.

    Args:
      host: dut host

    Returns:
      Snapshot version or short version.
    """
    lsb_release = query_dut_lsb_release(host)
    release_version = lsb_release.get('CHROMEOS_RELEASE_VERSION')
    builder_path = lsb_release.get('CHROMEOS_RELEASE_BUILDER_PATH', '')
    match = re.match(
        r'\S+-(?:snapshot|postsubmit)/(R\d+-\d+\.\d+\.\d+-\d+)-\d+',
        builder_path,
    )
    if match:
        return match.group(1)
    return release_version


def query_dut_is_by_official_builder(host):
    """Query if given DUT is build by official builder"""
    return query_dut_lsb_release(host).get('CHROMEOS_RELEASE_BUILDER_PATH', '')


def query_dut_boot_id(
    host: str,
    connect_timeout: int | None = None,
    max_attempts: int = DEFAULT_QUERY_BOOT_ID_ATTEMPTS,
    retry_interval: int = 60,
) -> str:
    """Query boot id.

    Args:
      host: DUT address
      connect_timeout: connection timeout in seconds
      max_attempts: the number of attempts to query the DUT
      retry_interval: retry interval in seconds

    Returns:
      boot uuid
    """
    return util.ssh_cmd(
        host,
        'cat',
        '/proc/sys/kernel/random/boot_id',
        connect_timeout=connect_timeout,
        max_attempts=max_attempts,
        retry_interval=retry_interval,
    ).strip()


def reboot(
    host, force_reboot_callback: typing.Callable[[str], bool] | None = None
):
    """Reboot a DUT and verify.

    Args:
      host: DUT address
      force_reboot_callback: powerful reboot hook (via servo). This will be
        invoked if normal reboot failed.
    """
    logger.debug('reboot %s', host)
    boot_id = None
    try:
        boot_id = query_dut_boot_id(host)

        max_attempts = 3
        for _ in range(0, max_attempts):
            try:
                util.ssh_cmd(host, 'reboot', connect_timeout=20)
            except errors.SshConnectionError as e:
                # Depends on timing, ssh may return failure due to broken pipe, which is
                # working as intended. Ignore such kind of errors.
                logger.info(
                    'Ignoring SshConnectionError while rebooting the DUT: %s', e
                )
            if wait_reboot_done(host) == boot_id:
                logger.info(
                    'rebooted but the boot id (%s) has not changed.',
                    boot_id,
                )
            else:
                # Reboot succeeded.
                return
        raise errors.ExternalError(
            'Boot ID has not changed after reboot: %s' % boot_id
        )
    # We need to catch errors.SshConnectionError here because
    # query_dut_boot_id() may raise it even though it is already caught in the
    # for loop above.
    except (errors.SshConnectionError, errors.ExternalError) as e:
        if force_reboot_callback and force_reboot_callback(host):
            if wait_reboot_done(host) == boot_id:
                raise errors.ExternalError(
                    'Boot ID has not changed after force reboot: %s' % boot_id
                ) from e
            return
        raise


def wait_reboot_done(host) -> str:
    # For dev-mode test image, the reboot time is roughly at least 16 seconds
    # (dev screen short delay) or more (long delay).
    time.sleep(15)
    try:
        # During boot, DUT does not response and thus ssh may hang a while. So
        # set a connect timeout. 3 seconds are enough and 2 are not. It's okay to
        # set tight limit because it's inside retry loop.
        return query_dut_boot_id(
            host,
            connect_timeout=5,
            max_attempts=60,
            retry_interval=1,
        )
    except errors.SshConnectionError as e:
        raise errors.ExternalError('reboot failed?') from e


def should_fetch_release_manifest_from_gs(version: str) -> bool:
    """Returns whether the manifest of given version is avaliable on gs.

    Args:
      version: CrOS full or snapshot version

    Returns:
      bool, True if manifest is avaliable on google storage.
    """
    assert is_cros_snapshot_version(version) or is_cros_full_version(version)
    milestone, _short_version = version_split(version)
    return int(milestone) >= gs_manifest_cutoff_milestone


def _iter_valid_prebuilt_gs_path(
    board: str,
    version: str,
    is_public_build: bool,
    buckets: list[str] | None = None,
) -> typing.Iterator[str]:
    """Iterate the valid paths of prebuilt images on `GS_ARCHIVE_PATH`.

    Args:
      board: The board name of the ChromeOS prebuilt image.
      version: The version of the ChromeOS prebuilt image. Both long version and
        short version are acceptable.
      is_public_build: whether to iterate public image archive.
      buckets: A list of bucket name to search. The default value is
        `prebuilt_image_buckets`.

    Yields:
      The next valid path of prebuilt image matches given `board`, `version`, and
      `buckets`. For example:

      `"gs://chromeos-image-archive/samus-release/R64-10123.0.0"`
    """
    if is_cros_short_version(version):
        version = f'R*-{version}'
    if buckets is None:
        buckets = _generate_possible_bucket_list(
            is_public_build=is_public_build,
            is_snapshot=is_cros_snapshot_version(version),
        )
    for bucket in buckets:
        path = GS_ARCHIVE_PATH.format(
            gcp_bucket=(
                GCP_BUCKET_PUBLIC if is_public_build else GCP_BUCKET_INTERNAL
            ),
            board=board,
            bucket=bucket,
            version=version,
        )
        for gs_path in gs_util.ls('-d', path, ignore_errors=True):
            yield gs_path.rstrip('/')


def query_prebuilt_gs_path(
    board: str,
    version: str,
    is_public_build: bool,
    buckets: list[str] | None = None,
) -> str:
    """Query the first valid path of prebuilt image on `GS_ARCHIVE_PATH`.

    Args:
      board: The board name of the ChromeOS prebuilt image.
      version: The version of the ChromeOS prebuilt image. Both long version and
        short version are acceptable.
      buckets: A list of bucket name to search. The default value is
        `prebuilt_image_buckets`.

    Returns:
      The first valid prebuilt path matches given `board`, `version`, and
      `buckets`.  An empty string `''` if there is no valid prebuilt path.
    """
    for archive_path in _iter_valid_prebuilt_gs_path(
        board, version, is_public_build, buckets
    ):
        return archive_path
    return ''


def has_release_prebuilt(
    board: str, version: str, is_public_build: bool
) -> bool:
    """Determine if the prebuilt image exist in the release bucket.

    Args:
      board: The board name of the ChromeOS prebuilt image.
      version: The version of the ChromeOS prebuilt image. Both long version and
        short version are acceptable.

    Returns:
      True for there exists a prebuilt in the release bucket. False otherwise.
    """
    return bool(
        query_prebuilt_gs_path(
            board, version, is_public_build, [RELEASE_BUCKET]
        )
    )


def query_milestone_by_version(
    board: str,
    short_version: str,
    spec_manager: typing.Optional[ChromeOSSpecManager] = None,
) -> str | None:
    """Query milestone by ChromeOS version number.

    Args:
      board: ChromeOS board name
      short_version: ChromeOS version number in short format, ex. 9300.0.0
      spec_manager: ChromeOS spec manager, to query version info in overlays

    Returns:
      ChromeOS milestone number (string). For example, '58' for '9300.0.0'.
      None if failed.
    """
    if spec_manager:
        info = spec_manager.lookup_chromeos_version(short_version)
        if info:
            return info.chrome_branch

    # Use the internal builds to look up the milestone.
    for archive_path in _iter_valid_prebuilt_gs_path(
        board,
        short_version,
        # Use the internal builds to look up the chrome version, since both
        # builds should have the same chrome version.
        is_public_build=False,
    ):
        if m := re.search(r'/R(?P<milestone>\d+)-', archive_path):
            return m.group('milestone')

    logger.debug('unable to query milestone of %s for %s', short_version, board)
    return None


def list_board_names(chromeos_root):
    """List board names.

    Args:
      chromeos_root: chromeos tree root

    Returns:
      list of board names
    """
    # Following logic is simplified from chromite/lib/portage_util.py
    cros_list_overlays = os.path.join(
        chromeos_root, 'chromite/bin/cros_list_overlays'
    )
    overlays = chromite_util.check_output(cros_list_overlays).splitlines()
    result = set()
    for overlay in overlays:
        conf_file = os.path.join(overlay, 'metadata', 'layout.conf')
        name = None
        if os.path.exists(conf_file):
            for line in open(conf_file):
                m = re.match(r'^repo-name\s*=\s*(\S+)\s*$', line)
                if m:
                    name = m.group(1)
                    break

        if not name:
            name_file = os.path.join(overlay, 'profiles', 'repo_name')
            if os.path.exists(name_file):
                with open(name_file) as f:
                    name = f.read().strip()

        if name:
            name = re.sub(r'-private$', '', name)
            result.add(name)

    return list(result)


def extract_major_version(version: str) -> str:
    """Converts a version to its major version.

    Args:
      version: ChromeOS version number or snapshot version

    Returns:
      major version number in string format
    """
    version = version_to_short(version)
    m = re.match(r'^(\d+)\.\d+\.\d+$', version)
    assert m
    return m.group(1)


def version_to_short(version):
    """Convert ChromeOS version number to short format.

    Args:
      version: ChromeOS version number in short or full format

    Returns:
      version number in short format
    """
    if is_cros_short_version(version):
        return version
    _, short_version = version_split(version)
    return short_version


def version_to_full(
    board, version, spec_manager: typing.Optional[ChromeOSSpecManager] = None
):
    """Convert ChromeOS version number to full format.

    Args:
      board: ChromeOS board name
      version: ChromeOS version number in short or full format
      spec_manager: ChromeOS spec manager, to query version info in overlays

    Returns:
      version number in full format
    """
    if is_cros_snapshot_version(version):
        milestone, short_version, _ = snapshot_version_split(version)
        return make_cros_full_version(milestone, short_version)
    if is_cros_full_version(version):
        return version
    milestone = query_milestone_by_version(
        board, version, spec_manager=spec_manager
    )
    if not milestone:
        raise errors.ExternalError(
            'incorrect board=%s or version=%s ?' % (board, version)
        )
    return make_cros_full_version(milestone, version)


def _find_files_from_image_archive(
    board: str,
    filenames: list[str],
    old: str,
    new: str,
    is_public_build: bool,
    is_snapshot: bool,
) -> list[tuple[str, str]]:
    assert is_cros_full_version(old) or is_cros_snapshot_version(old)
    assert is_cros_full_version(new) or is_cros_snapshot_version(new)

    old_milestone = int(version_split(old)[0])
    new_milestone = int(version_split(new)[0])

    # Sometimes older ChromeOS version has newer milestone number (b/260532030)
    if old_milestone > new_milestone:
        old_milestone, new_milestone = new_milestone, old_milestone

    version_prefixes = []
    if (
        old_milestone != new_milestone
        or
        # Prevent to send too much gsutil calls.
        old_milestone + 100 < (new_milestone)
    ):
        for milestone in range(old_milestone, new_milestone + 1):
            version_prefixes.append('R%d-' % milestone)
    else:
        for major in range(
            int(extract_major_version(old)), int(extract_major_version(new)) + 1
        ):
            version_prefixes.append('R%d-%d.' % (old_milestone, major))

    buckets = _generate_possible_bucket_list(
        is_public_build=is_public_build,
        is_snapshot=is_snapshot,
    )

    with multiprocessing.Pool() as pool:
        async_requests: list[dict[str, typing.Any]] = []
        for bucket in buckets:
            for version_prefix in version_prefixes:
                for filename in filenames:
                    gs_path = (
                        GS_ARCHIVE_PATH.format(
                            gcp_bucket=(
                                GCP_BUCKET_PUBLIC
                                if is_public_build
                                else GCP_BUCKET_INTERNAL
                            ),
                            board=board,
                            bucket=bucket,
                            version=version_prefix + '*',
                        )
                        + '/'
                        + filename
                    )
                    async_requests.append(
                        {
                            'path': gs_path,
                            'result': pool.apply_async(
                                gs_util.ls, [gs_path], {"ignore_errors": True}
                            ),
                        }
                    )

        result = []
        for async_request in async_requests:
            logger.debug(
                'waiting for the result of gsutil ls %s', async_request['path']
            )
            # timeout=3600 because `gsutil ls` shouldn't take more than 1 hour.
            try:
                lines = async_request['result'].get(timeout=3600)
            except multiprocessing.TimeoutError as e:
                raise errors.ExecutionTimeout(
                    'timed out getting the list of available prebuilts for %s'
                    % async_request['path']
                ) from e

            logger.debug('result: %s', lines)
            for gs_path in lines:
                m = re.search(r'(R\d+-\d+\.\d+\.\d+(-\d+)?)', gs_path)
                if not m:
                    continue
                version = m.group(1)
                if version != new and not is_ancestor_version(version, new):
                    continue
                if not (
                    is_cros_version_lesseq(old, version)
                    and is_cros_version_lesseq(version, new)
                ):
                    continue

                result.append((version, gs_path))
    return result


def is_bad_snapshot_to_ignore(version):
    assert is_cros_snapshot_version(version)
    _, _, snapshot_id = snapshot_version_split(version)

    # crbug/1170601: ignore small snapshot ids
    if int(snapshot_id) <= snapshot_cutover_id:
        return True

    # b/151054108: snapshot version in [29288, 29439] is broken
    if 29288 <= int(snapshot_id) <= 29439:
        return True

    # If the version is smaller than cutover, it might not
    # contain enough information for continuing android and chrome bisection.
    if not util.is_version_lesseq(
        snapshot_cutover_version, version_to_short(version)
    ):
        return True

    return False


def _list_prebuilt_from_image_archive(
    board: str, old: str, new: str, is_public_build: bool, use_snapshot: bool
) -> list[tuple[str, ImageInfo]]:
    """Lists ChromeOS prebuilt image available from gs://chromeos-image-archive.

    Args:
      board: ChromeOS board name
      old: start version (inclusive)
      new: end version (inclusive)
      use_snapshot: return snapshot versions if found

    Returns:
      list of (version, image_info):
        version: ChromeOS version in full format
        image_info: ImageInfo object
    """
    assert is_cros_full_version(old) or is_cros_snapshot_version(old)
    assert is_cros_full_version(new) or is_cros_snapshot_version(new)
    if is_vm_board(board):
        files_to_find = [vm_image_filename]
        files_to_find_snapshot = [vm_image_filename]
    else:
        files_to_find = [
            disk_image_bundle_filename,
            sample_partition_filename,
            test_image_zip_filename,
        ]
        files_to_find_snapshot = [
            test_image_zip_filename,
            sample_partition_filename,
        ]

    files = _find_files_from_image_archive(
        board,
        files_to_find,
        old,
        new,
        is_public_build=is_public_build,
        is_snapshot=False,
    )
    if use_snapshot:
        files += _find_files_from_image_archive(
            board,
            files_to_find_snapshot,
            old,
            new,
            is_public_build=is_public_build,
            is_snapshot=True,
        )
    logger.debug('Found prebuilt image files: %s', files)

    result = {}
    for version, gs_path in files:
        img_type = ImageType.determine(gs_path)
        assert img_type
        if version not in result:
            result[version] = ImageInfo()
        result[version][img_type] = gs_path

    return list(result.items())


def list_chromeos_prebuilt_versions(
    board: str,
    old: str,
    new: str,
    is_public_build: bool,
    use_snapshot: bool = False,
    spec_manager: typing.Optional[ChromeOSSpecManager] = None,
) -> tuple[list[str], dict[str, typing.Any]]:
    """Lists ChromeOS version numbers with prebuilt between given range

    Args:
      board: ChromeOS board name
      old: start version (inclusive)
      new: end version (inclusive)
      use_snapshot: return snapshot versions if found
      spec_manager: ChromeOS spec manager, to query version info in overlays

    Returns:
      (versions, details)
        versions: list of sorted version numbers (in full format) between
          [old, new] range (inclusive).
        details: A dict that contains detailed information for each version.
    """
    # Normalize.
    if is_cros_short_version(old):
        old = version_to_full(board, old, spec_manager=spec_manager)
    if is_cros_short_version(new):
        new = version_to_full(board, new, spec_manager=spec_manager)

    prebuilt_images = _list_prebuilt_from_image_archive(
        board,
        old,
        new,
        is_public_build=is_public_build,
        use_snapshot=use_snapshot,
    )
    logger.debug('prebuilt_images: %s', prebuilt_images)

    versions = []
    details: dict[str, typing.Any] = {}
    for version, image_info in prebuilt_images:
        if is_cros_snapshot_version(version) and is_bad_snapshot_to_ignore(
            version
        ):
            continue

        details[version] = {'images': {}}
        versions.append(version)
        for img_type, path in image_info.items():
            details[version]['images'][img_type.name] = path

    versions.sort(key=lambda v: list(map(int, re.findall(r'\d+', v))))
    return versions, details


def _prepare_image_for_quick_provision(image_info, is_public_build):
    path = image_info.get(ImageType.PARTITION_IMAGE)
    gs_archive_base = GS_ARCHIVE_BASE.format(
        gcp_bucket=GCP_BUCKET_PUBLIC if is_public_build else GCP_BUCKET_INTERNAL
    )
    if path and path.startswith(gs_archive_base):
        return urllib.parse.urlparse(path).path[1:]

    logger.warning(
        'image format or location are not supported by quick-provision: %s',
        image_info,
    )
    return None


def _cache_path_for_download(chromeos_root, url):
    os.makedirs(os.path.join(chromeos_root, cached_images_dir), exist_ok=True)
    name = urllib.parse.quote(url, safe='')
    return os.path.join(cached_images_dir, name)


def _generate_possible_bucket_list(
    is_public_build: typing.Optional[bool], is_snapshot: typing.Optional[bool]
):
    """Generates a list of possible bucket names.

    Args:
        is_public_build: Whether to use public build buckets.
            If None, use both public and internal buckets.
        is_snapshot: Whether to use snapshot buckets.
            If None, use both snapshot and non-snapshot buckets.

    Returns:
        A list of possible bucket names.
    """
    PREBUILT_IMAGE_BUCKETS_INTERNAL = [RELEASE_BUCKET, FACTORY_BUCKET]
    PREBUILT_IMAGE_BUCKETS_PUBLIC = [PUBLIC_BUCKET]
    SNAPSHOT_BUCKETS_INTERNAL = [SNAPSHOT_BUCKET]
    SNAPSHOT_BUCKETS_PUBLIC = [PUBLIC_SNAPSHOT_BUCKET]

    if is_public_build is None:
        non_snapshot_bucket_list = (
            PREBUILT_IMAGE_BUCKETS_INTERNAL + PREBUILT_IMAGE_BUCKETS_PUBLIC
        )
        snapshot_bucket_list = (
            SNAPSHOT_BUCKETS_INTERNAL + SNAPSHOT_BUCKETS_PUBLIC
        )
    elif is_public_build:
        non_snapshot_bucket_list = PREBUILT_IMAGE_BUCKETS_PUBLIC
        snapshot_bucket_list = SNAPSHOT_BUCKETS_PUBLIC
    else:
        non_snapshot_bucket_list = PREBUILT_IMAGE_BUCKETS_INTERNAL
        snapshot_bucket_list = SNAPSHOT_BUCKETS_INTERNAL

    if is_snapshot is None:
        bucket_list = non_snapshot_bucket_list + snapshot_bucket_list
    elif is_snapshot:
        bucket_list = snapshot_bucket_list
    else:
        bucket_list = non_snapshot_bucket_list

    return bucket_list


def extract_info_from_prebuilt_gs_path(gs_path) -> dict:
    """Extract the board, bucket, and version info from the given prebuilt path.

    Args:
      gs_path: A gs path of ChromeOS prebuilt image.

    Returns:
      A `dict` that contains board, bucket, and version info.  An empty `dict` if
      the given prebuilt path is invalid.
    """
    # TODO(acewu): Pre-compile all the regex patterns
    gcp_bucket_pattern = '|'.join([GCP_BUCKET_PUBLIC, GCP_BUCKET_INTERNAL])
    prebuilt_image_buckets = _generate_possible_bucket_list(
        is_public_build=None, is_snapshot=None
    )
    bucket_pattern = '|'.join(prebuilt_image_buckets)
    gs_path_pattern = (
        rf'gs://(?P<gcp_bucket>{gcp_bucket_pattern})/'
        rf'(?P<board>[^/]+?)-(?P<bucket>{bucket_pattern})?/'
        rf'(?P<version>[^/]+)/'
    )
    if m := re.search(gs_path_pattern, gs_path):
        return m.groupdict()
    return {}


def prepare_image_for_cros_flash(
    chromeos_root, image_info, is_public_build: bool
):
    """Prepares image path for 'cros flash'.

    Returns:
      path recognized by 'cros flash'. Local disk path will be relative to
      chromeos_root.
    """
    path = image_info.get(ImageType.DISK_IMAGE)
    gs_archive_base = GS_ARCHIVE_BASE.format(
        gcp_bucket=GCP_BUCKET_PUBLIC if is_public_build else GCP_BUCKET_INTERNAL
    )

    if path:
        # local path
        if '://' not in path:
            return path

        info = extract_info_from_prebuilt_gs_path(path)

        if info.get('bucket') == RELEASE_BUCKET:
            return f'xbuddy://remote/{info["board"]}/{info["version"]}/test'

        if info.get('bucket') == FACTORY_BUCKET:
            cache_path = _cache_path_for_download(
                chromeos_root, f'{path}.{test_image_filename}'
            )
            cache_path_full = os.path.join(chromeos_root, cache_path)
            if os.path.exists(cache_path_full):
                return cache_path
            with tempfile.TemporaryDirectory() as tmp_dir:
                gs_util.cp(path, tmp_dir)
                local_path = os.path.join(tmp_dir, os.path.basename(path))
                assert os.path.exists(local_path)
                util.check_call('tar', 'xf', local_path, cwd=tmp_dir)
                shutil.move(
                    os.path.join(tmp_dir, test_image_filename), cache_path_full
                )
            return cache_path

        if info.get('bucket') in [
            SNAPSHOT_BUCKET,
            PUBLIC_BUCKET,
            PUBLIC_SNAPSHOT_BUCKET,
        ]:
            # The image files place directly under the version-named directory.
            return os.path.dirname(path)

        if path.startswith(gs_archive_base):
            # TODO(yoshiki): Check if this logic is actually necessary.
            # Probably all the cases of `archive_base` are handled by the above
            # conditions.
            return path.replace(disk_image_bundle_filename, 'test')

        # 'cros flash' doesn't support other gs bucket, download to local.
        if path.startswith('gs://'):
            cache_path = _cache_path_for_download(chromeos_root, path)
            cache_path_full = os.path.join(chromeos_root, cache_path)
            if os.path.exists(cache_path_full):
                return cache_path
            gs_util.cp(path, cache_path_full)
            return cache_path

    path = image_info.get(ImageType.PARTITION_IMAGE)
    if path and path.startswith(gs_archive_base):
        # newer 'cros flash' support partition images
        if git_util.is_ancestor_commit(
            os.path.join(chromeos_root, 'chromite'), '191e7333cbeb7b', 'HEAD'
        ):
            return path

    path = image_info.get(ImageType.ZIP_FILE)
    if path:
        cache_path = _cache_path_for_download(
            chromeos_root, f'{path}.{test_image_filename}'
        )
        cache_path_full = os.path.join(chromeos_root, cache_path)
        if os.path.exists(cache_path_full):
            return cache_path

        with tempfile.TemporaryDirectory() as tmp_dir:
            local_path = path
            if path.startswith('gs://'):
                gs_util.cp(path, tmp_dir)
                local_path = os.path.join(tmp_dir, os.path.basename(path))
                assert os.path.exists(local_path)
            util.check_call(
                'unzip', '-j', local_path, test_image_filename, cwd=tmp_dir
            )
            shutil.move(
                os.path.join(tmp_dir, test_image_filename), cache_path_full
            )

        return cache_path

    return None


def _quick_provision(chromeos_root, host, image_info, is_public_build: bool):
    # TODO(kimjae): Transition to using TLS ProvisionDut for F20.
    logger.debug('quick_provision %s %s', host, image_info)
    build = _prepare_image_for_quick_provision(image_info, is_public_build)
    if not build:
        return False

    try:
        if ipaddress.ip_address(socket.gethostbyname(host)).is_private:
            # Unless you host a mirror by yourself, it will try all possible servers
            # and waste lots of time before give up.
            logger.debug('%s is in private network, skip quick_provision', host)
            return False
    except socket.gaierror as e:
        logger.warning('got %s, assume it is private address', str(e))
        return False

    autotest_path = os.path.join(
        chromeos_root_inside_chroot, in_tree_autotest_dir
    )
    quick_provision_cmd = [
        'test_that',
        '--args',
        "value='%s'" % build,
        host,
        'provision_QuickProvision',
        '--autotest_dir',
        autotest_path,
        '--debug',
    ]
    try:
        cros_sdk(chromeos_root, *quick_provision_cmd)
        wait_kernel_ready(host)
    except subprocess.CalledProcessError as e:
        raise errors.ExternalError('quick-provision failed') from e
    return True


def verify_dut_version(
    host: str, board: str, version: str, is_public_build: bool
):
    if version:
        # In the past, cros flash may fail with returncode=0
        # So let's have an extra check.
        if is_cros_snapshot_version(version):
            builder_path = query_dut_lsb_release(host).get(
                'CHROMEOS_RELEASE_BUILDER_PATH', ''
            )
            snapshot_info = SnapshotStore.query_snapshot_info(
                board, version, is_public_build
            )
            expect_prefix = '%s-%s/%s-' % (
                board,
                snapshot_info['bucket'],
                version,
            )
            if not builder_path.startswith(expect_prefix):
                raise errors.ExternalError(
                    'although provision succeeded, the OS builder path is '
                    'unexpected: actual=%s expect=%s'
                    % (builder_path, expect_prefix)
                )
        else:
            expect_version = version_to_short(version)
            dut_version = query_dut_short_version(host)
            if dut_version != expect_version:
                raise errors.ExternalError(
                    'although provision succeeded, the OS version is unexpected: '
                    'actual=%s expect=%s' % (dut_version, expect_version)
                )

    # "cros flash" may terminate successfully but the DUT starts self-repairing
    # (b/130786578), so it's necessary to do sanity check.
    if not is_good_dut(host):
        raise errors.ExternalError(
            'although provision succeeded, the DUT is in bad state'
        )


def provision_image(
    chromeos_root,
    host,
    board,
    image_info,
    is_public_build,
    version=None,
    clobber_stateful=False,
    disable_rootfs_verification=False,
    force_reboot_callback: typing.Callable[[str], bool] | None = None,
):
    # Try quick_provision first, but fallback to cros flash.
    # TODO(kcwu): only use quick_provision for DUTs in the lab
    # TODO(b/260296269): Find out the reason it fails and enable again.
    """
    try:
        if quick_provision(chromeos_root, host, image_info):
            verify_dut_version(host, board, version, is_public_build)
            return
        logger.debug('quick-provision is not supported; fallback to cros flash')
    except errors.ExternalError as e:
        logger.warning('quick-provision failed; fallback to cros flash: %s', e)
    """

    if not _cros_flash(
        chromeos_root,
        host,
        image_info,
        is_public_build=is_public_build,
        clobber_stateful=clobber_stateful,
        disable_rootfs_verification=disable_rootfs_verification,
        force_reboot_callback=force_reboot_callback,
    ):
        raise errors.InternalError('unsupported image: ' + str(image_info))
    workaround_b378126412(
        host, version, force_reboot_callback=force_reboot_callback
    )
    verify_dut_version(host, board, version, is_public_build)


def powerwash(
    dut: str,
    force_reboot_callback: typing.Callable[[str], bool] | None = None,
) -> bool:
    """Powerwash the DUT

    Args:
        dut: dut host name
        force_reboot_callback: reboot hook for passing to reboot function

    Returns:
        True if the powerwash was successful, False otherwise
    """
    try:
        # http://g3doc/company/users/timvp/ChromeOS/Debugging
        util.ssh_cmd(
            dut,
            'echo',
            '"fast safe"',
            '>',
            '/mnt/stateful_partition/factory_install_reset',
        )
        reboot(dut, force_reboot_callback=force_reboot_callback)
    except Exception as e:
        logger.debug('powerwashing %s failed %s', dut, e)
        return False
    logger.debug('powerwashing %s successful', dut)
    return True


def _search_prebuilt_image(board, version, is_public_build: bool) -> ImageInfo:
    """Searches chromeos prebuilt image.

    Args:
      chromeos_root: chromeos tree root
      board: ChromeOS board name
      version: ChromeOS version number in short or full format

    Returns:
      ImageInfo object
    """
    assert is_cros_version(version)

    image_info = ImageInfo()
    for gs_path in _iter_valid_prebuilt_gs_path(
        board, version, is_public_build
    ):
        if is_vm_board(board):
            vm_image_gs_path = f'{gs_path}/{vm_image_filename}'
            if gs_util.ls(vm_image_gs_path, ignore_errors=True):
                image_info[ImageType.VM_IMAGE] = vm_image_gs_path
        else:
            disk_image_gs_path = f'{gs_path}/{disk_image_bundle_filename}'
            if gs_util.ls(disk_image_gs_path, ignore_errors=True):
                image_info[ImageType.DISK_IMAGE] = disk_image_gs_path
            if gs_util.ls(
                f'{gs_path}/{sample_partition_filename}', ignore_errors=True
            ):
                image_info[ImageType.PARTITION_IMAGE] = gs_path
        if image_info:
            break
    return image_info


def search_image(board, version, is_public_build: bool) -> ImageInfo:
    if is_cros_snapshot_version(version):
        return SnapshotStore.search_snapshot_image(
            board, version, is_public_build
        )
    return _search_prebuilt_image(board, version, is_public_build)


def has_test_image(board, version, is_public_build: bool) -> bool:
    return bool(search_image(board, version, is_public_build=is_public_build))


def _cros_flash(
    chromeos_root,
    host,
    image_info,
    is_public_build,
    clobber_stateful=False,
    disable_rootfs_verification=True,
    force_reboot_callback: typing.Callable[[str], bool] | None = None,
):
    """Flash a DUT with given ChromeOS image.

    This is implemented by 'cros flash' command line.

    Args:
      chromeos_root: use 'cros flash' of which chromeos tree
      host: DUT address
      board: ChromeOS board name
      image_info: ImageInfo object
      version: ChromeOS version in short or full format
      clobber_stateful: Clobber stateful partition when performing update
      disable_rootfs_verification: Disable rootfs verification after update is
        completed
      force_reboot_callback: powerful reboot hook (via servo)

    Returns:
      False for unsupported images

    Raises:
      errors.ExternalError: cros flash failed
    """
    logger.info('cros_flash %s %s', host, image_info)

    # Powerwash is necessary to prevent some DUTs failing the flash with no space error. b/332260805
    # Powerwash will also reboot the DUT which is necessary because sometimes previous 'cros flash'
    # failed and entered a bad state.
    powerwash(host, force_reboot_callback=force_reboot_callback)

    # Stop service ap-update-manager to prevent rebooting during auto update.
    # The service is used in jetstream boards, but not other CrOS devices.
    if query_dut_os_release(host).get('GOOGLE_CRASH_ID') == 'Jetstream':
        try:
            # Sleep to wait ap-update-manager start, which may take up to 27 seconds.
            # For simplicity, we wait 60 seconds here, which is the timeout value of
            # jetstream_host.
            # https://chromium.googlesource.com/chromiumos/third_party/autotest/+/HEAD/server/hosts/jetstream_host.py#27
            time.sleep(60)
            util.ssh_cmd(host, 'stop', 'ap-update-manager')
        except subprocess.CalledProcessError:
            pass  # not started; do nothing

    image_path = prepare_image_for_cros_flash(
        chromeos_root, image_info, is_public_build=is_public_build
    )
    if not image_path:
        return False

    # Handle relative path.
    if '://' not in image_path and not os.path.isabs(image_path):
        image_path = os.path.join(chromeos_root, image_path)

        # Raise an exception with info in case of no image found.
        if not os.path.exists(image_path):
            logger.error('No image found in %s.', image_path)

            # The image may exists inside the chroot if we overlooked the code
            # path. Checking the existence inside the chroot just in case.
            chroot_image_path = os.path.join(
                chromeos_root_inside_chroot, image_path
            )
            chroot_image_existence = os.path.exists(chroot_image_path)
            logger.info(
                'FYI: Existence of the image in the chroot (%s): %s.',
                chroot_image_path,
                chroot_image_existence,
            )

            raise errors.InternalError(
                'Image does not exist in  %s.' % image_path
            )

    # The arguments --send-payload-in-parallel and --no-copy-payloads-to-device
    # have been deprecated. Please see
    # https://chromium.googlesource.com/chromiumos/chromite/+/9a0199fd48fb9466153d8598dc8602b26c9d21ba
    # https://chromium.googlesource.com/chromiumos/chromite/+/9ed30bc3ed292b02d85fde89c64207484b7a3aa4
    args = ['--debug', '--no-ping', host, image_path]
    if clobber_stateful:
        args.append('--clobber-stateful')
        args.append('--clear-tpm-owner')
    if disable_rootfs_verification:
        args.append('--disable-rootfs-verification')

    try:
        chromite_util.check_output('cros', 'flash', *args, cwd=chromeos_root)
        wait_kernel_ready(host)
    except subprocess.CalledProcessError as e:
        raise errors.ExternalError('cros flash failed') from e
    return True


def provision_image_with_retry(
    chromeos_root,
    host,
    board,
    image_info,
    is_public_build,
    version=None,
    clobber_stateful=False,
    disable_rootfs_verification=False,
    repair_callback=None,
    force_reboot_callback: typing.Callable[[str], bool] | None = None,
):
    # 'cros flash' is not 100% reliable, retry if necessary.
    retry = 2
    for attempt in range(retry):
        if attempt > 0:
            logger.info('will retry 60 seconds later')
            time.sleep(60)

        try:
            provision_image(
                chromeos_root,
                host,
                board,
                image_info,
                is_public_build=is_public_build,
                version=version,
                clobber_stateful=clobber_stateful,
                disable_rootfs_verification=disable_rootfs_verification,
                force_reboot_callback=force_reboot_callback,
            )
            workaround_b183567529(
                host,
                board,
                version,
                force_reboot_callback=force_reboot_callback,
            )
            return
        except errors.ExternalError:
            logger.exception('cros flash failed')
            if repair_callback and not repair_callback(host):
                logger.warning('not repaired, assume it is harmless')
            if attempt < retry - 1:
                continue
            raise


def cros_setup_board(chromeos_root: str, board: str) -> None:
    """Wrapper for the `setup_board` command.

    Args:
      chromeos_root: ChromeOS tree root directory
      board: ChromeOS board name
    """
    with raise_if_chroot_recreation_needed() as stderr_callback:
        cros_sdk(
            chromeos_root,
            'setup_board',
            f'--board={board}',
            stderr_callback=stderr_callback,
        )


@contextlib.contextmanager
def cros_workon(
    chromeos_root: str, board: str, package: str
) -> typing.Iterator[None]:
    """Wrapper for the `cros workon` command.

    Args:
      chromeos_root: ChromeOS tree root directory
      board: ChromeOS board name
      package: package name
    """
    cros_sdk(
        chromeos_root, 'cros', 'workon', f'--board={board}', 'start', package
    )
    try:
        yield
    finally:
        cros_sdk(
            chromeos_root, 'cros', 'workon', f'--board={board}', 'stop', package
        )


def cros_emerge(chromeos_root: str, board: str, package: str) -> str:
    """Wrapper for the `emerge` command.

    Args:
      chromeos_root: ChromeOS tree root directory
      board: ChromeOS board name
      package: package name
    """
    return cros_sdk(chromeos_root, f'emerge-{board}', package)


def cros_deploy(chromeos_root: str, dut: str, package: str) -> str:
    """Wrapper for the `cros deploy` command.

    Args:
      chromeos_root: ChromeOS tree root directory
      dut: the DUT address
      package: package name
    """
    return cros_sdk(chromeos_root, 'cros', 'deploy', '--debug', dut, package)


def _fetch_metadata_version_info(
    board: str, cros_version: str, is_public_build: bool
) -> VersionInfo:
    """Query version info of given version of ChromeOS from metadata

    Args:
      board: ChromeOS board name
      cros_version: ChromeOS full version or snapshot version

    Returns:
      dict of component and version info, including (if available):
        buildbucket_id: Buildbucket id for the image build task
        cros_short_version: ChromeOS version
        cros_full_version: ChromeOS version
        milestone: milestone of ChromeOS
        cr_version: Chrome version
        android_build_id: Android build id
        android_branch: Android branch, in format like 'git_nyc-mr1-arc'
    """
    assert is_cros_full_version(cros_version) or is_cros_snapshot_version(
        cros_version
    )
    # Some boards may have only partial-metadata.json but no metadata.json.
    # e.g. caroline R60-9462.0.0
    # Let's try both.
    metadata_filenames = ['metadata.json', 'partial-metadata.json']
    info = VersionInfo()
    for gs_path in _iter_valid_prebuilt_gs_path(
        board, cros_version, is_public_build
    ):
        for metadata_filename in metadata_filenames:
            metadata_str = gs_util.cat(
                f'{gs_path}/{metadata_filename}', ignore_errors=True
            )
            if not metadata_str:
                continue
            metadata = json.loads(metadata_str)
            v = metadata['version']
            board_metadata = metadata['board-metadata'].get(board, {})
            info.cros_short_version = v['platform']
            info.cros_full_version = v['full']
            info.milestone = v['milestone']

            if metadata.get('buildbucket_id'):
                info.buildbucket_id = int(metadata['buildbucket_id'])
            if 'chrome' in v:
                info.cr_version = v['chrome']
            if 'android' in v:
                info.android_build_id = v['android']
            if 'android-target' in v:
                info.android_target = v['android-target']
            if 'android-branch' in v:  # this appears since R58-9317.0.0
                info.android_branch = v['android-branch']
            elif 'android-container-branch' in board_metadata:
                info.android_branch = v['android-container-branch']
        metadata_str = gs_util.cat(
            f'{gs_path}/build_report.json', ignore_errors=True
        )
        if not metadata_str:
            continue
        config = json.loads(metadata_str).get('config', {})
        branch = config.get('androidContainerBranch', {}).get('name')
        if branch:
            info.android_branch = branch
        target = config.get('androidContainerTarget', {}).get('name')
        if target:
            info.android_target = target

    return info


def fetch_buildbucket_version_info(
    buildbucket_id: int, cros_version: str
) -> VersionInfo:
    """Query version info of given version of ChromeOS from buildbucket

    Args:
      buildbucket_id: buildbucket id
      cros_version: ChromeOS full version or snapshot version

    Returns:
      dict of component and version info, including (if available):
        cros_short_version: ChromeOS version
        cros_full_version: ChromeOS version
        milestone: milestone of ChromeOS
        cr_version: Chrome version
        android_build_id: Android build id
        android_branch: Android branch, in format like 'git_nyc-mr1-arc'
    """
    assert is_cros_full_version(cros_version) or is_cros_snapshot_version(
        cros_version
    )
    api = buildbucket_util.BuildbucketApi()
    milestone, short_version = version_split(cros_version)
    data = api.get_build(int(buildbucket_id)).output.properties
    info = VersionInfo(
        milestone=milestone,
        cros_full_version=cros_version,
        cros_short_version=short_version,
    )
    if 'target_versions' in data:
        target_versions = json_format.MessageToDict(data['target_versions'])
        if target_versions.get('chromeVersion'):
            info.cr_version = target_versions['chromeVersion']
        if target_versions.get('androidVersion'):
            info.android_build_id = target_versions['androidVersion']
        if target_versions.get('androidBranchVersion'):
            info.android_branch = target_versions['androidBranchVersion']
        if target_versions.get('androidTargetVersion'):
            info.android_target = target_versions['androidTargetVersion']
    return info


def query_version_info(
    board: str, cros_version: str, is_public_build: bool
) -> VersionInfo:
    """Query subcomponents version info of given version of ChromeOS

    Args:
      board: ChromeOS board name
      cros_version: ChromeOS version number in short or full format

    Returns:
      dict of component and version info, including (if available):
        buildbucket_id: Buildbucket id for the image build task
        cros_short_version: ChromeOS version
        cros_full_version: ChromeOS version
        milestone: milestone of ChromeOS
        cr_version: Chrome version
        android_build_id: Android build id
        android_branch: Android branch, in format like 'git_nyc-mr1-arc'
    """
    if is_cros_short_version(cros_version):
        cros_version = version_to_full(board, cros_version)
    info = _fetch_metadata_version_info(
        board, cros_version, is_public_build=is_public_build
    )
    buildbucket_id = (
        info.buildbucket_id
        or SnapshotStore.query_snapshot_buildbucket_id(
            board, cros_version, is_public_build
        )
    )

    if buildbucket_id is None:
        return info

    buildbucket_version_info = fetch_buildbucket_version_info(
        buildbucket_id, cros_version
    )
    if info.has_conflict(buildbucket_version_info):
        logger.warning(
            'Version data has conflict:\n  metadata: %s\n  buildbucket: %s',
            info,
            buildbucket_version_info,
        )
    return VersionInfo().update(info).update(buildbucket_version_info)


def query_chrome_version(board, cros_version) -> str | None:
    """Queries chrome version of chromeos build.

    Args:
      board: ChromeOS board name
      cros_version: ChromeOS version number in short or full format

    Returns:
      Chrome version number
    """

    return query_version_info(
        board,
        cros_version,
        # Use the internal builds to look up the chrome version, since both
        # builds should have the same chrome version.
        is_public_build=False,
    ).cr_version


def query_android_build_id(board, cros_version) -> str | None:
    return query_version_info(
        board,
        cros_version,
        # Use the internal builds to look up the Android build, since only the
        # internal builds contain Android.
        is_public_build=False,
    ).android_build_id


def query_android_branch(board, cros_version) -> str | None:
    return query_version_info(
        board,
        cros_version,
        # Use the internal builds to look up the Android branch, since only the
        # internal builds contain Android.
        is_public_build=False,
    ).android_branch


def is_inside_chroot():
    """Returns True if we are inside chroot."""
    return os.path.exists('/etc/cros_chroot_version')


def convert_path_outside_chroot(chromeos_root, path):
    """Converts path in chroot to outside.

    Args:
      chromeos_root: chromeos tree root
      path: path inside chroot; support starting with '~/'

    Returns:
      The corresponding path outside chroot assuming the chroot is mounted
    """
    if path.startswith('~/'):
        path = path.replace('~', '/home/' + os.environ['USER'])
        # http://crrev/c/4522314
        # e.g. /home/user1/bisect-workdir/template/chromeos/out/home/user1
        return os.path.join(chromeos_root, 'out', path[1:])
    if path.startswith(('/build', '/tmp')):
        return os.path.join(chromeos_root, 'out', path[1:])
    assert '~' not in path, 'tilde (~) character is not fully supported'

    assert os.path.isabs(path)
    assert path[0] == os.sep
    return os.path.join(chromeos_root, 'chroot', path[1:])


def cros_sdk(
    chromeos_root: str,
    *args: str,
    replace: bool = False,
    update: bool = False,
    chrome_root: str | None = None,
    env: dict[str, str] | None = None,
    log_stdout: bool = True,
    stdin: typing.Any = None,
    stderr_callback: typing.Callable | None = None,
    goma_dir: str | None = None,
    forward_host: str = '',
) -> str:
    """Runs commands inside chromeos chroot.

    Args:
      chromeos_root: chromeos tree root
      *args: command to run
      replace: whether replace an existing chroot
      update: whether update an existing chroot
      chrome_root: pass to cros_sdk; mount this path into the SDK chroot
      env: (dict) environment variables for the command
      log_stdout: Whether write the stdout output of the child process to log.
      stdin: standard input file handle for the command
      stderr_callback: Callback function for stderr. Called once per line.
      goma_dir: Goma installed directory to mount into the chroot
      forward_host: forward the host to localhost:2222, leave empty if no need to
        forward.
    """
    envs = []
    if env:
        for k, v in env.items():
            assert re.match(r'^[A-Za-z_][A-Za-z0-9_]*$', k)
            envs.append('%s=%s' % (k, v))

    # Use --no-ns-pid to prevent cros_sdk change our pgid, otherwise subsequent
    # commands would be considered as background process.
    prefix = ['chromite/bin/cros_sdk', '--no-ns-pid']

    if chrome_root:
        prefix += ['--chrome-root', chrome_root]
    if goma_dir:
        prefix += ['--goma-dir', goma_dir]
    if replace:
        prefix += ['--replace']
    if update:
        prefix += ['--update']

    prefix += envs + ['--']

    # In addition to the output of command we are interested, cros_sdk may
    # generate its own messages. For example, chroot creation messages if we run
    # cros_sdk the first time.
    # This is the hack to run dummy command once, so we can get clean output for
    # the command we are interested.
    cmd = prefix + ['true']
    stderr_lines: list[str] = []
    try:
        chromite_util.check_output(
            *cmd,
            cwd=chromeos_root,
            stderr_callback=stderr_lines.append,
        )
    except subprocess.CalledProcessError as e:
        # Extract the last line of stderr if any, since it normally contains
        # the error message from `cros_sdk` command.
        error_message = stderr_lines[-1] if stderr_lines else ""
        logger.exception('cros_sdk init/update failed: %s', error_message)
        raise errors.InternalError(
            f'cros_sdk init/update failed: {error_message}'
        ) from e

    if not args:
        return ''
    cmd = prefix + list(args)
    if forward_host:
        with util.forward_ssh(forward_host, FORWARDED_DUT_PORT):
            return chromite_util.check_output(
                *cmd,
                cwd=chromeos_root,
                log_stdout=log_stdout,
                stdin=stdin,
                stderr_callback=stderr_callback,
            )
    else:
        return chromite_util.check_output(
            *cmd,
            cwd=chromeos_root,
            log_stdout=log_stdout,
            stdin=stdin,
            stderr_callback=stderr_callback,
        )


def mount_chroot(chromeos_root, replace=False):
    """Creates ChromeOS chroot if necessary.

    Args:
      chromeos_root: chromeos tree root
      replace: whether replace an existing chroot
    """

    # Please refer https://crrev.com/c/2515959 for the new mount behavior.
    cros_sdk(chromeos_root, replace=replace)

    path = convert_path_outside_chroot(chromeos_root, '/bin/ls')
    assert os.path.exists(path)


def copy_into_chroot(chromeos_root, src, dst, overwrite=True):
    """Copies file into chromeos chroot.

    The side effect is chroot created and mounted.

    Args:
      chromeos_root: chromeos tree root
      src: path outside chroot
      dst: path inside chroot
      overwrite: overwrite if dst already exists
    """
    mount_chroot(chromeos_root)
    src = os.path.expanduser(src)
    dst_outside = convert_path_outside_chroot(chromeos_root, dst)
    if not overwrite and os.path.exists(dst_outside):
        return

    # Haven't support directory or special files yet.
    assert os.path.isfile(src)
    assert os.path.isfile(dst_outside) or not os.path.exists(dst_outside)

    dirname = os.path.dirname(dst_outside)
    if not os.path.exists(dirname):
        os.makedirs(dirname)
    shutil.copy(src, dst_outside)


def _copy_template_files(src, dst):
    if not os.path.exists(src):
        return

    def copy_if_nonexistent(src, dst):
        if not os.path.exists(dst):
            shutil.copy2(src, dst)

    shutil.copytree(
        src, dst, dirs_exist_ok=True, copy_function=copy_if_nonexistent
    )


@plugin_util.patch
def get_autotest_shadow_config():
    return textwrap.dedent(
        """\
        # This file is created by src/platform/bisect-kit/bisect_kit/cros_util.py
        [CROS]
        enable_ssh_tunnel_for_servo: True
        enable_ssh_tunnel_for_chameleon: True
        enable_ssh_connection_for_devserver: True
        enable_ssh_tunnel_for_moblab: True
        """
    )


def override_autotest_config(autotest_dir):
    shadow_config_path = os.path.join(autotest_dir, 'shadow_config.ini')
    with open(shadow_config_path, 'w') as f:
        f.write(get_autotest_shadow_config())


def prepare_chroot(chromeos_root, replace=False):
    """Creates and prepares chromeos chroot.

    Args:
      chromeos_root: chromeos tree root
      replace: whether replace an existing chroot
    """
    mount_chroot(chromeos_root, replace=replace)

    # Work around b/149077936:
    # The creds file is copied into the chroot since 12866.0.0.
    # But earlier versions need this file as well because of cipd ACL change.
    creds_path = '~/.config/chrome_infra/auth/creds.json'
    chromeos_build = int(
        ChromeOSVersionParser.query_repo_chromeos_version(
            chromeos_root
        ).chromeos_build
    )
    if chromeos_build < 12866 and os.path.exists(
        os.path.expanduser(creds_path)
    ):
        copy_into_chroot(chromeos_root, creds_path, creds_path, overwrite=False)

    # quick-provision requires special config for autotest.
    override_autotest_config(os.path.join(chromeos_root, in_tree_autotest_dir))

    # The directory where chroot mount its home directory to.
    home_mount_point = os.path.join(
        chromeos_root, 'out', 'home', os.environ['USER']
    )

    # Copy optional configure files into the home directory inside chromeos
    # chroot. For example, quick-provision may need special ssh config.
    assert os.environ.get('USER')
    _copy_template_files(
        os.path.join(
            bisect_kit.BISECT_KIT_ROOT, 'cros_template_files', 'at_home'
        ),
        home_mount_point,
    )

    # Copy local ssh configs into chromeos root.
    for name in ['config', 'chromeos_ssh_proxy']:
        path = os.path.expanduser(os.path.join('~/.ssh', name))
        to_folder = os.path.join(home_mount_point, '.ssh')
        to_file = os.path.join(to_folder, name)
        if not os.path.exists(path):
            logger.warning('File %s not found, ignored copying to chroot', name)
            continue
        if os.path.isfile(to_file):
            os.remove(to_file)
        if os.path.isfile(to_folder):
            os.remove(to_folder)
        os.makedirs(to_folder, mode=0o700, exist_ok=True)
        shutil.copy(path, to_folder)

    # Copy ssh keys into chromeos root.
    for name in [
        'testing_rsa',
        'testing_rsa.pub',
        'partner_testing_rsa',
        'partner_testing_rsa.pub',
    ]:
        possible_paths = [
            os.path.join(chromeos_root, 'sshkeys', name),
            os.path.join(
                chromeos_root,
                'src',
                'private-overlays',
                'chromeos-overlay',
                'chromeos-base',
                'chromeos-ssh-testkeys',
                'files',
                name,
            ),
            os.path.join(
                chromeos_root,
                'src',
                'scripts',
                'mod_for_test_scripts',
                'ssh_keys',
                name,
            ),
            os.path.expanduser(os.path.join('~/.ssh', name)),
        ]
        to_folder = os.path.join(home_mount_point, '.ssh')
        to_file = os.path.join(to_folder, name)
        if os.path.isfile(to_file):
            os.remove(to_file)

        if not any(os.path.exists(x) for x in possible_paths):
            logger.warning('File %s not found, ignored copying to chroot', name)
        for path in possible_paths:
            if os.path.exists(path):
                shutil.copy(path, to_folder)
                break

    # Remove the write permission for private keys.
    for name in ['testing_rsa', 'partner_testing_rsa', 'chromeos_ssh_proxy']:
        util.check_call(
            'chmod',
            'o-r,g-r',
            os.path.join(
                home_mount_point,
                '.ssh',
                name,
            ),
        )

    # Workaround b/297481113 to setup config and credential files in chroot home
    # directory correctly.
    #
    # When entering the chroot by running "cros_sdk", it tried to move data
    # from "chroot/..." to "out/..." (b/265885353). Before
    # https://chromium-review.googlesource.com/c/chromiumos/chromite/+/4522314,
    # the "home" directory is not considered yet. That is, "chroot/home/..."
    # is left intact and "out/home/..." is not populated yet. In chroot, the
    # home directory is bound to "chroot/home/...".
    #
    # After that CL, "chroot/home/..." is moved to "out/home/..." when
    # entering the chroot. The chroot home directory is bound to
    # "out/home/..." instead.
    #
    # As a result, when switching local ChromeOS source tree to a specific
    # version, we must ensure
    # 1. If the version is older than that CL, either "chroot/home/..." has
    #    not been moved to "out/home/..." yet. Or we need to copy it back.
    # 2. If the version is newer than that CL, "chroot/home/..." will be moved
    #    to "out/home/..." (again) via "rsync -aXH --remove-source-files" by
    #    cros_sdk. We must ensure the operation is valid.
    #
    # We can remove this after we drop the support of bisecting CrOS versions
    # before R118-15588.0.0.
    #
    if os.path.exists(home_mount_point):
        legacy_home_mount_point = os.path.join(
            chromeos_root, 'chroot', 'home', os.environ['USER']
        )
        logging.info(
            'workaround b/297481113 by copying %s back to %s',
            home_mount_point,
            legacy_home_mount_point,
        )
        util.check_call(
            'sudo',
            'rsync',
            '-aHX',
            # rsync requires an extra '/' to get rid of the top level source
            # directory in the dest directory.
            home_mount_point + '/',
            legacy_home_mount_point,
        )


@contextlib.contextmanager
def raise_if_chroot_recreation_needed():
    stderr_lines: list[str] = []
    stderr_callback = stderr_lines.append
    try:
        yield stderr_callback
    except subprocess.CalledProcessError as e:
        # Detect failures due to incompatibility between chroot and source tree.
        # If so, notify the caller to recreate chroot and retry.
        if reason := get_chroot_recreation_reason(
            e.output, ''.join(stderr_lines)
        ):
            raise NeedRecreateChrootException(reason) from e

        # For other failures, don't know how to handle. Just bail out.
        raise


def recreate_chroot_and_retry_if_needed(chromeos_root: str):
    def _decorator(func):
        def _wrapped(*args, **kwargs):
            try:
                return func(*args, **kwargs)
            except NeedRecreateChrootException as e:
                logger.warning('recreate chroot and retry again, reason: %s', e)
                prepare_chroot(chromeos_root, replace=True)
                return func(*args, **kwargs)

        return _wrapped

    return _decorator


def get_chroot_recreation_reason(stdout: str, stderr: str) -> str | None:
    """Analyze build log and determine if chroot should be recreated.

    Args:
      stdout: stdout output of build
      stderr: stderr output of build

    Returns:
      the reason if chroot needs recreated; None otherwise
    """
    if re.search(
        r"The current version of portage supports EAPI '\d+'. "
        'You must upgrade',
        stderr,
    ):
        return 'EAPI version mismatch'

    if 'Chroot is too new. Consider running:' in stderr:
        return 'chroot version is too new'

    # old message before Oct 2018
    if (
        'Chroot version is too new. Consider running cros_sdk --replace'
        in stderr
    ):
        return 'chroot version is too new'

    # https://groups.google.com/a/chromium.org/forum/#!msg/chromium-os-dev/uzwT5APspB4/NFakFyCIDwAJ
    if "undefined reference to 'std::__1::basic_string" in stdout:
        return 'might be due to compiler change'

    # Detect failures due to file collisions.
    # For example, kernel uprev from 3.x to 4.x, they are two separate packages
    # and conflict with each other. Other possible cases are package renaming or
    # refactoring. Let's recreate chroot to work around them.
    if 'Detected file collision' in stdout:
        # Using wildcard between words because the text wraps to the next line
        # depending on length of package name and each line is prefixed with
        # package name.
        # Using ".{,100}" instead of ".*" to prevent regex matching time explodes
        # exponentially. 100 is chosen arbitrarily. It should be longer than any
        # package name (65 now).
        m = re.search(
            r'Package (\S+).{,100}NOT.{,100}merged.{,100}'
            r'due.{,100}to.{,100}file.{,100}collisions',
            stdout,
            re.S,
        )
        if m:
            return (
                'failed to install package due to file collision: ' + m.group(1)
            )

    return None


def build_packages(
    chromeos_root,
    board,
    is_public_build,
    chrome_root=None,
    goma_dir=None,
    afdo_use=False,
):
    """Build ChromeOS packages.

    Args:
      chromeos_root: chromeos tree root
      board: ChromeOS board name
      is_public_build: Whether this is a public build.
      chrome_root: Chrome tree root. If specified, build chrome using the provided
        tree
      goma_dir: Goma installed directory to mount into the chroot. If specified,
        build chrome with goma.
      afdo_use: build chrome with AFDO optimization
    """
    use_flags = ['-cros-debug']
    if not is_public_build:
        use_flags.append('chrome_internal')

    common_env = {
        'USE': ' '.join(use_flags),
        'FEATURES': 'separatedebug',
    }
    with raise_if_chroot_recreation_needed() as stderr_callback:
        with locking.lock_file(locking.LOCK_FILE_FOR_BUILD):
            env = common_env.copy()
            env['FEATURES'] += ' -separatedebug splitdebug'

            env = common_env.copy()
            cmd = [
                'cros',
                'build-packages',
                '--board',
                board,
                '--withdev',
                '--no-workon',
                '--skip-chroot-upgrade',
                '--accept-licenses=@CHROMEOS',
                # `use_any_chrome` flag is default on and will force to use a
                # chrome prebuilt even if the version doesn't match.
                '--no-use-any-chrome',
            ]

            if goma_dir:
                # Tell build_packages to start and stop goma
                cmd.append('--run-goma')
                env['USE_GOMA'] = 'true'
            if afdo_use:
                env['USE'] += ' afdo_use'
            cros_sdk(
                chromeos_root,
                *cmd,
                env=env,
                chrome_root=chrome_root,
                stderr_callback=stderr_callback,
                goma_dir=goma_dir,
                update=True,
            )


def build_image(chromeos_root, board, is_public_build):
    """Build ChromeOS image.

    Args:
      chromeos_root: chromeos tree root
      board: ChromeOS board name
      is_public_build: Whether this is a public build.

    Returns:
      image folder; relative to chromeos_root
    """
    use_flags = ['-cros-debug']
    if not is_public_build:
        use_flags.append('chrome_internal')

    env = {
        'USE': ' '.join(use_flags),
        'FEATURES': 'separatedebug',
    }

    with raise_if_chroot_recreation_needed() as stderr_callback:
        with locking.lock_file(locking.LOCK_FILE_FOR_BUILD):
            cros_sdk(
                chromeos_root,
                'cros',
                'build-image',
                '--board',
                board,
                '--no-enable-rootfs-verification',
                'test',
                env=env,
                stderr_callback=stderr_callback,
            )

    image_symlink = os.path.join(
        chromeos_root, build_images_dir, board, 'latest'
    )
    assert os.path.exists(image_symlink)
    image_name = os.readlink(image_symlink)
    image_folder = os.path.join(build_images_dir, board, image_name)
    assert os.path.exists(
        os.path.join(chromeos_root, image_folder, test_image_filename)
    )
    return image_folder


def workaround_b183567529(
    host,
    board,
    version=None,
    force_reboot_callback: typing.Callable[[str], bool] | None = None,
):
    """Workaround for volteer failure.

    See b/183567529#comment8 and b/183020319#comment26 for more details.
    """
    broken_range = [
        ('13836.0.0', '13854.0.0'),
        ('13816.13.0', '13816.19.0'),
    ]
    if board != 'volteer' or not version:
        return

    if is_cros_short_version(version):
        short_version = version
    else:
        _, short_version = version_split(version)
    for old, new in broken_range:
        if (
            util.is_version_lesseq(old, short_version)
            and util.is_version_lesseq(short_version, new)
            and (
                util.is_direct_relative_version(old, short_version)
                and util.is_direct_relative_version(short_version, new)
            )
        ):
            logger.info('applying b183567529 cbi patch for volteer')
            cbi_override = os.path.join(
                bisect_kit.BISECT_KIT_ROOT,
                'patching/b183567529/volteer-cbi-override.conf',
            )
            util.scp_cmd(cbi_override, 'root@%s:/etc/init/' % host)

            patch_script = os.path.join(
                bisect_kit.BISECT_KIT_ROOT,
                'patching/b183567529/test_cbi_script.sh',
            )
            util.check_output(patch_script, host)
            reboot(host, force_reboot_callback)
            break


# TODO(b/378126412): Remove this function after CrOS version '16083.0.0' to
# '16089.0.0' are no longer active.
def workaround_b378126412(
    host,
    version,
    force_reboot_callback: typing.Callable[[str], bool] | None = None,
):
    """Workaround for python path failure.

    The python path was broken in the build range after cros flash.
    A simple reboot resolved it somehow.
    """
    broken_range = [
        ('16083.0.0', '16089.0.0'),
    ]

    if version:
        if is_cros_short_version(version):
            short_version = version
        else:
            _, short_version = version_split(version)
    else:
        short_version = None

    for old, new in broken_range:
        if short_version is None or (
            util.is_version_lesseq(old, short_version)
            and util.is_version_lesseq(short_version, new)
            and (
                util.is_direct_relative_version(old, short_version)
                and util.is_direct_relative_version(short_version, new)
            )
        ):
            logger.info(
                'CrOS version %s: reboot so the python path is set correctly (b/378126412).',
                version,
            )
            reboot(host, force_reboot_callback)
            break


class AutotestControlInfo:
    """Parsed content of autotest control file.

    Attributes:
      name: test name
      path: control file path
      variables: dict of top-level control variables. Sample keys: NAME, AUTHOR,
        DOC, ATTRIBUTES, DEPENDENCIES, etc.
    """

    def __init__(self, path, variables):
        assert 'NAME' in variables, 'invalid control file'
        self.name = variables['NAME']
        self.path = path
        self.variables = variables


def parse_autotest_control_file(path):
    """Parses autotest control file.

    This only parses simple top-level string assignments.

    Returns:
      AutotestControlInfo object
    """
    variables = {}
    with open(path) as f:
        code = ast.parse(f.read())
    for stmt in code.body:
        # Skip if not simple "NAME = *" assignment.
        if not (
            isinstance(stmt, ast.Assign)
            and len(stmt.targets) == 1
            and isinstance(stmt.targets[0], ast.Name)
        ):
            continue

        # Only support string value.
        if isinstance(stmt.value, ast.Str):
            variables[stmt.targets[0].id] = stmt.value.s

    return AutotestControlInfo(path, variables)


def enumerate_autotest_control_files(autotest_dir):
    """Enumerate autotest control files.

    Args:
      autotest_dir: autotest folder

    Returns:
      list of paths to control files
    """
    # Where to find control files. Relative to autotest_dir.
    subpaths = [
        'server/site_tests',
        'client/site_tests',
        'server/tests',
        'client/tests',
    ]

    denylist = ['site-packages', 'venv', 'results', 'logs', 'containers']
    result = []
    for subpath in subpaths:
        path = os.path.join(autotest_dir, subpath)
        for root, dirs, files in os.walk(path):
            for deny in denylist:
                if deny in dirs:
                    dirs.remove(deny)

            for filename in files:
                if filename == 'control' or filename.startswith('control.'):
                    result.append(os.path.join(root, filename))

    return result


def get_autotest_test_info(autotest_dir, test_name):
    """Get metadata of given test.

    Args:
      autotest_dir: autotest folder
      test_name: test name

    Returns:
      AutotestControlInfo object. None if test not found.
    """
    for control_file in enumerate_autotest_control_files(autotest_dir):
        try:
            info = parse_autotest_control_file(control_file)
        except SyntaxError:
            logger.warning('%s is not parsable, ignore', control_file)
            continue

        if info.name == test_name:
            return info
    return None


def _get_overlay_name(overlay):
    path = os.path.join(overlay, 'metadata', 'layout.conf')
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                m = re.search(r'repo-name\s*=\s*(\S+)', line)
                if m:
                    return m.group(1)

    path = os.path.join(overlay, 'profiles', 'repo_name')
    if os.path.exists(path):
        with open(path) as f:
            return f.readline().rstrip()

    return None


def parse_chromeos_overlays(chromeos_root: str) -> dict[str, list[str]]:
    # ref: chromite's lib/portage_util.py ListOverlays().
    overlays = {}
    paths = ['src/overlays', 'src/private-overlays']

    for path in paths:
        path = os.path.join(chromeos_root, path, 'overlay-*')
        for overlay in sorted(glob.glob(path)):
            name = _get_overlay_name(overlay)
            if not name:
                continue

            path = os.path.join(overlay, 'metadata', 'layout.conf')
            masters: list[str] = []
            if os.path.exists(path):
                with open(path) as f:
                    for line in f:
                        m = re.search(r'masters\s*=(.*)', line)
                        if m:
                            masters = m.group(1).split()
            overlays[name] = masters
    return overlays


def resolve_basic_boards(overlays: dict[str, list[str]]) -> str:
    def normalize(name: str) -> str:
        return name.replace('-private', '')

    def resolve(name: str) -> set[str]:
        # Special cases which have the parents but treated as basic boards.
        normalized_name = normalize(name)
        if normalized_name in ['nirva']:
            return {normalized_name}

        result = set()
        for parent in overlays[name]:
            assert parent != name, 'recursive overlays definition?'
            if parent not in overlays:
                continue
            for basic in resolve(parent):
                result.add(basic)
        if not result:
            result.add(name)
        return set(map(normalize, result))

    result = {}
    for name in overlays:
        board = normalize(name)
        basic = resolve(name)
        assert len(basic) == 1
        basic_board = basic.pop()
        result[board] = basic_board
    return result


def detect_branch_level(branch):
    """Given a branch name of manifest-internal, detect it's branch level.

    level1: if ChromeOS version is x.0.0
    level2: if ChromeOS version is x.x.0
    level3: if ChromeOS version is x.x.x
    Where x is an non-zero integer.

    Args:
      branch: branch name or ref name in manifest-internal

    Returns:
      An integer indicates the branch level, or zero if not detectable.
    """
    level1 = r'^(refs\/\S+(\/\S+)?/)?master$'
    level2 = r'^\S+-(\d+)(\.0)?\.B$'
    level3 = r'^\S+-(\d+)\.(\d+)(\.0)?\.B$'

    if re.match(level1, branch):
        return 1
    if re.match(level2, branch):
        return 2
    if re.match(level3, branch):
        return 3
    return 0


def get_crosland_link(old, new):
    """Generates crosland link between two versions.

    Args:
      old: ChromeOS version
      new: ChromeOS version

    Returns:
      A crosland url.
    """

    def version_to_url_parameter(ver):
        if is_cros_snapshot_version(ver):
            return snapshot_version_split(ver)[2]
        return version_to_short(ver)

    old_parameter = version_to_url_parameter(old)
    new_parameter = version_to_url_parameter(new)
    return CROSLAND_URL_TEMPLATE % (old_parameter, new_parameter)


class ChromeOSSpecManager(codechange.SpecManager):
    """Repo manifest related operations.

    This class enumerates chromeos manifest files, parses them,
    and sync to disk state according to them.
    """

    def __init__(self, config: dict):
        self.config = config
        self.manifest_dir = os.path.join(
            self.config.get('chromeos_root'), '.repo', 'manifests'
        )
        self.manifest_external_dir = os.path.join(
            self.config.get('chromeos_mirror'), 'chromiumos/manifest.git'
        )
        self.manifest_internal_dir = os.path.join(
            self.config.get('chromeos_mirror'), 'manifest-internal.git'
        )
        self.historical_manifest_git_dir = os.path.join(
            self.config.get('chromeos_mirror'), 'chromeos/manifest-versions.git'
        )
        self.chromiumos_overlay_git_dir = os.path.join(
            self.config.get('chromeos_mirror'),
            'chromiumos/overlays/chromiumos-overlay.git',
        )
        self.is_public_build = self.config.get('is_public_build', False)
        self.historical_manifest_branch_name = 'refs/heads/main'
        if not os.path.exists(self.historical_manifest_git_dir):
            raise errors.InternalError(
                'Manifest snapshots should be cloned into %s'
                % self.historical_manifest_git_dir
            )

    def lookup_snapshot_manifest_revisions(
        self, old: str, new: str
    ) -> list[tuple[int, str, str]]:
        """Get manifest commits between snapshot versions.

        Returns:
          list of (timestamp, commit_id, snapshot_id):
            timestamp: integer unix timestamp
            commit_id: a string indicates commit hash
            snapshot_id: a string indicates snapshot id
        """
        assert is_cros_snapshot_version(old)
        assert is_cros_snapshot_version(new)

        # Try to guess the commit time of a snapshot manifest, it is usually a few
        # minutes different between snapshot manifest commit and image.zip
        # generate.
        old_timestamp = None
        new_timestamp = None
        old_path = SnapshotStore.query_snapshot_image(
            self.config.get('board'), old, self.is_public_build
        )
        new_path = SnapshotStore.query_snapshot_image(
            self.config.get('board'), new, self.is_public_build
        )
        if old_path:
            old_timestamp = gs_util.stat_max_creation_time(old_path) - 86400
        if new_path:
            new_timestamp = gs_util.stat_max_creation_time(new_path) + 86400
            # 1558657989 is snapshot_id 5982's commit time, this ensures every time
            # we can find snapshot 5982
            # snapshot_id <= 5982 has different commit message format, so we need
            # to identify its id in different ways, see below comment for more info.
            new_timestamp = max(new_timestamp, 1558657989 + 1)

        result = []
        _, _, old_snapshot_id = snapshot_version_split(old)
        _, _, new_snapshot_id = snapshot_version_split(new)
        repo = (
            self.manifest_external_dir
            if self.is_public_build
            else self.manifest_internal_dir
        )
        path = 'snapshot.xml'
        branch = 'snapshot'
        commits = git_util.get_history(
            repo,
            path,
            branch,
            after=old_timestamp,
            before=new_timestamp,
            with_subject=True,
        )

        # Unfortunately, we can not identify snapshot_id <= 5982 from its commit
        # subject, as their subjects are all `Annealing manifest snapshot.`.
        # So instead we count the snapshot_id manually.
        count = 5982
        # There are two snapshot_id = 2633 in commit history, ignore the former
        # one.
        ignore_list = ['95c8526a7f0798d02f692010669dcbd5a152439a']
        # We examine the commits in reverse order as there are some testing
        # commits before snapshot_id=2, this method works fine after
        # snapshot 2, except snapshot 2633
        for commit in reversed(commits):
            msg = commit.subject
            if commit.rev in ignore_list:
                continue

            match = re.match(r'^annealing manifest snapshot (\d+)', msg)
            if match:
                snapshot_id = match.group(1)
            elif (
                not self.is_public_build
            ) and 'Annealing manifest snapshot' in msg:
                snapshot_id = str(count)
                count -= 1
            else:
                continue
            # b/151054108: snapshot version in [29288, 29439] is broken
            if (not self.is_public_build) and (
                29288 <= int(snapshot_id) <= 29439
            ):
                continue
            if int(old_snapshot_id) <= int(snapshot_id) <= int(new_snapshot_id):
                result.append((commit.timestamp, commit.rev, snapshot_id))
        # We find commits in reversed order, now reverse it again to chronological
        # order.
        return list(reversed(result))

    def lookup_build_timestamp(self, rev: str) -> int:
        assert is_cros_full_version(rev) or is_cros_snapshot_version(rev)
        if is_cros_full_version(rev):
            return self.lookup_release_build_timestamp(rev)
        return self.lookup_snapshot_build_timestamp(rev)

    def lookup_snapshot_build_timestamp(self, rev) -> int:
        assert is_cros_snapshot_version(rev)
        return int(self.lookup_snapshot_manifest_revisions(rev, rev)[0][0])

    def lookup_release_build_timestamp(self, rev: str) -> int:
        assert is_cros_full_version(rev)
        milestone, short_version = version_split(rev)
        path = os.path.join('buildspecs', milestone, short_version + '.xml')

        if should_fetch_release_manifest_from_gs(rev):
            timestamp = self.query_gs_release_manifest_timestamp(path)
        else:
            # b/224575508: timestamp for older versions on gsbucket may be incorrect,
            # use timestamp from git repository instead
            try:
                timestamp = git_util.get_commit_time(
                    self.historical_manifest_git_dir,
                    self.historical_manifest_branch_name,
                    path,
                )
            except ValueError as e:
                raise errors.InternalError(
                    '%s does not have %s'
                    % (self.historical_manifest_git_dir, path)
                ) from e
        return timestamp

    def detect_float_spec_branch_level(self, spec: codechange.Spec) -> int:
        results = [
            detect_branch_level(branch)
            for branch in git_util.get_branches(
                self.manifest_dir, commit=spec.name
            )
        ]
        results = [x for x in results if x > 0]
        return min(results) if results else 0

    def branch_between_float_specs(
        self, old_spec: codechange.Spec, new_spec: codechange.Spec
    ) -> bool:
        if not old_spec.is_float():
            return False
        if not new_spec.is_float():
            return False

        level_old = self.detect_float_spec_branch_level(old_spec)
        level_new = self.detect_float_spec_branch_level(new_spec)

        if not level_old or not level_new:
            logger.warning('branch level detect failed, assume not branched')
            return False
        return level_old != level_new

    def _determine_float_branch(
        self, old: str, new: str, fixed_specs: list[codechange.Spec]
    ) -> str:
        # There is no revision tag in snapshot's xml. We know snapshot
        # builds are on main branch.
        main_refname = 'refs/remotes/origin/main'
        if fixed_specs[0].revision:
            old_branches = git_util.get_branches(
                self.manifest_dir, commit=fixed_specs[0].revision, remote=True
            )
        else:
            old_branches = [main_refname]

        if fixed_specs[-1].revision:
            new_branches = git_util.get_branches(
                self.manifest_dir, commit=fixed_specs[-1].revision, remote=True
            )
        else:
            new_branches = [main_refname]

        common_branches = list(set(old_branches) & set(new_branches))
        assert common_branches, '%s and %s are not on common branches?' % (
            old,
            new,
        )

        if len(common_branches) == 1:
            return common_branches[0]

        # There are more than one common branches, use heuristic to tie breaking.
        # The heuristic is simple: choice the branch with "smallest" number.
        # "Smaller" means the more major branch (not branched) or branched later.
        #
        # Following is the commit graph of manifest-internal repo. It shows many
        # interesting cases.
        #
        #          84/13021.0.0   84/13022.0.0   84/13024.0.0
        # --A--+---X--------------X------B-------X-----------> master
        #       \
        #        \    83/13020.1.0    83/13020.56.0    83/13020.68.0
        #         C---X----D--+-------X-------+--------X-----> release-R83-13020.B
        #                      \               \
        #                       \               E------------> stabilize-13020.67.B
        #                        \      83/13020.55.1
        #                         F-----X--------------------> stabilize-13020.55.B
        #
        # How to read this graph:
        #  - Time goes from left to right. Branch names are on the right side of
        #    arrows.
        #  - Letters A-F are manifest commits.
        #  - Marker X means release image build at that time, the version numbers
        #    are labeled above the X marker.
        # For example,
        #  1) 13021.0.0 release is based on manifest A, which is on all branches
        #     shown on the graph.
        #     We know 13021.0.0 is on master (and R84 branch later, not shown in
        #     this graph), not on 13020* branches.
        #  2) 13020.56.0 release is based on manifest D, which is on 3 branches
        #     (R83-13020.B, 13020.67.B, and 13020.55.B).
        #     We know 13020.56.0 is on R83-13020.B and 13020.67.B, but not
        #     13020.55.B.
        #
        # There is an important property here. Every time a new branch is created,
        # there will always be a commit (like C, E, and F) to fix "revision" field
        # in the manifest file. In other words, xxxxx.1.0 is impossible based on
        # manifest on master branch. xxxxx.yy.1 is impossible based on manifest on
        # xxxxx.B branch.
        #
        # With such property, among the branches containing the given manifest
        # file, the branch with "smallest" number guarantees where the release is.

        def branch_key(s) -> tuple:
            if s == main_refname:
                return 0, 0, 0
            m = re.search(r'-(\d+)\.B$', s)
            if m:
                return int(m.group(1)), 0, 0
            m = re.search(r'-(\d+)\.(\d+)\.B$', s)
            if m:
                return int(m.group(1)), int(m.group(2)), 0
            m = re.search(r'-(\d+)\.(\d+)\.(\d+)\.B$', s)
            if m:
                return int(m.group(1)), int(m.group(2)), int(m.group(3))

            logger.warning('unexpected branch name: %s', s)
            return (sys.maxsize, sys.maxsize, sys.maxsize, s)

        common_branches.sort(key=branch_key)
        return common_branches[0]

    def collect_float_spec(
        self,
        old: str,
        new: str,
        fixed_specs: list[codechange.Spec] | None = None,
    ) -> list[codechange.Spec]:
        assert fixed_specs
        branch = self._determine_float_branch(old, new, fixed_specs)
        logger.debug('float branch=%s', branch)

        old_timestamp = self.lookup_build_timestamp(old)
        new_timestamp = self.lookup_build_timestamp(new)
        # snapshot time is different from commit time
        # usually it's a few minutes different
        # 30 minutes should be safe in most cases
        if is_cros_snapshot_version(old):
            old_timestamp = old_timestamp - 1800
        if is_cros_snapshot_version(new):
            new_timestamp = new_timestamp + 1800

        # TODO(zjchang): add logic to combine symlink target's (full.xml) history
        path = 'default.xml'
        parser = repo_util.ManifestParser(self.manifest_dir)
        commits = parser.enumerate_manifest_commits(
            old_timestamp, new_timestamp, path, branch=branch
        )
        return [
            codechange.Spec.new_float(commit.rev, commit.timestamp, path)
            for commit in commits
        ]

    def collect_fixed_spec(self, old: str, new: str) -> list[codechange.Spec]:
        assert is_cros_full_version(old) or is_cros_snapshot_version(old)
        assert is_cros_full_version(new) or is_cros_snapshot_version(new)

        # case 1: if both are snapshot, return a list of snapshot
        if is_cros_snapshot_version(old) and is_cros_snapshot_version(new):
            return self.collect_snapshot_specs(old, new)

        # case 2: if both are release version
        #         return a list of release version
        if is_cros_full_version(old) and is_cros_full_version(new):
            return self.collect_release_specs(old, new)

        # case 3: return a list of release version and append a snapshot
        #         before or at the end
        result = self.collect_release_specs(
            version_to_full(self.config.get('board'), old),
            version_to_full(self.config.get('board'), new),
            trim_old=is_cros_snapshot_version(old),
        )
        if is_cros_snapshot_version(old):
            result = self.collect_snapshot_specs(old, old) + result
        elif is_cros_snapshot_version(new):
            result += self.collect_snapshot_specs(new, new)
        return result

    def collect_snapshot_specs(
        self, old: str, new: str
    ) -> list[codechange.Spec]:
        assert is_cros_snapshot_version(old)
        assert is_cros_snapshot_version(new)

        result = []
        path = 'snapshot.xml'
        revisions = self.lookup_snapshot_manifest_revisions(old, new)
        for timestamp, _git_rev, snapshot_id in revisions:
            snapshot_version = SnapshotStore.query_snapshot_version_by_id(
                self.config.get('board'),
                snapshot_id,
                is_public_build=self.is_public_build,
            )
            if snapshot_version:
                result.append(
                    codechange.Spec.new_fixed(snapshot_version, timestamp, path)
                )
            else:
                logger.warning(
                    'snapshot id %s is not found, ignore', snapshot_id
                )
        return result

    def list_release_milestones(self) -> list[str]:
        path = '%s%s' % (gs_manifest_base, 'buildspecs')
        return [x.removeprefix(path).strip('/') for x in gs_util.ls(path)]

    def list_release_manifests(self, milestone: str) -> list[str]:
        if int(milestone) < gs_manifest_cutoff_milestone:
            return git_util.list_dir_from_revision(
                self.historical_manifest_git_dir,
                self.historical_manifest_branch_name,
                os.path.join('buildspecs', milestone),
            )

        path = '%s%s/%s' % (gs_manifest_base, 'buildspecs', milestone)
        return [x.removeprefix(path).lstrip('/') for x in gs_util.ls(path)]

    def get_gs_release_manifest(self, path: str) -> str:
        """Query the manifest content of a release version on gs."""
        return gs_util.cat(f'{gs_manifest_base}{path}')

    def query_gs_release_manifest_timestamp(self, path: str) -> int:
        """Query the timestamp of a release version on gs."""
        return gs_util.stat_max_creation_time(f'{gs_manifest_base}{path}')

    def query_release_manifest_timestamp(self, full_version: str) -> int:
        assert is_cros_full_version(full_version)

        if should_fetch_release_manifest_from_gs(full_version):
            milestone, short_version = version_split(full_version)
            path = gs_manifest_path.format(
                milestone=milestone, short_version=short_version
            )
            return gs_util.stat_max_creation_time(path)
        return git_util.get_commit_time(
            self.historical_manifest_git_dir,
            self.historical_manifest_branch_name,
            '%s.xml' % full_version,
        )

    def collect_release_specs(
        self, old: str, new: str, trim_old: bool = False
    ) -> list[codechange.Spec]:
        """Collects the release buildspecs between two version.

        Args:
            old: old ChromeOS full version
            new: new ChromeOS full version
            trim_old: returns the manifest in semi-open range (old, new] and
                tolerates if old doesn't exists.

        Returns:
            A list of codechange.Spec.
        """
        assert is_cros_full_version(old)
        assert is_cros_full_version(new)
        old_milestone, old_short_version = version_split(old)
        new_milestone, new_short_version = version_split(new)

        # Sometimes older ChromeOS version has newer milestone number (b/260532030)
        if old_milestone > new_milestone:
            old_milestone, new_milestone = new_milestone, old_milestone

        result = []
        for milestone in self.list_release_milestones():
            if not milestone.isdigit():
                continue
            if not int(old_milestone) <= int(milestone) <= int(new_milestone):
                continue

            files = self.list_release_manifests(milestone)

            for fn in files:
                path = os.path.join('buildspecs', milestone, fn)
                short_version, ext = os.path.splitext(fn)
                if ext != '.xml':
                    continue
                if (
                    util.is_version_lesseq(old_short_version, short_version)
                    and util.is_version_lesseq(short_version, new_short_version)
                    and util.is_direct_relative_version(
                        short_version, new_short_version
                    )
                ):
                    rev = make_cros_full_version(milestone, short_version)
                    timestamp = self.query_release_manifest_timestamp(rev)
                    result.append(
                        codechange.Spec.new_fixed(rev, timestamp, path)
                    )

        def version_key_func(spec: codechange.Spec):
            _milestone, short_version = version_split(spec.name)
            return util.version_key_func(short_version)

        result.sort(key=version_key_func)
        if trim_old and result[0].name == old:
            result = result[1:]
        if not trim_old:
            assert result[0].name == old
        assert result[-1].name == new
        return result

    def get_manifest(self, rev: str) -> str:
        assert is_cros_full_version(rev) or is_cros_snapshot_version(rev)
        if is_cros_full_version(rev):
            milestone, short_version = version_split(rev)
            path = os.path.join(
                'buildspecs', milestone, '%s.xml' % short_version
            )
            if should_fetch_release_manifest_from_gs(rev):
                manifest = self.get_gs_release_manifest(path)
            else:
                manifest = git_util.get_file_from_revision(
                    self.historical_manifest_git_dir,
                    self.historical_manifest_branch_name,
                    path,
                )
        else:
            revisions = self.lookup_snapshot_manifest_revisions(rev, rev)
            commit_id = revisions[0][1]
            manifest = git_util.get_file_from_revision(
                (
                    self.manifest_external_dir
                    if self.is_public_build
                    else self.manifest_internal_dir
                ),
                commit_id,
                'snapshot.xml',
            )
        return manifest

    def get_manifest_file(self, rev: str) -> str:
        assert is_cros_full_version(rev) or is_cros_snapshot_version(rev)
        manifest_name = 'manifest_%s.xml' % rev
        manifest_path = os.path.join(self.manifest_dir, manifest_name)
        with open(manifest_path, 'w') as f:
            f.write(self.get_manifest(rev))

        # workaround for b/150572399
        # for chromeOS version < 12931.0.0, manifests are included from incorrect
        # folder .repo instead of.repo/manifests
        if is_cros_version_lesseq(rev, '12931.0.0'):
            repo_path = os.path.join(self.config.get('chromeos_root'), '.repo')
            manifest_patch_path = os.path.join(repo_path, manifest_name)
            with open(manifest_patch_path, 'w') as f:
                f.write(self.get_manifest(rev))

        return manifest_name

    def parse_spec(self, spec: codechange.Spec) -> None:
        parser = repo_util.ManifestParser(self.manifest_dir)
        if spec.is_fixed():
            manifest_name = self.get_manifest_file(spec.name)
            manifest_path = os.path.join(self.manifest_dir, manifest_name)
            with open(manifest_path) as f:
                content = f.read()
            root = parser.parse_single_xml(content, allow_include=False)
        else:
            root = parser.parse_xml_recursive(spec.name, spec.path)

        spec.entries = parser.process_parsed_result(root)
        if spec.is_fixed() and not spec.is_static():
            raise ValueError(
                f'fixed spec {spec.name!r} has unexpected floating entries'
            )
        spec.revision = root.get('revision')

        # (b/257863941) If the root tag is lack of revision information, try to
        # get it from the manifest-internal project tag.
        if spec.revision is None:
            for subnode in root:
                if (
                    subnode.tag == 'project'
                    and subnode.get('name') == 'chromeos/manifest-internal'
                ):
                    spec.revision = subnode.get('revision')
                    break

    def sync_disk_state(self, rev: str) -> None:
        manifest_name = self.get_manifest_file(rev)

        manifest_url = (
            PUBLIC_MANIFEST_REPO_URL
            if self.is_public_build
            else INTERNAL_MANIFEST_REPO_URL
        )

        if self.is_public_build:
            groups = 'all'
        else:
            # Probably we can use "all" even for the internal builds.
            # TODO(yoshiki): Confirm 'all' works and replace it.
            # b/150753074: moblab is in non-default group and causes mark_as_stable
            # fail
            # b/264611730: satlab is also necessary for mark_as_stable
            groups = 'default,moblab,satlab,platform-linux'

        # For ChromeOS, mark_as_stable step requires 'repo init -m', which sticks
        # manifest. 'repo sync -m' is not enough
        repo_util.init(
            self.config.get('chromeos_root'),
            manifest_url,
            manifest_name=manifest_name,
            repo_url='https://gerrit.googlesource.com/git-repo',
            reference=self.config.get('chromeos_mirror'),
            groups=groups,
        )

        # Note, don't sync with current_branch=True for chromeos. One of its
        # build steps (inside mark_as_stable) executes "git describe" which
        # needs git tag information.
        repo_util.sync(self.config.get('chromeos_root'))

    def lookup_chromeos_version(
        self, version
    ) -> typing.Optional[ChromeOSVersion]:
        """Lookup ChromeOS version info from overlays history.

        Args:
            short_version: ChromeOS version.

        Returns:
            ChromeOS version info, or None if not found.
        """
        chromeos_version_path = 'chromeos/config/chromeos_version.sh'
        short_version = version_to_short(version)

        logs = git_util.get_history(
            self.chromiumos_overlay_git_dir,
            chromeos_version_path,
            all_branch=True,
            with_subject=True,
        )
        for log in logs:
            if re.match(
                rf'^Increment(ed)? to version.+{short_version}.*', log.subject
            ):
                return ChromeOSVersionParser.parse_chromeos_version(
                    git_util.get_file_from_revision(
                        self.chromiumos_overlay_git_dir,
                        log.rev,
                        chromeos_version_path,
                    )
                )
        return None


@contextlib.contextmanager
def _prepare_results_stores(src: str, dst: str):
    """Copy folders from `src` to `dst` and cleanup `dst` at exit"""
    try:
        if os.path.exists(dst):
            shutil.rmtree(dst)
        shutil.copytree(src, dst, symlinks=True, dirs_exist_ok=True)
        yield
    finally:
        if os.path.exists(dst):
            shutil.rmtree(dst)


def repair_dut(
    chromeos_root: str, dut: str, max_attempts=DEFAULT_REPAIR_DUT_ATTEMPTS
) -> bool:
    """Repairs the DUT based off the host-info files.

    Currently, the host-info file stores are under the stores folders.
    Only jetstream support exists at this time.

    The host-info files can be fetched from AdminRepair tasks from stainless to
    make this a generic solution for all CrOS devices.

    Args:
      chromeos_root: the chromeos tree
      dut: the CrOS DUT to repair
      max_attempts: the number of attempts to repair the DUT

    Returns:
      True on success, False otherwise
    """
    host_info_subdir = 'stores'
    repair_cmd = [
        os.path.join(
            chromeos_root_inside_chroot, in_tree_autotest_dir, 'server/autoserv'
        ),
        '-s',
        '--host-info-subdir',
        host_info_subdir,
        '-m',
        dut,
        '--lab',
        'True',
        '--local-only-host-info',
        'True',
        '-R',
        '-r',
        'results',
        '-p',
    ]

    # Reuse results if it exists.
    autoserv_results = os.path.join(chromeos_root, 'src', 'scripts', 'results')
    if os.path.exists(autoserv_results):
        repair_cmd.append('--use-existing-results')

    autoserv_results_stores = os.path.join(autoserv_results, host_info_subdir)

    with _prepare_results_stores('autoserv-stores', autoserv_results_stores):
        for attempt in range(1, max_attempts + 1):
            try:
                logger.info('Repairing the DUT, attempt=%d', attempt)
                cros_sdk(chromeos_root, *repair_cmd)
                logger.info('Repair successful')
                return True
            except subprocess.CalledProcessError:
                # The USB in the servo might have flaked or SSH connections.
                pass
        logger.info('Repair failed')
        return False


def normalize_test_name(test_name: str) -> str:
    """Normalizes test names

    Some test names need to be modified before passing it to some scripts.(e.g. "tauto."
    prefix needs to be removed from tauto tests in switch_autotest_prebuilt.py).

    Args:
      test_name: The test name

    Returns:
      The normalized test name
    """
    return test_name.removeprefix('tauto.')


class ChromeOSVersionParser:
    """Helper class to parse chromeos_version.sh."""

    _VERSION_RE_MAPPING = {
        'chrome_branch': re.compile(r'\bCHROME_BRANCH=(\d+)\b'),
        'chromeos_build': re.compile(r'\bCHROMEOS_BUILD=(\d+)\b'),
        'chromeos_branch': re.compile(r'\bCHROMEOS_BRANCH=(\d+)\b'),
        'chromeos_patch': re.compile(r'\bCHROMEOS_PATCH=(\d+)\b'),
    }

    @classmethod
    def query_repo_chromeos_version(cls, chromeos_root: str) -> ChromeOSVersion:
        """Queries the version file inside chromeos_root.

        Args:
           chromeos_root: the chromeos tree

         Returns:
           The parsed version info.
        """
        sh_path = os.path.join(
            chromeos_root,
            'src/third_party/chromiumos-overlay/chromeos/config/chromeos_version.sh',
        )
        if not os.path.exists(sh_path):
            raise errors.InternalError('Unable to find chromeos_version.sh')
        with open(sh_path) as f:
            contents = f.read()
            return cls.parse_chromeos_version(contents)

    @classmethod
    def parse_chromeos_version(cls, contents: str) -> ChromeOSVersion:
        """Parse version arguments from chromeos_version.sh.

        Args:
            contents: Content of chromeos_version.sh.

        Raises:
            ValueError: If the file is not parsable.

        Returns:
            The parsed version info.
        """
        version_args = {}
        for k, regex in cls._VERSION_RE_MAPPING.items():
            m = regex.search(contents)
            if m is None:
                raise ValueError(
                    'pattern %r did not match chromeos_version.sh'
                    % regex.pattern
                )
            version_args[k] = int(m.group(1))
        return ChromeOSVersion(**version_args)


def assert_dut_cros_version(expected_version: str, dut: str):
    """Asserts the CrOS version of the leased DUT.

    Args:
      dut: The DUT.
      expected_version: the expected CrOS short version.

    Raises:
      errors.DutLeaseException
    """
    got_version = query_dut_short_version(dut)
    logger.info(
        'checking cros version on dut %s, expected version: %s, '
        'got version: %s',
        dut,
        expected_version,
        got_version,
    )
    if got_version != expected_version:
        raise errors.DutLeaseException(
            'Someone else reflashed the DUT. DUT locking is not respected?'
        )


def build_revlist(
    chromeos_root: str,
    old: str,
    new: str,
    chromeos_mirror: str | None = None,
    board: str | None = None,
):
    """Build revlist for ChromeOS.

    Args:
      chromeos_root: root path of ChromeOS tree
      old: old version
      new: new version
      chromeos_mirror: optional mirror path
      board: optional board name

    Returns:
      (revlist, details):
        revlist: list of rev string
        details: dict of rev to rev detail
    """
    if chromeos_mirror is None:
        chromeos_mirror = os.environ.get('CHROMEOS_MIRROR', '')

    config = {
        'chromeos_root': chromeos_root,
        'chromeos_mirror': chromeos_mirror,
        'board': board,
    }
    spec_manager = ChromeOSSpecManager(config)
    cache = repo_util.RepoMirror(config['chromeos_mirror'])

    session = os.environ.get('BISECT_SESSION', 'dummy_session')
    session_cache_dir = common.get_session_cache_dir(session)

    code_manager = codechange.CodeManager(
        chromeos_root,
        spec_manager,
        cache,
        session_cache_dir,
    )
    return code_manager.build_revlist(old, new)
