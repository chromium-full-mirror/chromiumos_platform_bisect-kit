#!/usr/bin/env python2
# Copyright 2017 The Chromium OS Authors. All rights reserved.
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
from __future__ import print_function
import argparse
import logging

from bisect_kit import cli
from bisect_kit import common
from bisect_kit import git_util
from bisect_kit import util

logger = logging.getLogger(__name__)


def create_argument_parser():
  defaults = util.DefaultConfig()
  parser = argparse.ArgumentParser()
  common.add_logging_arguments(parser, defaults)
  parser.add_argument(
      'git_rev',
      nargs='?',
      type=git_util.argtype_git_rev,
      metavar='GIT_REV',
      default=defaults.get('GIT_REV', ''),
      help='Git revision id')
  parser.add_argument(
      '--git_repo',
      type=cli.argtype_dir_path,
      metavar='GIT_REPO',
      default=defaults.get('GIT_REPO'),
      help='Git repository path')

  return parser


def main(args=None):
  parser = create_argument_parser()
  opts = parser.parse_args(args)
  common.config_logging(opts)

  git_util.checkout_version(opts.git_repo, opts.git_rev)


if __name__ == '__main__':
  main()
