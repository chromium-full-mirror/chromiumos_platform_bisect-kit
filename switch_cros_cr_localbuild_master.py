#!/usr/bin/env python2
# -*- coding: utf-8 -*-
# Copyright 2017 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Switcher for chrome on chromeos local build bisecting

This script will sync the chrome source tree to the specified version, build,
and deploy.

Typical usage companion with bisect_cr_localbuild_master:
  $ ./bisect_cr_localbuild_master.py config switch \
      ./switch_cros_cr_localbuild_master.py

By default, it will build and deploy chrome. You can specify --target for
alternative binaries.
"""
from __future__ import print_function
import argparse
import logging
import os

from bisect_kit import cli
from bisect_kit import common
from bisect_kit import configure
from bisect_kit import cros_util
from bisect_kit import cr_util
from bisect_kit import gclient_util
from bisect_kit import git_util
import bisect_cr_localbuild_master

logger = logging.getLogger(__name__)


def create_argument_parser():
  parser = argparse.ArgumentParser()
  common.add_common_arguments(parser)
  parser.add_argument(
      'dut',
      nargs='?',
      type=cli.argtype_notempty,
      metavar='DUT',
      default=configure.get('DUT', ''),
      help='Address of DUT')
  parser.add_argument(
      'rev',
      nargs='?',
      type=cli.argtype_notempty,
      metavar='REV',
      default=configure.get('REV', ''),
      help='Version string')
  parser.add_argument(
      '--board', default=configure.get('BOARD'), help='ChromeOS board name')
  parser.add_argument(
      '--chrome_root',
      metavar='CHROME_ROOT',
      type=cli.argtype_dir_path,
      default=configure.get('CHROME_ROOT', ''),
      help='Root of Chrome source tree, like ~/chromium')
  parser.add_argument(
      '--target',
      action='append',
      help='Binary to build and deploy, like unittest or fuzzers. '
      'Example value: "video_decode_accelerator_unittest". '
      'This option could be specified multiple times.')

  return parser


def main(args=None):
  common.init()
  parser = create_argument_parser()
  opts = parser.parse_args(args)
  common.config_logging(opts)

  if not opts.board:
    opts.board = cros_util.query_dut_board(opts.dut)

  opts.chrome_src = os.path.join(opts.chrome_root, 'src')
  assert os.path.exists(opts.chrome_src)

  assert bisect_cr_localbuild_master.verify_gclient_dep(opts.chrome_root)
  rev = bisect_cr_localbuild_master.guess_git_rev(opts, opts.rev)

  git_util.checkout_version(opts.chrome_src, rev)
  gclient_util.sync(opts.chrome_src)

  if not opts.target:
    opts.target = ['chrome', 'chrome_sandbox', 'nacl_helper']

  # TODO(kcwu): make the action configurable, e.g. sync without build, build
  # without deploy, and apply local patch.
  cr_util.build_and_deploy(opts.chrome_src, opts.board, opts.dut, opts.target)


if __name__ == '__main__':
  main()
