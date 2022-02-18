#!/usr/bin/env python3
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
import typing

from bisect_kit import bisector_cli
from bisect_kit import cli
from bisect_kit import codechange
from bisect_kit import common
from bisect_kit import configure
from bisect_kit import cros_lab_util
from bisect_kit import cros_util
from bisect_kit import errors
from bisect_kit import repo_util
from bisect_kit import util

logger = logging.getLogger(__name__)


def add_build_and_deploy_arguments(parser_group):
  parser_group.add_argument(
      '--goma_chromeos_dir',
      type=cli.argtype_dir_path,
      default=configure.get('GOMA_CHROMEOS_DIR'),
      help='Goma-chromeos installed directory to mount into the Chrome OS '
      'chroot')
  parser_group.add_argument(
      '--clobber-stateful',
      '--clobber_stateful',
      action='store_true',
      help='Clobber stateful partition when performing update')
  parser_group.add_argument(
      '--no-disable-rootfs-verification',
      '--no_disable_rootfs_verification',
      dest='disable_rootfs_verification',
      action='store_false',
      help="Don't disable rootfs verification after update is completed")
  parser_group.add_argument(
      '--no-mark-as-stable',
      '--no_mark_as_stable',
      dest='mark_as_stable',
      action='store_false',
      help="Don't mark repo stable")


def create_argument_parser():
  parents = [common.common_argument_parser, common.session_optional_parser]
  parser = argparse.ArgumentParser(parents=parents)
  cli.patching_argparser_exit(parser)
  parser.add_argument(
      '--rich_result',
      action='store_true',
      help='Instead of mere exit code, output detailed information in json')
  parser.add_argument(
      '--dut',
      type=cli.argtype_notempty,
      metavar='DUT',
      default=configure.get('DUT'),
      help='DUT address')
  parser.add_argument(
      'rev',
      nargs='?',
      type=cli.argtype_notempty,
      metavar='CROS_VERSION',
      default=configure.get('CROS_VERSION', ''),
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
      '--skip_sync_code',
      action='store_true',
      help='Skip the step syncing source code')
  parser.add_argument(
      '--nobuild',
      action='store_true',
      help='Stop after source code sync; do not build; imply --noimage')
  parser.add_argument(
      '--noimage',
      action='store_true',
      help='Build packages only; do not build image; imply --nodeploy')
  parser.add_argument(
      '--nodeploy', action='store_true', help='Do not deploy after build')

  group = parser.add_argument_group(title='Build and deploy options')
  add_build_and_deploy_arguments(group)

  return parser


def mark_as_stable(opts):
  overlays = util.check_output(
      'chromite/bin/cros_list_overlays', '--all',
      cwd=opts.chromeos_root).splitlines()

  # Get overlays listed in manifest file.
  known_projects = []
  for path in repo_util.list_projects(opts.chromeos_root):
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
      '--list_revisions',
      '--overlays', ':'.join(overlays),
      '--debug',
      '--all',
      'commit',
  ]  # yapf: disable
  util.check_call(*cmd, cwd=opts.chromeos_root)


def cleanup(opts):
  logger.info('clean up leftover in the source tree from the last build')
  repo_util.cleanup_unexpected_files(opts.chromeos_root)

  logger.info('clean up previous result of "mark as stable"')
  repo_util.abandon(opts.chromeos_root, 'stabilizing_branch')


def _build_packages(opts):
  logger.info('build packages')

  # chrome_root is only defined if called from chrome's switcher.
  # This means using chromeos build_packages flow to build chrome during chrome
  # bisection.
  chrome_root = getattr(opts, 'chrome_root', None)

  if not opts.goma_chromeos_dir:
    default_goma_chromeos_dir = os.path.expanduser('~/goma')
    if os.path.exists(default_goma_chromeos_dir):
      logger.debug('found %s available, use goma automatically',
                   default_goma_chromeos_dir)
      opts.goma_chromeos_dir = default_goma_chromeos_dir

  cros_util.build_packages(
      opts.chromeos_root,
      opts.board,
      chrome_root=chrome_root,
      goma_dir=opts.goma_chromeos_dir,
      afdo_use=True)


def build(opts, do_mark_as_stable=True):
  # Used many times in this function, shorten it.
  chromeos_root = opts.chromeos_root

  # create and hotfix chroot if necessary
  cros_util.prepare_chroot(chromeos_root)

  if do_mark_as_stable:
    logger.info('mark as stable')
    mark_as_stable(opts)

  if opts.noimage:
    _build_packages(opts)
    return None

  cached_name = 'bisect-%s' % util.escape_rev(opts.rev)
  image_folder = os.path.join(cros_util.cached_images_dir, opts.board,
                              cached_name)
  image_path = os.path.join(image_folder, cros_util.test_image_filename)

  # If the given version is already built, reuse it.
  chromeos_root_image_folder = os.path.join(chromeos_root, image_folder)
  chromeos_root_image_path = os.path.join(chromeos_root, image_path)
  if not os.path.exists(chromeos_root_image_path):
    _build_packages(opts)
    built_image_folder = cros_util.build_image(chromeos_root, opts.board)
    os.makedirs(os.path.dirname(chromeos_root_image_folder), exist_ok=True)
    if os.path.exists(chromeos_root_image_folder):
      os.unlink(chromeos_root_image_folder)
    # Create a relative symlink, so the condition actually only comes in here if
    # the image folder is actually missing.
    os.symlink(
        os.path.join('../../..', built_image_folder),
        chromeos_root_image_folder)

  return image_path


def build_and_deploy(opts):
  try:
    image_path = build(opts, do_mark_as_stable=opts.mark_as_stable)
  except cros_util.NeedRecreateChrootException as e:
    logger.warning('recreate chroot and retry again, reason: %s', e)
    util.check_output(
        'chromite/bin/cros_sdk', '--delete', cwd=opts.chromeos_root)
    image_path = build(opts, do_mark_as_stable=opts.mark_as_stable)

  if opts.noimage or opts.nodeploy:
    return

  image_info = cros_util.ImageInfo()
  image_info[cros_util.ImageType.DISK_IMAGE] = image_path

  cros_util.provision_image_with_retry(
      opts.chromeos_root,
      opts.dut,
      opts.board,
      image_info,
      clobber_stateful=opts.clobber_stateful,
      disable_rootfs_verification=opts.disable_rootfs_verification,
      repair_callback=cros_lab_util.repair,
      force_reboot_callback=cros_lab_util.reboot_via_servo)


def switch(opts):
  cleanup(opts)

  if not opts.skip_sync_code:
    logger.info('switch source code')
    config = dict(
        board=opts.board,
        chromeos_root=opts.chromeos_root,
        chromeos_mirror=opts.chromeos_mirror)
    spec_manager = cros_util.ChromeOSSpecManager(config)
    cache = repo_util.RepoMirror(opts.chromeos_mirror)
    code_manager = codechange.CodeManager(opts.chromeos_root, spec_manager,
                                          cache)
    code_manager.switch(opts.rev)

  if opts.nobuild:
    return

  build_and_deploy(opts)


def parse_args(args):
  parser = create_argument_parser()
  return parser.parse_args(args)


def inner_main(opts):
  # --nobuild imply --nodeploy
  if opts.nobuild:
    opts.nodeploy = True

  if opts.skip_sync_code and opts.nobuild:
    raise errors.ArgumentError('--skip_sync_code and --nobuild',
                               'nothing to do')
  if not opts.dut:
    if not opts.nodeploy:
      raise errors.ArgumentError('--dut',
                                 'DUT can be omitted only if --nodeploy')
    if not opts.board:
      raise errors.ArgumentError('--board',
                                 'board must be specified if no --dut')
  if not opts.nodeploy:
    if not cros_util.is_good_dut(opts.dut):
      logger.fatal('%r is not a good DUT', opts.dut)
      if not cros_lab_util.repair(opts.dut):
        raise errors.BrokenDutException('%r is not a good DUT' % opts.dut)
  if not opts.board:
    opts.board = cros_util.query_dut_board(opts.dut)

  if cros_util.is_cros_short_version(opts.rev):
    opts.rev = cros_util.version_to_full(opts.board, opts.rev)

  cros_util.prepare_chroot(opts.chromeos_root)

  try:
    switch(opts)
  finally:
    if not opts.nodeploy:
      # No matter switching succeeded or not, DUT must be in good state.
      # switch() already tried repairing if possible, no repair here.
      if not cros_util.is_good_dut(opts.dut):
        raise errors.BrokenDutException('%r is not a good DUT' % opts.dut)
  logger.info('done')


def switch_main(args: typing.Optional[tuple[str]]):
  common.init()
  opts = parse_args(args)
  common.config_logging(opts)
  inner_main(opts)


def main(args: typing.Optional[tuple[str]] = None) -> int:
  return bisector_cli.switch_main_wrapper(switch_main, args)


if __name__ == '__main__':
  sys.exit(main())
