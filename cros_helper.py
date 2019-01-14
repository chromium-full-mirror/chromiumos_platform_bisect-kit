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

from bisect_kit import common
from bisect_kit import cros_lab_util
from bisect_kit import cros_util

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
      if info.get('Locked') == 'True':
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


def main():
  common.init()
  parser = argparse.ArgumentParser()
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
