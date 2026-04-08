# Copyright 2017 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Chrome utility."""

import json
import logging
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request

from bisect_kit import chromite_util
from bisect_kit import errors
from bisect_kit import git_util
from bisect_kit import locking
from bisect_kit import util


logger = logging.getLogger(__name__)

RE_CHROME_VERSION = r'^\d+\.\d+\.\d+\.\d+$'
RE_CHROME_VERSION_WITH_PRE = r'^\d+\.\d+\.\d+\.\d+_pre\d+$'

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
    # Removed binaries. Keep them here for users to bisect old versions.
    'video_decode_accelerator_unittest',
    'video_encode_accelerator_unittest',
]


def is_chrome_version(s):
    """Is a chrome version string?"""
    return bool(re.match(RE_CHROME_VERSION, s))


def is_chrome_version_with_pre(s):
    """Is a chrome version string with a '_preXXXX' suffix?"""
    return bool(re.match(RE_CHROME_VERSION_WITH_PRE, s))


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


def argtype_chrome_version_with_pre(s):
    if not is_chrome_version_with_pre(s):
        raise errors.ArgTypeError(
            'invalid chrome version', '144.0.7503.0_pre1537931'
        )
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


def query_git_rev_by_commit_position_remotely(number):
    number_str = str(number)
    assert number_str.isdigit()
    query = urllib.parse.urlencode(
        {
            'number': number_str,
            'numbering_identifier': 'refs/heads/main',
            'numbering_type': 'COMMIT_POSITION',
            'project': 'chromium',
            'repo': 'chromium/src',
        }
    )
    url = 'https://cr-rev.appspot.com/_ah/api/crrev/v1/get_numbering?' + query
    content = urllib.request.urlopen(url).read().decode('utf8')
    obj = json.loads(content)
    rev = obj['git_sha']
    if not git_util.is_git_rev(rev):
        raise ValueError(
            'The response of cr-rev.appspot.com, %s, is not a git commit hash'
            % rev
        )
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


def _simple_chrome_shell(
    chrome_src: str,
    board: str,
    command: list[str],
    is_public_build: bool = False,
    env: dict[str, str] | None = None,
    timeout: int | None = None,
    gn_extra_args: str | None = None,
) -> str:
    """Runs a command inside the chrome-sdk shell.

    This is a wrapper around `cros chrome-sdk`.

    Args:
      chrome_src: Git repo path of chrome/src.
      board: ChromeOS board name.
      command: Command to run inside the shell.
      is_public_build: Whether this is a public build.
      env: Environment variables for the command.
      timeout: Timeout in seconds for the command.
      gn_extra_args: Extra GN flags passed to the wrapper command.
    """
    prefix = [
        'cros',
        'chrome-sdk',
        '--board=%s' % board,
    ]
    if is_public_build:
        prefix.append('--use-external-config')
    else:
        prefix.append('--internal')

    if gn_extra_args:
        prefix.append('--gn-extra-args')
        prefix.append(gn_extra_args)

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

    env = (env if env is not None else os.environ).copy()

    # This work around http://crbug.com/658104, which depot_tool can't find GN
    # path correctly.
    env['CHROMIUM_BUILDTOOLS_PATH'] = os.path.abspath(
        os.path.join(chrome_src, 'buildtools')
    )

    # Add the environment variables for reproxy
    env.update(get_RBE_environment_variables())

    cmd = prefix + ['--'] + list(command)

    return chromite_util.check_output(
        *cmd, cwd=chrome_src, env=env, timeout=timeout
    )


def determine_targets_to_build(
    chrome_src: str,
    board: str,
    is_public_build: bool,
    with_tests: bool = True,
    gn_extra_args: str | None = None,
):
    logger.info('is_public_build %s', is_public_build)
    out_dir = os.path.join('out_%s' % board, 'Release')

    # Show the GN flags to the log.
    try:
        available_targets = _simple_chrome_shell(
            chrome_src,
            board,
            command=[
                'buildtools/linux64/gn',
                'ls',
                '--type=executable',
                '--as=output',
                out_dir,
            ],
            is_public_build=is_public_build,
            gn_extra_args=gn_extra_args,
        ).splitlines()
    except subprocess.CalledProcessError as e:
        raise errors.BuildError(
            'failed to determine chrome targets to build'
        ) from e

    # Show the GN flags to the log.
    try:
        test = chromite_util.check_output(
            'cat',
            os.path.join(out_dir, 'args.gn'),
            cwd=chrome_src,
        )
        logger.info("gn args: %s", test)
    except subprocess.CalledProcessError:
        logger.exception("Failed to show the GN flags.")

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


def build(
    chrome_src: str,
    board: str,
    targets: list[str],
    is_public_build: bool,
    gn_extra_args: str | None = None,
) -> str:
    """Build Chrome binaries.

    Args:
      chrome_src: git repo path of chrome/src.
      board: ChromeOS board name.
      targets: ninja targets. Must not be empty.
      is_public_build: Whether this is a public build.
      gn_extra_args: Extra GN flags used on build.

    Returns:
      The output directory of the build.
    """
    if not targets:
        raise errors.InternalError('Build target must not be empty')

    logger.info('build %s', targets)
    out_dir = os.path.join('out_%s' % board, 'Release')
    with locking.lock_file(locking.LOCK_FILE_FOR_BUILD):
        # Need to call .py directly, since it is calling the non-initialized
        # (non-bootstrapped) script.
        # See. https://chromium.googlesource.com/chromium/tools/depot_tools/+/HEAD/README.md#installing
        cmd = [
            './third_party/depot_tools/autoninja.py',
            '-C',
            out_dir,
        ] + targets

        env = os.environ.copy()
        # Recently clang started complaining with a "clang: warning: future
        # releases of the clang compiler will prefer GCC installations
        # containing libstdc++ include directories" on some systems (b/496032655).
        # But an old code uses the clang library and the build stops.
        # Disabling the warning by modifying CXXFLAGS.
        env['CXXFLAGS'] = (
            env.get('CXXFLAGS', '') + ' -Wno-gcc-install-dir-libstdcxx'
        )

        # Chrome build shouldn't take more than 3 hours. Set timeout to abort
        # the process in case it gets deadlocked. b/369498616
        timeout = 3 * 60 * 60
        try:
            _simple_chrome_shell(
                chrome_src,
                board,
                command=cmd,
                env=env,
                is_public_build=is_public_build,
                timeout=timeout,
                gn_extra_args=gn_extra_args,
            )
        except subprocess.CalledProcessError:
            logger.warning('build failed, delete out directory and retry again')
            # Some compiler processes are still terminating. Short delay is
            # necessary.
            time.sleep(10)
            shutil.rmtree(os.path.join(chrome_src, out_dir))
            try:
                _simple_chrome_shell(
                    chrome_src,
                    board,
                    command=cmd,
                    env=env,
                    is_public_build=is_public_build,
                    timeout=timeout,
                    gn_extra_args=gn_extra_args,
                )
            except subprocess.CalledProcessError as e:
                raise errors.BuildError('failed to build chrome') from e

    return os.path.join(chrome_src, out_dir)


def deploy_to_staging_dir(
    chrome_src: str,
    board: str,
    targets: list[str],
    out_dir: str,
    staging_dir: str,
) -> None:
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
            './third_party/chromite/bin/deploy_chrome',
            '--force',
            '--board',
            board,
            '--build-dir',
            out_dir,
            '--staging-dir',
            staging_dir,
            '--staging-only',
        ]
        chromite_util.check_output(*cmd, cwd=chrome_src)

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


def deploy(
    chrome_src: str | os.PathLike,
    board: str,
    dut: str,
    targets: list[str],
    with_tests: bool = True,
    out_dir: str | os.PathLike | None = None,
) -> None:
    """Deploy Chrome binaries.

    Args:
      chrome_src: git repo path of chrome/src.
      board: ChromeOS board name.
      dut: DUT address.
      targets: ninja targets. If empty, deploys any target found in out_dir.
      with_tests: Deploy test binaries as well.
      out_dir: Optional output dir from which to deploy binaries. A relative
         path would be interpreted as being based on `chrome_src`.
    """
    chrome_src = Path(chrome_src)

    if not out_dir:
        out_dir = chrome_src / ('out_%s' % board) / 'Release'
    else:
        out_dir = Path(out_dir)
        if not out_dir.is_absolute():
            out_dir = (chrome_src / out_dir).resolve()

    if not targets:
        targets = os.listdir(out_dir)
    logger.info('deploy %s', targets)

    # Deploy chrome if needed.
    if set(CHROME_BINARIES).intersection(targets):
        cmd = [
            './third_party/chromite/bin/deploy_chrome',
            '--force',
            '--board',
            board,
            '--build-dir',
            str(out_dir),
            '--device',
            dut,
        ]
        try:
            chromite_util.check_output(*cmd, cwd=chrome_src)
        except subprocess.CalledProcessError as e:
            raise errors.ExternalError('chrome deploy failed') from e

    # Deploy test binaries if needed.
    test_binaries = set(CHROME_TEST_BINARIES).intersection(targets)
    if with_tests and test_binaries:
        with tempfile.TemporaryDirectory() as staging_dir:
            # Deploy test binaries to the staging directory.
            deploy_to_staging_dir(
                str(chrome_src),
                board,
                list(test_binaries),
                out_dir=str(out_dir),
                staging_dir=staging_dir,
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
    env['RBE_credential_file'] = bisect_runner_json_path or ''
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
