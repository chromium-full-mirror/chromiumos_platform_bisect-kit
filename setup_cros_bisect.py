#!/usr/bin/env python3
# Copyright 2018 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Helper script to prepare source trees for ChromeOS bisection.

Typical usage:

  Initial setup:
    $ %(prog)s init --chromeos
    $ %(prog)s init --chrome
    $ %(prog)s init --android=pi-arc-dev

  Sync code if necessary:
    $ %(prog)s sync

  Create source trees for bisection
    $ %(prog)s new --session=12345

  After bisection finished, delete trees
    $ %(prog)s delete --session=12345
"""

import argparse
import glob
import logging
import multiprocessing
import os
import subprocess
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree

from bisect_kit import btrfs_util
from bisect_kit import cli
from bisect_kit import common
from bisect_kit import gclient_util
from bisect_kit import git_util
from bisect_kit import locking
from bisect_kit import repo_util
from bisect_kit import util


CHECKOUT_TEMPLATE_NAME = 'template'
MANIFEST_FOR_DELETED = 'deleted-repos.xml'

logger = logging.getLogger(__name__)


class DefaultProjectPathFactory:
    """Factory for chromeos/chrome/android source tree paths."""

    def __init__(self, mirror_base, work_base, session):
        self.mirror_base = mirror_base
        self.work_base = work_base
        self.session = session

    def get_chromeos_mirror(self):
        return os.path.join(self.mirror_base, 'chromeos')

    def get_chromeos_tree(self):
        return os.path.join(self.work_base, self.session, 'chromeos')

    def get_android_mirror(self, branch):
        return os.path.join(self.mirror_base, 'android.%s' % branch)

    def get_android_tree(self, branch):
        return os.path.join(self.work_base, self.session, 'android.%s' % branch)

    def get_chrome_cache(self):
        return os.path.join(self.mirror_base, 'chrome')

    def get_chrome_tree(self):
        return os.path.join(self.work_base, self.session, 'chrome')


def _collect_removed_manifest_repos(repo_dir, last_sync_time, only_branch=None):
    manifest_dir = os.path.join(repo_dir, '.repo', 'manifests')
    manifest_path = 'default.xml'
    manifest_full_path = os.path.join(manifest_dir, manifest_path)
    # hack for chromeos symlink
    if os.path.islink(manifest_full_path):
        manifest_path = os.readlink(manifest_full_path)

    parser = repo_util.ManifestParser(manifest_dir)
    git_rev = git_util.get_commit_hash(manifest_dir, 'HEAD')
    root = parser.parse_xml_recursive(git_rev, manifest_path)
    latest_all = parser.process_parsed_result(root, group_constraint='all')
    latest_default = parser.process_parsed_result(
        root, group_constraint='default'
    )

    removed = {}
    for commit in reversed(
        parser.enumerate_manifest_commits(last_sync_time, None, manifest_path)
    ):
        try:
            root = parser.parse_xml_recursive(commit.rev, manifest_path)
        except xml.etree.ElementTree.ParseError:
            logger.warning(
                '%s %s@%s syntax error, skip',
                manifest_dir,
                manifest_path,
                commit.rev[:12],
            )
            continue
        if (
            only_branch
            and root.find('default') is not None
            and root.find('default').get('revision') != only_branch
        ):
            break
        entries = parser.process_parsed_result(root)
        for path, path_spec in entries.items():
            if path in latest_default:
                continue
            if path in latest_all:
                logger.warning(
                    'path=%s was removed from default group; assume skip is harmless',
                    path,
                )
                continue
            if path in removed:
                continue
            removed[path] = path_spec

    return removed


def _add_local_mount(chromeos_tree: str):
    """Add gsutil cred in ~/.config folder in chromeos local mount setting.

    This is necessary to run gsutil commands under chroot.
    """
    user_config_dir = os.path.expanduser('~/.config')
    if not os.path.isdir(user_config_dir):
        return

    mount_config = []
    mount_config_file = os.path.join(chromeos_tree, 'src/scripts/.local_mounts')
    if os.path.exists(mount_config_file):
        with open(mount_config_file, 'r') as f:
            mount_config = [line.strip() for line in f.readlines()]

    if user_config_dir not in mount_config:
        logger.info('add %s in local_mounts', user_config_dir)
        mount_config.append(user_config_dir)
        with open(mount_config_file, 'w') as f:
            print('\n'.join(mount_config), file=f)


def _setup_chromeos_repos(path_factory, use_btrfs: bool):
    chromeos_mirror = path_factory.get_chromeos_mirror()
    chromeos_tree = path_factory.get_chromeos_tree()
    btrfs_util.makedirs(chromeos_mirror, use_btrfs)
    btrfs_util.makedirs(chromeos_tree, use_btrfs)

    manifest_url = (
        'https://chrome-internal.googlesource.com/chromeos/manifest-internal'
    )
    repo_url = 'https://chromium.googlesource.com/external/repo.git'

    if os.path.exists(os.path.join(chromeos_mirror, '.repo', 'manifests')):
        logger.warning(
            '%s has already been initialized, assume it is setup properly',
            chromeos_mirror,
        )
    else:
        logger.info('repo init for chromeos mirror')
        repo_util.init(
            chromeos_mirror,
            manifest_url=manifest_url,
            repo_url=repo_url,
            mirror=True,
        )

        local_manifest_dir = os.path.join(
            chromeos_mirror, repo_util.LOCAL_MANIFESTS_DIR
        )
        os.mkdir(local_manifest_dir)
        with open(
            os.path.join(local_manifest_dir, 'manifest-versions.xml'), 'w'
        ) as f:
            f.write(
                """<?xml version="1.0" encoding="UTF-8"?>
          <manifest>
            <project name="chromeos/manifest-versions" remote="cros-internal" />
          </manifest>
          """
            )

    logger.info('repo init for chromeos tree')
    repo_util.init(
        chromeos_tree,
        manifest_url=manifest_url,
        repo_url=repo_url,
        reference=chromeos_mirror,
    )

    with locking.lock_file(
        os.path.join(chromeos_mirror, locking.LOCK_FILE_FOR_MIRROR_SYNC)
    ):
        logger.info(
            'repo sync for chromeos mirror (this takes hours; be patient)'
        )
        repo_util.sync(chromeos_mirror, current_branch=False)

    logger.info('repo sync for chromeos tree')
    repo_util.sync(chromeos_tree)
    _add_local_mount(chromeos_tree)


def _read_last_sync_time(repo_dir):
    timestamp_path = os.path.join(repo_dir, 'last_sync_time')
    if os.path.exists(timestamp_path):
        with open(timestamp_path) as f:
            return int(f.read())
    else:
        # 4 months should be enough for most bisect cases.
        return int(time.time()) - 86400 * 120


def _write_sync_time(repo_dir, sync_time):
    timestamp_path = os.path.join(repo_dir, 'last_sync_time')
    with open(timestamp_path, 'w') as f:
        f.write('%d\n' % sync_time)


def _write_extra_manifest_to_mirror(repo_dir, removed):
    # project names that should be excluded in deleted-repos
    exclude_repositories = [
        'chromeos/overlays/overlay-goroh-private',  # removed in b/218319750
        'chromeos/vendor/mtk-optee-os',  # removed in b/275630676
    ]
    local_manifest_dir = os.path.join(repo_dir, repo_util.LOCAL_MANIFESTS_DIR)
    if not os.path.exists(local_manifest_dir):
        os.mkdir(local_manifest_dir)
    with open(os.path.join(local_manifest_dir, MANIFEST_FOR_DELETED), 'w') as f:
        f.write("""<?xml version="1.0" encoding="UTF-8"?>\n<manifest>\n""")
        remotes = {}
        for path_spec in removed.values():
            scheme, netloc, remote_path = urllib.parse.urlsplit(
                path_spec.repo_url
            )[:3]
            assert remote_path[0] == '/'
            remote_path = remote_path[1:]
            if (
                remote_path in exclude_repositories
            ):  # skip the removed repositories
                continue

            if (scheme, netloc) not in remotes:
                remote_name = 'remote_for_deleted_repo_%s' % (scheme + netloc)
                remotes[scheme, netloc] = remote_name
                f.write(
                    """  <remote name="%s" fetch="%s" />\n"""
                    % (remote_name, '%s://%s' % (scheme, netloc))
                )
            f.write(
                """  <project name="%s" path="%s" remote="%s" revision="%s" />\n"""
                % (
                    remote_path,
                    path_spec.path,
                    remotes[scheme, netloc],
                    path_spec.rev,
                )
            )
        f.write("""</manifest>\n""")


def _delete_extra_manifest(repo_dir):
    path = os.path.join(
        repo_dir, repo_util.LOCAL_MANIFESTS_DIR, MANIFEST_FOR_DELETED
    )
    if os.path.exists(path):
        os.unlink(path)


def _generate_extra_manifest_for_deleted_repo(repo_dir, only_branch=None):
    last_sync_time = _read_last_sync_time(repo_dir)
    removed = _collect_removed_manifest_repos(
        repo_dir, last_sync_time, only_branch=only_branch
    )
    _write_extra_manifest_to_mirror(repo_dir, removed)
    logger.info('since last sync, %d repo got removed', len(removed))
    return len(removed)


def _additional_chrome_mirror_sync(chrome_cache):
    # Why additional sync is necessary:
    #   1. `gclient sync` take cares only for the state of the DEPS files on the
    #      current working directory (main branch). If a repo has been removed
    #      from the main branch, gclient no longer fetch anything for the said
    #      repo. We need this additional sync to fetch
    #        a. history between the last sync and the removal time.
    #        b. history on other branches
    #   2. If the DEPS file refers a git repo with a static commit hash and the
    #      repo already contains the said commit on the local disk, gclient will
    #      skip to update the git mirror. We need this additional sync to fetch
    #      history on other branches. (b/206567489)
    #
    # However, there are still corner cases. If a repo was added and removed
    # after our last sync, we have no simple methods to detect them. They will
    # be synced during DEPS file processing because the necessary information
    # requires full DEPS parsing. (crbug.com/902238)
    #
    # One wasteful problem of this additional sync operation is, we keep sync
    # repos even if they are removed for a long time. This should be acceptable
    # because the cost to sync a repo with zero changes should be negligible.
    logger.info('additional sync for chrome mirror')
    with multiprocessing.Pool() as pool:
        results = []
        for git_repo_name in os.listdir(chrome_cache):
            # another gclient is running or leftover of previous run; skip
            if git_repo_name.startswith('_cache_tmp'):
                continue
            git_repo = os.path.join(chrome_cache, git_repo_name)
            if not git_util.is_git_bare_dir(git_repo):
                continue
            results.append(
                pool.apply_async(
                    git_util.fetch,
                    (git_repo,),
                    dict(retry_prune_if_conflict=True),
                )
            )
        for r in results:
            # git_util.fetch doesn't return values. Here we call get() in order to
            # propagate exceptions if any.
            r.get()


def _sync_chromeos_code(path_factory):
    start_sync_time = int(time.time())
    chromeos_mirror = path_factory.get_chromeos_mirror()

    logger.info('repo sync for chromeos mirror')
    _delete_extra_manifest(chromeos_mirror)
    # current_branch=False to ignore "sync-c=true" in the manifest file.
    repo_util.sync(chromeos_mirror, current_branch=False)
    # If there are repos deleted after last sync, generate custom manifest and
    # sync again for those repos. So we can mirror commits just before the repo
    # deletion.
    if _generate_extra_manifest_for_deleted_repo(chromeos_mirror) != 0:
        logger.info('repo sync again')
        repo_util.sync(chromeos_mirror, current_branch=False)
    _write_sync_time(chromeos_mirror, start_sync_time)

    logger.info('repo sync for chromeos tree')
    chromeos_tree = path_factory.get_chromeos_tree()
    repo_util.sync(chromeos_tree)


def _setup_chrome_repos(path_factory, use_btrfs: bool):
    chrome_cache = path_factory.get_chrome_cache()
    chrome_tree = path_factory.get_chrome_tree()
    btrfs_util.makedirs(chrome_cache, use_btrfs)
    btrfs_util.makedirs(chrome_tree, use_btrfs)

    spec = """
solutions = [
  { "name"        : "src",
    "url"         : "https://chromium.googlesource.com/a/chromium/src.git",
    "custom_deps" : {
    },
    "custom_vars": {
      'checkout_src_internal': True,
      'cros_boards': 'amd64-generic:arm-generic',
      'checkout_lacros_sdk': True,
      'download_remoteexec_cfg': True,
    },
  },
]
target_os = ['chromeos']
cache_dir = %r
""" % (
        chrome_cache
    )

    with locking.lock_file(
        os.path.join(chrome_cache, locking.LOCK_FILE_FOR_MIRROR_SYNC)
    ):
        logger.info('gclient config for chrome')
        gclient_util.config(chrome_tree, spec=spec)

        is_first_sync = not os.listdir(chrome_cache)
        if is_first_sync:
            logger.info(
                'gclient sync for chrome (this takes hours; be patient)'
            )
        else:
            logger.info('gclient sync for chrome')
        gclient_util.sync(chrome_tree, with_branch_heads=True, with_tags=True)
        _additional_chrome_mirror_sync(chrome_cache)


def _sync_chrome_code(path_factory, use_btrfs: bool):
    # The sync step is identical to the initial gclient config step.
    _setup_chrome_repos(path_factory, use_btrfs)


def _setup_android_repos(path_factory, branch, use_btrfs: bool):
    android_mirror = path_factory.get_android_mirror(branch)
    android_tree = path_factory.get_android_tree(branch)
    btrfs_util.makedirs(android_mirror, use_btrfs)
    btrfs_util.makedirs(android_tree, use_btrfs)

    manifest_url = (
        'persistent-https://googleplex-android.git.corp.google.com'
        '/platform/manifest'
    )
    repo_url = 'https://gerrit.googlesource.com/git-repo'

    if os.path.exists(os.path.join(android_mirror, '.repo', 'manifests')):
        logger.warning(
            '%s has already been initialized, assume it is setup properly',
            android_mirror,
        )
    else:
        logger.info('repo init for android mirror branch=%s', branch)
        repo_util.init(
            android_mirror,
            manifest_url=manifest_url,
            repo_url=repo_url,
            manifest_branch=branch,
            mirror=True,
        )

    logger.info('repo init for android tree branch=%s', branch)
    repo_util.init(
        android_tree,
        manifest_url=manifest_url,
        repo_url=repo_url,
        manifest_branch=branch,
        reference=android_mirror,
    )

    logger.info('repo sync for android mirror (this takes hours; be patient)')
    repo_util.sync(android_mirror, current_branch=True)

    logger.info('repo sync for android tree branch=%s', branch)
    repo_util.sync(android_tree, current_branch=True)


def _sync_android_code(path_factory, branch):
    start_sync_time = int(time.time())
    android_mirror = path_factory.get_android_mirror(branch)
    android_tree = path_factory.get_android_tree(branch)

    with locking.lock_file(
        os.path.join(android_mirror, locking.LOCK_FILE_FOR_MIRROR_SYNC)
    ):
        logger.info('repo sync for android mirror branch=%s', branch)
        _delete_extra_manifest(android_mirror)
        repo_util.sync(android_mirror, current_branch=True)
        # Android usually big jump between milestone releases and add/delete lots of
        # repos when switch releases. Because it's infeasible to bisect between such
        # big jump, the deleted repo is useless. In order to save disk, do not sync
        # repos deleted in other branches.
        if (
            _generate_extra_manifest_for_deleted_repo(
                android_mirror, only_branch=branch
            )
            != 0
        ):
            logger.info('repo sync again')
            repo_util.sync(android_mirror, current_branch=True)
        _write_sync_time(android_mirror, start_sync_time)

    logger.info('repo sync for android tree branch=%s', branch)
    repo_util.sync(android_tree, current_branch=True)


def cmd_init(opts):
    path_factory = DefaultProjectPathFactory(
        opts.mirror_base, opts.work_base, CHECKOUT_TEMPLATE_NAME
    )

    if opts.chromeos:
        _setup_chromeos_repos(path_factory, opts.btrfs)
    if opts.chrome:
        _setup_chrome_repos(path_factory, opts.btrfs)
    for branch in opts.android:
        _setup_android_repos(path_factory, branch, opts.btrfs)


def _enumerate_android_branches_available(base):
    branches = []
    for name in os.listdir(base):
        if name.startswith('android.'):
            branches.append(name.partition('.')[2])
    return branches


def do_sync(opts):
    path_factory = DefaultProjectPathFactory(
        opts.mirror_base, opts.work_base, CHECKOUT_TEMPLATE_NAME
    )

    sync_all = False
    if not opts.chromeos and not opts.chrome and not opts.android:
        logger.info('sync trees for all')
        sync_all = True

    if sync_all or opts.chromeos:
        try:
            _sync_chromeos_code(path_factory)
        except Exception:
            # b/222638221: repo may be broken due to infra issues
            # re-create the whole repo and sync again
            logger.warning(
                'chromeos sync failed, re-init chromeos repo '
                '(this takes hours; be patient)'
            )
            chromeos_tree = path_factory.get_chromeos_tree()
            use_btrfs = btrfs_util.is_btrfs_subvolume(chromeos_tree)
            btrfs_util.delete_tree(chromeos_tree)
            _setup_chromeos_repos(path_factory, use_btrfs)
    if sync_all or opts.chrome:
        use_btrfs = btrfs_util.is_btrfs_subvolume(
            path_factory.get_chrome_tree()
        )
        _sync_chrome_code(path_factory, use_btrfs)

    if sync_all:
        android_branches = _enumerate_android_branches_available(
            opts.mirror_base
        )
    else:
        android_branches = opts.android
    for branch in android_branches:
        _sync_android_code(path_factory, branch)


def cmd_sync(opts):
    try:
        do_sync(opts)
    except subprocess.CalledProcessError:
        # Sync may fail due to network or server issues.
        logger.exception('do_sync failed, will retry one minute later')
        time.sleep(60)
        do_sync(opts)


def cmd_new(opts):
    work_dir = os.path.join(opts.work_base, opts.session)
    if not os.path.exists(work_dir):
        os.makedirs(work_dir)

    template_factory = DefaultProjectPathFactory(
        opts.mirror_base, opts.work_base, CHECKOUT_TEMPLATE_NAME
    )
    path_factory = DefaultProjectPathFactory(
        opts.mirror_base, opts.work_base, opts.session
    )

    prepare_all = False
    if not opts.chromeos and not opts.chrome and not opts.android:
        logger.info('prepare trees for all')
        prepare_all = True

    chromeos_template = template_factory.get_chromeos_tree()
    if (prepare_all and os.path.exists(chromeos_template)) or opts.chromeos:
        # Add mount config again if it's missing.
        _add_local_mount(chromeos_template)
        logger.info(
            'prepare tree for chromeos, %s', path_factory.get_chromeos_tree()
        )
        btrfs_util.copy_tree(
            chromeos_template, path_factory.get_chromeos_tree()
        )

    chrome_template = template_factory.get_chrome_tree()
    if (prepare_all and os.path.exists(chrome_template)) or opts.chrome:
        logger.info(
            'prepare tree for chrome, %s', path_factory.get_chrome_tree()
        )
        btrfs_util.copy_tree(chrome_template, path_factory.get_chrome_tree())

    if prepare_all:
        android_branches = _enumerate_android_branches_available(
            opts.mirror_base
        )
    else:
        android_branches = opts.android
    for branch in android_branches:
        logger.info(
            'prepare tree for android branch=%s, %s',
            branch,
            path_factory.get_android_tree(branch),
        )
        btrfs_util.copy_tree(
            template_factory.get_android_tree(branch),
            path_factory.get_android_tree(branch),
        )


def cmd_list(opts):
    print('%-20s  %s' % ('Session', 'Path'))
    for name in os.listdir(opts.work_base):
        if name == CHECKOUT_TEMPLATE_NAME:
            continue
        path = os.path.join(opts.work_base, name)
        print('%-20s  %s' % (name, path))


def cmd_delete(opts):
    assert opts.session
    path_factory = DefaultProjectPathFactory(
        opts.mirror_base, opts.work_base, opts.session
    )

    chromeos_tree = path_factory.get_chromeos_tree()
    if os.path.exists(chromeos_tree):
        if os.path.exists(os.path.join(chromeos_tree, 'chromite')):
            # ignore error
            util.call('cros_sdk', '--unmount', cwd=chromeos_tree)
        btrfs_util.delete_tree(chromeos_tree, missing_ok=True)

    chrome_tree = path_factory.get_chrome_tree()
    btrfs_util.delete_tree(chrome_tree, missing_ok=True)

    android_branches = _enumerate_android_branches_available(opts.mirror_base)
    for branch in android_branches:
        android_tree = path_factory.get_android_tree(branch)
        btrfs_util.delete_tree(android_tree, missing_ok=True)

    os.rmdir(os.path.join(opts.work_base, opts.session))

    # remove caches
    chromeos_root = os.getenv(
        'DEFAULT_CHROMEOS_ROOT', common.DEFAULT_CHROMEOS_ROOT
    )
    if chromeos_root:
        path = os.path.join(chromeos_root, 'devserver/static')
        if os.path.exists(path):
            logger.debug('remove cache (cros flash): %s', path)
            util.call('cros', 'clean', '--flash', cwd=path)

        for path in glob.glob(os.path.join(chromeos_root, 'chroot/tmp/*')):
            logger.debug('remove cache (chroot/tmp): %s', path)
            btrfs_util.delete_tree(path)

        for path in glob.glob(os.path.join(chromeos_root, 'tmp/*')):
            logger.debug('remove cache (chromeos root tmp): %s', path)
            btrfs_util.delete_tree(path)

    for path in glob.glob(
        os.path.join(path_factory.get_chrome_cache(), '_cache_*')
    ):
        logger.debug('remove cache (chrome gclient cache): %s', path)
        btrfs_util.delete_tree(path)


def create_parser():
    base_parser = argparse.ArgumentParser(add_help=False)
    base_parser.add_argument(
        '--mirror_base',
        metavar='MIRROR_BASE',
        default=os.environ.get('MIRROR_BASE', common.DEFAULT_MIRROR_BASE),
        help='Directory for mirrors (default: %(default)s)',
    )
    base_parser.add_argument(
        '--work_base',
        metavar='WORK_BASE',
        default=os.environ.get('WORK_BASE', common.DEFAULT_WORK_BASE),
        help='Directory for bisection working directories'
        ' (default: %(default)s)',
    )
    parents_session_optional = [
        cli.create_session_optional_parser(),
        base_parser,
    ]
    parents_session_required = [
        cli.create_session_required_parser(),
        base_parser,
    ]

    parser = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=__doc__,
    )
    subparsers = parser.add_subparsers(
        dest='command', title='commands', metavar='<command>', required=True
    )

    parser_init = subparsers.add_parser(
        'init',
        help='Mirror source trees and create template checkout',
        parents=parents_session_optional,
    )
    parser_init.add_argument(
        '--chrome', action='store_true', help='init chrome mirror and tree'
    )
    parser_init.add_argument(
        '--chromeos', action='store_true', help='init chromeos mirror and tree'
    )
    parser_init.add_argument(
        '--android',
        metavar='BRANCH',
        action='append',
        default=[],
        help='init android mirror and tree of BRANCH',
    )
    parser_init.add_argument(
        '--btrfs',
        action='store_true',
        help='create btrfs subvolume for source tree',
    )
    parser_init.set_defaults(func=cmd_init)

    parser_sync = subparsers.add_parser(
        'sync',
        help='Sync source trees',
        description='Sync all if no projects are specified '
        '(--chrome, --chromeos, or --android)',
        parents=parents_session_optional,
    )
    parser_sync.add_argument(
        '--chrome', action='store_true', help='sync chrome mirror and tree'
    )
    parser_sync.add_argument(
        '--chromeos', action='store_true', help='sync chromeos mirror and tree'
    )
    parser_sync.add_argument(
        '--android',
        metavar='BRANCH',
        action='append',
        default=[],
        help='sync android mirror and tree of BRANCH',
    )
    parser_sync.set_defaults(func=cmd_sync)

    parser_new = subparsers.add_parser(
        'new',
        help='Create new source checkout for bisect',
        description='Create for all if no projects are specified '
        '(--chrome, --chromeos, or --android)',
        parents=parents_session_required,
    )
    parser_new.add_argument(
        '--chrome', action='store_true', help='create chrome checkout'
    )
    parser_new.add_argument(
        '--chromeos', action='store_true', help='create chromeos checkout'
    )
    parser_new.add_argument(
        '--android',
        metavar='BRANCH',
        action='append',
        default=[],
        help='create android checkout of BRANCH',
    )
    parser_new.set_defaults(func=cmd_new)

    parser_list = subparsers.add_parser(
        'list',
        help='List existing sessions with source checkout',
        parents=parents_session_optional,
    )
    parser_list.set_defaults(func=cmd_list)

    parser_delete = subparsers.add_parser(
        'delete',
        help='Delete source checkout',
        parents=parents_session_required,
    )
    parser_delete.set_defaults(func=cmd_delete)

    return parser


def main():
    parser = create_parser()
    opts = parser.parse_args()
    common.config_logging(opts)
    opts.func(opts)


if __name__ == '__main__':
    main()
