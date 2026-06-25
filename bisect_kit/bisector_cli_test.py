# Copyright 2019 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Test bisector_cli module."""

from __future__ import annotations

import argparse
import contextlib
import io
import typing
import unittest
from unittest import mock

from bisect_kit import bisector_cli
from bisect_kit import cli
from bisect_kit import core
from bisect_kit import dut_manager as dut_manager_module
from bisect_kit import errors
from bisect_kit import strategy
from bisect_kit import testing


class NoisyTestCaseType(typing.TypedDict):
    """Type Definition for a test case in test_run_noisy_many_times"""

    rate: tuple[float, float]
    times: int
    expect_correct_rate: float


class DummyDomain(core.BisectDomain):
    """Dummy subclass of BisectDomain."""

    revtype = staticmethod(cli.argtype_notempty)

    @staticmethod
    def add_init_arguments(parser):
        parser.add_argument('--num', required=True, type=int)
        parser.add_argument('--ans', type=int)
        parser.add_argument('--old-p', type=float, default=0.0)
        parser.add_argument('--new-p', type=float, default=1.0)
        parser.add_argument('--dut', type=str)

    @staticmethod
    def init(opts: argparse.Namespace) -> tuple[core.BisectConfig, dict]:
        config = {
            "ans": opts.ans,
            "old_p": opts.old_p,
            "new_p": opts.new_p,
            "old": opts.old,
            "new": opts.new,
            "dut": opts.dut,
        }

        revlist = [str(i) for i in range(opts.num)]
        return config, {'revlist': revlist}

    def __init__(self, config: core.BisectConfig):
        self.config = config

    def setenv(self, env, rev, rev_details=None):
        env['ANS'] = str(self.config.get('ans'))
        env['OLD_P'] = str(self.config.get('old_p'))
        env['NEW_P'] = str(self.config.get('new_p'))


class TestBisectorCli(unittest.TestCase):
    """Test bisector_cli functions."""

    def test_collect_bisect_result_values(self):
        # pylint: disable=protected-access
        values: list[float] = []
        bisector_cli._collect_bisect_result_values(values, '')
        self.assertEqual(values, [])

        values = []
        bisector_cli._collect_bisect_result_values(values, 'foo\n')
        bisector_cli._collect_bisect_result_values(values, 'bar\n')
        self.assertEqual(values, [])

        values = []
        bisector_cli._collect_bisect_result_values(
            values, 'BISECT_RESULT_VALUES=1 2\n'
        )
        bisector_cli._collect_bisect_result_values(
            values, 'fooBISECT_RESULT_VALUES=3 4\n'
        )
        bisector_cli._collect_bisect_result_values(
            values, 'BISECT_RESULT_VALUES=5\n'
        )
        self.assertEqual(values, [1, 2, 5])

        with self.assertRaises(errors.InternalError):
            bisector_cli._collect_bisect_result_values(
                values, 'BISECT_RESULT_VALUES=hello\n'
            )


@mock.patch('bisect_kit.common.config_logging', mock.Mock())
class TestBisectorCommandLine(unittest.TestCase):
    """Test bisector_cli.BisectorCommandLine class."""

    def setUp(self):
        self.session_base_patcher = testing.SessionBasePatcher()
        self.session_base_patcher.patch()
        self.random_list = testing.RandomNum()
        self.old_rate = None
        self.new_rate = None
        self.dut_manager = dut_manager_module.DutManager(
            'PreAllocate',
            states=None,
            dut_allocate_spec=None,
            pre_allocated_dut='pre_allocated_dut',
            should_auto_allocate=False,
        )

    def tearDown(self):
        self.session_base_patcher.reset()

    def test_run_true(self):
        bisector = bisector_cli.BisectorCommandLine(
            DummyDomain, self.dut_manager
        )
        bisector.main('init', '--num=10', '--old=0', '--new=9')
        bisector.main('config', 'switch', 'true')
        bisector.main('config', 'eval', 'true')
        with self.assertRaises(errors.VerificationFailed):
            bisector.main('run')

        result = bisector.current_status()
        self.assertEqual(result.get('inited'), True)
        self.assertEqual(result.get('verified'), False)

    def test_run_false(self):
        bisector = bisector_cli.BisectorCommandLine(
            DummyDomain, self.dut_manager
        )
        bisector.main('init', '--num=10', '--old=0', '--new=9')
        bisector.main('config', 'switch', 'true')
        bisector.main('config', 'eval', 'false')
        with self.assertRaises(errors.VerificationFailed):
            bisector.main('run')

        result = bisector.current_status()
        self.assertEqual(result.get('inited'), True)
        self.assertEqual(result.get('verified'), False)

    def test_run_true_with_endpoint_verification(self):
        bisector = bisector_cli.BisectorCommandLine(
            DummyDomain, self.dut_manager
        )
        bisector.main(
            'init', '--num=10', '--old=0', '--new=9', '--endpoint-verification'
        )
        bisector.main('config', 'switch', 'true')
        bisector.main('config', 'eval', 'true')
        with self.assertRaises(errors.VerificationFailed):
            bisector.main('run')
        result = bisector.current_status()
        self.assertEqual(result.get('inited'), True)
        self.assertEqual(result.get('verified'), False)

    def test_run_false_with_endpoint_verification(self):
        bisector = bisector_cli.BisectorCommandLine(
            DummyDomain, self.dut_manager
        )
        bisector.main(
            'init', '--num=10', '--old=0', '--new=9', '--endpoint-verification'
        )
        bisector.main('config', 'switch', 'true')
        bisector.main('config', 'eval', 'false')
        with self.assertRaises(errors.VerificationFailed):
            bisector.main('run')
        result = bisector.current_status()
        self.assertEqual(result.get('inited'), True)
        self.assertEqual(result.get('verified'), False)

    def test_simple(self):
        bisector = bisector_cli.BisectorCommandLine(
            DummyDomain, self.dut_manager
        )
        bisector.main('init', '--num=20', '--old=3', '--new=15')
        bisector.main('config', 'switch', 'true')
        bisector.main('config', 'eval', 'sh', '-c', '[ "$BISECT_REV" -lt 7 ]')
        bisector.main('run')
        self.assertEqual(bisector.strategy.get_best_guess(), 7)

        with contextlib.redirect_stdout(io.StringIO()):
            # Only make sure no exceptions. No output verification.
            bisector.main('log')

    def test_switch_fail(self):
        bisector = bisector_cli.BisectorCommandLine(
            DummyDomain, self.dut_manager
        )
        bisector.main('init', '--num=20', '--old=3', '--new=15')
        bisector.main('config', 'switch', 'false')
        bisector.main('config', 'eval', 'sh', '-c', '[ "$BISECT_REV" -lt 7 ]')
        with self.assertRaises(errors.TooManyTemporaryErrors):
            bisector.main('run')

        result = bisector.current_status()
        self.assertEqual(result.get('inited'), True)
        self.assertEqual(result.get('verified'), False)

    def do_evaluate(
        self, _cmd, _domain, rev, _rev_details, _log_file, capture_values=False
    ):
        del capture_values  # unused
        if int(rev) < 42:
            return core.StepResult('old')
        return core.StepResult('new')

    def do_noisy_evaluate(
        self, _cmd, _domain, rev, _rev_details, _log_file, capture_values=False
    ):
        del capture_values
        ans = self.random_list.get_random_num()

        if int(rev) < 42:
            p = self.old_rate
        else:
            p = self.new_rate

        if ans < p:
            return core.StepResult('new')
        return core.StepResult('old')

    def test_run_classic(self):
        bisector = bisector_cli.BisectorCommandLine(
            DummyDomain, self.dut_manager
        )
        bisector.main('init', '--num=200', '--old=0', '--new=99')
        bisector.main('config', 'switch', 'true')
        bisector.main('config', 'eval', 'false')

        with mock.patch(
            'bisect_kit.bisector_cli.do_evaluate', self.do_evaluate
        ):
            # two verify
            with contextlib.redirect_stdout(io.StringIO()) as mock_stdout:
                bisector.main('next')
                self.assertEqual(mock_stdout.getvalue(), '99\n')

            bisector.main('run', '-1')
            with contextlib.redirect_stdout(io.StringIO()) as mock_stdout:
                bisector.main('next')
                self.assertEqual(mock_stdout.getvalue(), '0\n')

            bisector.main('run', '-1')
            self.assertEqual(bisector.strategy.get_range(), (0, 99))
            bisector.main('run', '-1')
            self.assertIn(bisector.strategy.get_range(), {(0, 49), (0, 50)})

            bisector.main('run')
            self.assertEqual(bisector.strategy.get_best_guess(), 42)
        with contextlib.redirect_stdout(io.StringIO()) as mock_stdout:
            bisector.main('next')
            self.assertEqual(mock_stdout.getvalue(), 'done\n')

    def test_run_classic_with_endpoint_verification(self):
        bisector = bisector_cli.BisectorCommandLine(
            DummyDomain, self.dut_manager
        )
        bisector.main(
            'init',
            '--num=200',
            '--old=0',
            '--new=99',
            '--endpoint-verification',
        )
        bisector.main('config', 'switch', 'true')
        bisector.main('config', 'eval', 'false')

        with mock.patch(
            'bisect_kit.bisector_cli.do_evaluate', self.do_evaluate
        ):
            with contextlib.redirect_stdout(io.StringIO()) as mock_stdout:
                bisector.main('next')
                self.assertEqual(mock_stdout.getvalue(), '99\n')

            bisector.main('run', '-1')
            with contextlib.redirect_stdout(io.StringIO()) as mock_stdout:
                bisector.main('next')
                self.assertEqual(mock_stdout.getvalue(), '99\n')
            self.assertEqual(bisector.strategy.get_range(), (0, 99))

            bisector.main('run')
            self.assertEqual(bisector.strategy.get_best_guess(), 42)
        with contextlib.redirect_stdout(io.StringIO()) as mock_stdout:
            bisector.main('next')
            self.assertEqual(mock_stdout.getvalue(), 'done\n')

    def test_run_noisy(self):
        bisector = bisector_cli.BisectorCommandLine(
            DummyDomain, self.dut_manager
        )
        bisector.main(
            'init',
            '--num=100',
            '--old=0',
            '--new=99',
            '--noisy=old=1/10,new=9/10',
        )
        bisector.main('config', 'switch', 'true')
        bisector.main('config', 'eval', 'false')

        self.old_rate = 0.1
        self.new_rate = 0.9

        with mock.patch(
            'bisect_kit.bisector_cli.do_evaluate', self.do_noisy_evaluate
        ):
            bisector.main('run')
            self.assertEqual(bisector.strategy.get_best_guess(), 42)

        with contextlib.redirect_stdout(io.StringIO()) as mock_stdout:
            bisector.main('next')
            # There might be small math error near the boundary of confidence.
            self.assertIn(mock_stdout.getvalue(), ['done\n', '41\n', '42\n'])

    def test_run_noisy_with_endpoint_verification(self):
        bisector = bisector_cli.BisectorCommandLine(
            DummyDomain, self.dut_manager
        )
        bisector.main(
            'init',
            '--num=100',
            '--old=0',
            '--new=99',
            '--noisy=old=1/10,new=9/10',
            '--endpoint-verification',
        )
        bisector.main('config', 'switch', 'true')
        bisector.main('config', 'eval', 'false')

        self.old_rate = 0.1
        self.new_rate = 0.9

        with mock.patch(
            'bisect_kit.bisector_cli.do_evaluate', self.do_noisy_evaluate
        ):
            bisector.main('run')
            self.assertEqual(bisector.strategy.get_best_guess(), 42)

        with contextlib.redirect_stdout(io.StringIO()) as mock_stdout:
            bisector.main('next')
            self.assertIn(mock_stdout.getvalue(), ['done\n', '41\n', '42\n'])

    def test_cmd_old_and_new(self):
        """Tests cmd_old and cmd_new"""
        bisector = bisector_cli.BisectorCommandLine(
            DummyDomain, self.dut_manager
        )
        bisector.main('init', '--num=100', '--old=0', '--new=99')
        bisector.main('config', 'switch', 'true')
        bisector.main('config', 'eval', 'false')
        bisector.main('old', '0')
        bisector.main('new', '99')
        bisector.main('run', '-1')
        self.assertEqual(bisector.strategy.get_range(), (0, 49))

        bisector.main('old', '20')
        bisector.main('new', '40')
        bisector.main('run', '-1')
        self.assertEqual(bisector.strategy.get_range(), (20, 30))

        with self.assertRaises(errors.TooManyTemporaryErrors):
            bisector.main('skip', '20*10')
        with self.assertRaises(errors.TooManyTemporaryErrors):
            bisector.main('skip', '30*10')
        self.assertEqual(bisector.strategy.get_range(), (20, 30))

    def test_run_very_noisy(self):
        bisector = bisector_cli.BisectorCommandLine(
            DummyDomain, self.dut_manager
        )
        bisector.main(
            'init',
            '--num=100',
            '--old=0',
            '--new=99',
            '--noisy=old=3/10,new=7/10',
            '--endpoint-verification',
        )
        bisector.main('config', 'switch', 'true')
        bisector.main('config', 'eval', 'false')

        self.old_rate = 0.3
        self.new_rate = 0.7

        with mock.patch(
            'bisect_kit.bisector_cli.do_evaluate', self.do_noisy_evaluate
        ):
            bisector.main('run')
            self.assertEqual(bisector.strategy.get_best_guess(), 42)

    def test_run_noisy_many_times(self):
        """Test noisy cases with different ratio"""
        bisector = bisector_cli.BisectorCommandLine(
            DummyDomain, self.dut_manager
        )
        total_correct_count = 0
        total_wrong_count = 0

        cases: list[NoisyTestCaseType] = [
            {"rate": (0.1, 0.9), "times": 100, "expect_correct_rate": 0.99},
            {"rate": (0.2, 0.8), "times": 50, "expect_correct_rate": 0.99},
            {"rate": (0.3, 0.7), "times": 50, "expect_correct_rate": 0.8},
        ]
        correct_rate_list = []

        with mock.patch(
            'bisect_kit.bisector_cli.do_evaluate', self.do_noisy_evaluate
        ):
            for case in cases:
                self.old_rate, self.new_rate = case['rate']
                correct_count = 0
                wrong_count = 0
                noisy_flag = '--noisy=old=%d/10,new=%d/10' % (
                    self.old_rate * 10,
                    self.new_rate * 10,
                )

                for _ in range(case['times']):
                    try:
                        bisector.main(
                            'init',
                            '--num=100',
                            '--old=0',
                            '--new=99',
                            noisy_flag,
                            '--endpoint-verification',
                        )
                        bisector.main('config', 'switch', 'true')
                        bisector.main('config', 'eval', 'false')
                        bisector.main('run')

                        self.assertEqual(bisector.strategy.get_best_guess(), 42)
                        correct_count += 1

                    except errors.VerifyInitialRangeFailed:
                        wrong_count += 1

                correct_rate = correct_count / (correct_count + wrong_count)
                self.assertGreaterEqual(
                    correct_rate, case['expect_correct_rate']
                )

                correct_rate_list.append(correct_rate)
                total_correct_count += correct_count
                total_wrong_count += wrong_count

        total_correct_rate = total_correct_count / (
            total_correct_count + total_wrong_count
        )
        print('Correct rate of different test ratio: %s' % correct_rate_list)
        self.assertGreaterEqual(total_correct_rate, 0.95)

    def test_cmd_view(self):
        bisector = bisector_cli.BisectorCommandLine(
            DummyDomain, self.dut_manager
        )
        bisector.main('init', '--num=100', '--old=10', '--new=90')
        with mock.patch.object(
            DummyDomain, 'fill_candidate_summary'
        ) as mock_view:
            bisector.main('view')
            mock_view.assert_called()

    def test_cmd_config_clear_switch(self):
        bisector = bisector_cli.BisectorCommandLine(
            DummyDomain, self.dut_manager
        )
        bisector.main('init', '--num=10', '--old=0', '--new=9')
        bisector.main('config', 'switch', 'true')
        bisector.main('config', 'switch', 'true')
        bisector.main('config', 'eval', 'false')

        self.assertEqual(bisector.states.config['switch'], [['true'], ['true']])
        self.assertEqual(bisector.states.config['eval'], ['false'])

        # 'future_build' was not set previously. Make sure it doesn' break.
        bisector.main('config', 'clear', 'switch', 'eval', 'future_build')
        self.assertFalse('switch' in bisector.states.config)
        self.assertFalse('eval' in bisector.states.config)

        bisector.main('config', 'switch', 'true')
        self.assertEqual(bisector.states.config['switch'], [['true']])
        self.assertFalse('eval' in bisector.states.config)

    def test_cmd_config_confidence(self):
        bisector = bisector_cli.BisectorCommandLine(
            DummyDomain, self.dut_manager
        )
        bisector.main(
            'init', '--num=100', '--old=10', '--new=90', '--confidence=0.75'
        )

        with self.assertRaises(errors.ArgumentError):
            bisector.main('config', 'confidence', 'foo')
        with self.assertRaises(errors.ArgumentError):
            bisector.main('config', 'confidence', '0.9', '0.8')

        self.assertEqual(bisector.states.config['confidence'], 0.75)
        bisector.main('config', 'confidence', '0.875')
        self.assertEqual(bisector.states.config['confidence'], 0.875)

    def test_cmd_config_noisy(self):
        bisector = bisector_cli.BisectorCommandLine(
            DummyDomain, self.dut_manager
        )
        bisector.main(
            'init', '--num=100', '--old=10', '--new=90', '--noisy=new=9/10'
        )

        with self.assertRaises(errors.ArgumentError):
            bisector.main('config', 'noisy', 'hello', 'world')

        self.assertEqual(bisector.states.config['noisy'], 'new=9/10')
        bisector.main('config', 'noisy', 'old=1/10,new=8/9')
        self.assertEqual(bisector.states.config['noisy'], 'old=1/10,new=8/9')

        with contextlib.redirect_stdout(io.StringIO()):
            # Only make sure no exceptions. No output verification.
            bisector.main('view')

    def test_current_status(self):
        bisector = bisector_cli.BisectorCommandLine(
            DummyDomain, self.dut_manager
        )
        result = bisector.current_status()
        self.assertEqual(result.get('inited'), False)

        bisector.main(
            'init', '--num=100', '--old=10', '--new=90', '--noisy=new=9/10'
        )
        result = bisector.current_status()
        self.assertEqual(result.get('inited'), True)

    def test_future_switch_versions(self):
        bisector = bisector_cli.BisectorCommandLine(
            DummyDomain, self.dut_manager
        )
        bisector.main('init', '--num=100', '--old=0', '--new=99')
        bisector.main('view')  # init bisector.strategy

        self.assertEqual(bisector.future_switch_versions(None, 0), ['99'])
        self.assertEqual(bisector.future_switch_versions(None, 1), ['0', '99'])
        self.assertEqual(
            bisector.future_switch_versions(None, 2), ['0', '49', '99']
        )
        self.assertEqual(
            bisector.future_switch_versions(None, 3),
            ['0', '24', '49', '74', '99'],
        )

        # sample 1: 99 new
        sample1 = {
            'rev': '99',
            'index': 99,
            'status': 'new',
        }
        bisector.states.add_history('sample', **sample1)
        bisector.strategy.add_sample(99, **sample1)
        self.assertEqual(bisector.future_switch_versions('99', 0), ['0'])
        self.assertEqual(bisector.future_switch_versions('99', 1), ['0', '49'])
        self.assertEqual(
            bisector.future_switch_versions('99', 2), ['0', '24', '49', '74']
        )

        # sample 2: 0 new (unrepro)
        sample2 = {
            'rev': '0',
            'index': 0,
            'status': 'new',
        }
        with self.assertRaises(errors.VerifyOldBehaviorFailed):
            bisector.states.add_history('sample', **sample2)
            bisector.strategy.add_sample(0, **sample2)
            self.assertEqual(bisector.future_switch_versions('0', 0), [])

    def test_future_switch_versions_gemini(self):
        bisector = bisector_cli.BisectorCommandLine(
            DummyDomain, self.dut_manager
        )
        bisector.main('init', '--num=5', '--old=0', '--new=4')
        bisector.main('view')

        sample_old = {'rev': '0', 'index': 0, 'status': 'old'}
        sample_new = {'rev': '4', 'index': 4, 'status': 'new'}
        bisector.states.add_history('sample', **sample_old)
        bisector.strategy.add_sample(0, **sample_old)
        bisector.states.add_history('sample', **sample_new)
        bisector.strategy.add_sample(4, **sample_new)

        recorder = mock.MagicMock()
        mock_strategy = mock.MagicMock(spec=strategy.GeminiFusionStrategy)
        mock_strategy.is_done.return_value = False
        mock_strategy.gemini = mock.MagicMock()
        mock_strategy.next_idx.side_effect = lambda *a, **kw: (
            recorder(*a, **kw),
            [1, 2, 1, 3, 2][recorder.call_count - 1],
        )[1]
        bisector.strategy = mock_strategy

        # Depth 1 returns next revision and child predictions. With possible_status duplicated (4 trials), next_idx is called 5 times
        versions = bisector.future_switch_versions('0', 1)
        self.assertEqual(recorder.call_count, 5)
        self.assertEqual(sorted(versions), ['1', '2', '3'])


# Shorthand for testing.
SwitchAction = bisector_cli.SwitchAction
EvalAction = bisector_cli.EvalAction


@mock.patch('bisect_kit.common.config_logging', mock.Mock())
class TestSwitchAndEval(unittest.TestCase):
    """Test BisectorCommandLine._switch_and_eval()."""

    def setUp(self):
        super().setUp()
        self.mock_do_switch = self.enterContext(
            mock.patch.object(
                bisector_cli,
                'do_switch',
                autospec=True,
            )
        )
        self.mock_do_evaluate = self.enterContext(
            mock.patch.object(
                bisector_cli,
                'do_evaluate',
                autospec=True,
            )
        )
        self.mock_switch_eval_script_action = self.enterContext(
            mock.patch.object(
                bisector_cli,
                'switch_eval_script_action',
                autospec=True,
            )
        )
        self.mock_check_executable = self.enterContext(
            mock.patch.object(
                cli,
                'check_executable',
                autospec=True,
            )
        )
        # Always successful.
        self.mock_check_executable.return_value = None

        self.mock_dut_manager = mock.MagicMock()

    def run_test(
        self, bisector: bisector_cli.BisectorCommandLine, same_as_prev_rev=False
    ):
        # Workaround to initialize the instance so bisector._switch_and_eval()
        # can be run.
        bisector.domain = bisector.domain_cls(bisector.states.config)
        # pylint: disable=protected-access
        bisector.strategy = bisector._strategy_factory()
        # pylint: disable=protected-access
        return bisector._switch_and_eval('3', '3' if same_as_prev_rev else None)

    def test_no_dut_manager_switch_without_dut_eval_without_dut(self):
        bisector = bisector_cli.BisectorCommandLine(
            DummyDomain, dut_manager=None
        )
        bisector.main('init', '--num=10', '--old=0', '--new=9')
        bisector.main('config', 'switch', 'switch_cmd')
        bisector.main('config', 'eval', 'eval_cmd')

        # Success
        self.mock_do_switch.return_value = None
        self.mock_do_evaluate.return_value = core.StepResult('old')
        self.mock_switch_eval_script_action.side_effect = [
            SwitchAction.WITHOUT_DUT,
            EvalAction.WITHOUT_DUT,
        ]

        step, sample = self.run_test(bisector)

        self.assertEqual(step, 'eval')
        self.assertEqual(
            sample.keys(),
            set(
                [
                    'rev',
                    'index',
                    'status',
                    'switch_time',
                    'switch_without_dut_time',
                    'eval_time',
                ]
            ),
        )
        self.mock_do_switch.assert_called_once_with(
            ['switch_cmd'], bisector.domain, '3', mock.ANY, mock.ANY
        )
        self.mock_do_evaluate.assert_called_once_with(
            ['eval_cmd'], bisector.domain, '3', mock.ANY, mock.ANY, mock.ANY
        )

    def test_preset_dut_switch_with_dut_eval_with_dut(self):
        bisector = bisector_cli.BisectorCommandLine(
            DummyDomain, dut_manager=None
        )
        bisector.main(
            'init', '--num=10', '--old=0', '--new=9', '--dut=preset_dut'
        )
        bisector.main('config', 'switch', 'switch_cmd')
        bisector.main('config', 'eval', 'eval_cmd')

        # Success
        self.mock_do_switch.return_value = None
        self.mock_do_evaluate.return_value = core.StepResult('old')
        self.mock_switch_eval_script_action.side_effect = [
            SwitchAction.WITH_DUT,
            EvalAction.WITH_DUT,
        ]

        step, sample = self.run_test(bisector)

        self.assertEqual(step, 'eval')
        self.assertEqual(
            sample.keys(),
            set(
                [
                    'rev',
                    'index',
                    'status',
                    'switch_time',
                    'switch_with_dut_time',
                    'eval_time',
                ]
            ),
        )
        self.mock_do_switch.assert_called_once_with(
            ['switch_cmd', '--dut', 'preset_dut'],
            bisector.domain,
            '3',
            mock.ANY,
            mock.ANY,
        )
        self.mock_do_evaluate.assert_called_once_with(
            ['eval_cmd', 'preset_dut'],
            bisector.domain,
            '3',
            mock.ANY,
            mock.ANY,
            mock.ANY,
        )

    def test_preset_dut_multiple_switch_eval_with_dut(self):
        bisector = bisector_cli.BisectorCommandLine(
            DummyDomain, dut_manager=None
        )
        bisector.main(
            'init', '--num=10', '--old=0', '--new=9', '--dut=preset_dut'
        )
        bisector.main('config', 'switch', 'switch_without_dut_cmd')
        bisector.main('config', 'switch', 'switch_build_deploy_cmd')
        bisector.main('config', 'switch', 'switch_with_dut_cmd')
        bisector.main('config', 'eval', 'eval_cmd')

        # Success
        self.mock_do_switch.return_value = None
        self.mock_do_evaluate.return_value = core.StepResult('old')
        self.mock_switch_eval_script_action.side_effect = [
            SwitchAction.WITHOUT_DUT,
            SwitchAction.BUILD_AND_DEPLOY,
            SwitchAction.WITH_DUT,
            EvalAction.WITH_DUT,
        ]

        step, sample = self.run_test(bisector)

        self.assertEqual(step, 'eval')
        self.assertEqual(
            sample.keys(),
            set(
                [
                    'rev',
                    'index',
                    'status',
                    'switch_time',
                    'switch_without_dut_time',
                    'switch_build_time',
                    'switch_deploy_time',
                    'switch_with_dut_time',
                    'eval_time',
                ]
            ),
        )
        self.mock_do_switch.assert_has_calls(
            [
                mock.call(
                    ['switch_without_dut_cmd'],
                    bisector.domain,
                    '3',
                    mock.ANY,
                    mock.ANY,
                ),
                mock.call(
                    ['switch_build_deploy_cmd', '--no-deploy'],
                    bisector.domain,
                    '3',
                    mock.ANY,
                    mock.ANY,
                ),
                mock.call(
                    [
                        'switch_build_deploy_cmd',
                        '--dut',
                        'preset_dut',
                        '--deploy-only',
                    ],
                    bisector.domain,
                    '3',
                    mock.ANY,
                    mock.ANY,
                ),
                mock.call(
                    ['switch_with_dut_cmd', '--dut', 'preset_dut'],
                    bisector.domain,
                    '3',
                    mock.ANY,
                    mock.ANY,
                ),
            ]
        )
        self.mock_do_evaluate.assert_called_once_with(
            ['eval_cmd', 'preset_dut'],
            bisector.domain,
            '3',
            mock.ANY,
            mock.ANY,
            mock.ANY,
        )

    def test_preset_dut_multiple_switch_eval_without_dut(self):
        bisector = bisector_cli.BisectorCommandLine(
            DummyDomain, dut_manager=None
        )
        bisector.main(
            'init', '--num=10', '--old=0', '--new=9', '--dut=preset_dut'
        )
        bisector.main('config', 'switch', 'switch_without_dut_cmd')
        bisector.main('config', 'switch', 'switch_build_deploy_cmd')
        bisector.main('config', 'switch', 'switch_with_dut_cmd')
        bisector.main('config', 'eval', 'eval_cmd')

        # Success
        self.mock_do_switch.return_value = None
        self.mock_do_evaluate.return_value = core.StepResult('old')
        self.mock_switch_eval_script_action.side_effect = [
            SwitchAction.WITHOUT_DUT,
            SwitchAction.BUILD_AND_DEPLOY,
            SwitchAction.WITH_DUT,
            EvalAction.WITHOUT_DUT,
        ]

        step, sample = self.run_test(bisector)

        self.assertEqual(step, 'eval')
        self.assertEqual(
            sample.keys(),
            set(
                [
                    'rev',
                    'index',
                    'status',
                    'switch_time',
                    'switch_without_dut_time',
                    'switch_build_time',
                    'switch_deploy_time',
                    'switch_with_dut_time',
                    'eval_time',
                ]
            ),
        )
        self.mock_do_switch.assert_has_calls(
            [
                mock.call(
                    ['switch_without_dut_cmd'],
                    bisector.domain,
                    '3',
                    mock.ANY,
                    mock.ANY,
                ),
                mock.call(
                    ['switch_build_deploy_cmd', '--no-deploy'],
                    bisector.domain,
                    '3',
                    mock.ANY,
                    mock.ANY,
                ),
                mock.call(
                    [
                        'switch_build_deploy_cmd',
                        '--dut',
                        'preset_dut',
                        '--deploy-only',
                    ],
                    bisector.domain,
                    '3',
                    mock.ANY,
                    mock.ANY,
                ),
                mock.call(
                    ['switch_with_dut_cmd', '--dut', 'preset_dut'],
                    bisector.domain,
                    '3',
                    mock.ANY,
                    mock.ANY,
                ),
            ]
        )
        self.mock_do_evaluate.assert_called_once_with(
            ['eval_cmd'],
            bisector.domain,
            '3',
            mock.ANY,
            mock.ANY,
            mock.ANY,
        )

    def test_preset_dut_switch_without_dut_eval_with_dut(self):
        bisector = bisector_cli.BisectorCommandLine(
            DummyDomain, dut_manager=self.mock_dut_manager
        )
        bisector.main(
            'init', '--num=10', '--old=0', '--new=9', '--dut=preset_dut'
        )
        bisector.main('config', 'switch', 'switch_cmd')
        bisector.main('config', 'eval', 'eval_cmd')

        # Success
        self.mock_do_switch.return_value = None
        self.mock_do_evaluate.return_value = core.StepResult('old')
        self.mock_switch_eval_script_action.side_effect = [
            SwitchAction.WITHOUT_DUT,
            EvalAction.WITH_DUT,
        ]

        step, sample = self.run_test(bisector)

        self.assertEqual(step, 'eval')
        self.assertEqual(
            sample.keys(),
            set(
                [
                    'rev',
                    'index',
                    'status',
                    'switch_time',
                    'switch_without_dut_time',
                    'eval_time',
                ]
            ),
        )
        self.mock_do_switch.assert_called_once_with(
            ['switch_cmd'], bisector.domain, '3', mock.ANY, mock.ANY
        )
        self.mock_do_evaluate.assert_called_once_with(
            ['eval_cmd', 'preset_dut'],
            bisector.domain,
            '3',
            mock.ANY,
            mock.ANY,
            mock.ANY,
        )

    def test_preset_dut_same_as_prev_rev(self):
        bisector = bisector_cli.BisectorCommandLine(
            DummyDomain, dut_manager=None
        )
        bisector.main(
            'init', '--num=10', '--old=0', '--new=9', '--dut=preset_dut'
        )
        bisector.main('config', 'switch', 'switch_without_dut_cmd')
        bisector.main('config', 'switch', 'switch_build_deploy_cmd')
        bisector.main('config', 'switch', 'switch_with_dut_cmd')
        bisector.main('config', 'eval', 'eval_cmd')

        # Success
        self.mock_do_switch.return_value = None
        self.mock_do_evaluate.return_value = core.StepResult('old')
        self.mock_switch_eval_script_action.side_effect = [
            SwitchAction.WITHOUT_DUT,
            SwitchAction.BUILD_AND_DEPLOY,
            SwitchAction.WITH_DUT,
            EvalAction.WITH_DUT,
        ]

        step, sample = self.run_test(bisector, same_as_prev_rev=True)

        self.assertEqual(step, 'eval')
        self.assertEqual(
            sample.keys(),
            set(
                [
                    'rev',
                    'index',
                    'status',
                    'switch_time',
                    'switch_without_dut_time',
                    'eval_time',
                ]
            ),
        )
        # Since prev_rev == rev, no need to build and deploy again.
        self.mock_do_switch.assert_called_once_with(
            ['switch_without_dut_cmd'],
            bisector.domain,
            '3',
            mock.ANY,
            mock.ANY,
        )
        self.mock_do_evaluate.assert_called_once_with(
            ['eval_cmd', 'preset_dut'],
            bisector.domain,
            '3',
            mock.ANY,
            mock.ANY,
            mock.ANY,
        )

    def test_dut_manager_switch_with_dut_eval_with_dut(self):
        bisector = bisector_cli.BisectorCommandLine(
            DummyDomain, dut_manager=self.mock_dut_manager
        )
        bisector.main('init', '--num=10', '--old=0', '--new=9')
        bisector.main('config', 'switch', 'switch_cmd')
        bisector.main('config', 'eval', 'eval_cmd')

        # Success
        self.mock_do_switch.return_value = None
        self.mock_do_evaluate.return_value = core.StepResult('old')
        self.mock_dut_manager.provision.return_value.__enter__.return_value = (
            'dut'
        )
        self.mock_switch_eval_script_action.side_effect = [
            SwitchAction.WITH_DUT,
            EvalAction.WITH_DUT,
        ]

        step, sample = self.run_test(bisector)

        self.assertEqual(step, 'eval')
        self.assertEqual(
            sample.keys(),
            set(
                [
                    'rev',
                    'index',
                    'status',
                    'switch_time',
                    'switch_with_dut_time',
                    'eval_time',
                ]
            ),
        )
        self.mock_do_switch.assert_called_once_with(
            ['switch_cmd', '--dut', 'dut'],
            bisector.domain,
            '3',
            mock.ANY,
            mock.ANY,
        )
        self.mock_do_evaluate.assert_called_once_with(
            ['eval_cmd', 'dut'],
            bisector.domain,
            '3',
            mock.ANY,
            mock.ANY,
            mock.ANY,
        )

    def test_dut_manager_multiple_switch_eval_with_dut(self):
        bisector = bisector_cli.BisectorCommandLine(
            DummyDomain, dut_manager=self.mock_dut_manager
        )
        bisector.main('init', '--num=10', '--old=0', '--new=9')
        bisector.main('config', 'switch', 'switch_without_dut_cmd')
        bisector.main('config', 'switch', 'switch_build_deploy_cmd')
        bisector.main('config', 'switch', 'switch_with_dut_cmd')
        bisector.main('config', 'eval', 'eval_cmd')

        # Success
        self.mock_do_switch.return_value = None
        self.mock_do_evaluate.return_value = core.StepResult('old')
        self.mock_dut_manager.provision.return_value.__enter__.return_value = (
            'dut'
        )
        self.mock_switch_eval_script_action.side_effect = [
            SwitchAction.WITHOUT_DUT,
            SwitchAction.BUILD_AND_DEPLOY,
            SwitchAction.WITH_DUT,
            EvalAction.WITH_DUT,
        ]

        step, sample = self.run_test(bisector)

        self.assertEqual(step, 'eval')
        self.assertEqual(
            sample.keys(),
            set(
                [
                    'rev',
                    'index',
                    'status',
                    'switch_time',
                    'switch_without_dut_time',
                    'switch_build_time',
                    'switch_deploy_time',
                    'switch_with_dut_time',
                    'eval_time',
                ]
            ),
        )
        self.mock_do_switch.assert_has_calls(
            [
                mock.call(
                    ['switch_without_dut_cmd'],
                    bisector.domain,
                    '3',
                    mock.ANY,
                    mock.ANY,
                ),
                mock.call(
                    ['switch_build_deploy_cmd', '--no-deploy'],
                    bisector.domain,
                    '3',
                    mock.ANY,
                    mock.ANY,
                ),
                mock.call(
                    [
                        'switch_build_deploy_cmd',
                        '--dut',
                        'dut',
                        '--deploy-only',
                    ],
                    bisector.domain,
                    '3',
                    mock.ANY,
                    mock.ANY,
                ),
                mock.call(
                    ['switch_with_dut_cmd', '--dut', 'dut'],
                    bisector.domain,
                    '3',
                    mock.ANY,
                    mock.ANY,
                ),
            ]
        )
        self.mock_do_evaluate.assert_called_once_with(
            ['eval_cmd', 'dut'],
            bisector.domain,
            '3',
            mock.ANY,
            mock.ANY,
            mock.ANY,
        )

    def test_dut_manager_multiple_switch_eval_without_dut(self):
        bisector = bisector_cli.BisectorCommandLine(
            DummyDomain, dut_manager=self.mock_dut_manager
        )
        bisector.main('init', '--num=10', '--old=0', '--new=9')
        bisector.main('config', 'switch', 'switch_without_dut_cmd')
        bisector.main('config', 'switch', 'switch_build_deploy_cmd')
        bisector.main('config', 'switch', 'switch_with_dut_cmd')
        bisector.main('config', 'eval', 'eval_cmd')

        # Success
        self.mock_do_switch.return_value = None
        self.mock_do_evaluate.return_value = core.StepResult('old')
        self.mock_dut_manager.provision.return_value.__enter__.return_value = (
            'dut'
        )
        self.mock_switch_eval_script_action.side_effect = [
            SwitchAction.WITHOUT_DUT,
            SwitchAction.BUILD_AND_DEPLOY,
            SwitchAction.WITH_DUT,
            EvalAction.WITHOUT_DUT,
        ]

        step, sample = self.run_test(bisector)

        self.assertEqual(step, 'eval')
        self.assertEqual(
            sample.keys(),
            set(
                [
                    'rev',
                    'index',
                    'status',
                    'switch_time',
                    'switch_without_dut_time',
                    'switch_build_time',
                    'switch_deploy_time',
                    'switch_with_dut_time',
                    'eval_time',
                ]
            ),
        )
        self.mock_do_switch.assert_has_calls(
            [
                mock.call(
                    ['switch_without_dut_cmd'],
                    bisector.domain,
                    '3',
                    mock.ANY,
                    mock.ANY,
                ),
                mock.call(
                    ['switch_build_deploy_cmd', '--no-deploy'],
                    bisector.domain,
                    '3',
                    mock.ANY,
                    mock.ANY,
                ),
                mock.call(
                    [
                        'switch_build_deploy_cmd',
                        '--dut',
                        'dut',
                        '--deploy-only',
                    ],
                    bisector.domain,
                    '3',
                    mock.ANY,
                    mock.ANY,
                ),
                mock.call(
                    ['switch_with_dut_cmd', '--dut', 'dut'],
                    bisector.domain,
                    '3',
                    mock.ANY,
                    mock.ANY,
                ),
            ]
        )
        self.mock_do_evaluate.assert_called_once_with(
            ['eval_cmd'], bisector.domain, '3', mock.ANY, mock.ANY, mock.ANY
        )

    def test_dut_manager_switch_without_dut_eval_with_dut(self):
        bisector = bisector_cli.BisectorCommandLine(
            DummyDomain, dut_manager=self.mock_dut_manager
        )
        bisector.main('init', '--num=10', '--old=0', '--new=9')
        bisector.main('config', 'switch', 'switch_cmd')
        bisector.main('config', 'eval', 'eval_cmd')

        # Success
        self.mock_do_switch.return_value = None
        self.mock_do_evaluate.return_value = core.StepResult('old')
        self.mock_dut_manager.provision.return_value.__enter__.return_value = (
            'dut'
        )
        self.mock_switch_eval_script_action.side_effect = [
            SwitchAction.WITHOUT_DUT,
            EvalAction.WITH_DUT,
        ]

        step, sample = self.run_test(bisector)

        self.assertEqual(step, 'eval')
        self.assertEqual(
            sample.keys(),
            set(
                [
                    'rev',
                    'index',
                    'status',
                    'switch_time',
                    'switch_without_dut_time',
                    'eval_time',
                ]
            ),
        )
        self.mock_do_switch.assert_called_once_with(
            ['switch_cmd'], bisector.domain, '3', mock.ANY, mock.ANY
        )
        self.mock_do_evaluate.assert_called_once_with(
            ['eval_cmd', 'dut'],
            bisector.domain,
            '3',
            mock.ANY,
            mock.ANY,
            mock.ANY,
        )

    def test_dut_manager_same_as_prev_rev_is_previous_dut(self):
        bisector = bisector_cli.BisectorCommandLine(
            DummyDomain, dut_manager=self.mock_dut_manager
        )
        bisector.main('init', '--num=10', '--old=0', '--new=9')
        bisector.main('config', 'switch', 'switch_without_dut_cmd')
        bisector.main('config', 'switch', 'switch_build_deploy_cmd')
        bisector.main('config', 'switch', 'switch_with_dut_cmd')
        bisector.main('config', 'eval', 'eval_cmd')

        # Success
        self.mock_do_switch.return_value = None
        self.mock_do_evaluate.return_value = core.StepResult('old')
        self.mock_dut_manager.provision.return_value.__enter__.return_value = (
            'dut'
        )
        self.mock_dut_manager.is_previous_dut.return_value = True
        self.mock_switch_eval_script_action.side_effect = [
            SwitchAction.WITHOUT_DUT,
            SwitchAction.BUILD_AND_DEPLOY,
            SwitchAction.WITH_DUT,
            EvalAction.WITH_DUT,
        ]

        step, sample = self.run_test(bisector, same_as_prev_rev=True)

        self.assertEqual(step, 'eval')
        self.assertEqual(
            sample.keys(),
            set(
                [
                    'rev',
                    'index',
                    'status',
                    'switch_time',
                    'switch_without_dut_time',
                    'eval_time',
                ]
            ),
        )
        # No need to build and deploy since it is same_as_prev_rev and
        # is_previous_dut.
        self.mock_do_switch.assert_called_once_with(
            ['switch_without_dut_cmd'],
            bisector.domain,
            '3',
            mock.ANY,
            mock.ANY,
        )
        self.mock_do_evaluate.assert_called_once_with(
            ['eval_cmd', 'dut'],
            bisector.domain,
            '3',
            mock.ANY,
            mock.ANY,
            mock.ANY,
        )

    def test_dut_manager_same_as_prev_rev_not_previous_dut(self):
        bisector = bisector_cli.BisectorCommandLine(
            DummyDomain, dut_manager=self.mock_dut_manager
        )
        bisector.main('init', '--num=10', '--old=0', '--new=9')
        bisector.main('config', 'switch', 'switch_without_dut_cmd')
        bisector.main('config', 'switch', 'switch_build_deploy_cmd')
        bisector.main('config', 'switch', 'switch_with_dut_cmd')
        bisector.main('config', 'eval', 'eval_cmd')

        # Success
        self.mock_do_switch.return_value = None
        self.mock_do_evaluate.return_value = core.StepResult('old')
        self.mock_dut_manager.provision.return_value.__enter__.return_value = (
            'dut'
        )
        self.mock_dut_manager.is_previous_dut.return_value = False
        self.mock_switch_eval_script_action.side_effect = [
            SwitchAction.WITHOUT_DUT,
            SwitchAction.BUILD_AND_DEPLOY,
            SwitchAction.WITH_DUT,
            EvalAction.WITH_DUT,
        ]

        step, sample = self.run_test(bisector, same_as_prev_rev=True)

        self.assertEqual(step, 'eval')
        self.assertEqual(
            sample.keys(),
            set(
                [
                    'rev',
                    'index',
                    'status',
                    'switch_time',
                    'switch_without_dut_time',
                    'switch_deploy_time',
                    'switch_with_dut_time',
                    'eval_time',
                ]
            ),
        )
        # No need to build since it is same_as_prev_rev. But deploy is needed
        # because is_previous_dut is False.
        self.mock_do_switch.assert_has_calls(
            [
                mock.call(
                    ['switch_without_dut_cmd'],
                    bisector.domain,
                    '3',
                    mock.ANY,
                    mock.ANY,
                ),
                mock.call(
                    [
                        'switch_build_deploy_cmd',
                        '--dut',
                        'dut',
                        '--deploy-only',
                    ],
                    bisector.domain,
                    '3',
                    mock.ANY,
                    mock.ANY,
                ),
                mock.call(
                    ['switch_with_dut_cmd', '--dut', 'dut'],
                    bisector.domain,
                    '3',
                    mock.ANY,
                    mock.ANY,
                ),
            ]
        )
        self.mock_do_evaluate.assert_called_once_with(
            ['eval_cmd', 'dut'],
            bisector.domain,
            '3',
            mock.ANY,
            mock.ANY,
            mock.ANY,
        )

    def test_no_dut_anager_switch_without_dut_eval_with_dut(self):
        bisector = bisector_cli.BisectorCommandLine(
            DummyDomain, dut_manager=None
        )
        bisector.main('init', '--num=10', '--old=0', '--new=9')
        bisector.main('config', 'switch', 'switch_cmd')
        bisector.main('config', 'eval', 'eval_cmd')

        # Success
        self.mock_do_switch.return_value = None
        self.mock_switch_eval_script_action.side_effect = [
            SwitchAction.WITHOUT_DUT,
            EvalAction.WITH_DUT,
        ]

        # dut_manager == None
        with self.assertRaises(AssertionError):
            self.run_test(bisector)

        self.mock_do_switch.assert_called_once_with(
            ['switch_cmd'], bisector.domain, '3', mock.ANY, mock.ANY
        )

    def test_no_dut_anager_switch_with_dut_eval_without_dut(self):
        bisector = bisector_cli.BisectorCommandLine(
            DummyDomain, dut_manager=None
        )
        bisector.main('init', '--num=10', '--old=0', '--new=9')
        bisector.main('config', 'switch', 'switch_cmd')
        bisector.main('config', 'eval', 'eval_cmd')

        self.mock_switch_eval_script_action.side_effect = [
            SwitchAction.WITH_DUT,
            EvalAction.WITHOUT_DUT,
        ]

        # dut_manager == None
        with self.assertRaises(AssertionError):
            self.run_test(bisector)

    def test_dut_manager_no_eval_script(self):
        bisector = bisector_cli.BisectorCommandLine(
            DummyDomain, dut_manager=self.mock_dut_manager
        )
        bisector.main('init', '--num=10', '--old=0', '--new=9')
        bisector.main('config', 'switch', 'switch_cmd')

        # Success
        self.mock_switch_eval_script_action.return_value = SwitchAction.WITH_DUT

        with self.assertRaises(AssertionError):
            self.run_test(bisector)

    def test_no_dut_manager_switch_failed(self):
        bisector = bisector_cli.BisectorCommandLine(
            DummyDomain, dut_manager=None
        )
        bisector.main('init', '--num=10', '--old=0', '--new=9')
        bisector.main('config', 'switch', 'switch_cmd')
        bisector.main('config', 'eval', 'eval_cmd')

        # Success
        self.mock_do_switch.return_value = core.StepResult('fatal')
        self.mock_switch_eval_script_action.side_effect = [
            SwitchAction.WITHOUT_DUT,
            EvalAction.WITHOUT_DUT,
        ]

        step, sample = self.run_test(bisector)

        self.assertEqual(step, 'switch')
        self.assertEqual(
            sample.keys(),
            set(
                [
                    'rev',
                    'index',
                    'status',
                    'switch_time',
                    'switch_without_dut_time',
                ]
            ),
        )
        self.mock_do_switch.assert_called_once_with(
            ['switch_cmd'], bisector.domain, '3', mock.ANY, mock.ANY
        )

    def test_dut_manager_switch_failed(self):
        bisector = bisector_cli.BisectorCommandLine(
            DummyDomain, dut_manager=self.mock_dut_manager
        )
        bisector.main('init', '--num=10', '--old=0', '--new=9')
        bisector.main('config', 'switch', 'switch_cmd')
        bisector.main('config', 'eval', 'eval_cmd')

        # Success
        self.mock_do_switch.return_value = core.StepResult('fatal')
        self.mock_dut_manager.provision.return_value.__enter__.return_value = (
            'dut'
        )
        self.mock_switch_eval_script_action.side_effect = [
            SwitchAction.WITH_DUT,
            EvalAction.WITHOUT_DUT,
        ]

        step, sample = self.run_test(bisector)

        self.assertEqual(step, 'switch')
        self.assertEqual(
            sample.keys(),
            set(
                [
                    'rev',
                    'index',
                    'status',
                    'switch_time',
                    'switch_with_dut_time',
                ]
            ),
        )
        self.mock_do_switch.assert_called_once_with(
            ['switch_cmd', '--dut', 'dut'],
            bisector.domain,
            '3',
            mock.ANY,
            mock.ANY,
        )

    def test_dut_manager_switch_failed_on_deploy(self):
        bisector = bisector_cli.BisectorCommandLine(
            DummyDomain, dut_manager=self.mock_dut_manager
        )
        bisector.main('init', '--num=10', '--old=0', '--new=9')
        bisector.main('config', 'switch', 'switch_cmd')
        bisector.main('config', 'eval', 'eval_cmd')

        # Success
        self.mock_do_switch.side_effect = [
            None,
            core.StepResult('fatal'),
        ]
        self.mock_dut_manager.dut = None
        self.mock_dut_manager.provision.return_value.__enter__.return_value = (
            'dut'
        )
        self.mock_switch_eval_script_action.side_effect = [
            SwitchAction.BUILD_AND_DEPLOY,
            EvalAction.WITHOUT_DUT,
        ]

        step, sample = self.run_test(bisector)

        self.assertEqual(step, 'switch')
        self.assertEqual(
            sample.keys(),
            set(
                [
                    'rev',
                    'index',
                    'status',
                    'switch_time',
                    'switch_build_time',
                    'switch_deploy_time',
                ]
            ),
        )
        self.mock_do_switch.assert_has_calls(
            [
                mock.call(
                    ['switch_cmd', '--no-deploy'],
                    bisector.domain,
                    '3',
                    mock.ANY,
                    mock.ANY,
                ),
                mock.call(
                    [
                        'switch_cmd',
                        '--dut',
                        'dut',
                        '--deploy-only',
                    ],
                    bisector.domain,
                    '3',
                    mock.ANY,
                    mock.ANY,
                ),
            ]
        )


if __name__ == '__main__':
    unittest.main()
