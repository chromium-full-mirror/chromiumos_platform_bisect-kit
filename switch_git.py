#!/usr/bin/env python3
# Copyright 2017 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
r"""Switcher for git bisecting.

Typical usage:
  $ bisect_git.py config switch ./switch_git.py

This is actually equivalent to
  $ bisect_git.py config switch \
      sh -c 'cd $GIT_REPO && git checkout -q -f $GIT_REV'
(Note, use single quotes instead of double quotes because the variables need to
be expanded at switch time, not now.)
"""

import argparse
import logging

from bisect_kit import cli
from bisect_kit import common
from bisect_kit import configure
from bisect_kit import git_util


logger = logging.getLogger(__name__)


def create_argument_parser():
    parents = [common.create_session_optional_parser()]
    parser = argparse.ArgumentParser(parents=parents)
    cli.patching_argparser_exit(parser)
    parser.add_argument(
        'git_rev',
        nargs='?',
        type=git_util.argtype_git_rev,
        metavar='GIT_REV',
        default=configure.get('GIT_REV', ''),
        help='Git revision id',
    )
    parser.add_argument(
        '--git_repo',
        type=cli.argtype_dir_path,
        metavar='GIT_REPO',
        default=configure.get('GIT_REPO'),
        help='Git repository path',
    )

    return parser


@cli.fatal_error_handler
def main(args=None):
    parser = create_argument_parser()
    opts = parser.parse_args(args)
    common.config_logging(opts)

    git_util.checkout_version(opts.git_repo, opts.git_rev)


if __name__ == '__main__':
    main()
