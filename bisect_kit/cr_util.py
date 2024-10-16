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
import textwrap
import time
import urllib.request

from bisect_kit import codechange
from bisect_kit import errors
from bisect_kit import gclient_util
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
    'icudtl.dat',
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


def query_git_rev_by_commit_position(chrome_src, branch, commit_position):
    """Looks up git commit by chrome's commit position.

    This function cannot find early commits which is still using 'git-svn-id'.

    Args:
      chrome_src: git repo path of chrome/src
      branch: branch number or 'main'
      commit_position: chrome's commit position number

    Returns:
      git commit id

    Raises:
      ValueError: unable to find commits with given commit_position.
    """
    if branch == 'main':
        ref_path = 'refs/heads/main'
    else:
        ref_path = 'refs/branch-heads/%s' % branch

    rev = util.check_output(
        'git',
        'rev-list',
        '-1',
        '--all',
        '--grep',
        'Cr-Commit-Position: %s@{#%s}' % (ref_path, commit_position),
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
        return query_git_rev_by_commit_position(chrome_src, 'main', m.group(1))

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

    cmd = prefix + ['--'] + list(args)

    return util.check_output(*cmd, cwd=chrome_src, env=env)


def determine_targets_to_build(
    chrome_src: str,
    board: str,
    with_tests: bool = True,
    force_nacl: bool = False,
):
    out_dir = os.path.join('out_%s' % board, 'Release')
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
            and force_nacl
        ):
            result.append(binary)
            logger.warning(
                'nacl_helper not found in gn ls, force adding it in targets list'
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
            chrome_src, board, with_tests=with_tests, force_nacl=True
        )

    logger.info('build %s', targets)
    out_dir = os.path.join('out_%s' % board, 'Release')
    with locking.lock_file(locking.LOCK_FILE_FOR_BUILD):
        cmd = ['autoninja', '-C', out_dir] + targets
        try:
            simple_chrome_shell(chrome_src, board, *cmd, internal=True)
        except subprocess.CalledProcessError:
            logger.warning('build failed, delete out directory and retry again')
            # Some compiler processes are still terminating. Short delay is
            # necessary.
            time.sleep(10)
            shutil.rmtree(os.path.join(chrome_src, out_dir))
            simple_chrome_shell(chrome_src, board, *cmd, internal=True)

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
      targets: ninja targets. Default to build chrome.
      with_tests: build test binaries as well if targets is not specified
      out_dir: optional output dir from which to deploy binaries.
    """
    if not targets:
        targets = determine_targets_to_build(
            chrome_src,
            board,
            with_tests=with_tests,
            force_nacl=True,
        )
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
    if test_binaries:
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
    env[
        'RBE_credentials_helper_args'
    ] = '--auth_source=gcloud --gcert_refresh_timeout=20'
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


class ChromeSpecManager(codechange.SpecManager):
    """Gclient DEPS related operations.

    This class enumerates DEPS files of chrome, parses them, and sync to disk
    state according to them.
    """

    def __init__(self, config: dict):
        self.config = config
        self.spec_dir = os.path.join(config['chrome_root'], 'buildspec')
        self.chrome_src = os.path.join(config['chrome_root'], 'src')

    def lookup_build_timestamp(self, rev: str) -> int:
        assert is_chrome_version(rev)

        return git_util.get_commit_time(self.chrome_src, rev, 'DEPS')

    def generate_meta_deps(self, rev: str) -> str:
        """Generates a root DEPS file.

        This is kind of workaround for the limitation of our DEPS parser. When
        parsing src/, it returns content of src/DEPS but the revision of src/
        itself is lost. It is non-trivial to revise the recursive parser, so I
        created a root DEPS file to include src/ instead.

        Args:
          rev: chrome src/ branch or tag

        Returns:
          path to the synthesized repo
        """
        meta_dir = os.path.join(self.config.get('chrome_root'), 'meta-deps')
        if os.path.exists(meta_dir):
            shutil.rmtree(meta_dir)
        git_util.init(meta_dir, initial_branch=gclient_util.DEFAULT_BRANCH_NAME)

        # 1. Create initial commit with DEPS file referring to main.
        init_timestamp = '2018-01-01T00:00:00'
        meta_deps = textwrap.dedent(
            """
    gclient_gn_args_from = "src"

    vars = {
      "checkout_src_internal": True,
    }

    deps = {
      "src": "https://chromium.googlesource.com/chromium/src.git@%s",
    }

    recursedeps = [
      "src",
    ]
    """
            % rev
        )
        git_util.commit_file(
            meta_dir,
            'DEPS',
            'initial DEPS',
            meta_deps,
            commit_time=init_timestamp,
        )

        return meta_dir

    def collect_float_spec(
        self,
        old: str,
        new: str,
        fixed_specs: list[codechange.Spec] | None = None,
    ) -> list[codechange.Spec]:
        del fixed_specs  # unused
        logger.info('collect_float_spec')
        old_timestamp = self.lookup_build_timestamp(old)
        new_timestamp = self.lookup_build_timestamp(new)

        # Workaround for racing between DEPS file and other commits.
        # The steps for branching and releasing on a buildbot:
        #  1. sync code
        #  2. commit branch DEPS file
        #  3. flatten the dependency and commit the generated release DEPS file
        # What lookup_build_timestamp() returns is the commit time of step 3. The
        # content of the DEPS file recorded the tree snapshot of step 1. However,
        # there might be some commits arriving between step 1 and step 3. In order
        # to reconstruct complete commit history, we actually need to figure out
        # timestamp of step 1, which is non-trivial.
        # Here, we just assume buildbot can finish these steps in one day. It's
        # harmless to have old_timestamp slightly earlier than the actual time.
        # See crbug.com/900514 for a real case. 16 hours threshold is not enough.
        old_timestamp -= 86400  # 1 day

        branch = 'branch-heads/' + extract_branch_from_version(new)
        meta_dir = self.generate_meta_deps(branch)

        result = []
        code_storage = gclient_util.GclientCache(
            self.config.get('chrome_mirror')
        )
        parser = gclient_util.DepsParser(
            self.config.get('chrome_root'), code_storage
        )
        path = os.path.join(meta_dir, 'DEPS')

        logger.info('start enumerate_path_specs')
        for timestamp, path_specs in parser.enumerate_path_specs(
            old_timestamp, new_timestamp, meta_dir
        ):
            spec = codechange.Spec.new_float('(n/a)', timestamp, path)
            spec.entries = path_specs
            result.append(spec)
        return result

    def enumerate_ancestor(self, new: str) -> list[str]:
        result = []
        for rev in git_util.get_tags(self.chrome_src):
            if not is_chrome_version(rev):
                continue
            if is_version_lesseq(rev, new) and is_direct_relative_version(
                rev, new
            ):
                result.append(rev)
        return sorted(result, key=util.version_key_func)

    def get_lca_chrome_localbuild(
        self, old_version: str, new_version: str
    ) -> str:
        """Get the lowest common ancestor of 2 chrome localbuild version

        Args:
          old_version: Old chrome version
          new_version: New chrome version

        Returns:
          The lowest common ancestor of 2 chrome localbuild version
        """
        version_list_chrome: list[str] = []
        for rev in git_util.get_tags(self.chrome_src):
            version_list_chrome.append(rev)

        return get_lca(old_version, new_version, version_list_chrome)

    def collect_fixed_spec(self, old: str, new: str) -> list[codechange.Spec]:
        logger.info('collect_fixed_spec')
        assert is_direct_relative_version(old, new)
        result = []
        code_storage = gclient_util.GclientCache(
            self.config.get('chrome_mirror')
        )
        parser = gclient_util.DepsParser(
            self.config.get('chrome_root'), code_storage
        )
        for tag in self.enumerate_ancestor(new):
            if not is_version_lesseq(old, tag):
                continue
            tag_rev = git_util.get_commit_hash(self.chrome_src, tag)
            meta_dir = self.generate_meta_deps(tag_rev)
            meta_head_rev = git_util.get_commit_hash(meta_dir, 'HEAD')

            timestamp = git_util.get_commit_time(self.chrome_src, tag, 'DEPS')
            spec = codechange.Spec.new_fixed(tag, timestamp, 'DEPS')
            path_specs_result = list(
                parser.enumerate_path_specs(
                    timestamp, timestamp, meta_dir, meta_head_rev
                )
            )
            assert len(path_specs_result) == 1
            spec.entries = path_specs_result[0][1]
            result.append(spec)

        assert result[0].name == old, '%s != %s' % (result[0].name, old)
        assert result[-1].name == new, '%s != %s' % (result[-1].name, new)
        return result

    def parse_spec(self, spec: codechange.Spec) -> None:
        logger.debug('parse_spec %s', spec.name)
        code_storage = gclient_util.GclientCache(
            self.config.get('chrome_mirror')
        )
        assert spec.entries, 'it should already be parsed earlier'

        # Mirror git repos if they do not exist.
        with locking.lock_file(
            os.path.join(
                self.config.get('chrome_mirror'),
                locking.LOCK_FILE_FOR_MIRROR_SYNC,
            )
        ):
            for path_spec in spec.entries.values():
                git_repo = code_storage.cached_git_root(path_spec.repo_url)
                if os.path.exists(git_repo):
                    continue
                gclient_util.mirror(code_storage, path_spec.repo_url)

        # Additional dependency fix.
        # DEPS files are maintained in the same tree as code, so releasing DEPS
        # will add one more commit. Because we want to match release DEPS against
        # branch commit history, we need to recover the code state before DEPS
        # commit.
        # To be more specific, chrome-release-bot@ creates and updates new DEPS
        # file to src/ and some internal repos every releases. Here we check the
        # dependency state of all subprojects and use their parent commit if it is
        # committed by release bot.
        for path_spec in spec.entries.values():
            git_repo = code_storage.cached_git_root(path_spec.repo_url)
            if not path_spec.is_static():  # only surgery for fixed spec
                continue
            while True:
                metadata = git_util.get_commit_metadata(git_repo, path_spec.rev)
                if metadata.parent is None:
                    break
                bot_email = 'chrome-release-bot@chromium.org'
                if (
                    bot_email not in metadata.committer
                    and bot_email not in metadata.author
                ):
                    break

                path_spec.rev = metadata.parent[0]

    def sync_to_release(self, rev: str) -> None:
        """Switch source tree to given release version.

        Args:
          rev: chrome release version
        """
        assert is_chrome_version(rev)
        with locking.lock_file(
            os.path.join(
                self.config.get('chrome_mirror'),
                locking.LOCK_FILE_FOR_MIRROR_SYNC,
            )
        ):
            git_util.checkout_version(self.chrome_src, rev)
            custom_var_list = [
                'checkout_src_internal=True',
                'download_remoteexec_cfg=True',
            ]
            gclient_util.config(
                self.config.get('chrome_root'),
                url='https://chromium.googlesource.com/chromium/src.git',
                custom_var_list=custom_var_list,
                cache_dir=self.config.get('chrome_mirror'),
                managed=False,
                target_os='chromeos',
            )

            gclient_util.sync(
                self.config.get('chrome_root'),
                with_branch_heads=True,
                with_tags=True,
            )

    def sync_disk_state(self, rev: str) -> None:
        self.sync_to_release(rev)

    def get_release_deps(self, rev: str) -> str:
        """Get DEPS content of given release.

        Args:
          rev: chrome release version number

        Returns:
          DEPS string
        """
        # TODO(kcwu): flatten recursive DEPS content
        return git_util.get_file_from_revision(self.chrome_src, rev, 'DEPS')
