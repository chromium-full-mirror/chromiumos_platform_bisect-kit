#!/usr/bin/env python2
# -*- coding: utf-8 -*-
# Copyright 2017 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""ChromeOS bisector to bisect a range of chromeos versions.

Example:
  $ ./bisect_cros_version.py init --old rev1 --new rev2 --dut DUT
  $ ./bisect_cros_version.py config switch ./switch_cros_prebuilt.py
  $ ./bisect_cros_version.py config eval ./eval-manually.sh
  $ ./bisect_cros_version.py run

When running switcher and evaluator, following environment variables
will be set:
  BOARD (e.g. samus),
  CROS_FULL_VERSION (e.g. R62-9876.0.0),
  CROS_SHORT_VERSION (e.g. 9876.0.0),
  CROS_VERSION (e.g. R62-9876.0.0).
  DUT (e.g. samus-dut), and
  MILESTONE (e.g. 62).
"""

from __future__ import print_function
import re
import logging

from bisect_kit import cli
from bisect_kit import configure
from bisect_kit import core
from bisect_kit import cros_util
from bisect_kit import errors
from bisect_kit import util

logger = logging.getLogger(__name__)


def get_revlist(board, old, new, only_good_build=True):
  """Get ChromeOS version numbers between given range

  Args:
    board: ChromeOS board name
    old: start version (inclusive)
    new: end version (inclusive)
    only_good_build: only if test image is available

  Returns:
    list of version numbers (in either short or full format) between [old, new]
    range (inclusive).
  """
  logger.info('get_revlist %s %s %s', board, old, new)
  old = cros_util.version_to_short(old)
  new = cros_util.version_to_short(new)

  # First, search images from gs://chromeos-image-archive
  query_path = cros_util.gs_archive_path.format(board=board)
  if only_good_build:
    query_path += '/*/chromiumos_test_image.tar.xz'

  rev_map = {}  # dict: short version -> short or full version
  for line in cros_util.gsutil_ls(query_path):
    m = re.match(r'^gs:\S+(R\d+-\d+\.\d+\.\d+)', line)
    if m:
      full_version = m.group(1)
      rev_map[cros_util.version_to_short(full_version)] = full_version

  # Second, search images from gs://chromeos-releases
  # Because look up milestone by short version number is slow, defer the
  # look up.
  # TODO(kcwu): cache milestone number of short version number.
  for line in cros_util.gsutil_ls(
      cros_util.gs_release_path.format(
          channel='*', board=board, short_version=''),
      ignore_errors=True):
    m = re.match(r'gs:\S+/(\d+\.\d+\.\d+)/$', line)
    if m:
      short_version = m.group(1)
      if short_version not in rev_map:
        rev_map[short_version] = short_version

  result = []
  for rev in sorted(rev_map, key=util.version_key_func):
    if not util.is_direct_relative_version(new, rev):
      continue
    if not util.is_version_lesseq(old, rev):
      continue
    if not util.is_version_lesseq(rev, new):
      continue
    result.append(rev_map[rev])

  old = rev_map[old]
  new = rev_map[new]
  return old, new, result


class ChromeOSVersionDomain(core.BisectDomain):
  """BisectDomain for chromeos versions."""
  revtype = staticmethod(cros_util.argtype_cros_version)
  help = globals()['__doc__']

  @staticmethod
  def add_init_arguments(parser):
    parser.add_argument(
        '--dut',
        type=cli.argtype_notempty,
        metavar='DUT',
        default=configure.get('DUT'),
        help='Address of DUT (Device Under Test). Either --dut or '
        '--board need to be specified')
    parser.add_argument(
        '--board',
        metavar='BOARD',
        default=configure.get('BOARD'),
        help='ChromeOS board name. Either --dut or --board need '
        'to be specified')

  @staticmethod
  def init(opts):
    if not opts.dut and not opts.board:
      raise errors.ArgumentError('--dut and --board', 'Neither is specified')
    if opts.dut:
      assert cros_util.is_dut(opts.dut)
    if not opts.board:
      opts.board = cros_util.query_dut_board(opts.dut)

    old, new, revlist = get_revlist(opts.board, opts.old, opts.new, False)
    config = dict(dut=opts.dut, board=opts.board, old=old, new=new)
    return config, revlist

  def __init__(self, config):
    self.config = config

  def setenv(self, env, rev):
    if self.config['dut']:
      env['DUT'] = self.config['dut']
    env['BOARD'] = self.config['board']

    milestone, short_version = cros_util.recognize_version(
        self.config['board'], rev)
    full_version = cros_util.make_cros_full_version(milestone, short_version)
    env['MILESTONE'] = milestone
    env['CROS_SHORT_VERSION'] = short_version
    env['CROS_FULL_VERSION'] = full_version
    env['CROS_VERSION'] = full_version

  def view(self, revlist, old, new):
    old_short = cros_util.version_to_short(old)
    new_short = cros_util.version_to_short(new)
    print(
        'https://crosland.corp.google.com/log/%s..%s' % (old_short, new_short))


if __name__ == '__main__':
  cli.BisectorCommandLine(ChromeOSVersionDomain).main()
