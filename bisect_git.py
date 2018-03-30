#!/usr/bin/env python2
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

from bisect_kit import cli
from bisect_kit import core
from bisect_kit import git_util


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
    for rev in (opts.old, opts.new):
      if not git_util.is_containing_commit(opts.git_repo, rev):
        raise ValueError('rev=%s is not in git_repo=%r' % (rev, opts.git_repo))

    config = dict(git_repo=opts.git_repo)
    revlist = git_util.get_revlist(opts.git_repo, opts.old, opts.new)
    return config, revlist

  def __init__(self, config):
    self.config = config

  def setenv(self, env, rev):
    env['GIT_REPO'] = self.config['git_repo']
    env['GIT_REV'] = rev

  def view(self, old, new):
    print('git log %s..%s' % (old, new))


if __name__ == '__main__':
  cli.BisectorCommandLine(GitDomain).main()
