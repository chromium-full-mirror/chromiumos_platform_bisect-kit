#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright 2018 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Chrome bisector to bisect a range of chrome commits.

This bisector bisects commits between branched chrome and releases.
"""
from __future__ import print_function
import logging
import os

from bisect_kit import bisector_cli
from bisect_kit import cli
from bisect_kit import codechange
from bisect_kit import configure
from bisect_kit import core
from bisect_kit import cros_util
from bisect_kit import cr_util
from bisect_kit import errors
from bisect_kit import gclient_util

logger = logging.getLogger(__name__)


def guess_chrome_version(opts, rev):
  if cros_util.is_cros_version(rev):
    assert opts.board, 'need to specify BOARD for cros version'
    chrome_version = cros_util.query_chrome_version(opts.board, rev)
    assert cr_util.is_chrome_version(chrome_version)
    logger.info('Converted given CrOS version %s to Chrome version %s', rev,
                chrome_version)
    rev = chrome_version

  return rev


def generate_action_link(action):
  if action['action_type'] == 'commit':
    repo_url = action['repo_url']
    # normalize
    if repo_url == 'https://chromium.googlesource.com/a/chromium/src.git':
      repo_url = 'https://chromium.googlesource.com/chromium/src.git'
    action['link'] = repo_url + '/+/' + action['rev']


class ChromeSrcDomain(core.BisectDomain):
  """BisectDomain for Chrome branched tree"""
  revtype = staticmethod(
      cli.argtype_multiplexer(cr_util.argtype_chrome_version,
                              cros_util.argtype_cros_version))
  intra_revtype = staticmethod(
      codechange.argtype_intra_rev(cr_util.argtype_chrome_version))
  help = globals()['__doc__']

  @staticmethod
  def add_init_arguments(parser):
    parser.add_argument(
        '--chrome_root',
        required=True,
        metavar='CHROME_ROOT',
        type=cli.argtype_dir_path,
        default=configure.get('CHROME_ROOT'),
        help='Root of chrome source tree, like ~/chromium')
    parser.add_argument(
        '--chrome_mirror',
        required=True,
        metavar='CHROME_MIRROR',
        type=cli.argtype_dir_path,
        default=configure.get('CHROME_MIRROR'),
        help='gclient cache dir')

    # Only used for Chrome on ChromeOS.
    parser.add_argument(
        '--dut',
        type=cli.argtype_notempty,
        metavar='DUT',
        default=configure.get('DUT'),
        help='For ChromeOS, address of DUT (Device Under Test)')
    parser.add_argument(
        '--board',
        metavar='BOARD',
        default=configure.get('BOARD'),
        help='For ChromeOS, board name')

  @staticmethod
  def init(opts):
    chrome_src = os.path.join(opts.chrome_root, 'src')
    if not os.path.exists(chrome_src):
      raise errors.ArgumentError('--chrome_root',
                                 "chrome src directory doesn't exist")

    if opts.dut:
      if not cros_util.is_dut(opts.dut):
        raise errors.ArgumentError('--dut', 'invalid DUT or unable to connect')

      if not opts.board:
        opts.board = cros_util.query_dut_board(opts.dut)

    old = guess_chrome_version(opts, opts.old)
    new = guess_chrome_version(opts, opts.new)
    if old == new:
      raise errors.ArgumentError(
          '--old and --new', 'start and end of chrome versions are identical')

    if not cr_util.is_version_lesseq(old, new):
      raise errors.ArgumentError(
          '--old and --new',
          'start chrome version (%s) is newer than end version (%s)' %
          (old, new))

    config = dict(
        chrome_root=opts.chrome_root,
        old=old,
        new=new,
        board=opts.board,
        dut=opts.dut,
        chrome_mirror=opts.chrome_mirror)

    spec_manager = cr_util.ChromeSpecManager(config)
    cache = gclient_util.GclientCache(opts.chrome_mirror)

    # Initial sync to get all buildspecs.
    release_deps_file = cr_util.ChromeSpecManager.get_release_deps(new)
    path = os.path.join(opts.chrome_root, 'buildspec', release_deps_file)
    if not os.path.exists(path):
      spec_manager.sync_to_release(new)

    for rev in (old, new):
      release_deps_file = cr_util.ChromeSpecManager.get_release_deps(rev)
      path = os.path.join(opts.chrome_root, 'buildspec', release_deps_file)
      if not os.path.exists(path):
        raise errors.InternalError("%s doesn't exist" % path)

    if not cr_util.is_direct_relative_version(old, new):
      logger.warning('old=%s is not parent of new=%s', old, new)
      old_ancestor = spec_manager.enumerate_ancestor(old)
      new_ancestor = spec_manager.enumerate_ancestor(new)
      for rev in reversed(old_ancestor):
        if rev in new_ancestor:
          lowest_common_ancestor = rev
          break
      else:
        raise errors.InternalError(
            'Unable to find their common ancestor: %s, %s' % (old, new))
      logger.warning(
          'Assume their lowest common ancestor, %s,'
          'still have expected old behavior as %s', lowest_common_ancestor, old)
      config['old'] = old = lowest_common_ancestor

    # TODO(kcwu): verify full git history is available.
    code_manager = codechange.CodeManager(opts.chrome_root, spec_manager, cache)
    revlist = code_manager.build_revlist(old, new)

    return config, revlist

  def __init__(self, config):
    self.config = config

  def setenv(self, env, rev):
    env['CHROME_ROOT'] = self.config['chrome_root']
    env['CHROME_MIRROR'] = self.config['chrome_mirror']
    env['REV'] = rev

    if self.config['board']:
      env['BOARD'] = self.config['board']
    if self.config['dut']:
      env['DUT'] = self.config['dut']

  def fill_candidate_summary(self, summary, interesting_indexes):
    if 'current_range' in summary:
      old, new = summary['current_range']
      old_base, _, _ = codechange.parse_intra_rev(old)
      _, new_next, _ = codechange.parse_intra_rev(new)

      log_url = (
          'https://chromium.googlesource.com/chromium/src/+log/%s..%s?n=10000' %
          (old_base, new_next))
      summary['links'] = [
          {
              'name': 'change_list',
              'url': log_url,
              'note':
                  'The link of change list only lists chrome src/ commits. For '
                  'example, commits inside v8 and third party repos are not '
                  'listed.',
          },
          {
              'name': 'fuller',
              'url': log_url + '&pretty=fuller',
          },
      ]

    cache = gclient_util.GclientCache(self.config['chrome_mirror'])
    spec_manager = cr_util.ChromeSpecManager(self.config)
    code_manager = codechange.CodeManager(self.config['chrome_root'],
                                          spec_manager, cache)
    for i in interesting_indexes:
      rev_info = summary['rev_info'][i]
      rev_info.update(code_manager.get_rev_detail(rev_info['rev']))
      for action in rev_info.get('actions', []):
        generate_action_link(action)


if __name__ == '__main__':
  bisector_cli.BisectorCommandLine(ChromeSrcDomain).main()
