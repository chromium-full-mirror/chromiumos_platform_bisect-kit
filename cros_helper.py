#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright 2017 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Helper script to manipulate chromeos DUT or query info."""
from __future__ import print_function
import argparse
import asyncio
import json
import logging
import os
import random
import time

from bisect_kit import cli
from bisect_kit import common
from bisect_kit import configure
from bisect_kit import cros_lab_util
from bisect_kit import cros_util
from bisect_kit import errors

DEFAULT_DUT_POOL = 'DUT_POOL_QUOTA'
logger = logging.getLogger(__name__)

models_to_avoid = {
    # model: reason
}


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
  if not cros_util.is_dut(opts.dut):
    if opts.force:
      logger.warning('%s is not a Chrome OS device?', opts.dut)
    else:
      raise errors.ArgumentError(
          'dut', 'not a Chrome OS device (--force to continue)')

  cros_util.reboot(
      opts.dut, force_reboot_callback=cros_lab_util.reboot_via_servo)


def _get_label_by_prefix(info, prefix):
  for label in info['Labels']:
    if label.startswith(prefix + ':'):
      return label
  return None


def cmd_lease_dut(opts):
  if opts.duration is not None and opts.duration < 60:
    raise errors.ArgumentError('--duration', 'must be at least 60 seconds')
  reason = opts.reason or cros_lab_util.make_lease_reason(opts.session)
  host = cros_lab_util.dut_host_name(opts.dut)
  logger.info('trying to lease %s', host)
  if cros_lab_util.skylab_lease_dut(host, reason, opts.duration):
    logger.info('leased %s', host)
  else:
    raise Exception('unable to lease %s' % host)


def cmd_release_dut(opts):
  host = cros_lab_util.dut_host_name(opts.dut)
  cros_lab_util.skylab_release_dut(host)
  logger.info('%s released', host)


def verify_dimensions_by_lab(dimensions, tolerate_unknown=False):
  dimensions = sorted(set(dimensions))
  result = []
  bots_dimensions = cros_lab_util.swarming_bots_dimensions()
  for dimension in dimensions:
    key, value = dimension.split(':', 1)
    if value in bots_dimensions.get(key, []):
      result.append(dimension)
    else:
      if tolerate_unknown:
        logger.warning('dimension=%s is unknown in the lab, typo? ignored',
                       dimension)
      else:
        raise Exception('dimension=%s is unknown in the lab, typo? ignored' %
                        dimension)
  return result


def select_available_bots_randomly(dimensions,
                                   variants,
                                   num=1,
                                   is_busy=None,
                                   filter_func=None):
  bots = []
  for variant in variants:
    # There might be thousand bots available, set 'limit' to reduce swarming
    # API cost. This is not uniform random, but should be good enough.
    bots += cros_lab_util.swarming_bots_list(
        dimensions + [variant], is_busy=is_busy, limit=10)

  if filter_func:
    bots = list(filter(filter_func, bots))
  return random.sample(bots, min(num, len(bots)))


def filter_dimensions_by_board(boards_with_prebuilt, dimensions, pool):
  result = set()
  for dimension in dimensions:
    constraints = [dimension]
    if pool:
      constraints.append('label-pool:' + pool)
    # Ideally, we need only one result. Here we get more (limit=10) in order to
    # cope with incorrect metadata in swarming database (b/196499726).
    bots = cros_lab_util.swarming_bots_list(constraints, is_busy=None, limit=10)
    for bot in bots:
      board = bot['dimensions']['label-board'][0]
      if board not in boards_with_prebuilt:
        logger.warning(
            'dimension=%s (board=%s) does not have corresponding '
            'prebuilt image, ignore', dimension, board)
        continue
      result.add(dimension)
  return list(result)


def is_acceptable_bot(boards_with_prebuilt, bot):
  model = bot['dimensions']['label-model'][0]
  if model in models_to_avoid:
    logger.warning('model=%s is bad (reason:%s), ignore', model,
                   models_to_avoid[model])
    return False

  if boards_with_prebuilt is not None:
    # Sometimes swarming database has inconsistent records. For example,
    # label-model=kefka + label-board=strago are incorrect (should be
    # label-board=kefka). It is probably human errors (strago is kefka's
    # reference board).
    board = bot['dimensions']['label-board'][0]
    if board not in boards_with_prebuilt:
      logger.warning('%s has unexpected board=%s, ignore',
                     bot['dimensions']['dut_name'][0], board)
      return False

  return True


async def lease_dut_parallelly(duration, bots, reason, timeout=None):
  tasks = []
  hosts = []
  for bot in bots:
    host = bot['dimensions']['dut_name'][0]
    hosts.append(host)
    tasks.append(
        asyncio.create_task(
            cros_lab_util.async_lease_with_retry(
                host, reason, duration=duration)))

  try:
    logger.info('trying to lease %d DUTs: %s', len(hosts), hosts)
    for coro in asyncio.as_completed(tasks, timeout=timeout):
      host = await coro
      if host:
        logger.info('leased %s', host)
        # Unfinished lease tasks will be cancelled when asyncio.run is
        # finishing.
        return host
    return None
  except asyncio.TimeoutError:
    return None


def normalize_board_name(chromeos_root, board):
  """Normalize BOARD name.

  Here, we want to find the actual device board. Suffixes like -kernelnext will
  be removed. So we can use that name to query DUTs inside the lab.

  Args:
    chromeos_root: chromeos source root
    board: BOARD name

  Returns:
    normalized BOARD name
  """
  overlays = cros_util.parse_chromeos_overlays(chromeos_root)
  boards_info = cros_util.resolve_basic_boards(overlays)
  return boards_info[board]


def do_allocate_dut(opts):
  """Helper of cmd_allocate_dut.

  Returns:
    (todo, host, board_to_build)
      todo: 'ready' or 'wait'
      host: leased host name
      board_to_build: board name for building image
  """
  if not opts.dut_name and not opts.pool:
    raise errors.ArgumentError('--pool',
                               'need to be specified if not --dut_name')
  if opts.version_hint:
    for v in opts.version_hint.split(','):
      if cros_util.is_cros_version(v) or cros_util.is_cros_snapshot_version(v):
        continue
      raise errors.ArgumentError(
          '--version_hint',
          'should be Chrome OS version numbers, separated by comma')
  if opts.duration is not None and opts.duration < 60:
    raise errors.ArgumentError('--duration', 'must be at least 60 seconds')

  t0 = time.time()
  dimensions = ['dut_state:ready']
  if opts.dut_name:
    # If dut_name is specified, pool is ignored.
    opts.pool = None
  else:
    dimensions.append('label-pool:' + opts.pool)
  for key, value in opts.dimensions:
    dimensions.append('%s:%s' % (key, value))
  dimensions = verify_dimensions_by_lab(dimensions)

  variants = []
  if opts.board:
    for board in opts.board.split(','):
      variants.append('label-board:' +
                      normalize_board_name(opts.chromeos_root, board))
  if opts.model:
    for model in opts.model.split(','):
      if model in models_to_avoid:
        logger.warning('model=%s is bad (reason:%s), ignore', model,
                       models_to_avoid[model])
        continue
      variants.append('label-model:' + model)
    if not variants:
      raise errors.ArgumentError('--model',
                                 'all specified models are not supported')
  if opts.sku:
    for sku in opts.sku.split(','):
      variants.append('label-hwid_sku:' + cros_lab_util.normalize_sku_name(sku))
  if opts.dut_name:
    for dut_name in opts.dut_name.split(','):
      variants.append('dut_name:' + dut_name)

  variants = verify_dimensions_by_lab(variants, tolerate_unknown=True)
  if not variants:
    raise errors.NoDutAvailable(
        'Invalid constraints: %s;%s;%s;%s' %
        (opts.board, opts.model, opts.sku, opts.dut_name))

  # Filter variants by prebuilt images.
  boards_with_prebuilt = None
  if opts.version_hint:
    if not opts.builder_hint:
      opts.builder_hint = opts.board
    if not opts.builder_hint:
      raise errors.ArgumentError('--builder_hint',
                                 'must be specified along with --version_hint')
    boards_with_prebuilt = []
    versions = opts.version_hint.split(',')
    for builder in opts.builder_hint.split(','):
      if not all(cros_util.has_test_image(builder, v) for v in versions):
        logger.warning(
            'builder=%s does not have prebuilt test image for %s, ignore',
            builder, opts.version_hint)
        continue
      boards_with_prebuilt.append(
          normalize_board_name(opts.chromeos_root, builder))
    logger.info('boards with prebuilt: %s', boards_with_prebuilt)
    if not boards_with_prebuilt:
      raise errors.ArgumentError(
          '--version_hint',
          'given builders have no prebuilt for %s' % opts.version_hint)
    variants = filter_dimensions_by_board(boards_with_prebuilt, variants, None)
    if not variants:
      raise errors.NoDutAvailable(
          'Devices with specified constraints have no prebuilt. '
          'Wrong version number?')
    variants = filter_dimensions_by_board(boards_with_prebuilt, variants,
                                          opts.pool)
    if not variants:
      raise errors.NoDutAvailable(
          'The pool(%s) does not have desired boards: %s' %
          (opts.pool, boards_with_prebuilt))

  while True:
    filter_func = lambda bot: is_acceptable_bot(boards_with_prebuilt, bot)
    # Query every time because each iteration takes a few minutes
    bots = select_available_bots_randomly(
        dimensions,
        variants,
        num=opts.parallel,
        is_busy=False,
        filter_func=filter_func)
    if not bots:
      bots = select_available_bots_randomly(
          dimensions,
          variants,
          num=opts.parallel,
          is_busy=True,
          filter_func=filter_func)
    if not bots:
      raise errors.NoDutAvailable(
          'no bots satisfy constraints; all are in maintenance state?')

    remaining_time = opts.time_limit - (time.time() - t0)
    if remaining_time <= 0:
      break
    timeout = min(120, remaining_time)
    reason = cros_lab_util.make_lease_reason(opts.session)
    host = asyncio.run(
        lease_dut_parallelly(opts.duration, bots, reason, timeout))
    if host:
      # Resolve what board we should build during bisection.
      board_to_build = None
      bots = cros_lab_util.swarming_bots_list(['dut_name:' + host])
      host_board = bots[0]['dimensions']['label-board'][0]
      if opts.builder_hint:
        for builder in opts.builder_hint.split(','):
          if normalize_board_name(opts.chromeos_root, builder) == host_board:
            board_to_build = builder
            break
        else:
          raise errors.DutLeaseException('DUT with unexpected board:%s' %
                                         host_board)
      else:
        board_to_build = host_board

      return 'ready', host, board_to_build
    time.sleep(1)

  logger.warning('unable to lease DUT in time limit')
  return 'wait', None, None


def cmd_allocate_dut(opts):
  leased_dut = None
  try:
    todo, host, board = do_allocate_dut(opts)
    leased_dut = cros_lab_util.dut_name_to_address(host) if host else None
    result = {'result': todo, 'leased_dut': leased_dut, 'board': board}
    print(json.dumps(result))
  except Exception as e:
    logger.exception('cmd_allocate_dut failed')
    exception_name = e.__class__.__name__
    result = {
        'result': 'failed',
        'exception': exception_name,
        'text': str(e),
    }
    print(json.dumps(result))


def cmd_repair_dut(opts):
  cros_lab_util.repair(opts.dut)


@cli.fatal_error_handler
def main():
  common.init()
  parents = [common.common_argument_parser, common.session_optional_parser]
  parser = argparse.ArgumentParser()
  cli.patching_argparser_exit(parser)
  subparsers = parser.add_subparsers(
      dest='command', title='commands', metavar='<command>', required=True)

  parser_version_info = subparsers.add_parser(
      'version_info',
      help='Query version info of given chromeos build',
      parents=parents,
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
      'query_dut_board', help='Query board name of given DUT', parents=parents)
  parser_query_dut_board.add_argument('dut')
  parser_query_dut_board.set_defaults(func=cmd_query_dut_board)

  parser_reboot = subparsers.add_parser(
      'reboot',
      help='Reboot a DUT',
      parents=parents,
      description='Reboot a DUT and verify the reboot is successful.')
  parser_reboot.add_argument('--force', action='store_true')
  parser_reboot.add_argument('dut')
  parser_reboot.set_defaults(func=cmd_reboot)

  parser_lease_dut = subparsers.add_parser(
      'lease_dut',
      help='Lease a DUT in the lab',
      parents=[common.common_argument_parser],
      description='Lease a DUT in the lab. '
      'This is implemented by `skylab lease-dut` with additional checking.')
  group = parser_lease_dut.add_mutually_exclusive_group(required=True)
  group.add_argument('--session', help='session name')
  group.add_argument('--reason', help='specify lease reason manually')
  parser_lease_dut.add_argument('dut')
  parser_lease_dut.add_argument(
      '--duration',
      type=float,
      help='duration in seconds; will be round to minutes')
  parser_lease_dut.set_defaults(func=cmd_lease_dut)

  parser_release_dut = subparsers.add_parser(
      'release_dut',
      help='Release a DUT in the lab',
      parents=parents,
      description='Release a DUT in the lab. '
      'This is implemented by `skylab release-dut` with additional checking.')
  parser_release_dut.add_argument('dut')
  parser_release_dut.set_defaults(func=cmd_release_dut)

  parser_allocate_dut = subparsers.add_parser(
      'allocate_dut',
      help='Allocate a DUT in the lab',
      parents=[common.common_argument_parser],
      description='Allocate a DUT in the lab. It will lease a DUT in the lab '
      'for bisecting. The caller (bisect-kit runner) of this command should '
      'retry this command again later if no DUT available now.')
  parser_allocate_dut.add_argument(
      '--session', required=True, help='session name')
  parser_allocate_dut.add_argument(
      '--pool',
      help='Pool to search DUT (default: %(default)s)',
      default=DEFAULT_DUT_POOL)
  parser_allocate_dut.add_argument(
      '--dimensions',
      help='Dimension filters used for searching DUTs, in format '
      'key1=val1 key2=val2 ...',
      type=cli.argtype_key_value,
      nargs='+',
      default=[])
  group = parser_allocate_dut.add_mutually_exclusive_group(required=True)
  group.add_argument('--board', help='allocation criteria; comma separated')
  group.add_argument('--model', help='allocation criteria; comma separated')
  group.add_argument('--sku', help='allocation criteria; comma separated')
  group.add_argument('--dut_name', help='allocation criteria; comma separated')
  parser_allocate_dut.add_argument(
      '--version_hint', help='chromeos version; comma separated')
  parser_allocate_dut.add_argument(
      '--builder_hint', help='chromeos builder; comma separated')
  # Pubsub ack deadline is 10 minutes (b/143663659). Default 9 minutes with 1
  # minute buffer.
  parser_allocate_dut.add_argument(
      '--time_limit',
      type=int,
      default=9 * 60,
      help='Time limit to attempt lease in seconds (default: %(default)s)')
  parser_allocate_dut.add_argument(
      '--duration',
      type=float,
      help='lease duration in seconds; will be round to minutes')
  parser_allocate_dut.add_argument(
      '--parallel',
      type=int,
      default=1,
      help='Submit multiple lease tasks to speed up (default: %(default)d)')
  parser_allocate_dut.add_argument(
      '--chromeos_root',
      type=cli.argtype_dir_path,
      default=configure.get('DEFAULT_CHROMEOS_ROOT',
                            os.path.expanduser('~/chromiumos')),
      help='Chrome OS source tree, for overlay data (default: %(default)s)')
  parser_allocate_dut.set_defaults(func=cmd_allocate_dut)

  parser_repair_dut = subparsers.add_parser(
      'repair_dut',
      help='Repair a DUT in the lab',
      parents=parents,
      description='Repair a DUT in the lab. '
      'This is simply wrapper of "deploy repair" with additional checking.')
  parser_repair_dut.add_argument('dut')
  parser_repair_dut.set_defaults(func=cmd_repair_dut)

  opts = parser.parse_args()
  common.config_logging(opts)
  opts.func(opts)


if __name__ == '__main__':
  main()
