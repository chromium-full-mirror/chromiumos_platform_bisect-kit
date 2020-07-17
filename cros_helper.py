#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright 2017 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Helper script to manipulate chromeos DUT or query info."""
from __future__ import print_function
import asyncio
import argparse
import json
import logging
import random
import time

from bisect_kit import cli
from bisect_kit import common
from bisect_kit import cros_lab_util
from bisect_kit import cros_util
from bisect_kit import errors

DEFAULT_DUT_POOL = 'DUT_POOL_QUOTA'
logger = logging.getLogger(__name__)


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
  assert cros_util.is_dut(opts.dut)
  cros_util.reboot(opts.dut)


def _get_label_by_prefix(info, prefix):
  for label in info['Labels']:
    if label.startswith(prefix + ':'):
      return label
  return None


def cmd_lease_dut(opts):
  if opts.duration is not None and opts.duration < 60:
    raise errors.ArgumentError('--duration', 'must be at least 60 seconds')
  host = cros_lab_util.dut_host_name(opts.dut)
  logger.info('trying to lease %s', host)
  if cros_lab_util.skylab_lease_dut(host, opts.duration):
    logger.info('leased %s', host)
  else:
    raise Exception('unable to lease %s' % host)


def cmd_release_dut(opts):
  host = cros_lab_util.dut_host_name(opts.dut)
  cros_lab_util.skylab_release_dut(host)
  logger.info('%s released', host)


def verify_dimensions_by_lab(dimensions):
  result = []
  bots_dimensions = cros_lab_util.swarming_bots_dimensions()
  for dimension in dimensions:
    key, value = dimension.split(':', 1)
    if value in bots_dimensions.get(key, []):
      result.append(dimension)
    else:
      logger.warning('dimension=%s is unknown in the lab, typo? ignored',
                     dimension)
  return result


def select_available_bots_randomly(dimensions, variants, num=1, is_busy=None):
  bots = []
  for variant in variants:
    # There might be thousand bots available, set 'limit' to reduce swarming
    # API cost. This is not uniform random, but should be good enough.
    bots += cros_lab_util.swarming_bots_list(
        dimensions + [variant], is_busy=is_busy, limit=10)
  if not bots:
    return None
  return random.sample(bots, min(num, len(bots)))


async def lease_dut_parallelly(duration, bots, timeout=None):
  tasks = []
  hosts = []
  for bot in bots:
    host = bot['dimensions']['dut_name'][0]
    hosts.append(host)
    tasks.append(
        asyncio.create_task(cros_lab_util.async_lease(host, duration=duration)))

  try:
    logger.info('trying to lease %d DUTs: %s', len(hosts), hosts)
    for coro in asyncio.as_completed(tasks, timeout=timeout):
      host = await coro
      if host:
        logger.info('leased %s', host)
        # Unfinished lease tasks will be cancelled when asyncio.run is finishing.
        return host
    return None
  except asyncio.TimeoutError:
    return None


def do_allocate_dut(opts):
  """Helper of cmd_allocate_dut.

  Returns:
    (todo, host)
      todo: 'ready' or 'wait'
      host: leased host name
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
  if not opts.dut_name:
    dimensions.append('label-pool:' + opts.pool)

  variants = []
  if opts.board:
    for board in opts.board.split(','):
      if opts.version_hint:
        versions = opts.version_hint.split(',')
        if not all(cros_util.has_test_image(board, v) for v in versions):
          logger.warning(
              'board=%s does not have prebuilt test image for %s, ignore',
              board, opts.version_hint)
          continue
      variants.append('label-board:' +
                      cros_lab_util.normalize_board_name(board))
  if opts.model:
    for model in opts.model.split(','):
      variants.append('label-model:' + model)
  if opts.sku:
    for sku in opts.sku.split(','):
      variants.append('label-hwid_sku:' + cros_lab_util.normalize_sku_name(sku))
  if opts.dut_name:
    for dut_name in opts.dut_name.split(','):
      variants.append('dut_name:' + dut_name)

  verified_variants = verify_dimensions_by_lab(variants)
  if not verified_variants:
    raise errors.NoDutAvailable('No valid constraints (%s)' % variants)
  variants = verified_variants

  while True:
    # Query every time because each iteration takes a few minutes
    bots = select_available_bots_randomly(
        dimensions, variants, num=opts.parallel, is_busy=False)
    if not bots:
      bots = select_available_bots_randomly(
          dimensions, variants, num=opts.parallel, is_busy=True)
    if not bots:
      raise errors.NoDutAvailable(
          'no bots satisfy constraints; all are in maintenance state?')

    remaining_time = opts.time_limit - (time.time() - t0)
    if remaining_time <= 0:
      break
    timeout = min(120, remaining_time)
    host = asyncio.run(lease_dut_parallelly(opts.duration, bots, timeout))
    if host:
      return 'ready', host
    time.sleep(1)

  logger.warning('unable to lease DUT in time limit')
  return 'wait', None


def cmd_allocate_dut(opts):
  leased_dut = None
  try:
    todo, host = do_allocate_dut(opts)
    leased_dut = cros_lab_util.dut_name_to_address(host) if host else None
    result = {'result': todo, 'leased_dut': leased_dut}
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
  parser = argparse.ArgumentParser()
  cli.patching_argparser_exit(parser)
  common.add_common_arguments(parser)
  subparsers = parser.add_subparsers(
      dest='command', title='commands', metavar='<command>')

  parser_version_info = subparsers.add_parser(
      'version_info',
      help='Query version info of given chromeos build',
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
      'query_dut_board', help='Query board name of given DUT')
  parser_query_dut_board.add_argument('dut')
  parser_query_dut_board.set_defaults(func=cmd_query_dut_board)

  parser_reboot = subparsers.add_parser(
      'reboot',
      help='Reboot a DUT',
      description='Reboot a DUT and verify the reboot is successful.')
  parser_reboot.add_argument('dut')
  parser_reboot.set_defaults(func=cmd_reboot)

  parser_lease_dut = subparsers.add_parser(
      'lease_dut',
      help='Lease a DUT in the lab',
      description='Lease a DUT in the lab. '
      'This is implemented by `skylab lease-dut` with additional checking.')
  # "skylab lease-dut" doesn't take reason, so this is not required=True.
  parser_lease_dut.add_argument('--session', help='session name')
  parser_lease_dut.add_argument('dut')
  parser_lease_dut.add_argument(
      '--duration',
      type=float,
      help='duration in seconds; will be round to minutes')
  parser_lease_dut.set_defaults(func=cmd_lease_dut)

  parser_release_dut = subparsers.add_parser(
      'release_dut',
      help='Release a DUT in the lab',
      description='Release a DUT in the lab. '
      'This is implemented by `skylab release-dut` with additional checking.')
  parser_release_dut.add_argument('--session', help='session name')
  parser_release_dut.add_argument('dut')
  parser_release_dut.set_defaults(func=cmd_release_dut)

  parser_allocate_dut = subparsers.add_parser(
      'allocate_dut',
      help='Allocate a DUT in the lab',
      description='Allocate a DUT in the lab. It will lease a DUT in the lab '
      'for bisecting. The caller (bisect-kit runner) of this command should '
      'retry this command again later if no DUT available now.')
  parser_allocate_dut.add_argument(
      '--session', required=True, help='session name')
  parser_allocate_dut.add_argument(
      '--pool', help='Pool to search DUT', default=DEFAULT_DUT_POOL)
  group = parser_allocate_dut.add_mutually_exclusive_group(required=True)
  group.add_argument('--board', help='allocation criteria; comma separated')
  group.add_argument('--model', help='allocation criteria; comma separated')
  group.add_argument('--sku', help='allocation criteria; comma separated')
  group.add_argument('--dut_name', help='allocation criteria; comma separated')
  parser_allocate_dut.add_argument(
      '--version_hint', help='chromeos version; comma separated')
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
  parser_allocate_dut.set_defaults(func=cmd_allocate_dut)

  parser_repair_dut = subparsers.add_parser(
      'repair_dut',
      help='Repair a DUT in the lab',
      description='Repair a DUT in the lab. '
      'This is simply wrapper of "deploy repair" with additional checking.')
  parser_repair_dut.add_argument('dut')
  parser_repair_dut.set_defaults(func=cmd_repair_dut)

  opts = parser.parse_args()
  common.config_logging(opts)

  # It's optional by default since python3.
  if not opts.command:
    parser.error('command is missing')
  opts.func(opts)


if __name__ == '__main__':
  main()
