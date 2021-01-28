#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright 2019 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Switcher for chromeos localbuild bisecting."""

from __future__ import print_function
import argparse
import copy
import logging
import os
import sys
import time

from bisect_kit import buildbucket_util
from bisect_kit import cli
from bisect_kit import codechange
from bisect_kit import common
from bisect_kit import configure
from bisect_kit import cr_util
from bisect_kit import cros_lab_util
from bisect_kit import cros_util
from bisect_kit import errors
from bisect_kit import gclient_util
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


def create_common_argument_parser():
  """Common arguments parser for both Chrome and Chrome OS builds"""
  parser = argparse.ArgumentParser(add_help=False)
  parser.add_argument(
      '--dut',
      type=cli.argtype_notempty,
      metavar='DUT',
      default=configure.get('DUT'),
      help='DUT address')
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


def create_argument_parser():
  parser = argparse.ArgumentParser()
  subparsers = parser.add_subparsers(dest='subcommand', required=True)
  cli.patching_argparser_exit(parser)

  parents = [
      common.common_argument_parser,
      common.session_optional_parser,
      create_common_argument_parser(),
  ]
  # chromeos parser
  parser_cros = subparsers.add_parser(
      'bisect_chromeos', help='Bisect on Chrome OS versions', parents=parents)
  parser_cros.add_argument(
      'chromeos_rev',
      nargs='?',
      type=cli.argtype_notempty,
      metavar='CROS_VERSION',
      default=configure.get('CROS_VERSION', ''),
      help='ChromeOS local build version string, in format short version, '
      'full version, or "full,full+N"')

  # chrome parser
  parser_chrome = subparsers.add_parser(
      'bisect_chrome', help='Bisect on Chrome versions', parents=parents)
  parser_chrome.add_argument(
      '--chrome_root',
      metavar='CHROME_ROOT',
      type=cli.argtype_dir_path,
      default=configure.get('CHROME_ROOT', ''),
      help='Root of Chrome source tree, like ~/chromium')
  parser_chrome.add_argument(
      '--chrome_mirror',
      metavar='CHROME_MIRROR',
      type=cli.argtype_dir_path,
      default=configure.get('CHROME_MIRROR', ''),
      help='gclient cache dir')
  parser_chrome.add_argument(
      '--chromeos_rev',
      type=cli.argtype_notempty,
      metavar='CROS_VERSION',
      default=configure.get('CROS_VERSION', ''),
      help='ChromeOS local build version string, in format short version, '
      'full version, or "full,full+N"')
  parser_chrome.add_argument(
      '--deps',
      help='(for testing) '
      'Assign a Chrome DEPS instead of deriving from rev')
  parser_chrome.add_argument(
      'chrome_rev',
      nargs='?',
      type=cli.argtype_notempty,
      metavar='REV',
      default=configure.get('REV', ''),
      help='Chrome version, local build intra version (in format "%s").' %
      codechange.make_intra_rev('X', 'Y', 3))

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
      return spec
  raise errors.ExternalError(
      'Cannot predict a correct float spec before timestamp %d' % timestamp)


def get_chrome_managers(opts):
  cache_manager = gclient_util.GclientCache(opts.chrome_mirror)
  config = dict(chrome_root=opts.chrome_root, chrome_mirror=opts.chrome_mirror)
  spec_manager = cr_util.ChromeSpecManager(config)
  code_manager = codechange.CodeManager(opts.chrome_root, spec_manager,
                                        cache_manager)
  return spec_manager, code_manager, cache_manager


def get_chrome_buildspec_by_time(opts, timestamp, old_rev, new_rev):
  """Get a Chrome Deps buildspec (float spec) at given timestamp"""
  spec_manager, _code_manager, cache_manager = get_chrome_managers(opts)
  deps_path = spec_manager.generate_meta_deps(new_rev)
  old_timestamp = spec_manager.lookup_build_timestamp(old_rev)
  new_timestamp = spec_manager.lookup_build_timestamp(new_rev)
  if not old_timestamp <= timestamp <= new_timestamp:
    logger.warning('Adjust timestamp: [%d, %d], %d', old_timestamp,
                   new_timestamp, timestamp)
    old_timestamp = min(old_timestamp, timestamp)
    new_timestamp = max(new_timestamp, timestamp)

  result = None
  parser = gclient_util.DepsParser(opts.chrome_root, cache_manager)
  timeseries_forests = parser.enumerate_gclient_solutions(
      old_timestamp, new_timestamp, deps_path)
  for t, forest in timeseries_forests:
    if t <= timestamp:
      result = copy.deepcopy(forest)

  return parser.flatten(result, deps_path)


def get_chromeos_spec_manager(opts):
  config = dict(
      board=opts.board,
      chromeos_root=opts.chromeos_root,
      chromeos_mirror=opts.chromeos_mirror)
  return cros_util.ChromeOSSpecManager(config)


def get_deps(opts):
  if opts.deps:
    return read_file(opts.deps)

  spec_manager, code_manager, cache_manager = get_chrome_managers(opts)
  old_rev, new_rev, _ = codechange.parse_intra_rev(opts.chrome_rev)
  intra_revision, diff = code_manager.get_intra_and_diff(opts.chrome_rev)
  if intra_revision == opts.chrome_rev:
    return spec_manager.get_release_deps(intra_revision)

  # step1: Get a base deps with correct projects and sources by time.
  #        Then apply diffs after intra version.
  timestamp = get_last_commit_time(diff)
  deps = get_chrome_buildspec_by_time(opts, timestamp, old_rev, new_rev)
  deps.apply_action_groups(diff)

  # step2: Apply remaining projects from intra_version.
  parser = gclient_util.DepsParser(opts.chrome_root, cache_manager)
  intra_deps = parser.parse_single_deps(
      spec_manager.get_release_deps(intra_revision))
  deps.apply_deps(intra_deps)
  return deps.to_string()


def get_release_manifest(opts):
  """Get manifest of a release Chrome OS version or snapshot version"""
  if opts.manifest:
    return read_file(opts.manifest)
  spec_manager = get_chromeos_spec_manager(opts)
  return spec_manager.get_manifest(opts.chromeos_rev)


def get_manifest(opts):
  if opts.manifest:
    return read_file(opts.manifest)
  manifest_internal_dir = os.path.join(opts.chromeos_mirror,
                                       'manifest-internal.git')
  spec_manager = get_chromeos_spec_manager(opts)
  cache = repo_util.RepoMirror(opts.chromeos_mirror)
  code_manager = codechange.CodeManager(opts.chromeos_root, spec_manager, cache)

  old_rev, new_rev, _ = codechange.parse_intra_rev(opts.chromeos_rev)
  intra_revision, diff = code_manager.get_intra_and_diff(opts.chromeos_rev)
  if intra_revision == opts.chromeos_rev:
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
  result.load_from_commit(get_last_float_spec(float_specs, timestamp).name)

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
    raise errors.ExternalError('artifacts not found in buildbucket_id: %s' %
                               buildbucket_id)

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


def schedule_build(opts):
  build_tags = {'bisector_chromeos_version': opts.chromeos_rev}
  api = buildbucket_util.BuildbucketApi()

  if opts.subcommand == 'bisect_chrome':
    manifest = get_release_manifest(opts)
    deps = get_deps(opts)
    build_tags['bisector_chrome_version'] = opts.chrome_rev
    git_auth = False
  else:
    manifest = get_manifest(opts)
    deps = None
    git_auth = True

  build_id = int(
      api.schedule_build(
          opts.board,
          manifest,
          bucket=opts.bucket,
          build_tags=build_tags,
          deps=deps,
          git_auth=git_auth,
      ).id)
  logger.info('schedule a build, id = %d', build_id)
  logger.info(
      'build url: %s',
      buildbucket_util.get_luci_link(opts.board, build_id, bucket=opts.bucket))

  return build_id


def wait_build_complete(build_id):
  api = buildbucket_util.BuildbucketApi()
  last_log_time = 0
  while True:
    build = api.get_build(build_id)
    if not api.is_running(build):
      break
    now = time.time()
    if now - last_log_time >= 600:
      logger.info('Waiting for buildbucket build_id %d', build_id)
      last_log_time = now
    time.sleep(60)

  if not api.is_success(build):
    raise errors.ExternalError(
        'buildbucket build fail, id=%d, status=%s, summary=%r' %
        (build_id, buildbucket_util.Status.Name(
            build.status), build.summary_markdown))
  return build_id


def build_revlist(opts):
  old_rev, new_rev, _ = codechange.parse_intra_rev(opts.chromeos_rev)
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
  assert opts.board
  cached_name = 'bisect-%s' % util.escape_rev(opts.chromeos_rev)
  if opts.subcommand == 'bisect_chrome':
    cached_name += '-%s' % util.escape_rev(opts.chrome_rev)
  image_folder = os.path.join(opts.chromeos_root, cros_util.cached_images_dir,
                              opts.board, cached_name)
  image_path = os.path.join(image_folder, cros_util.test_image_filename)
  if not os.path.exists(image_folder):
    os.makedirs(image_folder)

  # schedule build request and prepare image
  buildbucket_id = None
  if not os.path.exists(image_path):
    if opts.buildbucket_id:
      buildbucket_id = opts.buildbucket_id
    else:
      buildbucket_id = schedule_build(opts)

  # If --nodeploy is given, the script will exit as soon as job scheduled and
  # ignore potential build errors.
  if opts.nodeploy:
    return 0

  if buildbucket_id:
    wait_build_complete(buildbucket_id)
    image_path = prepare_image(image_folder, buildbucket_id)
  rel_path = os.path.relpath(image_path, opts.chromeos_root)

  # deploy and flash
  if cros_util.cros_flash_with_retry(
      opts.chromeos_root,
      opts.dut,
      opts.board,
      rel_path,
      clobber_stateful=opts.clobber_stateful,
      disable_rootfs_verification=opts.disable_rootfs_verification,
      repair_callback=cros_lab_util.repair,
      force_reboot_callback=cros_lab_util.reboot_via_servo):
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

  if cros_util.is_cros_short_version(opts.chromeos_rev):
    opts.chromeos_rev = cros_util.version_to_full(opts.board, opts.chromeos_rev)

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
