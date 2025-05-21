#!/usr/bin/env python3
# Copyright 2018 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Evaluate ChromeOS autotest.

Note that by default 'test_that' will install dependency packages of autotest
if the package checksum mismatch. If you want to override content of autotest
package, e.g. chrome's test binary, please make sure the autotest version
matches. Otherwise your test binary will be overwritten.
"""

from __future__ import annotations

import glob
import logging
import os
import re
import subprocess
import sys
import xml.etree.ElementTree

from bisect_kit import bisector_cli
from bisect_kit import catapult_util
from bisect_kit import cli
from bisect_kit import common
from bisect_kit import core
from bisect_kit import cros_lab_util
from bisect_kit import cros_util
from bisect_kit import errors
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
    # exit_on_error=False: to capture invalid argument values
    parser = cli.ArgumentParser(
        description=__doc__, parents=parents, exit_on_error=False
    )
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
        '--chrome-root',
        metavar='CHROME_ROOT',
        type=cli.argtype_dir_path,
        default=os.environ.get('CHROME_ROOT'),
        help='Chrome tree root; necessary for telemetry tests',
    )
    parser.add_argument(
        '--prebuilt',
        action='store_true',
        help='Run autotest using existing prebuilt package if specified; '
        'otherwise use the default one',
    )
    parser.add_argument(
        '--reinstall',
        action='store_true',
        help='Remove existing autotest folder on the DUT first',
    )
    parser.add_argument(
        '--reboot-before-test',
        action='store_true',
        help='Reboot before test run',
    )

    group = parser.add_argument_group(title='Options for normal autotest tests')
    group.add_argument(
        '--test-name',
        help='Test name, like "video_VideoDecodeAccelerator.h264"',
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
    group.add_argument(
        '--args',
        help='Extra args passed to "test_that --args"; Overrides the default',
        action='append',
        default=[],
    )

    group = parser.add_argument_group(title='Options for CTS/GTS tests')
    group.add_argument('--cts-revision', help='CTS revision, like "9.0_r3"')
    group.add_argument('--cts-abi', choices=['arm', 'x86'])
    group.add_argument(
        '--cts-prefix',
        help='Prefix of autotest test name, '
        'like cheets_CTS_N, cheets_CTS_P, cheets_GTS',
    )
    group.add_argument(
        '--cts-module', help='CTS/GTS module name, like "CtsCameraTestCases"'
    )
    group.add_argument(
        '--cts-test',
        help='CTS/GTS test name, like '
        '"android.hardware.cts.CameraTest#testDisplayOrientation"',
    )
    group.add_argument('--cts-timeout', type=float, help='timeout, in seconds')

    return parser


def get_additional_test_args(test_name):
    """Gets extra arguments to specific test.

    Some tests may require special arguments to run.

    Args:
      test_name: test name

    Returns:
      arguments (list[str])
    """
    if test_name.startswith('telemetry_'):
        return ['local=True']
    return []


def prepare_to_run_test(opts):
    # Some versions of ChromeOS SDK is broken and ship bad 'ssh' executable. This
    # works around the issue. See crbug/906289 for detail.
    # TODO(kcwu): remove this workaround once we no longer support bisecting
    # versions earlier than R73-11445.0.0.
    ssh_path = os.path.join(opts.chromeos_root, 'chroot/usr/bin/ssh')
    if os.path.exists(ssh_path):
        with open(ssh_path, 'rb') as f:
            if b'file descriptor passing not supported' in f.read():
                cros_util.cros_sdk(
                    opts.chromeos_root, 'sudo', 'emerge', 'net-misc/openssh'
                )

    # Special handling for audio tests (b/136136270).
    if opts.prebuilt:
        autotest_dir = os.path.join(
            opts.chromeos_root, cros_util.prebuilt_autotest_dir
        )
    else:
        autotest_dir = os.path.join(
            opts.chromeos_root, cros_util.in_tree_autotest_dir
        )
    cros_util.prepare_chroot(opts.chromeos_root)
    cros_util.override_autotest_config(autotest_dir)
    sox_path = os.path.join(opts.chromeos_root, 'chroot/usr/bin/sox')
    if not os.path.exists(sox_path):
        try:
            cros_util.cros_sdk(opts.chromeos_root, 'sudo', 'emerge', 'sox')
        except subprocess.CalledProcessError:
            # It's known that installing sox would fail for earlier version of
            # chromeos (b/136136270), so ignore the failure.
            logger.debug(
                'Sox is only required by some audio tests. '
                'Assume the failure of installing sox is harmless'
            )

    if opts.reinstall:
        util.ssh_cmd(opts.dut, 'rm', '-rf', '/usr/local/autotest')

    if opts.reboot_before_test:
        cros_util.reboot(
            opts.dut, force_reboot_callback=cros_lab_util.reboot_via_servo
        )


def run_test(opts) -> str:
    """Runs an autotest test.

    Args:
      opts: An argparse.Namespace to hold command line arguments.

    Returns:
      path of test result (outside chroot)
    """
    # Enable port forwarding for CTS tests because ADB doesn't support SSH's
    # ProxyJump. Sometimes users want to run a group of CTS tests by specifying
    # opts.test_name only, instead of running a single test case by specifying
    # opts.cts_test. b/417324492
    if (
        is_cts(opts) or re.match(r'cheets_\wTS_\w+\.', opts.test_name)
    ) and cros_lab_util.is_dut_needs_forwarded(opts.dut):
        dut, forward_host = (
            '%s:%d'
            % (cros_util.FORWARDED_DUT_HOST, cros_util.FORWARDED_DUT_PORT),
            opts.dut,
        )
    else:
        dut, forward_host = opts.dut, ''
    prebuilt_autotest_dir = os.path.join(
        cros_util.chromeos_root_inside_chroot, cros_util.prebuilt_autotest_dir
    )
    # Set results dir inside source tree, so it's easier to access them outside
    # chroot.
    results_dir = os.path.join(
        cros_util.chromeos_root_inside_chroot, 'tmp/autotest_results_tmp'
    )
    if opts.prebuilt:
        test_that_bin = os.path.join(
            prebuilt_autotest_dir, 'site_utils/test_that.py'
        )
    else:
        test_that_bin = '/usr/bin/test_that'
    cmd = [
        test_that_bin,
        dut,
        opts.test_name,
        '--debug',
        '--results_dir',
        results_dir,
    ]
    if opts.prebuilt:
        cmd += ['--autotest_dir', prebuilt_autotest_dir]

    args = get_additional_test_args(opts.test_name)
    if opts.args:
        if args:
            logger.info(
                'default test_that args `%s` is overridden by '
                'command line option `%s`',
                args,
                opts.args,
            )
        cmd += ['--args', ' '.join(opts.args)]
    elif args:
        cmd += ['--args', ' '.join(args)]

    try:
        output = cros_util.cros_sdk(
            opts.chromeos_root,
            *cmd,
            chrome_root=opts.chrome_root,
            forward_host=forward_host,
        )
    except subprocess.CalledProcessError as e:
        if e.output is None:
            raise errors.ExternalError('cros_sdk failed before test started')
        output = e.output

    m = re.search(
        r'Finished running tests. Results can be found in (\S+)', output
    )
    if not m:
        raise errors.ExternalError('result dir is unknown')
    assert m.group(1) == results_dir
    return results_dir.replace(
        cros_util.chromeos_root_inside_chroot, opts.chromeos_root
    )


# Although tauto will generate results.json, its content may be empty and
# useless (see testdata/tast_test_result/fail.2 for example). So we have to
# parse the machine-unfriendly test_report.log instead.
def parse_tauto_results(
    result_dir: str, test_name: str | None
) -> tuple[bool, str | None]:
    result_file = os.path.join(result_dir, 'test_report.log')
    if not os.path.exists(result_file):
        raise errors.ExternalError('test_report.log not found')
    util.copy_file_to_log_folder(result_file)

    failed = None
    reason = None
    for line in open(result_file):
        line = line.strip()
        # Assume no space characters in the result path.
        m = re.match(r'^(\S+)\s+(.+)', line)
        if not m:
            continue

        # Match test name.
        path, content = m.groups()
        m = re.search(r'/(?:results-\d+-)?([^/]+)$', path)
        if not m:
            continue
        logger.debug('found test name: %s', m.group(1))
        if m.group(1) != test_name:
            continue

        if re.match(r'\[  (FAILED|PASSED)  \]', content):
            if 'PASSED' in content:
                return True, None
            failed = True
        if re.match(r'ABORT|ERROR|FAIL|WARN|TEST_NA', content):
            reason = content

    if failed is None:
        raise errors.ExternalError('test name not found in test_report.log')
    return False, reason


def parse_cts_results(result_dir, test_name) -> tuple[bool, str | None]:
    # The result path looks like
    #   results-1-cheets_CTS_P.tradefed-run-test/
    #   cheets_CTS_P.tradefed-run-test.CtsCameraTestCases.testFocalLengths/
    #   results/android-cts/2022.03.02_00.27.09/test_result.xml
    result_files = glob.glob(
        os.path.join(result_dir, 'results*/*/results/*/*/test_result.xml')
    )
    if not result_files:
        logger.error('failed to run cts test, fallback to tauto result')
        passed, reason = parse_tauto_results(result_dir, test_name)
        assert not passed, 'test_result.xml not found, but test passed?'
        raise errors.BisectionTemporaryError('failed to run test: %s' % reason)

    result_files.sort(key=os.path.getmtime)
    logger.info('found result file: %s', result_files)
    # Use the latest result file because it is the final result of retries.
    result_file = result_files[-1]
    util.copy_file_to_log_folder(result_file)

    tree = xml.etree.ElementTree.parse(result_file)
    root = tree.getroot()

    summary = root.find('Summary')
    assert summary is not None
    logger.info('summary: %s', summary.attrib)
    if summary.get('pass') == summary.get('failed') == '0':
        return False, 'No result found in the summary'

    # Returns the reason of the first failure.
    for test in root.iter('Test'):
        test_result = test.get('result')
        # See https://source.android.com/docs/compatibility/cts/interpret#test_summary
        if test_result in ('pass', 'ASSUMPTION_FAILURE', 'IGNORED'):
            continue
        reason = None
        failure = test.find('Failure')
        if failure:
            reason = failure.get('message')
        return False, reason

    assert root.findall(
        './/Test[@result="pass"]'
    ), 'there should be some tests passed'
    return True, None


def gather_test_result(opts, result_dir) -> core.StepResult:
    if opts.cts_test:
        passed, reason = parse_cts_results(result_dir, opts.test_name)
    else:
        passed, reason = parse_tauto_results(result_dir, opts.test_name)

    if opts.metric:
        for root, _, files in os.walk(result_dir):
            for filename in files:
                if filename != 'results-chart.json':
                    continue
                full_path = os.path.join(root, filename)
                try:
                    values = catapult_util.get_benchmark_values(
                        full_path, opts.metric
                    )
                except Exception as e:
                    if not passed:
                        raise errors.BisectionTemporaryError(
                            'test failed to generate metric values, reason: %s'
                            % reason
                        ) from e
                    raise
                return core.StepResult('value', values=values)
        if not passed:
            raise errors.BisectionTemporaryError(
                'test failed to generate metric values, reason: %s' % reason
            )
        raise errors.ExecutionFatalError(
            'test passed, but no metric values generated'
        )

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


def is_cts(opts) -> bool:
    return (
        opts.cts_revision
        or opts.cts_abi
        or opts.cts_prefix
        or opts.cts_module
        or opts.cts_test
        or opts.cts_timeout
    )


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

    dut_os_version = cros_util.query_dut_short_version(opts.dut)
    if opts.test_name:
        opts.test_name = cros_util.normalize_test_name(opts.test_name)

    if is_cts(opts):
        if opts.test_name or opts.metric or opts.args:
            raise errors.ArgumentError(
                None,
                'do not specify --test-name, --metric, --args for CTS/GTS tests',
            )
        if not opts.cts_prefix:
            raise errors.ArgumentError(
                None, '--cts-prefix should be specified for CTS/GTS tests'
            )
        if not opts.cts_module:
            raise errors.ArgumentError(
                None, '--cts-module should be specified for CTS/GTS tests'
            )
        opts.test_name = '%s.tradefed-run-test' % opts.cts_prefix
        opts.args = [
            'module=%s' % opts.cts_module,
            'test=%s' % opts.cts_test,
            'max_retry=0',
        ]
        if opts.cts_revision:
            opts.args.append('revision=%s' % opts.cts_revision)
        if opts.cts_abi:
            opts.args.append('abi=%s' % opts.cts_abi)
        if opts.cts_timeout:
            opts.args.append('timeout=%s' % opts.cts_timeout)
    else:
        if not opts.test_name:
            raise errors.ArgumentError(None, 'argument --test-name is required')

    # Verify command line options.
    if opts.metric:
        if opts.fail_to_pass:
            raise errors.ArgumentError(
                '--fail-to-pass',
                '--fail-to-pass is not for benchmark test (--metric)',
            )
    if opts.test_name.startswith('telemetry_'):
        if not opts.chrome_root:
            raise errors.ArgumentError(
                '--chrome-root',
                '--chrome-root is mandatory for telemetry tests',
            )
    if opts.prebuilt:
        autotest_dir = os.path.join(
            opts.chromeos_root, cros_util.prebuilt_autotest_dir
        )
        if not os.path.exists(autotest_dir):
            raise errors.ArgumentError(
                '--prebuilt',
                'no autotest prebuilt installed (%s); '
                'please run switch_autotest_prebuilt.py first' % autotest_dir,
            )

    try:
        prepare_to_run_test(opts)
    except Exception as e:
        raise errors.BisectionTemporaryError(
            'failed when prepare, assume it is temporary: %s' % e
        )

    result_dir = run_test(opts)
    result = gather_test_result(opts, result_dir)

    # The OS version should not change.
    cros_util.assert_dut_cros_version(dut_os_version, opts.dut)

    return result


def action() -> bisector_cli.EvalAction:
    return bisector_cli.EvalAction.WITH_DUT


def main(args: tuple[str] | None = None) -> int:
    return bisector_cli.step_main_wrapper(step_main, args)


if __name__ == '__main__':
    sys.exit(main())
