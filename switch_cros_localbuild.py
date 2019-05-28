#!/usr/bin/env python2
# -*- coding: utf-8 -*-
# Copyright 2018 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Switcher for chromeos localbuild bisecting."""

from __future__ import print_function
import argparse
import logging
import os
import sys

from bisect_kit import cli
from bisect_kit import codechange
from bisect_kit import common
from bisect_kit import configure
from bisect_kit import cros_lab_util
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
      '--chromeos_root',
      type=cli.argtype_dir_path,
      metavar='CHROMEOS_ROOT',
      default=configure.get('CHROMEOS_ROOT', ''),
      help='ChromeOS tree root (default: %(default)s)')
  parser.add_argument(
      '--chromeos_mirror',
      type=cli.argtype_dir_path,
      default=configure.get('CHROMEOS_MIRROR', ''),
      help='ChromeOS repo mirror path')
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
      '--nobuild',
      action='store_true',
      help='Sync source code only; do not build; imply --nodeploy')
  parser.add_argument(
      '--nodeploy', action='store_true', help='Do not deploy after build')
  return parser


def mark_as_stable(opts):
  overlays = util.check_output(
      'chromite/bin/cros_list_overlays', '--all',
      cwd=opts.chromeos_root).splitlines()

  # Get overlays listed in manifest file.
  known_projects = []
  for path in repo_util.projects_in_manifest(opts.chromeos_root):
    known_projects.append(
        os.path.realpath(os.path.join(opts.chromeos_root, path)))
  logger.debug('known_projects %s', known_projects)

  # Skip recent added overlays.
  # cros_mark_as_stable expects all overlays is recorded in manifest file
  # but we haven't synthesized manifest files for each intra versions yet.
  # TODO(kcwu): synthesize manifest file
  for overlay in list(overlays):
    if 'private-overlays/' in overlay and overlay not in known_projects:
      logger.warning(
          'bisect-kit cannot handle recently added overlay %s yet; ignore',
          overlay)
      overlays.remove(overlay)
      continue

  cmd = [
      'chromite/bin/cros_mark_as_stable',
      '-b', opts.board,
      '--overlays', ':'.join(overlays),
      '--debug',
      '--all',
      'commit',
  ]  # yapf: disable
  util.check_call(*cmd, cwd=opts.chromeos_root)


def cleanup(opts):
  logger.info('clean up previous result of "mark as stable"')
  repo_util.abandon(opts.chromeos_root, 'stabilizing_branch')


def build(opts):
  # create chroot if necessary
  if (not os.path.exists(os.path.join(opts.chromeos_root, 'chroot')) and
      not os.path.exists(os.path.join(opts.chromeos_root, 'chroot.img'))):
    util.check_output(
        'chromite/bin/cros_sdk', '--create', cwd=opts.chromeos_root)

  logger.info('mark as stable')
  mark_as_stable(opts)

  logger.info('build image')
  return cros_util.build_image(opts.chromeos_root, opts.board, opts.rev)


def switch(opts):
  cleanup(opts)

  logger.info('switch source code')
  config = dict(
      board=opts.board,
      chromeos_root=opts.chromeos_root,
      chromeos_mirror=opts.chromeos_mirror)
  spec_manager = cros_util.ChromeOSSpecManager(config)
  cache = repo_util.RepoMirror(opts.chromeos_mirror)
  code_manager = codechange.CodeManager(opts.chromeos_root, spec_manager, cache)
  code_manager.switch(opts.rev)

  if opts.nobuild:
    return 0

  try:
    image_path = build(opts)
  except cros_util.NeedRecreateChrootException as e:
    logger.warning('recreate chroot and retry again, reason: %s', e)
    util.check_output(
        'chromite/bin/cros_sdk', '--delete', cwd=opts.chromeos_root)
    image_path = build(opts)

  if opts.nodeploy:
    return 0

  if cros_util.cros_flash_with_retry(
      opts.chromeos_root,
      opts.dut,
      opts.board,
      image_path,
      clobber_stateful=opts.clobber_stateful,
      disable_rootfs_verification=opts.disable_rootfs_verification,
      repair_callback=cros_lab_util.repair):
    return 0
  return 1


def main(args=None):
  common.init()
  parser = create_argument_parser()
  opts = parser.parse_args(args)
  common.config_logging(opts)

  if not opts.nodeploy:
    if not cros_util.is_good_dut(opts.dut):
      logger.fatal('%r is not a good DUT', opts.dut)
      if not cros_lab_util.repair(opts.dut):
        sys.exit(cli.EXIT_CODE_FATAL)
  if not opts.board:
    opts.board = cros_util.query_dut_board(opts.dut)

  if cros_util.is_cros_short_version(opts.rev):
    opts.rev = cros_util.version_to_full(opts.board, opts.rev)

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
  sys.exit(returncode)


if __name__ == '__main__':
  main()
