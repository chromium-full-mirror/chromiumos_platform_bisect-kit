#!/usr/bin/env python3
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
from bisect_kit import core
from bisect_kit import cros_lab_util
from bisect_kit import cros_util

logger = logging.getLogger(__name__)


def create_argument_parser():
  parents = [common.common_argument_parser, common.session_optional_parser]
  parser = argparse.ArgumentParser(description=__doc__, parents=parents)
  cli.patching_argparser_exit(parser)
  parser.add_argument(
      '--dut',
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


def switch(opts):
  if opts.session:
    states = core.BisectStates.from_bisector_class('ChromeOSVersionDomain',
                                                   opts.session)
    if states.load_states():
      cros_util.SnapshotStore.init_with_state(states)
  # TODO(kcwu): clear cache of cros flash
  image_info = cros_util.search_image(opts.board, opts.version)
  if not image_info:
    logger.error('no images available for %s %s', opts.board, opts.version)
    return cli.EXIT_CODE_FATAL

  if cros_util.provision_image_with_retry(
      opts.default_chromeos_root,
      opts.dut,
      opts.board,
      image_info,
      version=opts.version,
      clobber_stateful=opts.clobber_stateful,
      disable_rootfs_verification=opts.disable_rootfs_verification,
      repair_callback=cros_lab_util.repair,
      force_reboot_callback=cros_lab_util.reboot_via_servo):
    return 0
  return 1


def parse_args(args):
  parser = create_argument_parser()
  return parser.parse_args(args)


def inner_main(opts):
  if not cros_util.is_good_dut(opts.dut):
    logger.error('%r is not a good DUT', opts.dut)
    if not cros_lab_util.repair(opts.dut):
      return cli.EXIT_CODE_FATAL
  if not opts.board:
    opts.board = cros_util.query_dut_board(opts.dut)

  cros_util.prepare_chroot(opts.default_chromeos_root)

  try:
    returncode = switch(opts)
  except Exception:
    logger.exception('switch failed')
    returncode = 1

  # No matter switching succeeded or not, DUT must be in good state.
  # switch() already tried repairing if possible, no repair here.
  if not cros_util.is_good_dut(opts.dut):
    logger.fatal('%r is not a good DUT', opts.dut)
    returncode = cli.EXIT_CODE_FATAL
  logger.info('done')
  return returncode


@cli.fatal_error_handler
def main(args=None):
  common.init()
  opts = parse_args(args)
  common.config_logging(opts)
  sys.exit(inner_main(opts))


if __name__ == '__main__':
  main()
