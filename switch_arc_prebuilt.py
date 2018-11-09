#!/usr/bin/env python2
# -*- coding: utf-8 -*-
# Copyright 2018 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Switcher for ChromeOS ARC container prebuilt.

Typical usage:
  $ bisect_android_build_id.py config switch ./switch_arc_prebuilt.py

It is implemented by calling Android's push_to_device.py.
"""

from __future__ import print_function
import argparse
import logging

from bisect_kit import android_util
from bisect_kit import arc_util
from bisect_kit import cli
from bisect_kit import common
from bisect_kit import configure
from bisect_kit import cros_util
from bisect_kit import errors

logger = logging.getLogger(__name__)


def create_argument_parser():
  parser = argparse.ArgumentParser(description=__doc__)
  common.add_common_arguments(parser)
  parser.add_argument(
      'dut',
      nargs='?',
      type=cli.argtype_notempty,
      metavar='DUT',
      default=configure.get('DUT', ''))
  parser.add_argument(
      'build_id',
      nargs='?',
      type=android_util.argtype_android_build_id,
      metavar='ANDROID_BUILD_ID',
      default=configure.get('ANDROID_BUILD_ID', ''))
  parser.add_argument(
      '--flavor',
      metavar='ANDROID_FLAVOR',
      default=configure.get('ANDROID_FLAVOR'),
      help='example: cheets_x86-user')
  parser.add_argument(
      '--android_root',
      type=cli.argtype_dir_path,
      default=configure.get('ANDROID_ROOT', ''),
      help='Android tree to search push_to_device.py')

  return parser


def main(args=None):
  common.init()
  parser = create_argument_parser()
  opts = parser.parse_args(args)
  common.config_logging(opts)

  if not opts.flavor:
    opts.flavor = arc_util.query_flavor(opts.dut)
    assert opts.flavor
  logger.info('use flavor=%s', opts.flavor)

  arc_util.push_prebuilt_to_device(opts.android_root, opts.dut, opts.flavor,
                                   opts.build_id)

  arc_version = cros_util.query_dut_lsb_release(
      opts.dut).get('CHROMEOS_ARC_VERSION')
  if str(opts.build_id) in arc_version:
    logger.info('updated to "%s", done', arc_version)
  else:
    raise errors.ExecutionFatalError(
        'arc version is expected matching "%s" but actual "%s"' %
        (opts.build_id, arc_version))


if __name__ == '__main__':
  main()
