#!/usr/bin/env python3
# Copyright 2018 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Android bisector to bisect local build commits.

Example:
  $ ./bisect_android_repo.py init --old rev1 --new rev2 \\
      --android-root ~/android \\
      --android-mirror ANDROID_MIRROR \\
      --branch BRANCH \\
      --flavor FLAVOR \\
      --board BOARD \\
      --dut DUT

  $ ./bisect_android_repo.py config switch ./switch_arc_localbuild.py
  $ ./bisect_android_repo.py config eval ./eval-manually.sh
  $ ./bisect_android_repo.py run

When running switcher and evaluator, following environment variables
will be set:
  ANDROID_BRANCH (e.g. git_mnc-dr-arc-dev),
  ANDROID_FLAVOR (e.g. cheets_x86-user),
  ANDROID_ROOT,
  DUT (e.g. samus-dut, if available).
"""

import argparse
import logging
import os

from bisect_kit import android_util
from bisect_kit import bisector_cli
from bisect_kit import cli
from bisect_kit import codechange
from bisect_kit import common
from bisect_kit import core
from bisect_kit import cros_util
from bisect_kit import errors
from bisect_kit import repo_util


logger = logging.getLogger(__name__)


def determine_android_build_id(opts, rev):
    """Determine android build id.

    If `rev` is ChromeOS version, query its corresponding Android build id.

    Args:
      opts: parse result of argparse
      rev: Android build id or ChromeOS version

    Returns:
      Android build id
    """
    if cros_util.is_cros_version(rev):
        android_build_id = cros_util.query_android_build_id(opts.board, rev)
        assert android_util.is_android_build_id(android_build_id)
        logger.info(
            'Converted given CrOS version %s to Android build id %s',
            rev,
            android_build_id,
        )
        rev = android_build_id
    return rev


def generate_action_link(action):
    if action['action_type'] == 'commit':
        repo_url = action['repo_url']
        action['link'] = repo_url + '/+/' + action['rev']


class AndroidRepoDomain(core.BisectDomain):
    """BisectDomain for Android code changes."""

    # Accepts Android build id or ChromeOS version
    revtype = staticmethod(
        cli.argtype_multiplexer(
            cros_util.argtype_cros_version,
            android_util.argtype_android_build_id,
        )
    )
    intra_revtype = staticmethod(
        codechange.argtype_intra_rev(android_util.argtype_android_build_id)
    )
    help = globals()['__doc__']

    @staticmethod
    def add_init_arguments(parser):
        parser.add_argument(
            '--dut',
            type=cli.argtype_notempty,
            metavar='DUT',
            default=os.environ.get('DUT'),
            help='DUT address',
        )
        parser.add_argument(
            '--android-root',
            metavar='ANDROID_ROOT',
            type=cli.argtype_dir_path,
            default=os.environ.get('ANDROID_ROOT', ''),
            help='Android tree root',
        )
        parser.add_argument(
            '--android-mirror',
            type=cli.argtype_dir_path,
            default=os.environ.get('ANDROID_MIRROR', ''),
            help='Android repo mirror path',
        )
        parser.add_argument(
            '--branch',
            type=cli.argtype_notempty,
            metavar='ANDROID_BRANCH',
            default=os.environ.get('ANDROID_BRANCH', ''),
            help='branch name like "git_mnc-dr-arc-dev"; '
            'default is auto detect via DUT',
        )
        parser.add_argument(
            '--flavor',
            type=cli.argtype_notempty,
            metavar='ANDROID_FLAVOR',
            default=os.environ.get('ANDROID_FLAVOR', ''),
            help='example: cheets_x86-user; default is auto detect via DUT',
        )
        parser.add_argument(
            '--board',
            type=cli.argtype_notempty,
            metavar='BOARD',
            default=os.environ.get('BOARD', ''),
            help='ChromeOS board name, if ARC++',
        )

    @staticmethod
    def init(opts: argparse.Namespace) -> tuple[core.BisectConfig, dict]:
        old = determine_android_build_id(opts, opts.old)
        new = determine_android_build_id(opts, opts.new)

        if int(old) >= int(new):
            raise errors.ArgumentError(
                '--old and --new', 'bad bisect range (%s, %s)' % (old, new)
            )

        config = {
            "dut": opts.dut,
            "android_root": opts.android_root,
            "android_mirror": opts.android_mirror,
            "branch": opts.branch,
            "flavor": opts.flavor,
            "old": old,
            "new": new,
        }

        spec_manager = android_util.AndroidSpecManager(config)
        cache = repo_util.RepoMirror(opts.android_mirror)
        code_manager = codechange.CodeManager(
            opts.android_root,
            spec_manager,
            cache,
            common.get_session_cache_dir(opts.session),
        )
        revlist, details = code_manager.build_revlist(old, new)
        for detail in details.values():
            for action in detail.get('actions', []):
                generate_action_link(action)

        return config, {'revlist': revlist, 'details': details}

    def __init__(self, config: core.BisectConfig):
        self.config = config

    def setenv(self, env, rev=None, rev_details=None):
        env['ANDROID_ROOT'] = self.config.get('android_root')
        env['ANDROID_FLAVOR'] = self.config.get('flavor')
        env['ANDROID_BRANCH'] = self.config.get('branch')
        env['ANDROID_MIRROR'] = self.config.get('android_mirror')
        env['INTRA_REV'] = rev

    def fill_candidate_summary(self, summary):
        if 'current_range' in summary:
            old, new = summary['current_range']
            old_base, _, _ = codechange.parse_intra_rev(old)
            _, new_next, _ = codechange.parse_intra_rev(new)
            url_template = (
                'https://android-build.googleplex.com/'
                'builds/{new}/branches/%s/targets/%s/cls?end={old}'
            ) % (self.config.get('branch'), self.config.get('flavor'))

            summary['links'] = [
                {
                    'name': 'change_list',
                    'url': url_template.format(old=old_base, new=new_next),
                },
            ]


if __name__ == '__main__':
    bisector_cli.BisectorCommandLine(AndroidRepoDomain).main()
