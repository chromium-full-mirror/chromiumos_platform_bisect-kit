#!/usr/bin/env python3
# Copyright 2019 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Switcher for chromeos localbuild bisecting."""

from __future__ import annotations

import argparse
import copy
import logging
import os
import socket
import sys
import time
import typing

from bisect_kit import bisector_cli
from bisect_kit import buildbucket_util
from bisect_kit import cli
from bisect_kit import codechange
from bisect_kit import common
from bisect_kit import core
from bisect_kit import cr_util
from bisect_kit import cros_lab_util
from bisect_kit import cros_util
from bisect_kit import errors
from bisect_kit import gclient_util
from bisect_kit import repo_util
import switch_helper


logger = logging.getLogger(__name__)


def add_build_and_deploy_arguments(parser_group):
    parser_group.add_argument(
        '--clobber-stateful',
        action='store_true',
        help='Clobber stateful partition when performing update',
    )
    parser_group.add_argument(
        '--disable-rootfs-verification',
        action=argparse.BooleanOptionalAction,
        default=False,
        help='Whether to disable rootfs verification after update is complete, default is %(default)s',
    )


def create_common_argument_parser():
    """Common arguments parser for both Chrome and ChromeOS builds"""
    parser = cli.ArgumentParser(add_help=False)
    parser.add_argument(
        '--rich-result',
        action='store_true',
        help='Instead of mere exit code, output detailed information in json',
    )
    parser.add_argument(
        '--board',
        type=cli.argtype_notempty,
        metavar='BOARD',
        default=os.environ.get('BOARD', ''),
        help='ChromeOS board name',
    )
    parser.add_argument(
        '--public-build',
        action='store_true',
        help='Use public build artifacts instead of internal ones.',
    )
    parser.add_argument(
        '--chromeos-root',
        type=cli.argtype_dir_path,
        metavar='CHROMEOS_ROOT',
        default=os.environ.get('CHROMEOS_ROOT', ''),
        help='ChromeOS tree root (default: %(default)s)',
    )
    parser.add_argument(
        '--chromeos-mirror',
        type=cli.argtype_dir_path,
        default=os.environ.get('CHROMEOS_MIRROR', ''),
        help='ChromeOS repo mirror path',
    )
    parser.add_argument(
        '--no-wait-for-build-completion',
        action='store_true',
        help='When used with --no-deploy, the script exits '
        'immediately after the job is scheduled.',
    )
    parser.add_argument(
        '--buildbucket-id',
        type=int,
        help='Assign a buildbucket id instead of sending a build request',
    )
    parser.add_argument(
        '--manifest',
        help='(for testing) '
        'Assign a chromeos manifest instead of deriving from rev',
    )
    parser.add_argument(
        '--bucket',
        default='bisector',
        help='Assign a buildbucket bucket to build it (default: %(default)s)',
    )
    parser.add_argument(
        '--build-revlist',
        action='store_true',
        help='Force to build revlist cache again. '
        'This flag is recommended if you need to run this script independently.',
    )

    group = parser.add_argument_group(title='Build and deploy options')
    add_build_and_deploy_arguments(group)
    return parser


def create_argument_parser():
    parser = cli.ArgumentParser()
    subparsers = parser.add_subparsers(dest='subcommand', required=True)

    parents = [
        cli.create_session_optional_parser(),
        create_common_argument_parser(),
        switch_helper.common_local_build_flags(),
    ]
    # chromeos parser
    parser_cros = subparsers.add_parser(
        'bisect_chromeos', help='Bisect on ChromeOS versions', parents=parents
    )
    parser_cros.add_argument(
        'chromeos_rev',
        nargs='?',
        type=cli.argtype_notempty,
        metavar='CROS_VERSION',
        default=os.environ.get('CROS_VERSION', ''),
        help='ChromeOS local build version string, in format short version, '
        'full version, or "full,full+N"',
    )

    # chrome parser
    parser_chrome = subparsers.add_parser(
        'bisect_chrome', help='Bisect on Chrome versions', parents=parents
    )
    parser_chrome.add_argument(
        '--chrome-root',
        metavar='CHROME_ROOT',
        type=cli.argtype_dir_path,
        default=os.environ.get('CHROME_ROOT', ''),
        help='Root of Chrome source tree, like ~/chromium',
    )
    parser_chrome.add_argument(
        '--chrome-mirror',
        metavar='CHROME_MIRROR',
        type=cli.argtype_dir_path,
        default=os.environ.get('CHROME_MIRROR', ''),
        help='gclient cache dir',
    )
    parser_chrome.add_argument(
        '--chromeos-rev',
        type=cli.argtype_notempty,
        metavar='CROS_VERSION',
        default=os.environ.get('CROS_VERSION', ''),
        help='ChromeOS local build version string, in format short version, '
        'full version, or "full,full+N"',
    )
    parser_chrome.add_argument(
        '--deps',
        help='(for testing) '
        'Assign a Chrome DEPS instead of deriving from rev',
    )
    parser_chrome.add_argument(
        '--chrome-rev',
        type=cli.argtype_notempty,
        metavar='REV',
        help='Chrome revision',
    )

    return parser


def get_last_commit_time(action_groups):
    for action_group in reversed(action_groups):
        if action_group.actions:
            return action_group.actions[-1].timestamp
    return 0


def read_file(file_name):
    with open(file_name) as f:
        result = f.read()
    return result


def get_last_float_spec(float_specs, timestamp):
    for spec in reversed(float_specs):
        if spec.timestamp <= timestamp:
            return spec
    raise errors.BuildError(
        'Cannot predict a correct float spec before timestamp %d' % timestamp
    )


def get_chrome_managers(opts):
    cache_manager = gclient_util.GclientCache(opts.chrome_mirror)
    config = {
        "chrome_root": opts.chrome_root,
        "chrome_mirror": opts.chrome_mirror,
    }
    spec_manager = cr_util.ChromeSpecManager(config)  # type: ignore
    code_manager = codechange.CodeManager(
        opts.chrome_root,
        spec_manager,
        cache_manager,
        common.get_session_cache_dir(opts.session),
    )
    return spec_manager, code_manager, cache_manager


def get_chrome_buildspec_by_time(opts, timestamp, old_rev, new_rev):
    """Get a Chrome Deps buildspec (float spec) at given timestamp"""
    spec_manager, _code_manager, cache_manager = get_chrome_managers(opts)
    branch = 'branch-heads/' + cr_util.extract_branch_from_version(new_rev)
    deps_path = spec_manager.generate_meta_deps(branch)
    old_timestamp = spec_manager.lookup_build_timestamp(old_rev)
    new_timestamp = spec_manager.lookup_build_timestamp(new_rev)
    if not old_timestamp <= timestamp <= new_timestamp:
        logger.warning(
            'Adjust timestamp: [%d, %d], %d',
            old_timestamp,
            new_timestamp,
            timestamp,
        )
        old_timestamp = min(old_timestamp, timestamp)
        new_timestamp = max(new_timestamp, timestamp)

    result = None
    parser = gclient_util.DepsParser(opts.chrome_root, cache_manager)
    timeseries_forests = parser.enumerate_gclient_solutions(
        old_timestamp, new_timestamp, deps_path
    )
    for t, forest in timeseries_forests:
        if t <= timestamp:
            result = copy.deepcopy(forest)

    return parser.flatten(result, deps_path)


def get_config_for_chromeos_spec_manager(opts):
    return {
        "board": opts.board,
        "chromeos_root": opts.chromeos_root,
        "chromeos_mirror": opts.chromeos_mirror,
        # TODO(b/441146529): Currently cros localbuild doesn't support a public
        # build.
        "is_public_build": False,
    }


def get_chromeos_spec_manager(opts):
    config = get_config_for_chromeos_spec_manager(opts)
    return cros_util.ChromeOSSpecManager(config)


def get_deps(opts):
    if opts.deps:
        return read_file(opts.deps)

    spec_manager, code_manager, cache_manager = get_chrome_managers(opts)
    old_rev, new_rev, _ = codechange.parse_intra_rev(opts.chrome_rev)
    intra_revision, diff = code_manager.get_intra_and_diff(opts.chrome_rev)
    if intra_revision == opts.chrome_rev:
        return spec_manager.get_release_deps(intra_revision)

    # step1: Get a base deps with correct projects and sources by time.
    #        Then apply diffs after intra version.
    timestamp = get_last_commit_time(diff)
    deps = get_chrome_buildspec_by_time(opts, timestamp, old_rev, new_rev)
    deps.apply_action_groups(diff)

    # step2: Apply remaining projects from intra_version.
    parser = gclient_util.DepsParser(opts.chrome_root, cache_manager)
    intra_deps = parser.parse_single_deps(
        spec_manager.get_release_deps(intra_revision)
    )
    deps.apply_deps(intra_deps)
    return deps.to_string()


def get_release_manifest(opts):
    """Get manifest of a release ChromeOS version or snapshot version"""
    if opts.manifest:
        return read_file(opts.manifest)
    spec_manager = get_chromeos_spec_manager(opts)
    return spec_manager.get_manifest(opts.chromeos_rev)


def get_manifest(opts):
    if opts.manifest:
        return read_file(opts.manifest)
    manifest_internal_dir = os.path.join(
        opts.chromeos_mirror, 'manifest-internal.git'
    )
    spec_manager = get_chromeos_spec_manager(opts)
    cache = repo_util.RepoMirror(opts.chromeos_mirror)
    code_manager = codechange.CodeManager(
        opts.chromeos_root,
        spec_manager,
        cache,
        common.get_session_cache_dir(opts.session),
    )

    old_rev, new_rev, _ = codechange.parse_intra_rev(opts.chromeos_rev)
    intra_revision, diff = code_manager.get_intra_and_diff(opts.chromeos_rev)
    if intra_revision == opts.chromeos_rev:
        return spec_manager.get_manifest(intra_revision)

    # get specs
    fixed_specs = spec_manager.collect_fixed_spec(old_rev, new_rev)
    for spec in fixed_specs:
        spec_manager.parse_spec(spec)
    float_specs = spec_manager.collect_float_spec(old_rev, new_rev, fixed_specs)
    for spec in float_specs:
        spec_manager.parse_spec(spec)

    # apply_manifest and apply_action_groups doesn't overwrite project's revision
    # by default, so we should apply projects by reverse chronological order here

    # step1: Get a base manifest with correct projects and sources by time.
    #        Then apply diffs after intra version.
    result = repo_util.Manifest(manifest_internal_dir)
    timestamp = get_last_commit_time(diff)
    result.load_from_commit(get_last_float_spec(float_specs, timestamp).name)

    # manifest from manifest-internal repository might contain revision which
    # value is a branch or tag name.
    # As every project in snapshot should have a commit hash, we remove all the
    # default revisions here to make it more significant when snapshot is not
    # complete.
    result.remove_project_revision()
    result.apply_action_groups(diff)

    # step2: Apply remaining projects from intra_version snapshot.
    snapshot = repo_util.Manifest(manifest_internal_dir)
    snapshot.load_from_string(spec_manager.get_manifest(intra_revision))
    result.apply_manifest(snapshot)

    if not result.is_static_manifest():
        raise errors.BuildError(
            'cannot recover project revision from snapshot and diff, '
            'there might be unnecessary project in manifest-internal, '
            'or snapshot might be incomplete.'
        )
    return result.to_string()


def search_build_image(buildbucket_id):
    api = buildbucket_util.BuildbucketApi()
    properties = api.get_build(buildbucket_id).output.properties

    if 'artifacts' not in properties:
        raise errors.BuildError(
            'artifacts not found in buildbucket_id: %s' % buildbucket_id
        )

    gs_path = 'gs://%s/%s' % (
        properties['artifacts']['gs_bucket'],
        properties['artifacts']['gs_path'],
    )

    image_info = cros_util.ImageInfo()
    image_info[cros_util.ImageType.PARTITION_IMAGE] = gs_path
    image_info[cros_util.ImageType.ZIP_FILE] = gs_path + '/image.zip'
    return image_info


def search_closest_snapshot_image(opts) -> typing.Optional[str]:
    """Returns the first snapshot image after or equal to the CrOS version.

    Returns:
        The first CrOS snapshot version equal to or after the CrOS built, or
        None if not found.
    """
    _, new_rev, _ = codechange.parse_intra_rev(opts.chromeos_rev)
    if cros_util.is_cros_snapshot_version(new_rev):
        return new_rev
    # Searches an earliest snapshot build in +10 major versions. Reasons:
    #     1: In practical there should be a snapshot build in every major
    #        versions.
    #     2: The performance improvement of using a far prebuilt is not
    #        significant.
    spec_manager = get_chromeos_spec_manager(opts)
    new_major_version = int(cros_util.extract_major_version(new_rev))
    search_end_version = f'{new_major_version}.0.0'
    # "{new_major_version+10}.0.0" may be an unexisting release, if it doesn't
    # exist, we pick the latest released version as an upper_bound version.
    for major_version in range(
        new_major_version + 10, new_major_version - 1, -1
    ):
        if info := spec_manager.lookup_chromeos_version(f'{major_version}.0.0'):
            search_end_version = (
                f'R{info.chrome_branch}-{info.chromeos_build}.0.0'
            )
            break
    versions, _ = cros_util.list_chromeos_prebuilt_versions(
        opts.board,
        new_rev,
        search_end_version,
        # TODO(b/441146529): Currently cros localbuild doesn't support a public
        # build.
        is_public_build=False,
        use_snapshot=True,
        spec_manager=get_chromeos_spec_manager(opts),
    )
    for v in versions:
        if cros_util.is_cros_snapshot_version(v):
            return v
    return None


def search_closest_snapshot_commit(opts) -> typing.Optional[str]:
    """Returns the first snapshot commit after or equal to the CrOS version.

    Returns:
        The first CrOS snapshot commit equal to or after the CrOS built, or
        None if not found.
    """
    closest_version = search_closest_snapshot_image(opts)
    if not closest_version:
        return None
    _, _, closest_snapshot_id = cros_util.snapshot_version_split(
        closest_version
    )

    spec_manager = get_chromeos_spec_manager(opts)
    for (
        _,
        commit_id,
        snapshot_id,
    ) in spec_manager.lookup_snapshot_manifest_revisions(
        closest_version, closest_version
    ):
        if int(snapshot_id) == int(closest_snapshot_id):
            return commit_id
    return None


def is_sysroot_archive_compatible(opts) -> bool:
    """Returns if the version is sysroot archive compatible.

    Returns:
        True if the version has sysroot archive API call.
    """
    # Sysroot archive API in chromite is avaliable from 15742.0.0.
    old_rev, _, _ = codechange.parse_intra_rev(opts.chromeos_rev)
    old_cros_major_version = int(cros_util.extract_major_version(old_rev))
    return old_cros_major_version >= 15742


def search_and_schedule_build(opts) -> int | None:
    build_tags = {
        'bisector_chromeos_version': opts.chromeos_rev,
        'bisector_session': opts.session,
        'bisector_host': socket.gethostname(),
    }
    api = buildbucket_util.BuildbucketApi()

    if opts.subcommand == 'bisect_chrome':
        logger.warning(
            'DEPS file flatten is not reimplemented and thus '
            'building chrome with builder is broken (b/178680123)'
        )
        manifest = get_release_manifest(opts)
        deps = get_deps(opts)
        build_tags['bisector_chrome_version'] = opts.chrome_rev
        git_auth = False
    else:
        manifest = get_manifest(opts)
        deps = None
        git_auth = True

    save_sysroot_archive = use_sysroot_archive = is_sysroot_archive_compatible(
        opts
    )
    build_info = api.search_and_schedule_build(
        opts.board,
        manifest,
        bucket=opts.bucket,
        build_tags=build_tags,
        deps=deps,
        git_auth=git_auth,
        search_only=opts.no_build,
        chromeos_version=opts.chromeos_rev,
        # The snapshot commit hash used to search prebuilt cache, if specified
        # buildbucket will incrementally build from old snapshot build.
        snapshot_commmit_id=search_closest_snapshot_commit(opts),
        save_sysroot_archive=save_sysroot_archive,
        use_sysroot_archive=use_sysroot_archive,
    )

    if build_info is None:
        return None

    build_id = int(build_info.id)
    logger.info('buildbucket id = %d', build_id)
    logger.info(
        'build url: %s',
        buildbucket_util.get_luci_link(
            opts.board, build_id, bucket=opts.bucket
        ),
    )
    return build_id


def wait_for_build_completion(opts, build_id: int):
    """Wait until a build completes.

    Raises:
      errors.BuildError
    """
    api = buildbucket_util.BuildbucketApi()
    last_log_time = 0.0
    while True:
        build = api.get_build(build_id)
        if not api.is_running(build):
            break
        now = time.time()
        if now - last_log_time >= 600:
            logger.info('Waiting for buildbucket build_id %d', build_id)
            last_log_time = now
        time.sleep(60)

    if not api.is_success(build):
        if build.status == buildbucket_util.Status.Value('INFRA_FAILURE'):
            # Infra failure is a temporary error.
            raise errors.ExternalError(
                'buildbucket infra failure id=%d, status=%s, summary=%r, url=%s'
                % (
                    build_id,
                    buildbucket_util.Status.Name(build.status),
                    build.summary_markdown,
                    buildbucket_util.get_luci_link(
                        opts.board, build_id, bucket=opts.bucket
                    ),
                )
            )
        raise errors.BuildError(
            'buildbucket build fail id=%d, status=%s, summary=%r, url=%s'
            % (
                build_id,
                buildbucket_util.Status.Name(build.status),
                build.summary_markdown,
                buildbucket_util.get_luci_link(
                    opts.board, build_id, bucket=opts.bucket
                ),
            )
        )


def build_revlist(opts):
    old_rev, new_rev, _ = codechange.parse_intra_rev(opts.chromeos_rev)
    config = get_config_for_chromeos_spec_manager(opts)
    spec_manager = get_chromeos_spec_manager(opts)
    cache = repo_util.RepoMirror(opts.chromeos_mirror)
    code_manager = codechange.CodeManager(
        config['chromeos_root'],
        spec_manager,
        cache,
        common.get_session_cache_dir(opts.session),
    )
    code_manager.build_revlist(old_rev, new_rev)


def switch(opts) -> core.StepResult:
    # schedule build request and prepare image
    if opts.buildbucket_id:
        buildbucket_id = opts.buildbucket_id
    else:
        buildbucket_id = search_and_schedule_build(opts)

    logger.info('buildbucket_id: %s', buildbucket_id)

    if not buildbucket_id:
        msg = 'no buildbucket_id found'
        logger.info(msg)
        return core.StepResult('fatal', reason=msg)

    # If --no-deploy is False, always wait for build completion because the
    # binary is needed for deploy.  Otherwise,
    # 1. If --no-wait-for-build-completion is True, exit as soon as job
    #   scheduled and ignore potential errors.
    # 2. If --no-wait-for-build-completion is False, exit after the build
    #   completes.
    if not opts.no_deploy or not opts.no_wait_for_build_completion:
        wait_for_build_completion(opts, buildbucket_id)

    if opts.no_deploy or cros_util.is_vm_board(opts.board):
        if not opts.no_deploy:
            logger.warning('skip cros_flash for vm boards')
        status = 'skip' if opts.no_wait_for_build_completion else 'old'
        return core.StepResult(
            status,
            reason=buildbucket_util.get_luci_link(
                opts.board, buildbucket_id, bucket=opts.bucket
            ),
        )

    image_info = search_build_image(buildbucket_id)

    # deploy and flash
    cros_util.provision_image_with_retry(
        opts.chromeos_root,
        opts.dut,
        opts.board,
        image_info,
        # TODO(b/441146529): Currently cros localbuild doesn't support a public
        # build.
        is_public_build=False,
        clobber_stateful=opts.clobber_stateful,
        disable_rootfs_verification=opts.disable_rootfs_verification,
        repair_callback=cros_lab_util.repair,
        force_reboot_callback=cros_lab_util.reboot_via_servo,
    )

    return core.StepResult(
        'old',
        reason=buildbucket_util.get_luci_link(
            opts.board, buildbucket_id, bucket=opts.bucket
        ),
    )


def switch_main(args: tuple[str] | None) -> core.StepResult:
    parser = create_argument_parser()
    opts = parser.parse_args(args)
    switch_helper.post_init_local_build_flags(opts)
    common.config_logging(opts)

    if opts.dut:
        if cros_lab_util.is_satlab_dut(opts.dut):
            cros_lab_util.write_satlab_ssh_config(opts.dut)
        if not cros_util.is_dut(opts.dut):
            raise errors.BrokenDutException(
                '%r is not a valid DUT address' % opts.dut
            )
        if not cros_util.is_good_dut(opts.dut):
            logger.fatal('%r is not a good DUT', opts.dut)
            if not cros_lab_util.repair(opts.dut, opts.chromeos_root):
                raise errors.BrokenDutException(
                    '%r is not a good DUT' % opts.dut
                )

    if cros_util.is_cros_short_version(opts.chromeos_rev):
        opts.chromeos_rev = cros_util.version_to_full(
            opts.board, opts.chromeos_rev
        )

    if opts.build_revlist:
        build_revlist(opts)

    cros_util.prepare_chroot(opts.chromeos_root)

    try:
        result = switch(opts)
    finally:
        if opts.dut:
            # No matter switching succeeded or not, DUT must be in good state.
            # switch() already tried repairing if possible, no repair here.
            if not cros_util.is_good_dut(opts.dut):
                raise errors.BrokenDutException(
                    '%r is not a good DUT' % opts.dut
                )
    logger.info('done')
    return result


def action() -> bisector_cli.SwitchAction:
    return bisector_cli.SwitchAction.BUILD_AND_DEPLOY


def main(args: tuple[str] | None = None) -> int:
    return bisector_cli.step_main_wrapper(switch_main, args)


if __name__ == '__main__':
    sys.exit(main())
