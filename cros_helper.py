#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright 2017 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Helper script to manipulate chromeos DUT or query info."""
from __future__ import print_function
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
from bisect_kit import util

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


def do_allocate_dut(opts):
  """Helper of cmd_allocate_dut.

  Returns:
    (todo, host)
      todo: 'ready' or 'wait'
      host: leased host name
  """
  if not opts.model and not opts.sku:
    raise errors.ArgumentError('--model or --sku', 'need to be specified')

  t0 = time.time()
  dimensions = ['dut_state:ready', 'label-pool:DUT_POOL_QUOTA']
  if opts.model:
    dimensions.append('label-model:' + opts.model)
  if opts.sku:
    dimensions.append('label-hwid_sku:' +
                      cros_lab_util.normalize_sku_name(opts.sku))

  while True:
    # Query every time because each iteration takes 10+ minutes
    bots = cros_lab_util.swarming_bots_list(dimensions, is_busy=False)
    if not bots:
      bots = cros_lab_util.swarming_bots_list(dimensions)
    if not bots:
      raise errors.NoDutAvailable(
          'no bots satisfy constraints; incorrect model/sku? %s' % dimensions)

    bot = random.choice(bots)
    host = bot['dimensions']['dut_name'][0]
    logger.info('trying to lease %s', host)
    remaining_time = opts.time_limit - (time.time() - t0)
    if remaining_time <= 0:
      break
    try:
      if cros_lab_util.skylab_lease_dut(
          host, opts.duration, timeout=remaining_time):
        logger.info('leased %s (bot_id=%s)', host, bot['bot_id'])
        dut = host + '.cros'
        if cros_util.is_good_dut(dut):
          return 'ready', host
        logger.warning('the leased DUT is broken; '
                       'return it and lease another one later')
        cros_lab_util.skylab_release_dut(host)
    except util.TimeoutExpired:
      break
    time.sleep(1)

  logger.warning('unable to lease DUT in time limit')
  return 'wait', None


def cmd_allocate_dut(opts):
  leased_dut = None
  try:
    todo, host = do_allocate_dut(opts)
    leased_dut = host + '.cros' if host else None
    # TODO(kcwu): remove "locked_dut" after bkr updated
    result = {
        'result': todo,
        'locked_dut': leased_dut,
        'leased_dut': leased_dut
    }
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

  # TODO(kcwu): remove `lock_dut` and `unlock_dut` after bkr updated
  parser_lock_dut = subparsers.add_parser(
      'lock_dut',
      help='Lock a DUT in the lab',
      description='Lock a DUT in the lab. '
      'This is simply wrapper of "atest" with additional checking.')
  # "skylab lease-dut" doesn't take reason, so this is not required=True.
  group = parser_lock_dut.add_mutually_exclusive_group()
  group.add_argument('--session', help='session name; for creating lock reason')
  group.add_argument('--reason', help='specify lock reason manually')
  parser_lock_dut.add_argument('dut')
  parser_lock_dut.add_argument(
      '--duration',
      type=float,
      help='duration in seconds; will be round to minutes')
  parser_lock_dut.set_defaults(func=cmd_lease_dut)

  parser_unlock_dut = subparsers.add_parser(
      'unlock_dut',
      help='Unlock a DUT in the lab',
      description='Unlock a DUT in the lab. '
      'This is simply wrapper of "atest" with additional checking.')
  parser_unlock_dut.add_argument(
      '--session', help='session name; for checking lock reason before unlock')
  parser_unlock_dut.add_argument('dut')
  parser_unlock_dut.set_defaults(func=cmd_release_dut)

  parser_allocate_dut = subparsers.add_parser(
      'allocate_dut',
      help='Allocate a DUT in the lab',
      description='Allocate a DUT in the lab. It will lease a DUT in the lab '
      'for bisecting. The caller (bisect-kit runner) of this command should '
      'retry this command again later if no DUT available now.')
  parser_allocate_dut.add_argument(
      '--session', required=True, help='session name')
  parser_allocate_dut.add_argument(
      '--pools', required=True, help='Pools to search dut, comma separated')
  parser_allocate_dut.add_argument('--model', help='allocation criteria')
  parser_allocate_dut.add_argument('--sku', help='allocation criteria')
  parser_allocate_dut.add_argument(
      '--label', '-b', help='Additional required labels, comma separated')
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
