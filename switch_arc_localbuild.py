#!/usr/bin/env python2
# Copyright 2018 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Switcher for Android container localbuild for ChromeOS bisecting."""

from __future__ import print_function
import argparse
import logging

from bisect_kit import android_util
from bisect_kit import repo_util
from bisect_kit import arc_util
from bisect_kit import common
from bisect_kit import configure
from bisect_kit import cli

logger = logging.getLogger(__name__)


def create_argument_parser():
  parser = argparse.ArgumentParser()
  common.add_common_arguments(parser)
  parser.add_argument(
      'dut',
      nargs='?',
      type=cli.argtype_notempty,
      metavar='DUT',
      default=configure.get('DUT', ''),
      help='DUT address')
  parser.add_argument(
      'rev',
      nargs='?',
      type=cli.argtype_notempty,
      metavar='INTRA_REV',
      default=configure.get('INTRA_REV', ''),
      help='Android build id, or bisecting intra version number')
  parser.add_argument(
      '--android_root',
      type=cli.argtype_notempty,
      metavar='ANDROID_ROOT',
      default=configure.get('ANDROID_ROOT', ''),
      help='Android tree root')
  parser.add_argument(
      '--flavor',
      metavar='ANDROID_FLAVOR',
      default=configure.get('ANDROID_FLAVOR'),
      help='example: cheets_x86-user')
  parser.add_argument(
      '--nodeploy', action='store_true', help='Build only; do not deploy')
  return parser


def switch(opts):
  config = dict(android_root=opts.android_root, flavor=opts.flavor)
  mm = android_util.AndroidManifestManager(config)
  dep = repo_util.DependencyManager(opts.android_root, mm)
  dep.switch(opts.rev)

  logger.info('build')
  # TODO(kcwu): use lower job count for non distributed compilation.
  android_util.lunch(opts.android_root, opts.flavor, 'm', '-j1000')

  if not opts.nodeploy:
    logger.info('deploy')
    arc_util.push_localbuild_to_device(opts.android_root, opts.dut, opts.flavor)


def main(args=None):
  common.init()
  parser = create_argument_parser()
  opts = parser.parse_args(args)
  common.config_logging(opts)

  if not opts.flavor:
    assert opts.dut
    opts.flavor = arc_util.query_flavor(opts.dut)
    assert opts.flavor
  logger.info('use flavor=%s', opts.flavor)

  switch(opts)


if __name__ == '__main__':
  main()
