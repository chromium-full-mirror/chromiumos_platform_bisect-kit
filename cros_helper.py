#!/usr/bin/env python2
# Copyright 2017 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Helper script to manipulate chromeos DUT or query info."""
from __future__ import print_function
import argparse
import json
import logging

from bisect_kit import common
from bisect_kit import cros_util

logger = logging.getLogger(__name__)


def cmd_version_info(opts):
  info = cros_util.version_info(opts.board, opts.version)
  if opts.name:
    if opts.name not in info:
      logger.error('unknown name=%s', opts.name)
    print(info[opts.name])
  else:
    print(json.dumps(info, sort_keys=True, indent=4))


def cmd_query_dut_board(opts):
  assert cros_util.is_dut(opts.dut)
  print(cros_util.query_dut_board(opts.dut))


def cmd_reboot(opts):
  assert cros_util.is_dut(opts.dut)
  cros_util.reboot(opts.dut)


def main():
  common.init()
  parser = argparse.ArgumentParser()
  common.add_common_arguments(parser)
  subparsers = parser.add_subparsers(
      dest='command', title='commands', metavar='<command>')

  parser_version_info = subparsers.add_parser(
      'version_info',
      help='Query version info of given chromeos build',
      description='Given chromeos `board` and `version`, '
      'print version information of components.')
  parser_version_info.add_argument(
      'board', help='ChromeOS board name, like "samus".')
  parser_version_info.add_argument(
      'version',
      type=cros_util.argtype_cros_version,
      help='ChromeOS version, like "9876.0.0" or "R62-9876.0.0"')
  parser_version_info.add_argument(
      'name',
      nargs='?',
      help='Component name. If specified, output its version string. '
      'Otherwise output all version info as dict in json format.')
  parser_version_info.set_defaults(func=cmd_version_info)

  parser_query_dut_board = subparsers.add_parser(
      'query_dut_board', help='Query board name of given DUT')
  parser_query_dut_board.add_argument('dut')
  parser_query_dut_board.set_defaults(func=cmd_query_dut_board)

  parser_reboot = subparsers.add_parser(
      'reboot',
      help='Reboot a DUT',
      description='Reboot a DUT and verify the reboot is successful.')
  parser_reboot.add_argument('dut')
  parser_reboot.set_defaults(func=cmd_reboot)

  opts = parser.parse_args()
  common.config_logging(opts)
  opts.func(opts)


if __name__ == '__main__':
  main()
