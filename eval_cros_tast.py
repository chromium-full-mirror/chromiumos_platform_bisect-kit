#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright 2019 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Evaluate ChromeOS tast tests."""
from __future__ import annotations
from __future__ import print_function

import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import typing

from bisect_kit import bisector_cli
from bisect_kit import catapult_util
from bisect_kit import cli
from bisect_kit import common
from bisect_kit import configure
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
    parents = [common.common_argument_parser, common.session_optional_parser]
    parser = argparse.ArgumentParser(description=__doc__, parents=parents)
    cli.patching_argparser_exit(parser)
    parser.add_argument(
        '--rich_result',
        action='store_true',
        help='Instead of mere exit code, output detailed information in json',
    )
    parser.add_argument(
        'dut',
        nargs='?',
        type=cli.argtype_notempty,
        metavar='DUT',
        default=configure.get('DUT', ''),
    )
    parser.add_argument(
        '--chromeos_root',
        type=cli.argtype_dir_path,
        metavar='CHROMEOS_ROOT',
        default=configure.get('CHROMEOS_ROOT', ''),
        help='ChromeOS tree root',
    )
    parser.add_argument(
        '--prebuilt',
        action='store_true',
        help='Run tast using existing server prebuilt package if specified; '
        'otherwise use the default one',
    )
    parser.add_argument(
        '--tast_build',
        action='store_true',
        help='Build tast test bundle (-build=true) if specified; '
        'default is using prebuilt bundle on the DUT',
    )
    parser.add_argument(
        '--with_private_bundles',
        action='store_true',
        help='Whether search tests in private bundles or not',
    )
    parser.add_argument(
        '--reboot_before_test',
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
        '--test_name',
        required=True,
        help='Test name, like "video_VideoDecodeAccelerator.h264"',
    )
    group.add_argument(
        '--fail_to_pass',
        action='store_true',
        help='For functional tests: old behavior is FAIL and new behavior is '
        'PASS; If not specified, default = old behavior is PASS and new '
        'behavior is FAIL',
    )
    group.add_argument(
        '--metric',
        help='Metric name of performance test; example: '
        '"cheets_SystemRawImageSize"',
    )

    return parser


def prepare_to_run_test(opts):
    if opts.reboot_before_test:
        cros_util.reboot(
            opts.dut, force_reboot_callback=cros_lab_util.reboot_via_servo
        )


def to_forwarded_host(host: str) -> tuple[str, str]:
    # Returns (dut address in chroot, dut address needs to be forwarded)
    if cros_lab_util.is_dut_needs_forwarded(host):
        return cros_util.FORWARDED_DUT_HOST, host
    return host, ''


def tast_cmd(opts, cmd, *args):
    flags = []
    if opts.prebuilt:
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
        if cmd == 'run':
            flags += [
                '-defaultvarsdir',
                os.path.join(tast_dir, 'vars', 'private'),
            ]
    else:
        tast_bin = 'tast'

    if cmd == 'run':
        flags += ['-var=%s=%s' % x for x in opts.args]

    # TODO(zjchang): ensure the tast version for buildbucket builds is correct
    if not opts.tast_build:
        flags.append('-build=false')
        if opts.with_private_bundles:
            flags.append('-downloadprivatebundles=true')

    return [tast_bin, '-verbose', cmd] + flags + list(args)


def get_tast_bundle_info(opts, pattern=None):
    dut, forward_host = to_forwarded_host(opts.dut)

    bundles = ['cros']
    if opts.with_private_bundles:
        bundles.append('crosint')

    args = [dut]
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
                forward_host=forward_host,
            )
        except subprocess.CalledProcessError as e:
            raise errors.ExternalError(
                'failed to get tast bundle info, assume it is temporary: %s' % e
            )
        for entry in json.loads(json_text):
            result[entry['name']] = bundle

    return result


def run_test(opts, bundle):
    """Runs an autotest test.

    Args:
      opts: An argparse.Namespace to hold command line arguments.
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

    dut, forward_host = to_forwarded_host(opts.dut)
    cmd = tast_cmd(opts, 'run', *flags, dut, opts.test_name)
    cros_util.cros_sdk(opts.chromeos_root, *cmd, forward_host=forward_host)

    return results_dir_output_chroot


# Be careful that both tauto and tast generate results.json, but their format
# is totally different.
def parse_results_json(
    result_dir, test_name
) -> tuple[bool, typing.Optional[str]]:
    passed = None
    reason = None
    results_path = os.path.join(result_dir, 'results.json')
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
    if passed is None:
        raise errors.ExternalError('no test result for "%s"?' % test_name)
    return passed, reason


def gather_test_result(opts, result_dir) -> core.StepResult:
    error_path = os.path.join(result_dir, 'run_error.txt')
    if os.path.exists(error_path):
        with open(error_path) as f:
            message = f.read()
        raise errors.ExternalError('tast global error: %s' % message)

    passed, reason = parse_results_json(result_dir, opts.test_name)
    if opts.metric:
        chart_path = os.path.join(
            result_dir, 'tests', opts.test_name, 'results-chart.json'
        )
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


def step_main(args: 'typing.Optional[tuple[str]]') -> core.StepResult:
    common.init()
    parser = create_argument_parser()
    opts = parser.parse_args(args)
    common.config_logging(opts)

    if cros_lab_util.is_satlab_dut(opts.dut):
        cros_lab_util.write_satlab_ssh_config(opts.dut)

    if not cros_util.is_dut(opts.dut):
        raise errors.BrokenDutException(
            '%r is not a valid DUT address' % opts.dut
        )

    # Private bundles are not included in the OS image, make sure they are
    # available.
    if opts.with_private_bundles and not opts.tast_build:
        if not cros_util.query_dut_is_by_official_builder(opts.dut):
            # Official builders uploaded private tast bundles and tast can download
            # them with -downloadprivatebundles=true, so --tast_build is not
            # required. Otherwise, we must build the bundles by ourselves.
            raise errors.ArgumentError(
                '--tast_build',
                'for non-official chromeos image, --tast_build must be specified',
            )

    # Verify command line options.
    if opts.metric:
        if opts.fail_to_pass:
            raise errors.ArgumentError(
                '--fail_to_pass',
                '--fail_to_pass is not for benchmark test (--metric)',
            )
    # Remove the "tast." prefix prepended by autotest.
    opts.test_name = re.sub(r'^tast\.', '', opts.test_name)

    cros_util.prepare_chroot(opts.chromeos_root)
    tast_bundle_info = get_tast_bundle_info(opts, opts.test_name)
    if not tast_bundle_info:
        tast_bundle_info = get_tast_bundle_info(opts)
        util.report_similar_candidates(
            'test name', opts.test_name, list(tast_bundle_info)
        )
        assert 0  # unreachable

    if len(tast_bundle_info) != 1 or opts.test_name not in tast_bundle_info:
        # For example, tast in chroot after 12205.0.0 is IPC incompatible with
        # tast on DUT earlier than 12028.0.0 (crbug/932307)
        raise errors.ExecutionFatalError(
            '"tast list" returns unexpected tests; '
            'incompatible tast on DUT and in chroot?'
        )

    try:
        prepare_to_run_test(opts)
    except Exception as e:
        raise errors.BisectionTemporaryError(
            'failed when prepare, assume it is temporary: %s' % e
        )

    bundle = tast_bundle_info[opts.test_name]
    try:
        result_dir = run_test(opts, bundle)
    except subprocess.CalledProcessError as e:
        raise errors.BisectionTemporaryError(
            'failed to run tast; maybe build, ssh, or setup failures'
        ) from e

    return gather_test_result(opts, result_dir)


def main(args: typing.Optional[tuple[str]] = None) -> int:
    return bisector_cli.step_main_wrapper(step_main, args)


if __name__ == '__main__':
    sys.exit(main())
