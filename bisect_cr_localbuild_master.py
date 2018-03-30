#!/usr/bin/env python2
# Copyright 2017 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Chrome bisector to bisect a range of chrome commits.

The supported version format for --old and --new are:
  - ChromeOS version: 9876.0.0 or R62-9876.0.0
  - Chrome version: 62.0.1234.0
  - Chrome commit position: #1234
  - git commit hash

Example:
  $ ./bisect_cr_localbuild_master.py init --old rev1 --new rev2 --dut DUT
  $ ./bisect_cr_localbuild_master.py config switch \
      ./switch_cros_cr_localbuild_master.py
  $ ./bisect_cr_localbuild_master.py config eval ./eval-manually.sh
  $ ./bisect_cr_localbuild_master.py run

When running switcher and evaluator, following environment variables
will be set:
  CHROME_ROOT (e.g. ~/chromium),
  CHROME_BRANCH (i.e. master),
  GIT_REPO (e.g. ~/chromium/src),
  REV (git commit hash),
  BOARD (e.g. samus, if available), and
  DUT (e.g. samus-dut, if available).
"""
# Note, this script only works for master branch of chrome.
from __future__ import print_function
import os
import logging

from bisect_kit import cli
from bisect_kit import configure
from bisect_kit import core
from bisect_kit import cros_util
from bisect_kit import cr_util
from bisect_kit import git_util
from bisect_kit import util

logger = logging.getLogger(__name__)

revtype = cli.argtype_multiplexer(cr_util.argtype_chrome_version,
                                  cros_util.argtype_cros_version,
                                  git_util.argtype_git_rev, r'^#(\d+)$')


def guess_git_rev(opts, rev):
  """Recognizes version number and determines the corresponding git hash.

  It will also fetch latest source if necessary.

  Args:
    opts: An argparse.Namespace to hold command line arguments.
    rev: could be chromeos version, chrome version, chrome commit position, or
        git commit hash

  Returns:
    full git commit hash
  """
  if cros_util.is_cros_version(rev):
    if not opts.board and not opts.dut:
      raise core.ExecutionFatalError('You specified ChromeOS version number, '
                                     'thus either DUT or BOARD is required')
    assert opts.board
    chrome_version = cros_util.query_chrome_version(opts.board, rev)
    assert cr_util.is_chrome_version(chrome_version)
    logger.info('Converted given CrOS version %s to Chrome version %s', rev,
                chrome_version)
    rev = chrome_version

  chrome_src = os.path.join(opts.chrome_root, 'src')

  if cr_util.is_chrome_version(rev):
    if not git_util.is_containing_commit(chrome_src, rev):
      util.check_call('git', 'fetch', '--tags', cwd=chrome_src)
  else:
    util.check_call('git', 'fetch', cwd=chrome_src)

  git_rev = cr_util.query_git_rev(chrome_src, rev)
  logger.info('Git hash of %s is %s', rev, git_rev)
  assert git_util.is_containing_commit(chrome_src, git_rev)
  return git_rev


def verify_gclient_dep(chrome_root):
  """Makes sure gclient deps_file follow git checkout."""
  context = {}
  exec open(os.path.join(chrome_root, '.gclient')) in context
  for solution in context['solutions']:
    if solution['name'] == 'src':
      return solution['deps_file'] == '.DEPS.git'
  return False


class ChromeSrcMasterDomain(core.BisectDomain):
  """BisectDomain for Chrome master tree"""
  revtype = staticmethod(revtype)
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
      raise core.ExecutionFatalError("chrome src directory doesn't exist")

    if opts.dut:
      assert cros_util.is_dut(opts.dut)
      if not opts.board:
        opts.board = cros_util.query_dut_board(opts.dut)

    old = guess_git_rev(opts, opts.old)
    new = guess_git_rev(opts, opts.new)

    revlist = git_util.get_revlist(chrome_src, old, new)

    # If opts.old is chromeos version or chrome version, `old` is pointed to a
    # git tag commit. But what we used to bisect is the parent of the tag. In
    # other words, `old` is not in the returned list of get_revlist(). Here I
    # don't verify the commit graph carefully and just re-assign revlist[0] as
    # `old`.
    old = revlist[0]

    config = dict(
        chrome_root=opts.chrome_root,
        chrome_src=chrome_src,
        old=old,
        new=new,
        board=opts.board,
        dut=opts.dut)
    return config, revlist

  def __init__(self, config):
    self.config = config

  def setenv(self, env, rev):
    env['CHROME_ROOT'] = self.config['chrome_root']
    env['CHROME_BRANCH'] = 'master'
    env['GIT_REPO'] = self.config['chrome_src']
    env['REV'] = rev

    if self.config['board']:
      env['BOARD'] = self.config['board']
    if self.config['dut']:
      env['DUT'] = self.config['dut']

  def view(self, old, new):
    url = ('https://chromium.googlesource.com/'
           'chromium/src/+log/{old}..{new}?pretty=fuller&n=10000').format(
               old=old[:12], new=new[:12])
    print(url)


if __name__ == '__main__':
  cli.BisectorCommandLine(ChromeSrcMasterDomain).main()
