#!/usr/bin/env python2
# -*- coding: utf-8 -*-
# Copyright 2017 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Helper script to manipulate chromeos DUT or query info."""
from __future__ import print_function
import argparse
import collections
import functools
import json
import logging

from bisect_kit import cli
from bisect_kit import common
from bisect_kit import cros_lab_util
from bisect_kit import cros_util
from bisect_kit import errors

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


def cmd_search_dut(opts):
  labels = []
  if opts.label:
    labels += opts.label.split(',')

  def match(info):
    if not opts.condition:
      return True

    for label in info['Labels']:
      for condition in opts.condition:
        if ':' in condition:
          keys = [condition]
        else:
          keys = [
              'board:' + condition,
              'model:' + condition,
              'sku:' + condition,
          ]
        for key in keys:
          if label.lower().startswith(key.lower()):
            return True
    return False

  for pool in opts.pools.split(','):
    print('pool:' + pool)

    group = collections.defaultdict(dict)
    counter = collections.defaultdict(collections.Counter)
    for host, info in cros_lab_util.list_host(labels=labels +
                                              ['pool:' + pool]).items():
      if not match(info):
        continue

      model = _get_label_by_prefix(info, 'model')
      if info.get('Locked'):
        state = 'Locked'
      else:
        state = info['Status']
      if state not in group[model]:
        group[model][state] = {}
      group[model][state][host] = info
      counter[model][state] += 1

    def availability(counter, model):
      return -counter[model]['Ready']

    for model in sorted(group, key=functools.partial(availability, counter)):
      print('%s\t%s' % (model, dict(counter[model])))
      for host, info in group[model].get('Ready', {}).items():
        print('\t%s\t%s' % (host, _get_label_by_prefix(info, 'board')))
        if not opts.all:
          break
    print('-' * 30)

  if not opts.all:
    print('Only list one host per model by default. Use --all to output all.')


def cmd_lock_dut(opts):
  host = cros_lab_util.dut_host_name(opts.dut)
  if opts.session:
    reason = cros_lab_util.make_lock_reason(opts.session)
  else:
    reason = opts.reason
  cros_lab_util.lock_host(host, reason)
  logger.info('%s locked', host)


def cmd_unlock_dut(opts):
  host = cros_lab_util.dut_host_name(opts.dut)
  hosts = cros_lab_util.list_host(host=host)
  assert hosts, 'host=%s does not exist' % host
  info = hosts[host]
  assert info['Locked'], '%s is not locked?' % host
  if opts.session:
    reason = cros_lab_util.make_lock_reason(opts.session)
    assert info['Lock Reason'] == reason

  cros_lab_util.unlock_host(host)
  logger.info('%s unlocked', host)


def do_allocate_dut(opts):
  """Helper of cmd_allocate_dut.

  Returns:
    (todo, host)
      todo: 'ready' or 'wait'
      host: locked host name
  """
  if not opts.model and not opts.sku:
    raise errors.ArgumentError('--model or --sku', 'need to be specified')

  reason = cros_lab_util.make_lock_reason(opts.session)
  waiting = None
  if opts.locked_dut:
    host = cros_lab_util.dut_host_name(opts.locked_dut)
    logger.info('check current status of previous locked host %s', host)
    hosts = cros_lab_util.list_host(host=host)
    if not hosts:
      logger.warning('we have ever locked %s but it disappeared now', host)
      waiting = None
    else:
      waiting = hosts[host]

      logger.info('current status=%r, locked=%s, by=%s, reason=%r',
                  waiting['Status'], waiting.get('Locked'),
                  waiting.get('Locked by'), waiting.get('Lock Reason'))
      if not waiting['Locked'] or waiting['Lock Reason'] != reason:
        # Special case: not locked by us, so do not unlock it.
        opts.locked_dut = None
        raise errors.ExternalError(
            '%s should be locked by us with reason=%r' % (host, reason))

      if waiting['Status'] == cros_lab_util.READY_STATE:
        return 'ready', host
      elif waiting['Status'] in cros_lab_util.BAD_STATES:
        logger.warning('locked host=%s, became %s; give it up', host,
                       waiting['Status'])
        waiting = None

  # No matter we have locked a host or not, check if any other hosts are
  # available.
  candidates = cros_lab_util.seek_host(
      opts.pools.split(','), opts.model, opts.sku, opts.label)

  for info in candidates:
    if info['Locked']:
      continue

    to_lock = False
    if info['Status'] == cros_lab_util.READY_STATE:
      if waiting:
        logger.info(
            'although we locked and are waiting for %s, '
            'we found another host=%s is ready', waiting['Host'], info['Host'])
      to_lock = True
    elif info['Status'] in cros_lab_util.GOOD_STATES and not waiting:
      to_lock = True

    if not to_lock:
      continue

    after_lock = cros_lab_util.lock_host(info['Host'], reason)

    if after_lock['Status'] == cros_lab_util.READY_STATE:
      # Lucky, became ready just before we lock.
      return 'ready', after_lock['Host']

    if waiting:
      logger.info('but %s became %s just before we lock it', after_lock['Host'],
                  after_lock['Status'])
      cros_lab_util.unlock_host(after_lock['Host'])
      continue

    logger.info('locked %s and wait it ready', after_lock['Host'])
    return 'wait', after_lock['Host']

  if waiting:
    logger.info('continue to wait %s', waiting['Host'])
    return 'wait', waiting['Host']

  if not candidates:
    raise errors.NoDutAvailable('all are in bad states')

  logger.info(
      'we did not lock any hosts, but are waiting %d hosts in '
      'transient states', len(candidates))
  return 'wait', None


def cmd_allocate_dut(opts):
  locked_dut = None
  try:
    todo, host = do_allocate_dut(opts)
    locked_dut = host + '.cros' if host else None
    result = {'result': todo, 'locked_dut': locked_dut}
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
  finally:
    # For any reasons, if we locked a new DUT, unlock the previous one.
    if opts.locked_dut and opts.locked_dut != locked_dut:
      cros_lab_util.unlock_host(cros_lab_util.dut_host_name(opts.locked_dut))


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

  parser_lock_dut = subparsers.add_parser(
      'lock_dut',
      help='Lock a DUT in the lab',
      description='Lock a DUT in the lab. '
      'This is simply wrapper of "atest" with additional checking.')
  group = parser_lock_dut.add_mutually_exclusive_group(required=True)
  group.add_argument('--session', help='session name; for creating lock reason')
  group.add_argument('--reason', help='specify lock reason manually')
  parser_lock_dut.add_argument('dut')
  parser_lock_dut.set_defaults(func=cmd_lock_dut)

  parser_unlock_dut = subparsers.add_parser(
      'unlock_dut',
      help='Unlock a DUT in the lab',
      description='Unlock a DUT in the lab. '
      'This is simply wrapper of "atest" with additional checking.')
  parser_unlock_dut.add_argument(
      '--session', help='session name; for checking lock reason before unlock')
  parser_unlock_dut.add_argument('dut')
  parser_unlock_dut.set_defaults(func=cmd_unlock_dut)

  parser_allocate_dut = subparsers.add_parser(
      'allocate_dut',
      help='Allocate a DUT in the lab',
      description='Allocate a DUT in the lab. It will lock a DUT in the lab '
      'for bisecting. If no DUT is available (ready), it will lock one. The '
      'caller (bisect-kit runner) of this command should keep note of the '
      'locked DUT name and retry this command again later.')
  parser_allocate_dut.add_argument(
      '--session', required=True, help='session name')
  parser_allocate_dut.add_argument(
      '--pools', required=True, help='Pools to search dut, comma separated')
  parser_allocate_dut.add_argument('--model', help='allocation criteria')
  parser_allocate_dut.add_argument('--sku', help='allocation criteria')
  parser_allocate_dut.add_argument(
      '--label', '-b', help='Additional required labels, comma separated')
  parser_allocate_dut.add_argument(
      '--locked_dut', help='Locked DUT name by last run')
  parser_allocate_dut.set_defaults(func=cmd_allocate_dut)

  parser_repair_dut = subparsers.add_parser(
      'repair_dut',
      help='Repair a DUT in the lab',
      description='Repair a DUT in the lab. '
      'This is simply wrapper of "deploy repair" with additional checking.')
  parser_repair_dut.add_argument('dut')
  parser_repair_dut.set_defaults(func=cmd_repair_dut)

  parser_search_dut = subparsers.add_parser(
      'search_dut',
      help='Search DUT with conditions',
      description='Search hosts in the lab. Grouped by model and ordered by '
      'availability. When your test could be run on many models, this command '
      'help you to decide what model to use.')
  parser_search_dut.add_argument(
      '--pools',
      default='performance,crosperf,suites',
      help='Pools to search, comma separated. The searching will be performed '
      'for each pool.')
  parser_search_dut.add_argument(
      '--label',
      '-b',
      help='Additional required labels, comma separated. '
      'If more than one, all need to be satisfied.')
  parser_search_dut.add_argument(
      '--all',
      action='store_true',
      help='List all DUTs; (list only one DUT per board by default)')
  parser_search_dut.add_argument(
      'condition',
      nargs='*',
      help='Conditions, ex. board name, model name, sku name, etc. If more '
      'than one is specified, matching any one is sufficient. If none is '
      'specified, all hosts will be listed.')
  parser_search_dut.set_defaults(func=cmd_search_dut)

  opts = parser.parse_args()
  common.config_logging(opts)
  opts.func(opts)


if __name__ == '__main__':
  main()
