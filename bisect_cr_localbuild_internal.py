#!/usr/bin/env python3
# Copyright 2018 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Chrome bisector to bisect a range of chrome commits.

This bisector bisects commits between branched chrome and releases.
"""

import argparse
import logging
import os

from bisect_kit import bisector_cli
from bisect_kit import cli
from bisect_kit import codechange
from bisect_kit import core
from bisect_kit import cr_util
from bisect_kit import cros_lab_util
from bisect_kit import cros_util
from bisect_kit import errors
from bisect_kit import gclient_util
from bisect_kit import git_util


logger = logging.getLogger(__name__)


def workaround_b378019087(chrome_src: str):
    """Workaround for b/378019087"""
    chromite_path = os.path.join(chrome_src, 'third_party/chromite')
    if (
        git_util.is_git_root(chromite_path)
        and git_util.is_ancestor_commit(
            chromite_path, '217ba94e17971b5de16f384ce955a147ea347a28', 'HEAD'
        )
        and not git_util.is_ancestor_commit(
            chromite_path, 'd9ade857400136815e6b4403ff9ce2e301779437', 'HEAD'
        )
    ):
        # Checkout a known good revision of chromite.
        git_util.checkout_version(
            chromite_path,
            'a251e44dc0010c09918189bb51376c27bfd2e93d',
        )


def guess_chrome_version(opts, rev):
    if cros_util.is_cros_version(rev):
        assert opts.board, 'need to specify BOARD for cros version'
        chrome_version = cros_util.query_chrome_version(opts.board, rev)
        assert cr_util.is_chrome_version(chrome_version)
        logger.info(
            'Converted given CrOS version %s to Chrome version %s',
            rev,
            chrome_version,
        )
        rev = chrome_version

    if cr_util.is_chrome_version_with_pre(rev):
        splitted = rev.split('_pre')
        assert len(splitted) == 2
        snapshot_identifier = splitted[1]
        assert snapshot_identifier.isdigit()
        return cr_util.query_git_rev_by_commit_position_remotely(
            snapshot_identifier
        )

    return rev


def generate_action_link(action):
    if action['action_type'] == 'commit':
        repo_url = action['repo_url']
        # normalize
        if repo_url == 'https://chromium.googlesource.com/a/chromium/src.git':
            repo_url = 'https://chromium.googlesource.com/chromium/src.git'
        action['link'] = repo_url + '/+/' + action['rev']


class ChromeSrcDomain(core.BisectDomain):
    """BisectDomain for Chrome branched tree"""

    revtype = staticmethod(
        cli.argtype_multiplexer(
            cr_util.argtype_chrome_version,
            cr_util.argtype_chrome_version_with_pre,
            cros_util.argtype_cros_version,
            git_util.argtype_git_rev,
        )
    )
    intra_revtype = staticmethod(
        codechange.argtype_intra_rev(cr_util.argtype_chrome_version)
    )
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
        parser.add_argument(
            '--chrome-mirror',
            required=True,
            metavar='CHROME_MIRROR',
            type=cli.argtype_dir_path,
            default=os.environ.get('CHROME_MIRROR'),
            help='gclient cache dir',
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
                '--chrome-root', "chrome src directory doesn't exist"
            )

        if opts.dut:
            if cros_lab_util.is_satlab_dut(opts.dut):
                cros_lab_util.write_satlab_ssh_config(opts.dut)
            if not cros_util.is_dut(opts.dut):
                raise errors.ArgumentError(
                    '--dut', 'invalid DUT or unable to connect'
                )

            if not opts.board:
                opts.board = cros_util.query_dut_board(opts.dut)

        old = guess_chrome_version(opts, opts.old)
        new = guess_chrome_version(opts, opts.new)
        if old == new:
            raise errors.ArgumentError(
                '--old and --new',
                'start and end of chrome versions are identical',
            )

        # b/227415522: Sync Chrome source tree to old and new to fetch unreachable
        # commits.
        # This needs to be run to initialize the chrome repository to confirm
        # opts.old is ancestor of opts.new if they are git hashes in the next step.
        gclient_util.sync(opts.chrome_root, revision=new)
        gclient_util.sync(opts.chrome_root, revision=old)
        workaround_b378019087(chrome_src)
        old_hash = cr_util.query_git_rev(chrome_src, old)
        new_hash = cr_util.query_git_rev(chrome_src, new)

        # Disable guessing LCA if any of the versions are git hash and not a
        # chrome version.
        if git_util.is_git_rev(old) or git_util.is_git_rev(new):
            if not git_util.is_ancestor_commit(chrome_src, old_hash, new_hash):
                raise errors.ArgumentError(
                    '--old and --new',
                    '--old chrome git hash (%s) is not an ancestor of the --new git hash (%s)'
                    % (old, new),
                )
        else:
            if not cr_util.is_version_lesseq(old, new):
                raise errors.ArgumentError(
                    '--old and --new',
                    '--old chrome version (%s) is newer than --new version (%s)'
                    % (old, new),
                )
            if not cr_util.is_direct_relative_version(old, new):
                logger.warning('old=%s is not parent of new=%s', old, new)
                lowest_common_ancestor = cr_util.get_lca_chrome_localbuild(
                    chrome_src, old, new
                )
                logger.warning(
                    'Assume their lowest common ancestor, %s,'
                    'still have expected old behavior as %s',
                    lowest_common_ancestor,
                    old,
                )
                old = lowest_common_ancestor

        old_commit_position = cr_util.get_commit_position(
            git_util.get_commit_metadata(chrome_src, old_hash)
        )
        new_commit_position = cr_util.get_commit_position(
            git_util.get_commit_metadata(chrome_src, new_hash)
        )

        revlist, details = cr_util.build_revlist(
            chrome_src, old_commit_position, new_commit_position
        )
        for detail in details.values():
            for action in detail.get('actions', []):
                generate_action_link(action)

        config = {
            "chrome_root": opts.chrome_root,
            "old": old_commit_position,
            "new": new_commit_position,
            "board": opts.board,
            "dut": opts.dut,
            "chrome_mirror": opts.chrome_mirror,
            "is_public_build": opts.public_build,
        }
        return config, {'revlist': revlist, 'details': details}

    def __init__(self, config: core.BisectConfig):
        self.config = config

    def setenv(self, env, rev, rev_details=None):
        pass

    def get_extra_args(
        self, phase: core.Phase, _rev: str, rev_details=None
    ) -> list[str]:
        if phase in (core.Phase.SWITCH, core.Phase.FUTURE_BUILD):
            args = [
                '--chrome-root',
                self.config.get('chrome_root'),
                '--chrome-mirror',
                self.config.get('chrome_mirror'),
            ]
            if self.config.get('board'):
                args += ['--board', self.config.get('board')]
            git_hash = cr_util.get_chrome_commit_hash(rev_details)
            args += ['--chrome-rev', git_hash]
            return args
        return []

    def fill_candidate_summary(self, summary):
        if 'current_range' in summary:
            old, new = summary['current_range']
            chrome_src = os.path.join(self.config.get('chrome_root'), 'src')
            old_rev = cr_util.query_git_rev_by_commit_position(chrome_src, old)
            new_rev = cr_util.query_git_rev_by_commit_position(chrome_src, new)
            log_url = (
                'https://chromium.googlesource.com/chromium/src/+log/%s..%s?n=10000'
                % (old_rev, new_rev)
            )
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
    bisector_cli.BisectorCommandLine(ChromeSrcDomain).main()
