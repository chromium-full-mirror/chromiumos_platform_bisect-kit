#!/usr/bin/env python2
# -*- coding: utf-8 -*-
# Copyright 2017 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Git bisector to bisect a range of git commits.

Example:
  $ ./bisect_git.py init --old rev1 --new rev2 --git_repo /path/to/git/repo
  $ ./bisect_git.py config switch ./switch_git.py
  $ ./bisect_git.py config eval ./eval-manually.sh
  $ ./bisect_git.py run
"""

from __future__ import print_function
import logging
import subprocess

from bisect_kit import cli
from bisect_kit import core
from bisect_kit import errors
from bisect_kit import git_util

logger = logging.getLogger(__name__)


class GitDomain(core.BisectDomain):
  """BisectDomain for git revisions."""
  revtype = staticmethod(git_util.argtype_git_rev)
  help = globals()['__doc__']

  @staticmethod
  def add_init_arguments(parser):
    parser.add_argument(
        '--git_repo',
        required=True,
        type=cli.argtype_dir_path,
        help='Git repository path')

  @staticmethod
  def init(opts):
    if not git_util.is_containing_commit(opts.git_repo, opts.old):
      raise errors.ArgumentError(
          '--old', '%s is not in git_repo=%r' % (opts.old, opts.git_repo))
    if not git_util.is_containing_commit(opts.git_repo, opts.new):
      raise errors.ArgumentError(
          '--new', '%s is not in git_repo=%r' % (opts.new, opts.git_repo))

    config = dict(git_repo=opts.git_repo)
    revlist = git_util.get_revlist(opts.git_repo, opts.old, opts.new)
    return config, revlist

  def __init__(self, config):
    self.config = config

  def setenv(self, env, rev):
    env['GIT_REPO'] = self.config['git_repo']
    env['GIT_REV'] = rev

  def fill_candidate_summary(self, summary, interesting_indexes):
    for i in interesting_indexes:
      rev_info = summary['rev_info'][i]
      rev = rev_info['rev']
      try:
        commit_summary = git_util.get_commit_log(self.config['git_repo'],
                                                 rev).splitlines()[0]
      except subprocess.CalledProcessError:
        logger.warning('failed to get commit log of %s at %s', rev[:10],
                       self.config['git_repo'])
        commit_summary = '(unknown)'
      text = 'commit %s %r' % (rev[:10], commit_summary)
      rev_info.update({
          'actions': [{
              'text': text,
          }],
      })


if __name__ == '__main__':
  cli.BisectorCommandLine(GitDomain).main()
