#!/usr/bin/env python3
# Copyright 2019 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Evaluate ChromeOS tast tests."""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import sys
from typing import Optional

from bisect_kit import bisector_cli
from bisect_kit import catapult_util
from bisect_kit import cli
from bisect_kit import common
from bisect_kit import core
from bisect_kit import cros_lab_util
from bisect_kit import cros_util
from bisect_kit import errors
from bisect_kit import git_util
from bisect_kit import repo_util
from bisect_kit import util


logger = logging.getLogger(__name__)

OLD = 'old'
NEW = 'new'
SKIP = 'skip'
FATAL = 'fatal'

EXIT_CODE_MAP = {
    OLD: cli.EXIT_CODE_OLD,
    NEW: cli.EXIT_CODE_NEW,
    SKIP: cli.EXIT_CODE_SKIP,
    FATAL: cli.EXIT_CODE_FATAL,
}


def create_argument_parser():
    parents = [cli.create_session_optional_parser()]
    parser = cli.ArgumentParser(description=__doc__, parents=parents)
    parser.add_argument(
        '--rich-result',
        action='store_true',
        help='Instead of mere exit code, output detailed information in json',
    )
    parser.add_argument(
        'dut',
        nargs='?',
        type=cli.argtype_notempty,
        metavar='DUT',
        default=os.environ.get('DUT', ''),
    )
    parser.add_argument(
        '--chromeos-root',
        type=cli.argtype_dir_path,
        metavar='CHROMEOS_ROOT',
        default=os.environ.get('CHROMEOS_ROOT', ''),
        help='ChromeOS tree root',
    )
    parser.add_argument(
        '--prebuilt',
        action='store_true',
        help='Run tast using existing server prebuilt package if specified; '
        'otherwise use the default one',
    )
    parser.add_argument(
        '--tast-build',
        action='store_true',
        help='Build tast test bundle (-build=true) if specified; '
        'default is using prebuilt bundle on the DUT',
    )
    parser.add_argument(
        '--tast-patch-cl',
        action='append',
        help='A gerrit CL to patch the chromiumos/platform/tast-tests repository; ',
    )
    parser.add_argument(
        '--tast-revision',
        help='A git revision of chromiumos/platform/tast-tests repository '
        'to be specified',
    )
    parser.add_argument(
        '--tast-private-revision',
        help='A git revision of chromeos/platform/tast-tests-private '
        'repository to be specified',
    )
    parser.add_argument(
        '--tast-runtime-revision',
        help='A git revision of chromiumos/platform/tast repository to be '
        'specified',
    )
    parser.add_argument(
        '--with-private-bundles',
        action='store_true',
        help='Whether search tests in private bundles or not',
    )
    parser.add_argument(
        '--reboot-before-test',
        action='store_true',
        help='Reboot before test run',
    )
    parser.add_argument(
        '--args',
        help='Extra variables passed to `tast -var`, as "key=value"',
        type=cli.argtype_key_value,
        action='append',
        default=[],
    )

    group = parser.add_argument_group(title='Options for normal autotest tests')
    group.add_argument(
        '--test-name',
        help='Test name, like "video_VideoDecodeAccelerator.h264"',
        default='bisector.Bisect',
    )
    group.add_argument(
        '--fail-to-pass',
        action='store_true',
        help='For functional tests: old behavior is FAIL and new behavior is '
        'PASS; If not specified, default = old behavior is PASS and new '
        'behavior is FAIL',
    )
    group.add_argument(
        '--consider-test-crash-as-failure',
        action='store_true',
        help='Consider test crashes as failure instead of skip',
    )
    group.add_argument(
        '--metric',
        help='Metric name of performance test; example: '
        '"cheets_SystemRawImageSize"',
    )

    return parser


def add_tast_patch_cl(
    chromeos_root: str,
    tast_patch_cls: list[str],
    tast_revision: Optional[str],
    tast_private_revision: Optional[str],
    tast_runtime_revision: Optional[str],
):
    # Use 'main' branch code as the base if the patch CL is specified.
    # It's compatible as the previous behavior.
    if tast_patch_cls:
        if not tast_runtime_revision:
            tast_runtime_revision = 'main'
        if not tast_private_revision:
            tast_private_revision = 'main'
        if not tast_revision:
            tast_revision = 'main'

    # checkout tast runtime repo to main or the specified revision.
    if tast_runtime_revision:
        tast_runtime_repo = os.path.join(
            chromeos_root, 'src', 'platform', 'tast'
        )
        if tast_runtime_revision == 'main':
            git_util.checkout_branch(tast_runtime_repo, 'main')
            git_util.reset_hard_to_remote_branch(tast_runtime_repo, 'cros/main')
        else:
            git_util.checkout_version(tast_runtime_repo, tast_runtime_revision)
            git_util.reset_hard(tast_runtime_repo)

    # checkout tast tests private repo to main or the specified revision.
    if tast_private_revision:
        tast_tests_private_repo = os.path.join(
            chromeos_root, 'src', 'platform', 'tast-tests-private'
        )
        if tast_private_revision == 'main':
            git_util.checkout_branch(tast_tests_private_repo, 'main')
            git_util.reset_hard_to_remote_branch(
                tast_tests_private_repo, 'cros-internal/main'
            )
        else:
            git_util.checkout_version(
                tast_tests_private_repo, tast_private_revision
            )
            git_util.reset_hard(tast_tests_private_repo)

    # checkout tast tests repo to main or the specified revision.
    if tast_revision:
        tast_tests_repo = os.path.join(
            chromeos_root, 'src', 'platform', 'tast-tests'
        )
        if tast_revision == 'main':
            git_util.checkout_branch(tast_tests_repo, 'main')
            git_util.reset_hard_to_remote_branch(tast_tests_repo, 'cros/main')
        else:
            git_util.checkout_version(tast_tests_repo, tast_revision)
            git_util.reset_hard(tast_tests_repo)

    # Patch the CL to the tast tests repo.
    for patch_cl in tast_patch_cls:
        repo_util.cherry_pick(tast_tests_repo, patch_cl)


def prepare_to_run_test(opts):
    if opts.reboot_before_test:
        cros_util.reboot(
            opts.dut, force_reboot_callback=cros_lab_util.reboot_via_servo
        )

    # opts.tast_patch_cl can be None.
    tast_patch_cls = opts.tast_patch_cl or []

    if (
        tast_patch_cls
        or opts.tast_revision
        or opts.tast_private_revision
        or opts.tast_runtime_revision
    ):
        add_tast_patch_cl(
            opts.chromeos_root,
            tast_patch_cls,
            opts.tast_revision,
            opts.tast_private_revision,
            opts.tast_runtime_revision,
        )


def tast_cmd(opts, cmd, *args):
    flags = []
    use_custom_tast_test = (
        opts.tast_patch_cl
        or opts.tast_revision
        or opts.tast_private_revision
        or opts.tast_runtime_revision
    )
    if opts.prebuilt and not use_custom_tast_test:
        tast_dir = os.path.join(
            cros_util.chromeos_root_inside_chroot, cros_util.prebuilt_tast_dir
        )
        tast_bin = os.path.join(tast_dir, 'tast')
        flags += [
            '-remotebundledir',
            os.path.join(tast_dir, 'bundles', 'remote'),
        ]
        flags += ['-remotedatadir', os.path.join(tast_dir, 'data')]
        flags += ['-remoterunner', os.path.join(tast_dir, 'remote_test_runner')]
        flags += [
            '-defaultvarsdir',
            os.path.join(tast_dir, 'vars', 'private'),
            '-defaultvarsdir',
            os.path.join(tast_dir, 'vars', 'public'),
        ]
    else:
        tast_bin = 'tast'

    if cmd == 'run':
        flags += ['-var=%s=%s' % x for x in opts.args]

    # TODO(zjchang): ensure the tast version for buildbucket builds is correct
    if not opts.tast_build and not use_custom_tast_test:
        flags.append('-build=false')
        if opts.with_private_bundles:
            flags.append('-downloadprivatebundles=true')

    return [tast_bin, '-verbose', cmd] + flags + list(args)


def get_tast_bundle_info(opts, pattern: str | None = None) -> dict[str, str]:
    bundles = ['cros']
    if opts.with_private_bundles:
        bundles.append('crosint')

    args = [opts.dut]
    if pattern:
        args.append(pattern)

    result = {}
    for bundle in bundles:
        cmd = tast_cmd(opts, 'list', '-json', '-buildbundle=' + bundle, *args)
        try:
            json_text = cros_util.cros_sdk(
                opts.chromeos_root,
                *cmd,
                log_stdout=False,
            )
        except subprocess.CalledProcessError as e:
            raise errors.ExternalError(
                'failed to get tast bundle info, assume it is temporary: %s' % e
            )
        for entry in json.loads(json_text):
            result[entry['name']] = bundle

    return result


def run_test(opts: argparse.Namespace, test_name: str, bundle: str) -> str:
    """Runs an autotest test.

    Args:
      opts: An argparse.Namespace to hold command line arguments.
      test_name: Name of the test
      bundle: tast's test bundle

    Returns:
      path of test result (outside chroot)
    """
    # Set results dir inside source tree, so it's easier to access them outside
    # chroot.
    results_dir = os.path.join(
        cros_util.chromeos_root_inside_chroot, 'tmp/tast_results_tmp'
    )
    results_dir_output_chroot = results_dir.replace(
        cros_util.chromeos_root_inside_chroot, opts.chromeos_root
    )
    # Don't reuse existing results dir, otherwise tast may rename output files.
    if os.path.exists(results_dir_output_chroot):
        shutil.rmtree(results_dir_output_chroot)

    # TODO(kcwu): add -timeout
    flags = ['-resultsdir', results_dir]
    if opts.tast_build:
        flags.append('-buildbundle=' + bundle)

    cmd = tast_cmd(opts, 'run', *flags, opts.dut, test_name)
    cros_util.cros_sdk(opts.chromeos_root, *cmd)

    return results_dir_output_chroot


# Be careful that both tauto and tast generate results.json, but their format
# is totally different.
def parse_results_json(result_dir, test_name) -> tuple[bool, str | None]:
    passed = None
    reason = None
    results_path = os.path.join(result_dir, 'results.json')
    util.copy_file_to_log_folder(results_path)
    with open(results_path) as f:
        for result in json.load(f):
            if result['name'] != test_name:
                logger.warning('unexpected test ran: %s', result['name'])
                continue
            if not result['errors']:
                passed = True
            else:
                passed = False
                # We capture the reason of the last error because usually the last one
                # is fatal.
                for error in reversed(result['errors']):
                    reason = error.get('reason')
                    if reason:
                        break
                # In case of multiple test runs, return passed=False if any of the
                # test failed.
                break
    if passed is None:
        raise errors.ExternalError('no test result for "%s"?' % test_name)
    return passed, reason


def gather_test_result(
    opts: argparse.Namespace, test_name: str, result_dir: str
) -> core.StepResult:
    error_path = os.path.join(result_dir, 'run_error.txt')
    if os.path.exists(error_path):
        with open(error_path) as f:
            message = f.read()
        raise errors.ExternalError('tast global error: %s' % message)

    passed, reason = parse_results_json(result_dir, test_name)
    if opts.metric:
        chart_path = os.path.join(
            result_dir, 'tests', test_name, 'results-chart.json'
        )
        util.copy_file_to_log_folder(chart_path)
        try:
            values = catapult_util.get_benchmark_values(chart_path, opts.metric)
        except Exception as e:
            if not passed:
                raise errors.BisectionTemporaryError(
                    'test failed to generate metric values, reason: %s' % reason
                ) from e
            raise
        return core.StepResult('value', values=values)

    if opts.fail_to_pass:
        if passed:
            logger.info('passed')
            return core.StepResult('new', reason)
        logger.info('failed')
        return core.StepResult('old', reason)
    if passed:
        logger.info('passed')
        return core.StepResult('old', reason)
    logger.info('failed')
    return core.StepResult('new', reason)


def step_main(args: tuple[str] | None) -> core.StepResult:
    parser = create_argument_parser()
    opts = parser.parse_args(args)
    common.config_logging(opts)

    if cros_lab_util.is_satlab_dut(opts.dut):
        cros_lab_util.write_satlab_ssh_config(opts.dut)

    if not cros_util.is_dut(opts.dut):
        raise errors.BrokenDutException(
            '%r is not a valid DUT address' % opts.dut
        )

    if not cros_util.is_good_dut(opts.dut):
        logger.fatal('%r is not a good DUT', opts.dut)
        if not cros_lab_util.repair(opts.dut, opts.chromeos_root):
            raise errors.BrokenDutException('%r is not a good DUT' % opts.dut)

    tests: list[str] = [
        value.strip() for value in opts.test_name.split(',') if value.strip()
    ]
    if len(tests) == 0:
        raise errors.ArgumentError('--test-name', 'no valid test name found.')
    if len(tests) > 1 and opts.metric:
        raise errors.ArgumentError(
            '--test-name', 'multiple performance tests are not supported.'
        )

    # Private bundles are not included in the OS image, make sure they are
    # available.
    if opts.with_private_bundles and not opts.tast_build:
        if not cros_util.query_dut_is_by_official_builder(opts.dut):
            # Official builders uploaded private tast bundles and tast can download
            # them with -downloadprivatebundles=true, so --tast-build is not
            # required. Otherwise, we must build the bundles by ourselves.
            raise errors.ArgumentError(
                '--tast-build',
                'for non-official chromeos image, --tast-build must be specified',
            )

    # Verify command line options.
    if opts.metric:
        if opts.fail_to_pass:
            raise errors.ArgumentError(
                '--fail-to-pass',
                '--fail-to-pass is not for benchmark test (--metric)',
            )

    cros_util.prepare_chroot(opts.chromeos_root)
    try:
        prepare_to_run_test(opts)
    except Exception as e:
        raise errors.BisectionTemporaryError(
            'failed when prepare, assume it is temporary: %s' % e
        )

    last_test_result: core.StepResult | None = None
    for test in tests:
        # Remove the "tast." prefix prepended by autotest.
        test = re.sub(r'^tast\.', '', test)
        tast_bundle_info = get_tast_bundle_info(opts, test)
        if not tast_bundle_info:
            tast_bundle_info = get_tast_bundle_info(opts)
            util.report_similar_candidates(
                'test name', test, list(tast_bundle_info)
            )
            assert 0  # unreachable

        if len(tast_bundle_info) != 1 or test not in tast_bundle_info:
            # For example, tast in chroot after 12205.0.0 is IPC incompatible with
            # tast on DUT earlier than 12028.0.0 (crbug/932307)
            raise errors.ExecutionFatalError(
                '"tast list" returns unexpected tests; '
                'incompatible tast on DUT and in chroot?'
            )

        bundle = tast_bundle_info[test]
        try:
            result_dir = run_test(opts, test, bundle)
        except subprocess.CalledProcessError as e:
            reason = (
                f'failed to run {test}; maybe build, ssh, or setup failures'
            )
            # TODO(njrafi): Differentiate between ssh failure and test crash
            if opts.consider_test_crash_as_failure:
                return util.get_test_crash_step_result(
                    reason, opts.fail_to_pass
                )
            raise errors.BisectionTemporaryError(reason) from e
        last_test_result = gather_test_result(opts, test, result_dir)
        if last_test_result.data['status'] in ['fatal', 'skip']:
            break

    return last_test_result


def action() -> bisector_cli.EvalAction:
    return bisector_cli.EvalAction.WITH_DUT


def main(args: tuple[str] | None = None) -> int:
    return bisector_cli.step_main_wrapper(step_main, args)


if __name__ == '__main__':
    sys.exit(main())
