#!/usr/bin/env python2
# -*- coding: utf-8 -*-
# Copyright 2018 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Switcher for chromeos localbuild bisecting."""

from __future__ import print_function
import argparse
import logging

from bisect_kit import cli
from bisect_kit import common
from bisect_kit import configure
from bisect_kit import cros_util
from bisect_kit import repo_util
from bisect_kit import util

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
      help='DUT address')
  parser.add_argument(
      'rev',
      nargs='?',
      type=cli.argtype_notempty,
      metavar='INTRA_REV',
      default=configure.get('INTRA_REV', ''),
      help='ChromeOS local build version string, in format short version, '
      'full version, or "full,full+N"')
  parser.add_argument(
      '--board',
      metavar='BOARD',
      default=configure.get('BOARD', ''),
      help='ChromeOS board name')
  parser.add_argument(
      '--repo_dir',
      type=cli.argtype_dir_path,
      metavar='REPO_DIR',
      default=configure.get('REPO_DIR', ''),
      help='ChromeOS tree root (default: %(default)s)')
  parser.add_argument(
      '--clobber-stateful',
      action='store_true',
      help='Clobber stateful partition when performing update')
  parser.add_argument(
      '--no-disable-rootfs-verification',
      dest='disable_rootfs_verification',
      action='store_false',
      help="Don't disable rootfs verification after update is completed")
  parser.add_argument(
      '--nodeploy', action='store_true', help='Build only; do not deploy')
  return parser


def mark_as_stable(opts):
  cmd = ['chromite/bin/cros_list_overlays', '--all']
  output = util.check_output(*cmd, cwd=opts.repo_dir)
  overlays = ':'.join(output.splitlines())

  cmd = [
      'chromite/bin/cros_mark_as_stable',
      '-b', opts.board,
      '-o', overlays,
      '--debug',
      '--all',
      'commit',
  ]  # yapf: disable
  util.check_call(*cmd, cwd=opts.repo_dir)


def cleanup(opts):
  # TODO(kcwu): need to recreate chroot for some cases, such as
  #   - downgrade chroot version
  #   - portage upgrade for EAPI=6
  logger.info('clean up previous result of "mark as stable"')
  repo_util.abandon(opts.repo_dir, 'stabilizing_branch')


def switch(opts):
  cleanup(opts)

  config = dict(board=opts.board, repo_dir=opts.repo_dir)
  mm = cros_util.ChromeOSManifestManager(config)
  dep = repo_util.DependencyManager(opts.repo_dir, mm)
  dep.switch(opts.rev)

  logger.info('mark as stable')
  mark_as_stable(opts)

  image_path = cros_util.build_image(opts.repo_dir, opts.board, opts.rev)

  if opts.nodeploy:
    return

  cros_util.cros_flash(
      opts.repo_dir,
      opts.dut,
      opts.board,
      image_path,
      clobber_stateful=opts.clobber_stateful,
      disable_rootfs_verification=opts.disable_rootfs_verification,
      run_inside_chroot=True)


def main(args=None):
  common.init()
  parser = create_argument_parser()
  opts = parser.parse_args(args)
  common.config_logging(opts)

  assert opts.nodeploy or cros_util.is_dut(opts.dut)
  if not opts.board:
    opts.board = cros_util.query_dut_board(opts.dut)

  if cros_util.is_cros_short_version(opts.rev):
    opts.rev = cros_util.version_to_full(opts.board, opts.rev)

  switch(opts)


if __name__ == '__main__':
  main()
