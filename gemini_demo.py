#!/usr/bin/env python3
# Copyright 2026 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Gemini bisect demo script.

Simulates running Gemini-assisted bisection on known test regression cases.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from collections.abc import Callable
import logging
import os
import random
import sys
import uuid

from bisect_kit import core
from bisect_kit import cr_util
from bisect_kit import cros_util
from bisect_kit import strategy
from bisect_kit.gemini_bisect import gemini_util


def _webui_js_errors_reason() -> str:
    h = uuid.uuid4().hex
    return (
        'checkUnhandledPromiseRejectionError failed: WaitForCrashFiles '
        f'failed for directory [/run/daemon-store/crash/{h}]: '
        'timed out while waiting for crash files: no file matched '
        'jserror\\.\\d{8}\\.\\d{6}\\.\\d+\\.\\d+\\.meta, '
        'jserror\\.\\d{8}\\.\\d{6}\\.\\d+\\.\\d+\\.js_stack, '
        'jserror\\.\\d{8}\\.\\d{6}\\.\\d+\\.\\d+\\.chrome.txt.gz '
        f'(dirs /run/daemon-store/crash/{h}) (found )'
    )


DEMO_CASES: list[dict] = [
    {
        'testname': 'tast.ui.WebUIJSErrors',
        'good': 'refs/heads/main@{#1416515}',
        'bad': 'refs/heads/main@{#1416531}',
        'culprit': 'refs/heads/main@{#1416520}',
        'reason_func': _webui_js_errors_reason,
    },
    {
        'testname': 'tast.a11y.Smoke',
        'good': 'refs/heads/main@{#1513012}',
        'bad': 'refs/heads/main@{#1513038}',
        'culprit': 'refs/heads/main@{#1513018}',
        'reason_func': lambda: (
            'Failed to find the UI element (Browser: New Tab) in the browser: '
            'Uncaught (in promise): context deadline exceeded during a poll with '
            'timeout 15s; "failed to find node with properties: {className: '
            '/\\\\bBrowserFrame\\\\b/, role: window}"'
        ),
    },
    {
        'testname': 'tast.arc.RemovableMedia.vm',
        'good': 'refs/heads/main@{#1634593}',
        'bad': 'refs/heads/main@{#1634613}',
        'culprit': 'refs/heads/main@{#1634610}',
        'reason_func': lambda: (
            'Failed to test Files app integration: failed to open a file '
            'from Files app: failed to open Files app: launching the '
            'Files App failed: Uncaught (in promise): context deadline '
            'exceeded during a poll with timeout 60s; "failed to find '
            'node with properties: {name: /^Downloads$/, role: treeItem, '
            'ancestor: {name: /^Files/, className: '
            '/\\\\bBrowserWidget\\\\b/, role: window, first: true}}"'
        ),
    },
    {
        'testname': 'tast.camera.CCAUISmoke.fake_hal',
        'good': 'refs/heads/main@{#1661095}',
        'bad': 'refs/heads/main@{#1661333}',
        'culprit': 'refs/heads/main@{#1661260}',
        'reason_func': lambda: (
            'Failed to start app: failed to open CCA: Video is not active within '
            'given time: failed to wait for the video state to be true within 5s: '
            'context deadline exceeded during a poll with no set timeout; last '
            'error follows: "!!(CcaTest.isVideoActive() === true)" is false'
        ),
    },
    {
        'testname': 'tast.video.CDMOEMCrypto.ce_cdm',
        'good': 'R149-16656.0.0-118296',
        'bad': 'R149-16656.0.0-118297',
        'culprit': 'R149-16656.0.0-118296~R149-16656.0.0-118297/111',
        'reason_func': lambda: (
            'widevine_ce_cdm_hw_tests failed: signal: aborted (core dumped)'
        ),
        'board': 'volteer',
        'old_p': (0.5, 0.5),
        'new_p': (0.95, 1.0),
    },
]


def _get_simulated_cost_table(
    good: int, bad: int, prev_rev: int
) -> list[tuple[float, float]]:
    cost_table = []
    for x in range(good, bad + 1):
        if x == prev_rev:
            # Skips rebuild (still reflashes & runs test)
            cost_table.append((60.0, 30.0))
        else:
            # Incremental build & deploy (240s) + Tast run (PASS: 60s, FAIL: 30s)
            cost_table.append((300.0, 270.0))
    return cost_table


def demo(
    testname: str,
    good: str,
    bad: str,
    culprit: str,
    reason_func: Callable[[], str | None] | None = None,
    disabled: bool = False,
    board: str | None = None,
    old_p: tuple[float, float] = (0.05, 0.0),
    new_p: tuple[float, float] = (0.95, 1.0),
    target_confidence: float = 0.999,
    model: str | None = None,
):
    if disabled:
        return

    model_old_p, sim_old_p = old_p
    model_new_p, sim_new_p = new_p

    def get_reason() -> str | None:
        return reason_func() if reason_func else None

    is_chrome = '@' in good or good.startswith('refs/') or good.isdigit()
    chromeos_mirror = os.path.join(
        os.getenv('HOME', ''), 'git-mirrors-ext4/chromeos'
    )
    if is_chrome:
        repo = os.path.join(
            os.getenv('HOME', ''), 'bisect-workdir/test/chrome/src'
        )
        revlist, _ = cr_util.build_revlist(repo, good, bad)
    else:
        repo = os.path.join(
            os.getenv('HOME', ''), 'bisect-workdir/test/chromeos'
        )
        revlist, _ = cros_util.build_revlist(
            repo,
            good,
            bad,
            chromeos_mirror=chromeos_mirror,
            board=board,
        )

    culprit_idx = revlist.index(culprit)

    agent = gemini_util.BisectAgent(
        testname,
        repo,
        revlist,
        model=model or gemini_util.MODEL_DEFAULT,
        chromeos_mirror=chromeos_mirror if not is_chrome else None,
        board=board if not is_chrome else None,
        old_p=model_old_p,
        new_p=model_new_p,
    )
    agent.add_sample(good, 'PASS')
    agent.add_sample(bad, 'FAIL', reason=get_reason())
    stat = defaultdict(list)
    stat[good].append('PASS')
    stat[bad].append('FAIL')

    bsearch = strategy.NoisyBinarySearch(
        [core.RevInfo(x) for x in revlist],
        old_idx=0,
        new_idx=len(revlist) - 1,
        oracle=(model_old_p, model_new_p),
        confidence=target_confidence,
    )
    bsearch.add_sample(0, 'old')
    bsearch.add_sample(len(revlist) - 1, 'new')
    assert bsearch.state == bsearch.STARTED
    prob = bsearch.prob
    assert prob is not None

    # The last run revision was 'bad' (index len(revlist) - 1)
    cost_table = _get_simulated_cost_table(
        0, len(revlist) - 1, len(revlist) - 1
    )
    # pylint: disable=protected-access
    utilities = strategy.NoisyBinarySearch._calculate_utilities(
        model_old_p, model_new_p, prob, cost_table=cost_table
    )

    agent.update_stats(
        {
            revlist[x]: {
                'prob': prob[x],
                'status': {
                    'PASS': stat[revlist[x]].count('PASS'),
                    'FAIL': stat[revlist[x]].count('FAIL'),
                },
                'info_gain': utilities[x][0],
                'info_gain_per_cost': utilities[x][1],
            }
            for x in range(len(revlist))
        },
        old_p=model_old_p,
        new_p=model_new_p,
    )
    attempt = agent.next()
    runs_llm = 0
    while attempt:
        attempt_idx = revlist.index(attempt)
        runs_llm += 1
        run_reason = None
        is_fail = (
            random.random() < sim_new_p
            if attempt_idx >= culprit_idx
            else random.random() < sim_old_p
        )
        if is_fail:
            status = 'FAIL'
            run_reason = get_reason()
        else:
            status = 'PASS'
        stat[attempt].append(status)
        agent.add_sample(attempt, status, reason=run_reason)

        bsearch.add_sample(attempt_idx, 'old' if status == 'PASS' else 'new')
        prob = bsearch.prob
        assert prob is not None
        # Current revision is 'attempt_idx'
        cost_table = _get_simulated_cost_table(0, len(revlist) - 1, attempt_idx)
        # pylint: disable=protected-access
        utilities = strategy.NoisyBinarySearch._calculate_utilities(
            model_old_p, model_new_p, prob, cost_table=cost_table
        )

        agent.update_stats(
            {
                revlist[x]: {
                    'prob': prob[x],
                    'status': {
                        'PASS': stat[revlist[x]].count('PASS'),
                        'FAIL': stat[revlist[x]].count('FAIL'),
                    },
                    'info_gain': utilities[x][0],
                    'info_gain_per_cost': utilities[x][1],
                }
                for x in range(len(revlist))
            },
            old_p=model_old_p,
            new_p=model_new_p,
        )
        likelihood_new = float(sum(prob[: attempt_idx + 1]))
        attempt = agent.next(
            {
                'status': status,
                'reason': run_reason,
                'likelihood_new': likelihood_new,
            }
        )
    concluded_culprit = agent.get_result()
    assert bsearch.prob is not None
    concluded_prob_on_stop = (
        bsearch.prob[revlist.index(concluded_culprit)]
        if concluded_culprit in revlist
        else 0.0
    )
    real_prob_on_stop = bsearch.prob[culprit_idx]

    runs_additional_math = 0
    while not bsearch.is_done():
        runs_additional_math += 1
        idx = bsearch.next_idx()
        is_fail = (
            random.random() < sim_new_p
            if idx >= culprit_idx
            else random.random() < sim_old_p
        )
        bsearch.add_sample(idx, 'new' if is_fail else 'old')

    bsearch_std = strategy.NoisyBinarySearch(
        [core.RevInfo(x) for x in revlist],
        old_idx=0,
        new_idx=len(revlist) - 1,
        oracle=(model_old_p, model_new_p),
        confidence=target_confidence,
    )
    bsearch_std.add_sample(0, 'old')
    bsearch_std.add_sample(len(revlist) - 1, 'new')
    assert bsearch_std.state == bsearch_std.STARTED
    runs_bisect = 0
    while not bsearch_std.is_done():
        runs_bisect += 1
        idx = bsearch_std.next_idx()
        is_fail = (
            random.random() < sim_new_p
            if idx >= culprit_idx
            else random.random() < sim_old_p
        )
        bsearch_std.add_sample(idx, 'new' if is_fail else 'old')

    assert (
        agent.get_result() == culprit
    ), f'Agent result {agent.get_result()} does not match expected culprit {culprit}'
    bsearch_culprit = revlist[bsearch.get_best_guess()]
    assert (
        bsearch_culprit == culprit
    ), f'Math result {bsearch_culprit} does not match expected culprit {culprit}'
    bsearch_std_culprit = revlist[bsearch_std.get_best_guess()]
    assert (
        bsearch_std_culprit == culprit
    ), f'Pure math result {bsearch_std_culprit} does not match expected culprit {culprit}'

    # ANSI color formatting:
    # \033[1;32m = bold green (text)
    # \033[1;36m = bold cyan (culprit)
    # \033[1;33m = bold yellow (run counts)
    # \033[0m    = reset
    print(
        f'\033[1;32mConcluded culprit \033[1;36m{concluded_culprit}\033[1;32m '
        f'in \033[1;33m{runs_llm}+{runs_additional_math}\033[1;32m runs instead of '
        f'\033[1;33m{runs_bisect}\033[1;32m runs by bisect (confidence: {target_confidence:.2%}, '
        f'likelihood when agent stopped: concluded={concluded_prob_on_stop:.2%}, real={real_prob_on_stop:.2%}).\033[0m'
    )


def parse_case_ids(spec: str, max_cases: int) -> list[int]:
    """Parses a comma-separated list of indices and ranges (e.g. '0-1,3')."""
    indices: set[int] = set()
    for part in spec.split(','):
        part = part.strip()
        if not part:
            continue
        if '-' in part:
            bounds = part.split('-')
            if (
                len(bounds) != 2
                or not bounds[0].strip().isdigit()
                or not bounds[1].strip().isdigit()
            ):
                raise argparse.ArgumentTypeError(
                    f'Invalid case ID range: {part!r}'
                )
            start, end = int(bounds[0].strip()), int(bounds[1].strip())
            if start > end:
                raise argparse.ArgumentTypeError(
                    f'Invalid case ID range (start > end): {part!r}'
                )
            for i in range(start, end + 1):
                if i < 0 or i >= max_cases:
                    raise argparse.ArgumentTypeError(
                        f'Case ID out of range [0, {max_cases - 1}]: {i}'
                    )
                indices.add(i)
        else:
            if not part.isdigit():
                raise argparse.ArgumentTypeError(f'Invalid case ID: {part!r}')
            i = int(part)
            if i < 0 or i >= max_cases:
                raise argparse.ArgumentTypeError(
                    f'Case ID out of range [0, {max_cases - 1}]: {i}'
                )
            indices.add(i)
    if not indices:
        raise argparse.ArgumentTypeError(f'No valid case IDs found in {spec!r}')
    return sorted(indices)


def prompt_case_ids(max_cases: int) -> list[int]:
    """Interactively prompts the user to select demo cases."""
    print('Available demo cases:')
    for idx, case in enumerate(DEMO_CASES):
        print(f'  [{idx}] {case["testname"]}')
    while True:
        try:
            val = input(
                f'Select case ID(s) (e.g. 0-1,3, or press Enter for all [0-{max_cases - 1}]): '
            ).strip()
            if not val or val.lower() == 'all':
                return list(range(max_cases))
            return parse_case_ids(val, max_cases)
        except (argparse.ArgumentTypeError, ValueError) as e:
            print(f'Invalid input: {e}. Please try again.')
        except (KeyboardInterrupt, EOFError):
            print('\nAborted.')
            sys.exit(1)


def main(args: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        '--case-id',
        type=lambda s: parse_case_ids(s, len(DEMO_CASES)),
        help='Specific demo case ID(s) to run (e.g. 0, 0-1,3). If omitted, prompts interactively.',
    )
    parser.add_argument(
        '--model',
        default=gemini_util.MODEL_DEFAULT,
        help='Gemini model to use (default: %(default)s)',
    )
    opts = parser.parse_args(args)

    test_logger = logging.getLogger('bisect_kit.gemini_bisect.gemini_util')
    test_logger.setLevel(logging.DEBUG)
    handler = logging.StreamHandler(sys.stderr)
    handler.setLevel(logging.DEBUG)
    test_logger.addHandler(handler)

    if opts.case_id is not None:
        case_indices = opts.case_id
    else:
        case_indices = prompt_case_ids(len(DEMO_CASES))

    for idx in case_indices:
        demo(**DEMO_CASES[idx], model=opts.model)


if __name__ == '__main__':
    main(sys.argv[1:])
