#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright 2018 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Switcher for ChromeOS autotest prebuilt

It unpacks autotest prebuilt (both client and server packages) into
$CHROMEOS_ROOT/tmp/autotest-prebuilt (that is, autotest_dir).
Later, you can run tests using "eval_cros_autotest.py --prebuilt" or
"test_that --autotest_dir".
"""

from __future__ import print_function
import argparse
import logging
import os
import shutil
import sys
import tempfile

from bisect_kit import cli
from bisect_kit import common
from bisect_kit import configure
from bisect_kit import cros_util
from bisect_kit import util

logger = logging.getLogger(__name__)

AUTOTEST_CLIENT_TARBALL = 'autotest_packages.tar'
AUTOTEST_SERVER_TARBALL = 'autotest_server_package.tar.bz2'
GS_BUILD_PATH = 'gs://chromeos-image-archive/{board}-release/{full_version}'
GS_AUTOTEST_CLIENT_PATH = GS_BUILD_PATH + '/' + AUTOTEST_CLIENT_TARBALL
GS_AUTOTEST_SERVER_PATH = GS_BUILD_PATH + '/' + AUTOTEST_SERVER_TARBALL

GS_SNAPSHOT_PATH = (
    'gs://chromeos-image-archive/{board}-postsubmit/{snapshot_version}-*')
GS_SNAPSHOT_AUTOTEST_CLIENT_PATH = (
    GS_SNAPSHOT_PATH + '/' + AUTOTEST_CLIENT_TARBALL)
GS_SNAPSHOT_AUTOTEST_SERVER_PATH = (
    GS_SNAPSHOT_PATH + '/' + AUTOTEST_SERVER_TARBALL)


def create_argument_parser():
  parser = argparse.ArgumentParser(description=__doc__)
  cli.patching_argparser_exit(parser)
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


def switch(autotest_dir, board, version, test_name):
  logger.info('Unpack autotest packages for %s %s', board, version)
  if cros_util.is_cros_snapshot_version(version):
    autotest_client_path = GS_SNAPSHOT_AUTOTEST_CLIENT_PATH.format(
        board=board, snapshot_version=version)
    autotest_server_path = GS_SNAPSHOT_AUTOTEST_SERVER_PATH.format(
        board=board, snapshot_version=version)
  else:
    full_version = cros_util.version_to_full(board, version)
    autotest_client_path = GS_AUTOTEST_CLIENT_PATH.format(
        board=board, full_version=full_version)
    autotest_server_path = GS_AUTOTEST_SERVER_PATH.format(
        board=board, full_version=full_version)
  logger.info('autotest_client_path %s', autotest_client_path)
  logger.info('autotest_server_path %s', autotest_server_path)

  # TODO(kcwu): cache downloaded tarballs
  tmp_dir = tempfile.mkdtemp()
  if os.path.exists(autotest_dir):
    shutil.rmtree(autotest_dir)
  os.makedirs(autotest_dir)

  cros_util.gsutil('cp', autotest_client_path, tmp_dir)
  tarball = os.path.join(tmp_dir, AUTOTEST_CLIENT_TARBALL)
  # strip 'autotest/'
  util.check_call(
      'tar', 'xvf', tarball, '--strip-components=1', cwd=autotest_dir)
  cros_util.gsutil('cp', autotest_server_path, tmp_dir)
  tarball = os.path.join(tmp_dir, AUTOTEST_SERVER_TARBALL)
  util.check_call(
      'tar', 'xf', tarball, '--strip-components=1', cwd=autotest_dir)

  # Need to extract the control file if the target is a client site test.
  if test_name:
    test_name = test_name.split('.')[0]
    client_tarball = os.path.abspath(
        os.path.join(autotest_dir, 'packages', 'test-%s.tar.bz2' % test_name))
    if os.path.exists(client_tarball):
      client_test_dir = os.path.join(autotest_dir, 'client', 'site_tests',
                                     test_name)
      if not os.path.exists(client_test_dir):
        os.makedirs(client_test_dir)
        util.check_call('tar', 'xvf', client_tarball, cwd=client_test_dir)

  shutil.rmtree(tmp_dir)


@cli.fatal_error_handler
def main(args=None):
  common.init()
  parser = create_argument_parser()
  opts = parser.parse_args(args)
  common.config_logging(opts)

  autotest_dir = os.path.join(opts.chromeos_root,
                              cros_util.prebuilt_autotest_dir)
  switch(autotest_dir, opts.board, opts.version, opts.test_name)

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
      sys.exit(cli.EXIT_CODE_FATAL)


if __name__ == '__main__':
  main()
