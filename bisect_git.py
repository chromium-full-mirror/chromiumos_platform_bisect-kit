#!/usr/bin/env python3
# Copyright 2017 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Git bisector to bisect a range of git commits.

Example:
  $ ./bisect_git.py init --old rev1 --new rev2 --git-repo /path/to/git/repo
  $ ./bisect_git.py config switch ./switch_git.py
  $ ./bisect_git.py config eval ./eval-manually.sh
  $ ./bisect_git.py run
"""

import argparse
import logging

from bisect_kit import bisector_cli
from bisect_kit import cli
from bisect_kit import core
from bisect_kit import errors
from bisect_kit import git_util


logger = logging.getLogger(__name__)


class GitDomain(core.BisectDomain):
    """BisectDomain for git revisions."""

    revtype = staticmethod(cli.argtype_notempty)
    help = globals()['__doc__']

    @staticmethod
    def add_init_arguments(parser):
        parser.add_argument(
            '--git-repo',
            required=True,
            type=cli.argtype_dir_path,
            help='Git repository path',
        )

    @staticmethod
    def init(opts: argparse.Namespace) -> tuple[core.BisectConfig, dict]:
        try:
            opts.old = git_util.get_commit_hash(opts.git_repo, opts.old)
        except ValueError as e:
            raise errors.ArgumentError('--old', str(e))
        try:
            opts.new = git_util.get_commit_hash(opts.git_repo, opts.new)
        except ValueError as e:
            raise errors.ArgumentError('--new', str(e))

        config = {"git_repo": opts.git_repo}
        revlist = git_util.get_revlist(opts.git_repo, opts.old, opts.new)

        details = {}
        metas = git_util.get_batch_commit_metadata(opts.git_repo, revlist)
        for rev in revlist:
            meta = metas.get(rev)
            commit_summary = git_util.CommitMeta.get_summary(meta)
            action = {'rev': rev, 'commit_summary': commit_summary}
            details[rev] = {'actions': [action]}

        return config, {'revlist': revlist}

    def __init__(self, config: core.BisectConfig):
        self.config = config

    def setenv(self, env, rev, rev_details=None):
        env['GIT_REPO'] = self.config.get('git_repo')
        env['GIT_REV'] = rev

    def fill_candidate_summary(self, summary):
        # Do nothing.
        pass


if __name__ == '__main__':
    bisector_cli.BisectorCommandLine(GitDomain).main()
