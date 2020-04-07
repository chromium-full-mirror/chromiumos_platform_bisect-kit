#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright 2019 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Switcher for chromeos localbuild bisecting."""

from __future__ import print_function
import argparse
import logging
import os
import sys
import time

from bisect_kit import buildbucket_util
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


def create_argument_parser():
  parser = argparse.ArgumentParser()
  cli.patching_argparser_exit(parser)
  common.add_common_arguments(parser)
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
      '--nodeploy', action='store_true', help='Do not deploy after build')
  parser.add_argument(
      '--buildbucket_id',
      type=int,
      help='Assign a buildbucket id instead of sending a build request')
  parser.add_argument(
      '--manifest',
      help='(for testing) '
      'Assign a chromeos manifest instead of deriving from rev')
  parser.add_argument(
      '--bucket',
      default='bisector',
      help='Assign a buildbucket bucket to build it (default: %(default)s)')
  parser.add_argument(
      '--build_revlist',
      action='store_true',
      help='Force to build revlist cache again. '
      'This flag is recommended if you need to run this script independently.')

  group = parser.add_argument_group(title='Build and deploy options')
  add_build_and_deploy_arguments(group)

  return parser


def get_last_commit_time(action_groups):
  for action_group in reversed(action_groups):
    if action_group.actions:
      return action_group.actions[-1].timestamp
  return 0


def read_file(file_name):
  with open(file_name) as f:
    result = f.read()
  return result


def get_last_float_spec(float_specs, timestamp):
  for spec in reversed(float_specs):
    if spec.timestamp <= timestamp:
      return spec.name
  return None


def get_manifest(opts):
  if opts.manifest:
    return read_file(opts.manifest)
  config = dict(
      board=opts.board,
      chromeos_root=opts.chromeos_root,
      chromeos_mirror=opts.chromeos_mirror)
  manifest_internal_dir = os.path.join(opts.chromeos_mirror,
                                       'manifest-internal.git')
  spec_manager = cros_util.ChromeOSSpecManager(config)
  cache = repo_util.RepoMirror(opts.chromeos_mirror)
  code_manager = codechange.CodeManager(config['chromeos_root'], spec_manager,
                                        cache)

  old_rev, new_rev, _ = codechange.parse_intra_rev(opts.rev)
  intra_revision, diff = code_manager.get_intra_and_diff(opts.rev)
  if intra_revision == opts.rev:
    return spec_manager.get_manifest(intra_revision)

  # get specs
  fixed_specs = spec_manager.collect_fixed_spec(old_rev, new_rev)
  for spec in fixed_specs:
    spec_manager.parse_spec(spec)
  float_specs = spec_manager.collect_float_spec(old_rev, new_rev, fixed_specs)
  for spec in float_specs:
    spec_manager.parse_spec(spec)

  # apply_manifest and apply_action_groups doesn't overwrite project's revision
  # by default, so we should apply projects by reverse chronological order here

  # step1: Get a base manifest with correct projects and sources by time.
  #        Then apply diffs after intra version.
  result = repo_util.Manifest(manifest_internal_dir)
  timestamp = get_last_commit_time(diff)
  result.load_from_commit(get_last_float_spec(float_specs, timestamp))

  # manifest from manifest-internal repository might contain revision which
  # value is a branch or tag name.
  # As every project in snapshot should have a commit hash, we remove all the
  # default revisions here to make it more significant when snapshot is not
  # complete.
  result.remove_project_revision()
  result.apply_action_groups(diff)

  # step2: Apply remaining projects from intra_version snapshot.
  snapshot = repo_util.Manifest(manifest_internal_dir)
  snapshot.load_from_string(spec_manager.get_manifest(intra_revision))
  result.apply_manifest(snapshot)

  if not result.is_static_manifest():
    raise errors.ExternalError(
        'cannot recover project revision from snapshot and diff, '
        'there might be unnecessary project in manifest-internal, '
        'or snapshot might be incomplete.')
  return result.to_string()


def prepare_image(image_folder, buildbucket_id):
  image_path = os.path.join(image_folder, cros_util.test_image_filename)
  api = buildbucket_util.BuildbucketApi()
  properties = api.get_build(buildbucket_id).output.properties

  if 'artifacts' not in properties:
    raise errors.ExternalError(
        'artifacts not found in buildbucket_id: %s' % buildbucket_id)

  gs_path = 'gs://%s/%s/image.zip' % (properties['artifacts']['gs_bucket'],
                                      properties['artifacts']['gs_path'])
  cros_util.gsutil('cp', gs_path, image_folder)
  util.check_call(
      'unzip',
      '-j',
      'image.zip',
      cros_util.test_image_filename,
      cwd=image_folder)
  os.remove(os.path.join(image_folder, 'image.zip'))

  assert os.path.exists(image_path)
  return image_path


def schedule_build(board, rev, manifest, bucket):
  build_tags = {'bisector_chromeos_version': rev}
  api = buildbucket_util.BuildbucketApi()
  build_id = int(
      api.schedule_build(
          board,
          manifest,
          bucket=bucket,
          build_tags=build_tags,
      ).id)
  logger.info('schedule a build, id = %d', build_id)

  last_log_time = 0
  while api.is_running(build_id):
    now = time.time()
    if now - last_log_time >= 600:
      logger.info('Waiting for buildbucket build_id %d', build_id)
      last_log_time = now
    time.sleep(60)

  if not api.is_success(build_id):
    raise errors.ExternalError('buildbucket build fail, id = %d' % build_id)
  return build_id


def build_revlist(opts):
  old_rev, new_rev, _ = codechange.parse_intra_rev(opts.rev)
  config = dict(
      board=opts.board,
      chromeos_root=opts.chromeos_root,
      chromeos_mirror=opts.chromeos_mirror)
  spec_manager = cros_util.ChromeOSSpecManager(config)
  cache = repo_util.RepoMirror(opts.chromeos_mirror)
  code_manager = codechange.CodeManager(config['chromeos_root'], spec_manager,
                                        cache)
  code_manager.build_revlist(old_rev, new_rev)


def switch(opts):
  cached_name = 'bisect-%s' % opts.rev.replace('/', '_')
  image_folder = os.path.join(opts.chromeos_root, cros_util.cached_images_dir,
                              opts.board, cached_name)
  image_path = os.path.join(image_folder, cros_util.test_image_filename)
  if not os.path.exists(image_folder):
    os.makedirs(image_folder)

  # schedule build request and prepare image
  if not os.path.exists(image_path):
    if opts.buildbucket_id:
      buildbucket_id = opts.buildbucket_id
    else:
      buildbucket_id = schedule_build(opts.board, opts.rev, get_manifest(opts),
                                      opts.bucket)
    image_path = prepare_image(image_folder, buildbucket_id)
  rel_path = os.path.relpath(image_path, opts.chromeos_root)

  if opts.nodeploy:
    return 0

  # deploy and flash
  if cros_util.cros_flash_with_retry(
      opts.chromeos_root,
      opts.dut,
      opts.board,
      rel_path,
      clobber_stateful=opts.clobber_stateful,
      disable_rootfs_verification=opts.disable_rootfs_verification,
      repair_callback=cros_lab_util.repair):
    return 0
  return 1


@cli.fatal_error_handler
def main(args=None):
  common.init()
  parser = create_argument_parser()
  opts = parser.parse_args(args)
  common.config_logging(opts)

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
        sys.exit(cli.EXIT_CODE_FATAL)
  if not opts.board:
    opts.board = cros_util.query_dut_board(opts.dut)

  if cros_util.is_cros_short_version(opts.rev):
    opts.rev = cros_util.version_to_full(opts.board, opts.rev)

  if opts.build_revlist:
    build_revlist(opts)

  try:
    returncode = switch(opts)
  except Exception:
    logger.exception('switch failed')
    returncode = 1

  if not opts.nodeploy:
    # No matter switching succeeded or not, DUT must be in good state.
    # switch() already tried repairing if possible, no repair here.
    if not cros_util.is_good_dut(opts.dut):
      logger.fatal('%r is not a good DUT', opts.dut)
      returncode = cli.EXIT_CODE_FATAL
  logger.info('done')
  sys.exit(returncode)


if __name__ == '__main__':
  main()
