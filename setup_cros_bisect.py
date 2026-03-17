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
import datetime
import glob
import logging
import os
import pathlib
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree

from bisect_kit import btrfs_util
from bisect_kit import cli
from bisect_kit import common
from bisect_kit import cros_util
from bisect_kit import gclient_util
from bisect_kit import git_util
from bisect_kit import locking
from bisect_kit import repo_util
from bisect_kit import util
import experiment


PathLike = os.PathLike | str

MANIFEST_FOR_DELETED = 'deleted-repos.xml'

logger = logging.getLogger(__name__)

HOUR_IN_SECONDS = 3600


def _should_sync(repo_dir: str, threshold_seconds=8 * HOUR_IN_SECONDS) -> bool:
    """should_sync returns true if it is necessary to do a sync for a particular repo.

    It reads the last sync time for a specific repo(e.g. chromeos/chrome/android)
    and compares it to the current time. Returns True if the time since the last sync exceeds
    the threshold or if the config file is not found.

    Args:
        repo_dir: the repository directory
        threshold_seconds: Time to wait for the next sync in seconds

    Returns:
        True if a sync is required. False otherwise.
    """
    try:
        last_sync_timestamp = _read_last_sync_time(repo_dir)
        last_sync = datetime.datetime.fromtimestamp(last_sync_timestamp)
        logger.info('last sync time for %s: %s', repo_dir, last_sync)
        time_diff = datetime.datetime.now() - last_sync
        return time_diff.total_seconds() > threshold_seconds
    except Exception as e:
        logger.info('exception raised in should_sync for %s: %s', repo_dir, e)
        # sync anyway for exceptions
        return True


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


def _setup_chromeos_repos(path_factory, use_btrfs: bool, is_public_build: bool):
    chromeos_mirror = path_factory.get_chromeos_mirror()
    chromeos_tree = path_factory.get_chromeos_tree()
    btrfs_util.makedirs(chromeos_mirror, use_btrfs=False)
    btrfs_util.makedirs(chromeos_tree, use_btrfs)

    manifest_url = (
        cros_util.PUBLIC_MANIFEST_REPO_URL
        if is_public_build
        else cros_util.INTERNAL_MANIFEST_REPO_URL
    )
    repo_url = 'https://gerrit.googlesource.com/git-repo'

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


def _sync_chromeos_code(path_factory):
    start_sync_time = int(time.time())
    chromeos_mirror = path_factory.get_chromeos_mirror()

    if not _should_sync(chromeos_mirror):
        logger.info(
            "repo sync for chromeos is skipped.  current time: %s",
            datetime.datetime.now(),
        )
        return

    logger.info(
        "repo sync for chromeos mirror. current time: %s",
        datetime.datetime.now(),
    )
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

    logger.info(
        'repo sync finished. start time: %s, end_time: %s',
        datetime.datetime.fromtimestamp(start_sync_time),
        datetime.datetime.now(),
    )


def _configure_chrome_repos(
    path_factory: common.ProjectPathFactory,
    use_btrfs: bool,
    is_public_build: bool,
) -> None:
    chrome_cache = path_factory.get_chrome_cache()
    chrome_tree = path_factory.get_chrome_tree()
    btrfs_util.makedirs(chrome_cache, use_btrfs=False)
    btrfs_util.makedirs(chrome_tree, use_btrfs)

    # The 'src' directory is expected to exist in many places. Create it now.
    chrome_src = pathlib.Path(chrome_tree) / 'src'
    chrome_src.mkdir(exist_ok=True)

    spec = f"""
solutions = [
  {{ "name"        : "src",
    "url"         : "https://chromium.googlesource.com/a/chromium/src.git",
    "custom_deps" : {{
    }},
    "custom_vars": {{
      'checkout_src_internal': {not is_public_build},
      'download_remoteexec_cfg': True,
    }},
  }},
]
target_os = ['chromeos']
cache_dir = {chrome_cache!r}
"""

    n_retries = 0
    while True:
        try:
            with locking.lock_file(
                os.path.join(chrome_cache, locking.LOCK_FILE_FOR_MIRROR_SYNC)
            ):
                logger.info('gclient config for chrome')
                gclient_util.config(chrome_tree, spec=spec)
                break
        except subprocess.CalledProcessError as e:
            n_retries += 1
            logger.error('gclient config failed (%d): %s', n_retries, e)
            if n_retries == 3:
                raise
            time.sleep(60)


def _setup_chrome_repos(
    path_factory: common.ProjectPathFactory,
    use_btrfs: bool,
    is_public_build: bool,
) -> None:
    _configure_chrome_repos(path_factory, use_btrfs, is_public_build)

    chrome_cache = path_factory.get_chrome_cache()
    with locking.lock_file(
        os.path.join(chrome_cache, locking.LOCK_FILE_FOR_MIRROR_SYNC)
    ):
        is_first_sync = not os.listdir(chrome_cache)
        if is_first_sync:
            logger.info(
                'gclient sync for chrome (this takes hours; be patient)'
            )
        else:
            logger.info('gclient sync for chrome')
        chrome_tree = path_factory.get_chrome_tree()
        gclient_util.sync(chrome_tree, with_branch_heads=True, with_tags=True)


def _sync_chrome_code(
    path_factory: common.ProjectPathFactory,
    use_btrfs: bool,
    is_public_build: bool,
) -> None:
    # The sync step is identical to the initial gclient config step.
    _setup_chrome_repos(path_factory, use_btrfs, is_public_build)


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
    path_factory = common.TemplatePathFactory(opts.work_base, opts.mirror_base)

    if opts.chromeos:
        _setup_chromeos_repos(
            path_factory, opts.btrfs, is_public_build=opts.public_build
        )
    if opts.chrome:
        _setup_chrome_repos(
            path_factory, opts.btrfs, is_public_build=opts.public_build
        )
    for branch in opts.android:
        _setup_android_repos(path_factory, branch, opts.btrfs)


def cmd_update(opts):
    try:
        repo_util.sync(opts.chromiumos_checkout_path, current_branch=True)
    except subprocess.CalledProcessError:
        # Sync may fail due to network or server issues.
        logger.exception('repo sync failed, will retry one minute later')
        time.sleep(60)
        repo_util.sync(opts.chromiumos_checkout_path, current_branch=True)


def do_sync_template(opts):
    template_factory = common.TemplatePathFactory(
        opts.work_base, opts.mirror_base
    )

    sync_all = False
    if not opts.chromeos and not opts.chrome and not opts.android:
        logger.info('sync trees for all')
        sync_all = True

    if sync_all or opts.chromeos:
        try:
            _sync_chromeos_code(template_factory)
        except Exception:
            # b/222638221: repo may be broken due to infra issues
            # re-create the whole repo and sync again
            logger.exception(
                'chromeos sync failed, re-init chromeos repo '
                '(this takes hours; be patient)'
            )
            chromeos_tree = template_factory.get_chromeos_tree()
            btrfs_util.delete_tree(chromeos_tree, missing_ok=True)
            # Alaways re-create it in btrfs.
            _setup_chromeos_repos(
                template_factory,
                use_btrfs=True,
                is_public_build=opts.public_build,
            )
    if sync_all or opts.chrome:
        use_btrfs = btrfs_util.is_btrfs_subvolume(
            template_factory.get_chrome_tree()
        )
        # Sync fetches the internal code (assuimng the public code is a subset
        # of the public).
        _sync_chrome_code(
            template_factory, use_btrfs, is_public_build=opts.public_build
        )

    if sync_all:
        android_branches = template_factory.iter_android_branches()
    else:
        android_branches = opts.android
    for branch in android_branches:
        _sync_android_code(template_factory, branch)


def cmd_sync(opts):
    try:
        do_sync_template(opts)
    except subprocess.CalledProcessError:
        # Sync may fail due to network or server issues.
        logger.exception('do_sync_template failed, will retry one minute later')
        time.sleep(60)
        do_sync_template(opts)


def cmd_new(opts):
    template_factory = common.TemplatePathFactory(
        opts.work_base, opts.mirror_base
    )
    path_factory = common.ProjectPathFactory(
        opts.session, opts.work_base, opts.mirror_base, opts.chrome_work_base
    )
    pathlib.Path(path_factory.session_workdir).mkdir(
        parents=True, exist_ok=True
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
        if experiment.is_in_experiment(
            opts.experiments,
            experiment.ID.EXT4_CHROME_CHECKOUT,
        ):
            _configure_chrome_repos(
                path_factory, use_btrfs=False, is_public_build=opts.public_build
            )
        else:
            btrfs_util.copy_tree(
                chrome_template, path_factory.get_chrome_tree()
            )
            _configure_chrome_repos(
                path_factory, use_btrfs=True, is_public_build=opts.public_build
            )

    if prepare_all:
        android_branches = path_factory.iter_android_branches()
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
    def _iter_rows():
        yield 'Session', 'Path'
        path_factory = common.WorkBasePathFactory(work_base=opts.work_base)
        for session_name in path_factory.iter_session_names(
            exclude_non_uuid=False
        ):
            yield session_name, path_factory.get_session_workdir(session_name)

    for session_name, session_path in _iter_rows():
        print(f'{session_name:<20}  {session_path}')


def cmd_delete(opts):
    if opts.all_uuid_sessions:
        # If this function is called manually, the user should stop the bkr
        # process first to avoid deleting the running session accidentally.
        path_factory = common.WorkBasePathFactory(work_base=opts.work_base)
        for session_name in path_factory.iter_session_names():
            _delete_session(
                session_name,
                opts.work_base,
                opts.mirror_base,
                opts.chrome_work_base,
            )
    elif opts.session:
        _delete_session(
            opts.session,
            opts.work_base,
            opts.mirror_base,
            opts.chrome_work_base,
        )

    _clear_global_caches()


def _delete_session(
    session: str,
    work_base: PathLike,
    mirror_base: PathLike,
    chrome_work_base: PathLike,
):
    path_factory = common.ProjectPathFactory(
        session, work_base, mirror_base, chrome_work_base
    )

    chromeos_tree = path_factory.get_chromeos_tree()
    if os.path.exists(chromeos_tree):
        btrfs_util.delete_tree(chromeos_tree, missing_ok=True)

    chrome_tree = path_factory.get_chrome_tree()
    btrfs_util.delete_tree(chrome_tree, missing_ok=True)

    if work_base == chrome_work_base:
        # Delete a chrome work dir in the DEFAULT_EXT4_CHROME_WORK_BASE
        # directory if any. Even if we're currently running without the
        # ext4_chrome_checkout experiment, existing sessions may have chrome
        # checkouts in the DEFAULT_EXT4_CHROME_WORK_BASE directory.
        btrfs_util.delete_tree(
            common.ProjectPathFactory(
                session,
                work_base,
                mirror_base,
                common.DEFAULT_EXT4_CHROME_WORK_BASE,
            ).get_chrome_workdir(),
            missing_ok=True,
        )
    else:
        # Delete a chrome checkout in the work_base directory if any.
        # Even if we're currently running with the ext4_chrome_checkout experiment,
        # existing sessions may have chrome checkouts in the work_base directory.
        btrfs_util.delete_tree(
            common.ProjectPathFactory(
                session, work_base, mirror_base, work_base
            ).get_chrome_tree(),
            missing_ok=True,
        )

    android_branches = path_factory.iter_android_branches()
    for branch in android_branches:
        android_tree = path_factory.get_android_tree(branch)
        btrfs_util.delete_tree(android_tree, missing_ok=True)

    btrfs_util.delete_tree(path_factory.session_workdir, missing_ok=True)
    btrfs_util.delete_tree(path_factory.get_chrome_workdir(), missing_ok=True)

    for path in glob.glob(
        os.path.join(path_factory.get_chrome_cache(), '_cache_*')
    ):
        logger.debug('remove cache (chrome gclient cache): %s', path)
        btrfs_util.delete_tree(path)


def _clear_global_caches():
    chromeos_root = common.get_default_chromeos_root()
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


def cmd_check(opts):
    if _check_environments(opts.work_base, opts.mirror_base, opts.fixme):
        return 0
    return -1


def _check_environments(
    work_base: PathLike, mirror_base: PathLike, should_fix: bool
) -> bool:
    template_factory = common.TemplatePathFactory(work_base, mirror_base)
    is_chromeos_tree_ok = _check_chromeos_tree(template_factory, should_fix)
    is_chomre_tree_ok = _check_chrome_tree(template_factory, should_fix)
    is_chrome_deps_ok = _check_chrome_build_deps(template_factory, should_fix)

    return is_chromeos_tree_ok and is_chomre_tree_ok and is_chrome_deps_ok


def _check_chromeos_tree(
    path_factory: common.ProjectPathFactory, should_fix: bool
) -> bool:
    chromeos_tree = pathlib.Path(path_factory.get_chromeos_tree())
    if btrfs_util.is_btrfs_subvolume(chromeos_tree, raise_error=False):
        return True
    logger.warning(
        'chromeos_tree not exists or is not a valid btrfs subvolume: %s',
        chromeos_tree,
    )
    if not should_fix:
        return False
    btrfs_util.delete_tree(chromeos_tree, missing_ok=True)
    # Fix fetches the internal code (assuming the public code is a subset of
    # the internal).
    _setup_chromeos_repos(path_factory, use_btrfs=True, is_public_build=False)
    return True


def _check_chrome_tree(
    path_factory: common.ProjectPathFactory, should_fix: bool
) -> bool:
    chrome_tree = pathlib.Path(path_factory.get_chrome_tree())
    if btrfs_util.is_btrfs_subvolume(chrome_tree, raise_error=False):
        return True
    logger.warning(
        'chrome_tree not exists or is not a valid btrfs subvolume: %s',
        chrome_tree,
    )
    if not should_fix:
        return False
    btrfs_util.delete_tree(chrome_tree, missing_ok=True)
    # Fix fetches the internal code (assuming the public code is a subset of
    # the internal).
    _setup_chrome_repos(path_factory, use_btrfs=True, is_public_build=False)
    return True


def _check_chrome_build_deps(
    path_factoroy: common.ProjectPathFactory, should_fix: bool
):
    chrome_tree = pathlib.Path(path_factoroy.get_chrome_tree())
    cmds = ['src/build/install-build-deps.sh', '--no-prompt']
    if should_fix:
        util.check_output(*cmds, cwd=chrome_tree)
        return True

    try:
        cmds.append('--quick-check')
        util.check_output(*cmds, cwd=chrome_tree)
        return True
    except FileNotFoundError as e:
        logger.warning('deps ckecking script not found: %s', e)
        return False
    except subprocess.CalledProcessError:
        logger.warning('missing dependencies for chrome build: %s', chrome_tree)
        return False


def create_parser():
    base_parser = cli.ArgumentParser(add_help=False)
    base_parser.add_argument(
        '--mirror-base',
        metavar='MIRROR_BASE',
        default=os.environ.get('MIRROR_BASE', common.DEFAULT_MIRROR_BASE),
        help='Directory for mirrors (default: %(default)s)',
    )
    base_parser.add_argument(
        '--work-base',
        metavar='WORK_BASE',
        default=os.environ.get('WORK_BASE', common.DEFAULT_WORK_BASE),
        help='Directory for bisection working directories'
        ' (default: %(default)s)',
    )
    base_parser.add_argument(
        '--chrome-work-base',
        metavar='CHROME_WORK_BASE',
        default=os.environ.get('CHROME_WORK_BASE'),
        help='Directory for chrome working directories',
    )
    parents_session_optional = [
        cli.create_session_optional_parser(),
        base_parser,
        experiment.common_flags(),
    ]
    parents_session_required = [
        cli.create_session_required_parser(),
        base_parser,
        experiment.common_flags(),
    ]

    parser = cli.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=__doc__,
        raise_bad_status=False,
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
        '--public-build',
        action='store_true',
        help='Use public build artifacts instead of internal ones.',
    )
    parser_init.add_argument(
        '--btrfs',
        action='store_true',
        help='create btrfs subvolume for source tree',
    )
    parser_init.set_defaults(func=cmd_init)

    parser_update = subparsers.add_parser(
        'update',
        help='Updates bisect-kit itself and all other projects',
        description='Sync all projects in the repo',
        parents=parents_session_optional,
    )
    parser_update.add_argument(
        '--chromiumos-checkout-path',
        metavar='CHROMIUMOS_CHECKOUT_PATH',
        help='ChromiumOS checkout path to-be-updated.',
        required=True,
    )
    parser_update.set_defaults(func=cmd_update)

    parser_sync = subparsers.add_parser(
        'sync',
        help='Sync template source trees',
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
    parser_sync.add_argument(
        '--public-build',
        action='store_true',
        help='Use public build artifacts instead of internal ones.',
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
    parser_new.add_argument(
        '--public-build',
        action='store_true',
        help='Use public build artifacts instead of internal ones.',
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
        parents=parents_session_optional,
    )
    parser_delete.add_argument(
        '--all-uuid-sessions',
        action='store_true',
        help='Delete all sessions with names in UUID format',
    )
    parser_delete.set_defaults(func=cmd_delete)

    parser_check = subparsers.add_parser(
        'check',
        help='Check if the environments is sufficient to run a full bisection run',
        parents=parents_session_optional,
    )
    parser_check.add_argument(
        '--fixme',
        action='store_true',
        help='Attempt to fix the environments if it is in an unhealthy state',
    )
    parser_check.set_defaults(func=cmd_check)

    return parser


def main():
    parser = create_parser()
    opts = parser.parse_args()

    if not opts.chrome_work_base:
        if experiment.is_in_experiment(
            opts.experiments,
            experiment.ID.EXT4_CHROME_CHECKOUT,
        ):
            # Use a directory on an ext4 file system for chrome checkouts.
            opts.chrome_work_base = common.DEFAULT_EXT4_CHROME_WORK_BASE
        else:
            # Use the work base directory on btrfs for chrome checkouts.
            opts.chrome_work_base = opts.work_base

    common.config_logging(opts)
    return opts.func(opts)


if __name__ == '__main__':
    sys.exit(main())
