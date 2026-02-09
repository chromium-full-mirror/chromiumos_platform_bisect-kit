#!/usr/bin/env python3
# Copyright 2017 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Helper script to report exit code according to execution result."""

import argparse
import collections
import logging
import re
import subprocess
import sys
import textwrap
import time

from bisect_kit import cli
from bisect_kit import common
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


def argtype_ratio(s):
    """Checks ratio syntax for argument parsing.

    Args:
      s: argument string.

    Returns:
      (op, value):
        op: Operator, should be one of <, <=, =, ==, >, >=.
        value: Should be between 0 and 1.
    """
    m = re.match(r'^([<>=]=?)\s*(\d+(?:\.\d+)?)$', s)
    if not m:
        raise argparse.ArgumentTypeError('invalid ratio condition')
    op, value = m.group(1), float(m.group(2))
    if not 0 <= value <= 1:
        raise argparse.ArgumentTypeError('value should be between 0 and 1')
    return op, value


def create_argument_parser():
    """Creates command line argument parser.

    Returns:
      An cli.ArgumentParser instance.
    """
    parents = [cli.create_session_optional_parser()]
    parser = cli.ArgumentParser(
        description=textwrap.dedent(
            """
          Helper script to report exit code according to execution result.

          If precondition specified but not meet, returns SKIP. Returns WANT
          only if any conditions meet.
      """
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        parents=parents,
    )
    bisect_range_group = parser.add_mutually_exclusive_group(required=True)
    bisect_range_group.add_argument(
        '--new',
        dest='want',
        action='store_const',
        const='new',
        help='Let WANT=NEW',
    )
    bisect_range_group.add_argument(
        '--old',
        dest='want',
        action='store_const',
        const='old',
        help='Let WANT=OLD',
    )
    parser.add_argument('exec_cmd')
    parser.add_argument('exec_args', nargs=argparse.REMAINDER)

    precondition_group = parser.add_argument_group(
        title='Preconditions to match (optional)',
        description='All specified preconditions must match, '
        'otherwise return SKIP.',
    )
    precondition_group.add_argument(
        '--precondition-output',
        metavar='REGEX',
        type=re.compile,
        help='Precondition to match %(metavar)s',
    )

    condition_group = parser.add_argument_group(
        title='Conditions to match (mutual exclusive, required)',
        description='If the specified condition matches, return WANT.',
    )
    condition_exclusive_group = condition_group.add_mutually_exclusive_group(
        required=True
    )
    condition_exclusive_group.add_argument(
        '--output',
        metavar='REGEX',
        type=re.compile,
        help='Regex to match stdout|stderr',
    )
    condition_exclusive_group.add_argument(
        '--returncode', type=int, help='Value of exit code'
    )
    condition_exclusive_group.add_argument(
        '--timeout',
        type=float,
        metavar='SECONDS',
        help='If command executes longer than SECONDS secs',
    )

    execution_group = parser.add_argument_group(
        title='Execution options',
        description='Controls how to execute, terminate, and how many times',
    )
    execution_group.add_argument(
        '--cwd', type=cli.argtype_dir_path, help='Working directory'
    )
    execution_group.add_argument(
        '--repeat',
        type=int,
        default=1,
        metavar='NUM',
        help='Repeat NUM times (default: %(default)s)',
    )
    execution_group.add_argument(
        '--ratio',
        default='=1',
        metavar='{op}{value}',
        type=argtype_ratio,
        help='Match if meet |ratio| condition. Example: ">0", "<0.5", "=1", etc. '
        '(default: %(default)r)',
    )
    execution_group.add_argument(
        '--noshortcut',
        action='store_true',
        help="Don't stop earlier if we know ratio will meet or not by calculation",
    )
    execution_group.add_argument(
        '--terminate-output',
        metavar='REGEX',
        type=re.compile,
        help='Once there is one line matching %(metavar)s, '
        'terminate the running program',
    )

    return parser


def opposite(want):
    return NEW if want == OLD else OLD


def run_once(opts):
    """Runs command once and returns corresponding exit code.

    This is the main function of runner.py. It controls command execution and
    converts execution result (output, exit code, duration, etc.) to
    corresponding exit code according to conditions from command line arguments.

    Returns:
      OLD: Execution result is considered as old behavior.
      NEW: Execution result is considered as old behavior.
      SKIP: Preconditions are not meet.
      FATAL: Fatal errors like command not found.
    """
    cmdline = subprocess.list2cmdline([opts.exec_cmd] + opts.exec_args)

    output_result = {
        "output_matched": False,
        "precondition_output_matched": False,
        "meet_terminate": False,
    }

    def output_handler(line):
        if output_result['meet_terminate']:
            return
        if opts.output and not output_result['output_matched']:
            if opts.output.search(line):
                logger.debug('matched output')
                output_result['output_matched'] = True
        if (
            opts.precondition_output
            and not output_result['precondition_output_matched']
        ):
            if opts.precondition_output.search(line):
                logger.debug('matched precondition_output')
                output_result['precondition_output_matched'] = True
        if opts.terminate_output and opts.terminate_output.search(line):
            logger.debug('terminate condition matched, stop execution')
            output_result['meet_terminate'] = True
            p.terminate()

    p = util.Popen(
        cmdline,
        cwd=opts.cwd,
        shell=True,
        stdout_callback=output_handler,
        stderr_callback=output_handler,
    )

    logger.debug('returncode %s', p.wait())

    found = False
    if opts.output and output_result['output_matched']:
        found = True
    if opts.timeout and p.duration > opts.timeout:
        found = True
    if opts.returncode is not None and opts.returncode == p.returncode:
        found = True

    if not found and not output_result['meet_terminate']:
        if p.returncode >= 128:
            logger.warning('fatal return, FATAL')
            return FATAL
        if p.returncode < 0:
            logger.warning('got signal, FATAL')
            return FATAL

    if (
        opts.precondition_output
        and not output_result['precondition_output_matched']
    ):
        logger.warning("precondition doesn't meet, SKIP")
        return SKIP

    return opts.want if found else opposite(opts.want)


def criteria_is_met(ratio, counter, rest, want):
    """Determines if current count of exit code meets specified threshold.

    After several runs of execution, `counter` contains how many times the exit
    code is 'new' or 'old'. This function answers whether the ratio, 'new' versus
    'old', meets specified threshold.

    For example,
      >>> criteria_is_met(('>=', 0.9), dict(new=20, old=1), 0, 'new')
      True
    It's True because 20/(20+1) >= 0.9.

    This function may be called before all loop iterations are completed (rest >
    0). For such case, the result (meet or not) is unknown (due to uncertainty)
    and thus return value is None.

    Args:
      ratio: (operator, value). For example, ('>=', 0.9) means the calculated
          value needs to be greater than or equal to 0.9.
      counter: (dict) count of execution result.
      rest: remaining execution count.
      want: the goal metric. 'new' or 'old'.

    Returns:
      None if the result is unknown, True if the `ratio` condition is meet, False
      otherwise.
    """
    other = opposite(want)
    if counter[want] + counter[other] == 0:
        return None

    # Assume no more SKIP.
    max_run = counter[want] + counter[other] + rest
    max_possible = float(counter[want] + rest) / max_run
    min_possible = float(counter[want]) / max_run

    op, v = ratio
    if op == '>':
        if max_possible > v >= min_possible:
            return None
        return min_possible > v
    if op == '>=':
        if max_possible >= v > min_possible:
            return None
        return min_possible >= v
    if op == '<':
        if min_possible < v <= max_possible:
            return None
        return min_possible < v
    if op == '<=':
        if min_possible <= v < max_possible:
            return None
        return min_possible <= v

    assert op in ('=', '==')
    # If the final count is not near an integer, it is certain (impossible to
    # meet).
    if abs(round(v * max_run) - v * max_run) > 1e-5:
        return False
    if min_possible != max_possible and min_possible <= v <= max_possible:
        return None
    return abs(min_possible - v) < 1e-3


def main(args=None):
    parser = create_argument_parser()
    opts = parser.parse_args(args)
    common.config_logging(opts)

    counter: collections.Counter[str] = collections.Counter()

    meet = None
    runtime = []
    for i in range(opts.repeat):
        t0 = time.time()
        status = run_once(opts)
        t1 = time.time()
        runtime.append(t1 - t0)

        counter[status] += 1
        logger.info(
            '%(ith)d/%(num)d old=%(old)d, new=%(new)d, skip=%(skip)d;'
            ' avg time=%(avgtime).2f',
            {
                "ith": i + 1,
                "num": opts.repeat,
                "old": counter[OLD],
                "new": counter[NEW],
                "skip": counter[SKIP],
                "avgtime": float(sum(runtime)) / len(runtime),
            },
        )

        if status == SKIP:
            continue
        if status == FATAL:
            return FATAL
        assert status in (OLD, NEW)

        rest = opts.repeat - i - 1
        meet = criteria_is_met(opts.ratio, counter, rest, opts.want)
        if rest > 0 and not opts.noshortcut and meet is not None:
            logger.info(
                'impossible to change result of ratio:"%s", break the loop',
                opts.ratio,
            )
            break

    if counter[OLD] == counter[NEW] == 0:
        status = SKIP
    elif meet:
        status = opts.want
    else:
        assert meet is not None
        status = opposite(opts.want)

    logger.info(
        'Runner final result %(status)s: old=%(old)d, new=%(new)d, skip=%(skip)d;'
        ' avg time=%(avgtime).2f',
        {
            "status": status,
            "old": counter[OLD],
            "new": counter[NEW],
            "skip": counter[SKIP],
            "avgtime": float(sum(runtime)) / len(runtime),
        },
    )

    return status


if __name__ == '__main__':
    sys.exit(EXIT_CODE_MAP[main()])
