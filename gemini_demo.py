#!/usr/bin/env python3
# Copyright 2026 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Gemini bisect demo script.

Simulates running Gemini-assisted bisection on known test regression cases.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
import dataclasses
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


@dataclasses.dataclass
class SimulationResult:
    """Stores bisection simulation run metrics."""

    total_runs: int
    agent_runs: int = 0
    additional_runs: int = 0
    total_cost: float = 0.0
    agent_cost: float = 0.0
    additional_cost: float = 0.0
    concluded_prob_on_stop: float | None = None
    real_prob_on_stop: float | None = None
    agent_concluded_culprit: str | None = None


def _extract_prob_on_stop(
    bsearch: strategy.NoisyBinarySearch,
    agent_concluded_culprit: str | None,
    culprit_idx: int,
) -> tuple[float | None, float | None]:
    prob = bsearch.prob
    if not prob:
        return None, None
    concluded_prob = None
    if agent_concluded_culprit:
        revlist = [x.rev for x in bsearch.rev_info]
        if agent_concluded_culprit in revlist:
            concluded_prob = prob[revlist.index(agent_concluded_culprit)]
    return concluded_prob, prob[culprit_idx]


def _run_simulation(
    bsearch: strategy.NoisyBinarySearch,
    culprit_idx: int,
    sim_old_p: float,
    sim_new_p: float,
    reason_func: Callable[[], str | None] | None = None,
) -> SimulationResult:
    """Runs a simulated bisection loop on the given strategy until done."""
    num_revs = len(bsearch.rev_info)
    prev_rev_idx = num_revs - 1
    agent_runs = 0
    additional_runs = 0
    agent_cost = 0.0
    additional_cost = 0.0
    agent_stopped = False
    concluded_prob_on_stop = None
    real_prob_on_stop = None
    agent_concluded_culprit = None

    while not bsearch.is_done():
        cost_table = _get_simulated_cost_table(0, num_revs - 1, prev_rev_idx)
        idx = bsearch.next_idx(cost_table=cost_table)

        gemini = getattr(bsearch, 'gemini', None)
        if gemini is not None:
            if gemini.get_result() and not agent_stopped:
                agent_stopped = True
                agent_concluded_culprit = gemini.get_result()
                concluded_prob_on_stop, real_prob_on_stop = (
                    _extract_prob_on_stop(
                        bsearch, agent_concluded_culprit, culprit_idx
                    )
                )

        is_fail = (
            random.random() < sim_new_p
            if idx >= culprit_idx
            else random.random() < sim_old_p
        )
        if idx == prev_rev_idx:
            step_cost = 30.0 if is_fail else 60.0
        else:
            step_cost = 270.0 if is_fail else 300.0

        if gemini is not None and not agent_stopped:
            agent_runs += 1
            agent_cost += step_cost
        else:
            additional_runs += 1
            additional_cost += step_cost

        prev_rev_idx = idx

        run_reason = reason_func() if (is_fail and reason_func) else None
        bsearch.add_sample(
            idx,
            'new' if is_fail else 'old',
            reason=run_reason,
        )

    gemini = getattr(bsearch, 'gemini', None)
    if gemini and gemini.get_result() and not agent_stopped:
        agent_stopped = True
        agent_concluded_culprit = gemini.get_result()
        concluded_prob_on_stop, real_prob_on_stop = _extract_prob_on_stop(
            bsearch, agent_concluded_culprit, culprit_idx
        )

    return SimulationResult(
        total_runs=agent_runs + additional_runs,
        agent_runs=agent_runs,
        additional_runs=additional_runs,
        total_cost=agent_cost + additional_cost,
        agent_cost=agent_cost,
        additional_cost=additional_cost,
        concluded_prob_on_stop=concluded_prob_on_stop,
        real_prob_on_stop=real_prob_on_stop,
        agent_concluded_culprit=agent_concluded_culprit,
    )


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

    bsearch = strategy.GeminiFusionStrategy(
        [core.RevInfo(x) for x in revlist],
        old_idx=0,
        new_idx=len(revlist) - 1,
        oracle=(model_old_p, model_new_p),
        confidence=target_confidence,
        term_map={'old': 'PASS', 'new': 'FAIL'},
        enable_gemini=True,
        test_name=testname,
        source_root=repo,
        chromeos_mirror=chromeos_mirror if not is_chrome else None,
        board=board if not is_chrome else None,
        model=model,
    )
    bsearch.add_sample(0, 'old')
    bsearch.add_sample(
        len(revlist) - 1,
        'new',
        reason=reason_func() if reason_func else None,
    )
    assert bsearch.state == bsearch.STARTED
    res_strategy = _run_simulation(
        bsearch,
        culprit_idx,
        sim_old_p,
        sim_new_p,
        reason_func=reason_func,
    )

    bsearch_std = strategy.NoisyBinarySearch(
        [core.RevInfo(x) for x in revlist],
        old_idx=0,
        new_idx=len(revlist) - 1,
        oracle=(model_old_p, model_new_p),
        confidence=target_confidence,
        term_map={'old': 'PASS', 'new': 'FAIL'},
    )
    bsearch_std.add_sample(0, 'old')
    bsearch_std.add_sample(len(revlist) - 1, 'new')
    assert bsearch_std.state == bsearch_std.STARTED
    res_bisect = _run_simulation(
        bsearch_std,
        culprit_idx,
        sim_old_p,
        sim_new_p,
    )

    if res_strategy.agent_concluded_culprit:
        assert (
            res_strategy.agent_concluded_culprit == culprit
        ), f'Agent result {res_strategy.agent_concluded_culprit} does not match expected culprit {culprit}'

    concluded_culprit = revlist[bsearch.get_best_guess()]
    assert (
        concluded_culprit == culprit
    ), f'Strategy result {concluded_culprit} does not match expected culprit {culprit}'
    bsearch_std_culprit = revlist[bsearch_std.get_best_guess()]
    assert (
        bsearch_std_culprit == culprit
    ), f'Pure math result {bsearch_std_culprit} does not match expected culprit {culprit}'

    if (
        res_strategy.concluded_prob_on_stop is not None
        and res_strategy.real_prob_on_stop is not None
    ):
        confidence_info = (
            f', likelihood when agent stopped: '
            f'concluded={res_strategy.concluded_prob_on_stop:.2%}, '
            f'real={res_strategy.real_prob_on_stop:.2%}'
        )
    else:
        confidence_info = ''

    # ANSI color formatting:
    # \033[1;32m = bold green (text)
    # \033[1;36m = bold cyan (culprit)
    # \033[1;33m = bold yellow (run counts / times)
    # \033[0m    = reset
    print(
        f'\033[1;32mConcluded culprit \033[1;36m{concluded_culprit}\033[1;32m '
        f'in \033[1;33m{res_strategy.agent_runs}+{res_strategy.additional_runs}\033[1;32m runs '
        f'(\033[1;33m{res_strategy.total_cost:.0f}s\033[1;32m) instead of '
        f'\033[1;33m{res_bisect.total_runs}\033[1;32m runs '
        f'(\033[1;33m{res_bisect.total_cost:.0f}s\033[1;32m) by bisect '
        f'(confidence: {target_confidence:.2%}{confidence_info}).\033[0m'
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


def main(args: Sequence[str] | None = None) -> None:
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
