# -*- coding: utf-8 -*-
# Copyright 2018 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""ChromeOS ARC utility."""

from __future__ import print_function
import logging
import re
import os

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


def _push_to_device(dut, args, android_root, flavor):
  """Helper function to run push_to_device.py"""
  boot_id = cros_util.query_dut_boot_id(dut)

  push_script = search_push_to_device_script_path(android_root)
  assert push_script
  cmd = [push_script] + args
  stderr_lines = []

  def collect_stderr(line):
    stderr_lines.append(line)

  # Build push_to_device's dependency. We have to build it explicitly because
  # it's possible that no build is generated yet.
  android_util.lunch(android_root, flavor, 'make', 'unsquashfs',
                     'simg2img_host', 'mksquashfs')

  # New version of push_to_device can run without lunch, but go with lunch is
  # easier for now and compatible with old versions.
  android_util.lunch(android_root, flavor, *cmd, stderr_callback=collect_stderr)

  stderr = ''.join(stderr_lines)
  if '*** Reboot required. ***' in stderr:
    cros_util.wait_reboot_done(dut, boot_id)


def push_prebuilt_to_device(android_root, dut, flavor, build_id):
  """Pushes prebuilt ARC container to DUT.

  Args:
    android_root: root path of android tree
    dut: address of DUT
    flavor: android build flavor
    build_id: android build id.
  """
  # assume ARC's flavor is always product-variant
  assert '-' in flavor
  product, variant = flavor.split('-')
  args = [
      '--force',
      dut,
      '--use-prebuilt',
      '{product}/{variant}/{build_id}'.format(
          product=product, variant=variant, build_id=build_id),
  ]

  _push_to_device(dut, args, android_root, flavor)


def push_localbuild_to_device(android_root, dut, flavor):
  """Pushes localbuild ARC container to DUT.

  Args:
    android_root: root path of android tree.
    dut: address of DUT
    flavor: android build flavor
  """
  _push_to_device(dut, ['--force', dut], android_root, flavor)


def get_build_prop(host):
  """Get build property of a host.

  Args:
    host: host address

  Returns:
    dict of property
  """
  cmd = [
      'ssh', host, 'cat',
      '/opt/google/containers/android/rootfs/root/system/build.prop'
  ]
  data = util.check_output(*cmd)
  if not data:
    return None
  result = {}
  for k, v in re.findall(r'^([^#=]+)=(.*)$', data, re.M):
    result[k] = v
  return result


def query_variant(host):
  """Queries DUT's build variant (ro.build.type)."""
  prop = get_build_prop(host)
  return prop and prop.get('ro.build.type')


def query_product(host):
  """Queries DUT's build product (ro.build.product)."""
  prop = get_build_prop(host)
  return prop and prop.get('ro.build.product')


def query_flavor(host):
  """Queries DUT's build flavor (ro.build.flavor)."""
  prop = get_build_prop(host)
  return prop and prop.get('ro.build.flavor')
