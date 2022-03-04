#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright 2020 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Switcher for ChromeOS tast prebuilt

It unpacks tast server side prebuilt into $CHROMEOS_ROOT/tmp/tast-prebuilt
Later, you can run tests using "eval_cros_tast.py --prebuilt" or
"$CHROMEOS_ROOT/tmp/tast-prebuilt/run_tast.sh".
"""

from __future__ import annotations
from __future__ import print_function
import argparse
import logging
import os
import shutil
import sys
import tempfile
import typing

from bisect_kit import bisector_cli
from bisect_kit import cli
from bisect_kit import common
from bisect_kit import configure
from bisect_kit import cros_util
from bisect_kit import util

logger = logging.getLogger(__name__)

AUTOTEST_SERVER_TARBALL = 'autotest_server_package.tar.bz2'
GS_BUILD_PATH = 'gs://chromeos-image-archive/{board}-release/{full_version}'
GS_AUTOTEST_SERVER_PATH = GS_BUILD_PATH + '/' + AUTOTEST_SERVER_TARBALL

GS_BUILDBUCKET_BUILD_PATH = 'gs://chromeos-image-archive/{path}'
GS_BUILDBUCKET_BUILD_AUTOTEST_SERVER_PATH = (
    GS_BUILDBUCKET_BUILD_PATH + '/' + AUTOTEST_SERVER_TARBALL)


def create_argument_parser():
  parents = [common.common_argument_parser, common.session_optional_parser]
  parser = argparse.ArgumentParser(description=__doc__, parents=parents)
  cli.patching_argparser_exit(parser)
  parser.add_argument(
      '--rich_result',
      action='store_true',
      help='Instead of mere exit code, output detailed information in json')
  parser.add_argument(
      '--chromeos_root',
      type=cli.argtype_dir_path,
      default=configure.get('CHROMEOS_ROOT', ''),
      help='ChromeOS tree root')
  parser.add_argument(
      '--board',
      metavar='BOARD',
      default=configure.get('BOARD', ''),
      help='ChromeOS board name')
  parser.add_argument(
      '--dut',
      type=cli.argtype_notempty,
      metavar='DUT',
      default=configure.get('DUT', ''),
      help='Address of DUT')
  parser.add_argument(
      'version',
      nargs='?',
      type=cli.argtype_notempty,
      metavar='CROS_VERSION',
      default=configure.get('CROS_VERSION', ''),
      help='ChromeOS local build version string, in format short version, '
      'full version, or "full,full+N"')

  return parser


def switch(opts, board, version, dut):
  logger.info('Unpack autotest packages for %s %s', board, version)
  if cros_util.is_cros_short_version(version) or cros_util.is_cros_full_version(
      version):
    full_version = cros_util.version_to_full(board, version)
    autotest_server_path = GS_AUTOTEST_SERVER_PATH.format(
        board=board, full_version=full_version)
  else:
    gs_path = cros_util.query_dut_lsb_release(dut).get(
        'CHROMEOS_RELEASE_BUILDER_PATH')
    autotest_server_path = GS_BUILDBUCKET_BUILD_AUTOTEST_SERVER_PATH.format(
        path=gs_path)

  logger.info('autotest_server_path %s', autotest_server_path)

  # TODO(kcwu): cache downloaded tarballs
  tmp_dir = tempfile.mkdtemp()
  tast_dir = os.path.join(opts.chromeos_root, cros_util.prebuilt_tast_dir)
  if os.path.exists(tast_dir):
    shutil.rmtree(tast_dir)
  os.makedirs(tast_dir)

  cros_util.gsutil('cp', autotest_server_path, tmp_dir)
  tarball = os.path.join(tmp_dir, AUTOTEST_SERVER_TARBALL)
  util.check_call(
      'tar', 'xf', tarball, '--strip-components=1', 'tast', cwd=tast_dir)

  shutil.rmtree(tmp_dir)


def switch_main(args: typing.Optional[tuple[str]]):
  common.init()
  parser = create_argument_parser()
  opts = parser.parse_args(args)
  common.config_logging(opts)

  switch(opts, opts.board, opts.version, opts.dut)


def main(args: typing.Optional[tuple[str]] = None) -> int:
  return bisector_cli.switch_main_wrapper(switch_main, args)


if __name__ == '__main__':
  sys.exit(main())
