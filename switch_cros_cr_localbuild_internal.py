#!/usr/bin/env python2
# -*- coding: utf-8 -*-
# Copyright 2018 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Switcher for chrome on chromeos local internal build bisecting

This script will sync the chrome source tree to the specified version, build,
and deploy.

Typical usage companion with bisect_cr_localbuild_master:
  $ ./bisect_cr_localbuild_internal.py config switch \
      ./switch_cros_cr_localbuild_internal.py

By default, it will build and deploy chrome. You can specify --target for
alternative binaries.
"""
from __future__ import print_function
import argparse
import logging
import os

from bisect_kit import cli
from bisect_kit import codechange
from bisect_kit import common
from bisect_kit import configure
from bisect_kit import cros_util
from bisect_kit import cr_util
from bisect_kit import errors
from bisect_kit import gclient_util
from bisect_kit import util
import bisect_cr_localbuild_internal

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
      help='Address of DUT')
  parser.add_argument(
      'rev',
      nargs='?',
      type=cli.argtype_notempty,
      metavar='REV',
      default=configure.get('REV', ''),
      help='ChromeOS version, Chrome version, or Chrome local build intra '
      'version (in format "%s"). For ChromeOS version, the corresponding '
      'Chrome version will be determined by ChromeOS build.' %
      codechange.make_intra_rev('X', 'Y', 3))
  parser.add_argument(
      '--board',
      metavar='BOARD',
      default=configure.get('BOARD'),
      help='ChromeOS board name')
  parser.add_argument(
      '--chrome_root',
      metavar='CHROME_ROOT',
      type=cli.argtype_dir_path,
      default=configure.get('CHROME_ROOT', ''),
      help='Root of Chrome source tree, like ~/chromium')
  parser.add_argument(
      '--chrome_mirror',
      metavar='CHROME_MIRROR',
      type=cli.argtype_dir_path,
      default=configure.get('CHROME_MIRROR', ''),
      help='gclient cache dir')
  parser.add_argument(
      '--target',
      action='append',
      help='Binary to build and deploy, like unittest or fuzzers. '
      'Example value: "video_decode_accelerator_unittest". '
      'This option could be specified multiple times.')
  parser.add_argument(
      '--nobuild',
      action='store_true',
      help='Sync source code only; do not build; imply --nodeploy')
  parser.add_argument(
      '--nodeploy', action='store_true', help='Do not deploy after build')
  return parser


def _apply_additional_patch(root_dir, rev):
  # TODO(kcwu): generalize patching mechanism to make it user configurable.
  base_rev, _, _ = codechange.parse_intra_rev(rev)

  # Hack to disable assistant
  # Assistant use custom build rules, which may break build randomly (see
  # b/126633034 and b/127926563).
  # This patch file can support up to 73.0.3669.0, for easier versions, let
  # it go (just rely on retry).
  if (util.is_version_lesseq('73.0.3669.0', base_rev) and
      util.is_version_lesseq(base_rev, '74.0.3699.0')):
    patch_file = os.path.join(common.BISECT_KIT_ROOT,
                              'patching/0001-disable-assistant.patch')
    path = os.path.join(root_dir, 'src')
    util.check_call('git', 'am', patch_file, cwd=path)


def main(args=None):
  common.init()
  parser = create_argument_parser()
  opts = parser.parse_args(args)
  common.config_logging(opts)

  if not opts.board:
    opts.board = cros_util.query_dut_board(opts.dut)

  opts.chrome_src = os.path.join(opts.chrome_root, 'src')
  assert os.path.exists(opts.chrome_src)

  rev = bisect_cr_localbuild_internal.guess_chrome_version(opts, opts.rev)
  config = dict(chrome_root=opts.chrome_root, chrome_mirror=opts.chrome_mirror)

  cache = gclient_util.GclientCache(opts.chrome_mirror)
  spec_manager = cr_util.ChromeSpecManager(config)
  code_manager = codechange.CodeManager(opts.chrome_root, spec_manager, cache)

  if cr_util.is_chrome_version(rev):
    release_deps_file = spec_manager.get_release_deps(rev)
    path = os.path.join(spec_manager.spec_dir, release_deps_file)
    if not os.path.exists(path):
      raise errors.ArgumentError(
          'rev',
          '%s does not exists; run setup_cros_bisect.py sync first' % path)

  logger.info('switch source code')
  code_manager.switch(rev)
  _apply_additional_patch(opts.chrome_root, rev)

  if opts.nobuild:
    return

  gclient_util.runhook(opts.chrome_root)

  # TODO(kcwu): support local patch.
  cr_util.build_and_deploy(
      opts.chrome_src,
      opts.board,
      opts.dut,
      opts.target,
      nodeploy=opts.nodeploy)


if __name__ == '__main__':
  main()
