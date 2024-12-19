# Copyright 2018 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""ChromeOS ARC utility."""

import logging
import os
import shutil
import subprocess
import tempfile

from bisect_kit import android_util
from bisect_kit import cros_util
from bisect_kit import util


logger = logging.getLogger(__name__)


def search_push_to_device_script_path(android_root):
    """Search push_to_device.py.

    Args:
      android_root: root path of android tree to search.

    Returns:
      the path of push_to_device.py; None if not found
    """
    # push_to_device.py has ever moved.
    for path in [
        'device/google/cheets2/scripts/push_to_device.py',
        'tools/vendor/google_prebuilts/arc/push_to_device.py',
    ]:
        fullpath = os.path.join(android_root, path)
        if os.path.exists(fullpath) and os.access(fullpath, os.X_OK):
            return path

    return None


def _push_to_device(dut, push_script, args, android_root=None, flavor=None):
    """Helper function to run push_to_device.py"""
    assert push_script
    cmd = [push_script] + args
    stderr_lines = []

    def collect_stderr(line):
        stderr_lines.append(line)

    if android_root:
        assert flavor
        # New version of push_to_device can run without lunch, but go with lunch is
        # easier for now and compatible with old versions.
        android_util.lunch(
            android_root, flavor, *cmd, stderr_callback=collect_stderr
        )
    else:
        util.check_call(*cmd, stderr_callback=collect_stderr)

    stderr = ''.join(stderr_lines)
    if (
        '*** Reboot required. ***' in stderr
        or '*** Rebooting device. ***' in stderr
        or 'root:Rebooting device...' in stderr
    ):
        cros_util.wait_reboot_done(dut)


def push_prebuilt_to_device(dut, flavor, build_id):
    """Pushes prebuilt ARC container to DUT.

    Args:
      dut: address of DUT
      flavor: android build flavor
      build_id: android build id.
    """
    # assume ARC's flavor is always product-variant
    assert '-' in flavor
    product, _ = flavor.split('-')
    build_filename = f'{product}-img-{build_id}.zip'
    sepolicy_filename = 'sepolicy.zip'

    tmp_dir = tempfile.mkdtemp()
    try:
        logger.debug('fetch and unpack push_to_device.zip to %s', tmp_dir)
        android_util.fetch_artifact(
            flavor, build_id, 'push_to_device.zip', tmp_dir
        )
        android_util.fetch_artifact(flavor, build_id, build_filename, tmp_dir)
        android_util.fetch_artifact(
            flavor, build_id, sepolicy_filename, tmp_dir
        )

        util.check_call('unzip', 'push_to_device.zip', cwd=tmp_dir)
        push_script = os.path.join(tmp_dir, 'push_to_device.py')

        # Don't use --use-prebuilt because push_to_device.py doesn't support
        # fetching artifacts with service account.
        args = [
            '--force',
            dut,
            '--use-prebuilt-file',
            os.path.join(tmp_dir, build_filename),
            '--sepolicy-artifacts-path',
            os.path.join(tmp_dir, sepolicy_filename),
        ]
        _push_to_device(dut, push_script, args)
    finally:
        logger.debug('clean up %s', tmp_dir)
        shutil.rmtree(tmp_dir)


def push_localbuild_to_device(android_root, dut, flavor):
    """Pushes localbuild ARC container to DUT.

    Args:
      android_root: root path of android tree.
      dut: address of DUT
      flavor: android build flavor
    """
    push_script = search_push_to_device_script_path(android_root)

    # Build push_to_device's dependency. We have to build it explicitly because
    # it's possible that no build is generated yet.
    # push_to_device.zip is valid since 2018-06.
    try:
        android_util.lunch(android_root, flavor, 'make', 'push_to_device.zip')
    except subprocess.CalledProcessError:
        # TODO(kcwu): remove this and build only push_to_device.zip once we don't
        #             care 2018-06 and earlier versions.
        android_util.lunch(
            android_root,
            flavor,
            'make',
            'unsquashfs',
            'simg2img_host',
            'mksquashfs',
            'secilc',
        )

    _push_to_device(dut, push_script, ['--force', dut], flavor)
