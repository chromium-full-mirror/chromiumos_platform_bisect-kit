#!/usr/bin/env python2
# -*- coding: utf-8 -*-
# Copyright 2017 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Switcher for ChromeOS prebuilt"""

from __future__ import print_function
import argparse
import logging
import os
import sys

from bisect_kit import cli
from bisect_kit import common
from bisect_kit import configure
from bisect_kit import cros_util

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
      'version',
      nargs='?',
      type=cros_util.argtype_cros_version,
      metavar='CROS_VERSION',
      default=configure.get('CROS_VERSION', ''),
      help='ChromeOS version number, short (10162.0.0) or full (R64-10162.0.0)')
  parser.add_argument(
      '--board',
      metavar='BOARD',
      default=configure.get('BOARD', ''),
      help='ChromeOS board name')
  parser.add_argument(
      '--clobber-stateful',
      '--clobber_stateful',
      action='store_true',
      help='Clobber stateful partition when performing update')
  parser.add_argument(
      '--no-disable-rootfs-verification',
      '--no_disable_rootfs_verification',
      dest='disable_rootfs_verification',
      action='store_false',
      help="Don't disable rootfs verification after update is completed")
  parser.add_argument(
      '--default_chromeos_root',
      type=cli.argtype_dir_path,
      default=configure.get('DEFAULT_CHROMEOS_ROOT',
                            os.path.expanduser('~/chromiumos')),
      help='Default chromeos tree to run "cros flash" (default: %(default)s)')

  return parser


def main(args=None):
  common.init()
  parser = create_argument_parser()
  opts = parser.parse_args(args)
  common.config_logging(opts)

  if not cros_util.is_good_dut(opts.dut):
    logger.error('%r is not a good DUT', opts.dut)
    sys.exit(cli.EXIT_CODE_FATAL)
  board = opts.board or cros_util.query_dut_board(opts.dut)

  # TODO(kcwu): clear cache of cros flash
  image_path = cros_util.prepare_prebuilt_image(board, opts.version)
  try:
    cros_util.cros_flash(
        opts.default_chromeos_root,
        opts.dut,
        board,
        image_path,
        version=opts.version,
        clobber_stateful=opts.clobber_stateful,
        disable_rootfs_verification=opts.disable_rootfs_verification)
  except Exception:
    if not cros_util.is_good_dut(opts.dut):
      sys.exit(cli.EXIT_CODE_FATAL)
    raise


if __name__ == '__main__':
  main()
