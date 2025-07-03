# Copyright 2017 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Chrome utility."""

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.request

from bisect_kit import errors
from bisect_kit import git_util
from bisect_kit import locking
from bisect_kit import util


logger = logging.getLogger(__name__)

RE_CHROME_VERSION = r'^\d+\.\d+\.\d+\.\d+$'

CHROME_BINARIES = ['chrome']
CHROME_BINARIES_OPTIONAL = ['nacl_helper']
# This list is created manually by inspecting
# src/third_party/chromiumos-overlay/chromeos-base/chrome-binary-tests/
# chrome-binary-tests-0.0.1.ebuild
# TODO(kcwu): we can get rid of this table once we migrated to build chrome
#             using chromeos ebuild rules.
CHROME_TEST_BINARIES = [
    'capture_unittests',
    'dawn_end2end_tests',
    'dawn_unittests',
    'decode_test',
    'fake_dmserver',
    'libtest_trace_processor.so',
    'image_processor_test',
    'image_processor_perf_test',
    'jpeg_decode_accelerator_unittest',
    'jpeg_encode_accelerator_unittest',
    'ozone_gl_unittests',
    'ozone_integration_tests',
    'sandbox_linux_unittests',
    'screen_ai_ocr_perf_test',
    'v4l2_stateless_decoder',
    'v4l2_unittest',
    'vaapi_unittest',
    'video_decode_accelerator_perf_tests',
    'video_decode_accelerator_tests',
    'video_encode_accelerator_perf_tests',
    'video_encode_accelerator_tests',
    'vulkan_overlay_adaptor_test',
    'wayland_client_integration_tests',
    'wayland_client_perftests',
    'wayland_hdr_client',
    # Removed binaries. Keep them here for users to bisect old versions.
    'video_decode_accelerator_unittest',
    'video_encode_accelerator_unittest',
]


def is_chrome_version(s):
    """Is a chrome version string?"""
    return bool(re.match(RE_CHROME_VERSION, s))


def is_commit_position(s):
    """Returns whether the given string is a chrome commit position."""
    return bool(re.match(r'^[^@]+@{#\d+}$', s))


def _canonical_build_number(s):
    """Returns canonical build number.

    "The BUILD and PATCH numbers together are the canonical representation of
    what code is in a given release."
    ref: https://www.chromium.org/developers/version-numbers

    This format is used for version comparison.

    Args:
      s: chrome version string, in format MAJOR.MINOR.BUILD.PATCH

    Returns:
      BUILD.PATCH
    """
    assert s.count('.') == 3
    return '.'.join(s.split('.')[2:])


def is_version_lesseq(a, b):
    """Compares whether Chrome version `a` is less or equal to version `b`."""
    return util.is_version_lesseq(
        _canonical_build_number(a), _canonical_build_number(b)
    )


def is_direct_relative_version(a, b):
    return util.is_direct_relative_version(
        _canonical_build_number(a), _canonical_build_number(b)
    )


def extract_branch_from_version(rev):
    """Extracts branch number from version string

    Args:
      rev: chrome version string

    Returns:
      branch number (str). For example, return '3064' for '59.0.3064.0'.
    """
    return rev.split('.')[2]


def extract_milestone_from_version(rev: str) -> str:
    """Extracts milestone from version string

    Args:
      rev: chrome version string

    Returns:
      milestone (str). For example, return '59' for '59.0.3064.0'.
    """
    return rev.split('.')[0]


def argtype_chrome_version(s):
    if not is_chrome_version(s):
        raise errors.ArgTypeError('invalid chrome version', '59.0.3064.0')
    return s


def get_commit_position(metadata: git_util.CommitMeta) -> str | None:
    if not metadata.message:
        return None
    for line in reversed(metadata.message.splitlines()):
        if line.startswith('Cr-Commit-Position: '):
            return line.split(' ', 1)[1]
        if line == '' or line.isspace():
            break
    return None


def query_git_rev_by_commit_position(chrome_src, commit_position):
    """Looks up git commit by chrome's commit position.

    This function cannot find early commits which is still using 'git-svn-id'.

    Args:
      chrome_src: git repo path of chrome/src
      commit_position: chrome commit position (e.g. 'refs/heads/main@{#1364755}')

    Returns:
      git commit id

    Raises:
      ValueError: unable to find commits with given commit_position.
    """
    rev = util.check_output(
        'git',
        'rev-list',
        '-1',
        '--all',
        '--grep',
        '^Cr-Commit-Position: %s' % commit_position,
        cwd=chrome_src,
    ).strip()
    if not rev:
        raise ValueError('unable to find commits with given commit_position')
    return rev


def query_git_rev(chrome_src, rev):
    """Guesses git commit by heuristic.

    Args:
      chrome_src: git repo path of chrome/src
      rev: could be
        chrome version, commit position, or git hash

    Returns:
      git commit hash
    """
    if is_chrome_version(rev):
        try:
            # Query via git tag
            return git_util.get_commit_hash(chrome_src, rev)
        except ValueError as e:
            # Failed due to source code not synced yet? Anyway, query by web api
            url = 'https://omahaproxy.appspot.com/deps.json?version=' + rev
            logger.debug('fetch %s', url)
            content = urllib.request.urlopen(url).read().decode('utf8')
            obj = json.loads(content)
            rev = obj['chromium_commit']
            if not git_util.is_git_rev(rev):
                raise ValueError(
                    'The response of omahaproxy, %s, is not a git commit hash'
                    % rev
                ) from e

    if git_util.is_git_rev(rev):
        return rev

    # Cr-Commit-Position
    m = re.match(r'^#(\d+)$', rev)
    if m:
        return query_git_rev_by_commit_position(
            chrome_src, 'refs/heads/main@{#%s}' % m.group(1)
        )

    raise ValueError('unknown rev format: %s' % rev)


def simple_chrome_shell(chrome_src, board, *args, **kwargs):
    """Run commands inside chrome-sdk shell.

    Args:
      chrome_src: git repo path of chrome/src
      board: ChromeOS board name
      *args: command to run
      **kwargs:
        env: (dict) environment variables for the command
    """
    prefix = [
        'cros',
        'chrome-sdk',
        '--board=%s' % board,
    ]
    if kwargs.get('internal'):
        prefix.append('--internal')

    prefix.append('--use-remoteexec')

    lkgm_file = os.path.join(chrome_src, 'chromeos', 'CHROMEOS_LKGM')
    if os.path.exists(lkgm_file):
        with open(lkgm_file) as f:
            lkgm_version = f.read().strip()
            # This work around b/205818006
            if util.is_version_lesseq(
                '14285.0.0', lkgm_version
            ) and util.is_version_lesseq(lkgm_version, '14309.0.0'):
                prefix += ['--version', '14284.0.0']

    env = kwargs.get('env', os.environ).copy()

    # This work around http://crbug.com/658104, which depot_tool can't find GN
    # path correctly.
    env['CHROMIUM_BUILDTOOLS_PATH'] = os.path.abspath(
        os.path.join(chrome_src, 'buildtools')
    )

    # Add the environment variables for reproxy
    env.update(get_RBE_environment_variables())

    timeout = kwargs.get('timeout')

    cmd = prefix + ['--'] + list(args)

    return util.check_output(*cmd, cwd=chrome_src, env=env, timeout=timeout)


def determine_targets_to_build(
    chrome_src: str,
    board: str,
    with_tests: bool = True,
):
    out_dir = os.path.join('out_%s' % board, 'Release')
    try:
        available_targets = simple_chrome_shell(
            chrome_src,
            board,
            'buildtools/linux64/gn',
            'ls',
            '--type=executable',
            '--as=output',
            out_dir,
            internal=True,
        ).splitlines()
    except subprocess.CalledProcessError as e:
        raise errors.BuildError(
            'failed to determine chrome targets to build'
        ) from e

    mandatory = CHROME_BINARIES
    optional = list(CHROME_BINARIES_OPTIONAL)
    if with_tests:
        optional += CHROME_TEST_BINARIES

    result = []
    for binary in mandatory:
        assert binary in available_targets, (
            'build rule for %r is not found?' % binary
        )
        result.append(binary)
    for binary in optional:
        # b/258539287: nacl_helper is not shown in `gn ls`
        if (
            binary == 'nacl_helper'
            and binary not in available_targets
            and 'nacl_helper_arm32/nacl_helper' in available_targets
        ):
            result.append(binary)
            logger.warning(
                '"nacl_helper" not found but "nacl_helper_arm32/nacl_helper" '
                'in gn ls, force adding "nacl_helper" to targets list'
            )
            continue
        if binary not in available_targets:
            continue
        result.append(binary)
    return result


def build(chrome_src, board, targets, with_tests=True):
    """Build Chrome binaries.

    Args:
      chrome_src: git repo path of chrome/src
      board: ChromeOS board name
      targets: ninja targets. Default to build chrome.
      with_tests: build test binaries as well if targets is not specified

    Returns:
      The output directory of the build.
    """
    if not targets:
        targets = determine_targets_to_build(
            chrome_src, board, with_tests=with_tests
        )

    logger.info('build %s', targets)
    out_dir = os.path.join('out_%s' % board, 'Release')
    with locking.lock_file(locking.LOCK_FILE_FOR_BUILD):
        cmd = ['autoninja', '-C', out_dir] + targets
        # Chrome build shouldn't take more than 3 hours. Set timeout to abort
        # the process in case it gets deadlocked. b/369498616
        timeout = 3 * 60 * 60
        try:
            simple_chrome_shell(
                chrome_src, board, *cmd, internal=True, timeout=timeout
            )
        except subprocess.CalledProcessError:
            logger.warning('build failed, delete out directory and retry again')
            # Some compiler processes are still terminating. Short delay is
            # necessary.
            time.sleep(10)
            shutil.rmtree(os.path.join(chrome_src, out_dir))
            try:
                simple_chrome_shell(
                    chrome_src, board, *cmd, internal=True, timeout=timeout
                )
            except subprocess.CalledProcessError as e:
                raise errors.BuildError('failed to build chrome') from e

    return os.path.join(chrome_src, out_dir)


def deploy_to_staging_dir(chrome_src, board, targets, out_dir, staging_dir):
    """Deploy Chrome binaries to the specified staging directory.

    Args:
      chrome_src: git repo path of chrome/src
      board: ChromeOS board name
      targets: ninja targets.
      out_dir: output dir from which to deploy binaries.
      staging_dir: staging dir to which to deploy binaries.
    """
    # Deploy chrome to the staging dir if needed.
    if set(CHROME_BINARIES).intersection(targets):
        cmd = [
            'deploy_chrome',
            '--force',
            '--build-dir',
            out_dir,
            '--staging-dir',
            staging_dir,
            '--staging-only',
        ]
        simple_chrome_shell(chrome_src, board, *cmd, internal=True)
    # Deploy test binaries to the staging dir if needed.
    test_binaries = set(CHROME_TEST_BINARIES).intersection(targets)
    for target in test_binaries:
        src = os.path.join(out_dir, target)
        dest = os.path.join(staging_dir, target)
        # Strip the target file.
        util.check_call('eu-strip', '--strip-debug', src, '-o', dest)
        # chmod o+x.
        stat = os.stat(dest)
        os.chmod(dest, stat.st_mode | 0o001)


def deploy(chrome_src, board, dut, targets, with_tests=True, out_dir=None):
    """Deploy Chrome binaries.

    Args:
      chrome_src: git repo path of chrome/src
      board: ChromeOS board name
      dut: DUT address
      targets: ninja targets. Default to deploy any target found in out_dir.
      with_tests: build test binaries as well if targets is not specified
      out_dir: optional output dir from which to deploy binaries.
    """
    if not targets:
        targets = os.listdir(out_dir)
    logger.info('deploy %s', targets)

    if not out_dir:
        out_dir = os.path.join('out_%s' % board, 'Release')

    # Deploy chrome if needed.
    if set(CHROME_BINARIES).intersection(targets):
        cmd = [
            'deploy_chrome',
            '--force',
            '--build-dir',
            out_dir,
            '--device',
            dut,
        ]
        try:
            simple_chrome_shell(chrome_src, board, *cmd, internal=True)
        except subprocess.CalledProcessError as e:
            raise errors.ExternalError('chrome deploy failed') from e

    # Deploy test binaries if needed.
    test_binaries = set(CHROME_TEST_BINARIES).intersection(targets)
    if with_tests and test_binaries:
        with tempfile.TemporaryDirectory() as staging_dir:
            # Deploy test binaries to the staging directory.
            deploy_to_staging_dir(
                chrome_src, board, test_binaries, out_dir, staging_dir
            )
            try:
                autotest_test_binary_path = (
                    "/usr/local/autotest/deps/chrome_test/test_src/out/Release/"
                )
                tast_test_binary_path = (
                    "/usr/local/libexec/chrome-binary-tests/"
                )

                # Copy test binaries to the autotest binary directory.
                util.ssh_cmd(
                    dut,
                    'mkdir',
                    '-p',
                    autotest_test_binary_path,
                )
                # NOTE: A trailing slash (i.e. '/' at the end of the path) needs
                # to be added to staging_dir to copy its contents instead of the
                # directory itself (see `man rsync`).
                util.check_call(
                    'rsync',
                    '-r',
                    f'{staging_dir}/',
                    f'{dut}:{autotest_test_binary_path}',
                )
                # Copy test binaries to the tast binary directory.
                util.ssh_cmd(
                    dut,
                    'rsync',
                    '-r',
                    autotest_test_binary_path,
                    tast_test_binary_path,
                )
            except subprocess.CalledProcessError as e:
                raise errors.ExternalError(
                    'Failed to depoy test binaries'
                ) from e


def _get_ancestors(target_version: str, version_list: list[str]) -> list[str]:
    """Get the ancestor version list of a target version.

    Args:
      target_version: The version we want to find the ancestors for
      version_list: A list of chrome versions

    Returns:
      A list of ancestor version of target version
    """
    ancestors: list[str] = []
    for candidate_ancestor in version_list:
        if not is_chrome_version(candidate_ancestor):
            continue
        if is_version_lesseq(
            candidate_ancestor, target_version
        ) and is_direct_relative_version(candidate_ancestor, target_version):
            ancestors.append(candidate_ancestor)
    return sorted(ancestors, key=util.version_key_func)


def get_lca(old_version: str, new_version: str, version_list: list[str]) -> str:
    """Get the lowest common ancestor of old_version and new_version.

    Args:
      old_version: Old chrome version
      new_version: New chrome version
      version_list: A list of chrome versions

    Returns:
      The lowest common ancestor of old_version and new_version
    """
    ancestors_of_old_version = _get_ancestors(old_version, version_list)
    ancestors_of_new_version = _get_ancestors(new_version, version_list)

    ancestors_of_old_version.sort(key=util.version_key_func)

    for old_ancestor in reversed(ancestors_of_old_version):
        if old_ancestor in ancestors_of_new_version:
            return old_ancestor
    raise errors.InternalError(
        'Unable to find their common ancestor: %s, %s'
        % (old_version, new_version)
    )


def get_RBE_environment_variables() -> dict[str, str]:
    """Generate environment variables for reproxy

    Returns:
        A dictionary containing required variables
    """
    env = os.environ.copy()

    bisect_runner_json_path = os.environ.get(
        'SKYLAB_CLOUD_SERVICE_ACCOUNT_JSON'
    )
    env['RBE_credential_file'] = bisect_runner_json_path
    env['RBE_use_application_default_credentials'] = 'true'
    env['RBE_use_gce_credentials'] = 'false'
    env['RBE_automatic_auth'] = 'false'
    # (b/328578714) reclient now delegate auth to "credentials_helper".
    # We need to overwrite its default args to make it work properly.
    env['RBE_credentials_helper_args'] = (
        '--auth_source=gcloud --gcert_refresh_timeout=20'
    )
    # Set the same value to experimental_credentials_helper_args which is
    # needed to build Chrome older than crrev.com/c/5863651
    env['RBE_experimental_credentials_helper_args'] = env[
        'RBE_credentials_helper_args'
    ]
    # Autoninja does not allow external accounts on corp machines to
    # authenticate with reproxy by default. This flag is used to skip
    # that check. For more details, see b/320639792.
    env['AUTONINJA_SKIP_EXTERNAL_ACCOUNT_CHECK'] = '1'

    return env


def build_revlist(chrome_src: str, old: str, new: str):
    """Build revlist.
    Args:
      chrome_src: chrome src directory path
      old: chrome commit position from which the returned revlist starts
      new: chrome commit position on which the returned revlist ends

    Returns:
      (revlist, details):
        revlist: list of rev string
        details: dict of rev to rev detail
    """

    old_commit_hash = query_git_rev_by_commit_position(chrome_src, old)
    new_commit_hash = query_git_rev_by_commit_position(chrome_src, new)

    revisions = [old_commit_hash] + git_util.git_output(
        [
            'rev-list',
            '--reverse',
            '--first-parent',
            '%s..%s' % (old_commit_hash, new_commit_hash),
        ],
        cwd=chrome_src,
    ).splitlines()
    metadata = git_util.get_batch_commit_metadata(chrome_src, revisions)

    revlist = []
    details = {}

    for revision in revisions:
        commit_position = get_commit_position(metadata[revision])
        if not commit_position:
            logger.warning('Failed to get the commit position of %s', revision)
            continue

        revlist.append(commit_position)
        details[commit_position] = {
            'actions': [
                {
                    'action_type': 'commit',
                    'commit_summary': git_util.CommitMeta.get_summary(
                        git_util.get_commit_metadata(chrome_src, revision)
                    ),
                    'path': 'src',
                    'repo_url': 'https://chromium.googlesource.com/chromium/src.git',
                    'rev': revision,
                }
            ]
        }
    return (revlist, details)


def get_chrome_commit_hash(details) -> str:
    """Gets the chrome commit hash from details dictionary

    Args:
      details: details dict returned by `build_revlist()`

    Returns:
      chrome git commit hash
    """
    return details['actions'][0]['rev']


def get_lca_chrome_localbuild(
    chrome_src, old_version: str, new_version: str
) -> str:
    """Get the lowest common ancestor of 2 chrome localbuild version

    Args:
      old_version: Old chrome version
      new_version: New chrome version

    Returns:
      The lowest common ancestor of 2 chrome localbuild version
    """
    version_list_chrome: list[str] = []
    for rev in git_util.get_tags(chrome_src):
        version_list_chrome.append(rev)

    return get_lca(old_version, new_version, version_list_chrome)
