#!/usr/bin/env python2
# -*- coding: utf-8 -*-
# Copyright 2018 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Switcher for ChromeOS autotest prebuilt

It unpacks autotest prebuilt packages into
$CHROMEOS_ROOT/src/third_party/autotest (that is, autotest_dir).
Later, you can run tests using "eval_cros_autotest.py --prebuilt" or
"test_that --autotest_dir".
"""

from __future__ import print_function
import argparse
import logging
import os
import shutil
import tempfile

from bisect_kit import cli
from bisect_kit import common
from bisect_kit import configure
from bisect_kit import cros_util
from bisect_kit import util

logger = logging.getLogger(__name__)

AUTOTEST_CLIENT_TARBALL = 'autotest_packages.tar'
GS_BUILD_PATH = 'gs://chromeos-image-archive/{board}-release/{full_version}'
GS_AUTOTEST_CLIENT_PATH = GS_BUILD_PATH + '/' + AUTOTEST_CLIENT_TARBALL


def create_argument_parser():
  parser = argparse.ArgumentParser(description=__doc__)
  common.add_common_arguments(parser)
  parser.add_argument(
      '--chromeos_root',
      type=cli.argtype_dir_path,
      default=configure.get('CHROMEOS_ROOT', ''),
      help='ChromeOS tree root')
  parser.add_argument(
      '--test_name',
      help='Client test name, like "video_VideoDecodeAccelerator.h264"')
  parser.add_argument(
      '--board',
      metavar='BOARD',
      default=configure.get('BOARD', ''),
      help='ChromeOS board name')
  parser.add_argument(
      'version',
      nargs='?',
      type=cros_util.argtype_cros_version,
      metavar='CROS_VERSION',
      default=configure.get('CROS_VERSION', ''),
      help='ChromeOS version number, short (10162.0.0) or full (R64-10162.0.0)')

  return parser


def switch(autotest_dir, board, version):
  full_version = cros_util.version_to_full(board, version)
  logger.info('Unpack autotest packages for %s %s', board, full_version)

  autotest_client_path = GS_AUTOTEST_CLIENT_PATH.format(
      board=board, full_version=full_version)

  # TODO(kcwu): cache downloaded tarballs
  tmp_dir = tempfile.mkdtemp()
  packages_dir = os.path.join(autotest_dir, 'packages')
  if os.path.exists(packages_dir):
    shutil.rmtree(packages_dir)

  cros_util.gsutil('cp', autotest_client_path, tmp_dir)
  tarball = os.path.join(tmp_dir, AUTOTEST_CLIENT_TARBALL)
  # strip 'autotest/'
  util.check_call(
      'tar',
      'xvf',
      tarball,
      '--strip-components=1',
      'autotest/packages',
      cwd=autotest_dir)

  shutil.rmtree(tmp_dir)


def main(args=None):
  common.init()
  parser = create_argument_parser()
  opts = parser.parse_args(args)
  common.config_logging(opts)

  autotest_dir = os.path.join(opts.chromeos_root, cros_util.autotest_path)
  # Verify test control file exists.
  if opts.test_name:
    found = cros_util.get_autotest_test_info(autotest_dir, opts.test_name)
    if not found:
      names = []
      for control_file in cros_util.enumerate_autotest_control_files(
          autotest_dir):
        info = cros_util.parse_autotest_control_file(control_file)
        names.append(info.name)
      util.show_similar_candidates('test_name', opts.test_name, names)

  switch(autotest_dir, opts.board, opts.version)


if __name__ == '__main__':
  main()
