#!/usr/bin/env python3
# Copyright 2017 The ChromiumOS Authors
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

# Note, this script only works for the main branch of chrome.

from __future__ import annotations

import argparse
import logging
import os
import typing

from bisect_kit import bisector_cli
from bisect_kit import cli
from bisect_kit import core
from bisect_kit import cr_util
from bisect_kit import cros_lab_util
from bisect_kit import cros_util
from bisect_kit import errors
from bisect_kit import gclient_util
from bisect_kit import git_util


logger = logging.getLogger(__name__)

revtype = cli.argtype_multiplexer(
    cr_util.argtype_chrome_version,
    cros_util.argtype_cros_version,
    git_util.argtype_git_rev,
    cli.argtype_re(r'^#(\d+)$', '#12345'),
)

chrome_repo_url = 'https://chromium.googlesource.com/chromium/src'


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
            raise errors.ArgumentError(
                '--dut and --board',
                'You specified ChromeOS version number, '
                'thus either DUT or BOARD is required',
            )
        assert opts.board
        chrome_version = cros_util.query_chrome_version(opts.board, rev)
        assert cr_util.is_chrome_version(chrome_version)
        logger.info(
            'Converted given CrOS version %s to Chrome version %s',
            rev,
            chrome_version,
        )
        rev = chrome_version

    chrome_src = os.path.join(opts.chrome_root, 'src')

    if cr_util.is_chrome_version(rev):
        if not git_util.is_containing_commit(chrome_src, rev):
            git_util.fetch(chrome_src, '--tags')
    else:
        git_util.fetch(chrome_src)

    git_rev = cr_util.query_git_rev(chrome_src, rev)
    logger.info('Git hash of %s is %s', rev, git_rev)
    assert git_util.is_containing_commit(chrome_src, git_rev)
    return git_rev


def verify_gclient_dep(chrome_root):
    """Makes sure gclient deps_file follow git checkout."""
    context: dict[str, typing.Any] = {}
    with open(os.path.join(chrome_root, '.gclient')) as f:
        code = compile(f.read(), '.gclient', 'exec')
        # pylint: disable=exec-used
        exec(code, context)
    for solution in context.get('solutions', []):
        if solution['name'] == 'src':
            return (
                'deps_file' not in solution
                or solution['deps_file'] == '.DEPS.git'
            )
    return False


class ChromeSrcMasterDomain(core.BisectDomain):
    """BisectDomain for Chrome main tree"""

    revtype = staticmethod(revtype)
    help = globals()['__doc__']

    @staticmethod
    def add_init_arguments(parser):
        parser.add_argument(
            '--chrome-root',
            required=True,
            metavar='CHROME_ROOT',
            type=cli.argtype_dir_path,
            default=os.environ.get('CHROME_ROOT'),
            help='Root of chrome source tree, like ~/chromium',
        )

        # Only used for Chrome on ChromeOS.
        parser.add_argument(
            '--dut',
            type=cli.argtype_notempty,
            metavar='DUT',
            default=os.environ.get('DUT'),
            help='For ChromeOS, address of DUT (Device Under Test)',
        )
        parser.add_argument(
            '--board',
            metavar='BOARD',
            default=os.environ.get('BOARD'),
            help='For ChromeOS, board name',
        )
        parser.add_argument(
            '--public-build',
            action='store_true',
            help='Use public build artifacts instead of internal ones.',
        )

    @staticmethod
    def init(opts: argparse.Namespace) -> tuple[core.BisectConfig, dict]:
        chrome_src = os.path.join(opts.chrome_root, 'src')
        if not os.path.exists(chrome_src):
            raise errors.ArgumentError(
                '--chrome-src', "chrome src directory doesn't exist"
            )

        if opts.dut:
            if cros_lab_util.is_satlab_dut(opts.dut):
                cros_lab_util.write_satlab_ssh_config(opts.dut)
            assert cros_util.is_dut(opts.dut)
            if not opts.board:
                opts.board = cros_util.query_dut_board(opts.dut)

        # b/227415522: Sync Chrome source tree to old and new to fetch unreachable
        # commits.
        gclient_util.sync(opts.chrome_root, revision=opts.new)
        gclient_util.sync(opts.chrome_root, revision=opts.old)

        old = guess_git_rev(opts, opts.old)
        new = guess_git_rev(opts, opts.new)

        revlist = git_util.get_revlist(chrome_src, old, new)

        # If opts.old is chromeos version or chrome version, `old` is pointed to a
        # git tag commit. But what we used to bisect is the parent of the tag. In
        # other words, `old` is not in the returned list of get_revlist(). Here I
        # don't verify the commit graph carefully and just re-assign revlist[0] as
        # `old`.
        old = revlist[0]

        config = {
            "chrome_root": opts.chrome_root,
            "chrome_src": chrome_src,
            "old": old,
            "new": new,
            "board": opts.board,
            "dut": opts.dut,
            "is_public_build": opts.public_build,
        }

        details = {}
        for rev in revlist:
            link = '%s/+/%s' % (chrome_repo_url, rev)
            action = {'link': link}
            details[rev] = {'actions': [action]}

        return config, {'revlist': revlist, 'details': details}

    def __init__(self, config: core.BisectConfig):
        self.config = config

    def setenv(self, env, rev, rev_details=None):
        pass

    def get_extra_args(
        self, phase: core.Phase, rev: str, _rev_details=None
    ) -> list[str]:
        if phase in (core.Phase.SWITCH, core.Phase.FUTURE_BUILD):
            args = [
                '--chrome-root',
                self.config.get('chrome_root'),
            ]
            if self.config.get('board'):
                args += ['--board', self.config.get('board')]
            args += ['--chrome-rev', rev]
            return args
        return []

    def fill_candidate_summary(self, summary):
        if 'current_range' in summary:
            old, new = summary['current_range']
            log_url = '%s/+log/%s..%s?n=10000' % (chrome_repo_url, old, new)
            summary['links'] = [
                {
                    'name': 'change_list',
                    'url': log_url,
                    'note': 'The link of change list only lists chrome src/ commits. For '
                    'example, commits inside v8 and third party repos are not '
                    'listed.',
                },
                {
                    'name': 'fuller',
                    'url': log_url + '&pretty=fuller',
                },
            ]


if __name__ == '__main__':
    bisector_cli.BisectorCommandLine(ChromeSrcMasterDomain).main()
