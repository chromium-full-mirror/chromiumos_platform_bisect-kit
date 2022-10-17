# -*- coding: utf-8 -*-
# Copyright 2019 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Test bisector_cli module."""

from __future__ import print_function

import io
import unittest
from unittest import mock

from bisect_kit import bisector_cli
from bisect_kit import cli
from bisect_kit import common
from bisect_kit import core
from bisect_kit import errors
from bisect_kit import testing


class DummyDomain(core.BisectDomain):
  """Dummy subclass of BisectDomain."""
  revtype = staticmethod(cli.argtype_notempty)

  @staticmethod
  def add_init_arguments(parser):
    parser.add_argument('--num', required=True, type=int)
    parser.add_argument('--ans', type=int)
    parser.add_argument('--old_p', type=float, default=0.0)
    parser.add_argument('--new_p', type=float, default=1.0)

  @staticmethod
  def init(opts):
    config = dict(
        ans=opts.ans,
        old_p=opts.old_p,
        new_p=opts.new_p,
        old=opts.old,
        new=opts.new)

    revlist = [str(i) for i in range(opts.num)]
    return config, {'revlist': revlist}

  def __init__(self, config):
    self.config = config

  def setenv(self, env, rev):
    env['ANS'] = str(self.config['ans'])
    env['OLD_P'] = str(self.config['old_p'])
    env['NEW_P'] = str(self.config['new_p'])
    env['REV'] = rev


class TestBisectorCli(unittest.TestCase):
  """Test bisector_cli functions."""

  def test_collect_bisect_result_values(self):
    # pylint: disable=protected-access
    values = []
    bisector_cli._collect_bisect_result_values(values, '')
    self.assertEqual(values, [])

    values = []
    bisector_cli._collect_bisect_result_values(values, 'foo\n')
    bisector_cli._collect_bisect_result_values(values, 'bar\n')
    self.assertEqual(values, [])

    values = []
    bisector_cli._collect_bisect_result_values(values,
                                               'BISECT_RESULT_VALUES=1 2\n')
    bisector_cli._collect_bisect_result_values(values,
                                               'fooBISECT_RESULT_VALUES=3 4\n')
    bisector_cli._collect_bisect_result_values(values,
                                               'BISECT_RESULT_VALUES=5\n')
    self.assertEqual(values, [1, 2, 5])

    with self.assertRaises(errors.InternalError):
      bisector_cli._collect_bisect_result_values(
          values, 'BISECT_RESULT_VALUES=hello\n')


@mock.patch('bisect_kit.common.config_logging', mock.Mock())
class TestBisectorCommandLine(unittest.TestCase):
  """Test bisector_cli.BisectorCommandLine class."""

  def setUp(self):
    self.session_base_patcher = testing.SessionBasePatcher()
    self.session_base_patcher.patch()
    self.random_list = testing.RandomNum()
    self.old_rate = None
    self.new_rate = None

  def tearDown(self):
    self.session_base_patcher.reset()

  def test_run_true(self):
    bisector = bisector_cli.BisectorCommandLine(DummyDomain)
    bisector.main('init', '--num=10', '--old=0', '--new=9')
    bisector.main('config', 'switch', 'true')
    bisector.main('config', 'eval', 'true')
    with self.assertRaises(errors.VerificationFailed):
      bisector.main('run')

    result = bisector.current_status()
    self.assertEqual(result.get('inited'), True)
    self.assertEqual(result.get('verified'), False)

  def test_run_false(self):
    bisector = bisector_cli.BisectorCommandLine(DummyDomain)
    bisector.main('init', '--num=10', '--old=0', '--new=9')
    bisector.main('config', 'switch', 'true')
    bisector.main('config', 'eval', 'false')
    with self.assertRaises(errors.VerificationFailed):
      bisector.main('run')

    result = bisector.current_status()
    self.assertEqual(result.get('inited'), True)
    self.assertEqual(result.get('verified'), False)

  def test_run_true_with_endpoint_verification(self):
    bisector = bisector_cli.BisectorCommandLine(DummyDomain)
    bisector.main('init', '--num=10', '--old=0', '--new=9',
                  '--endpoint_verification')
    bisector.main('config', 'switch', 'true')
    bisector.main('config', 'eval', 'true')
    with self.assertRaises(errors.VerificationFailed):
      bisector.main('run')
    result = bisector.current_status()
    self.assertEqual(result.get('inited'), True)
    self.assertEqual(result.get('verified'), False)

  def test_run_false_with_endpoint_verification(self):
    bisector = bisector_cli.BisectorCommandLine(DummyDomain)
    bisector.main('init', '--num=10', '--old=0', '--new=9',
                  '--endpoint_verification')
    bisector.main('config', 'switch', 'true')
    bisector.main('config', 'eval', 'false')
    with self.assertRaises(errors.VerificationFailed):
      bisector.main('run')
    result = bisector.current_status()
    self.assertEqual(result.get('inited'), True)
    self.assertEqual(result.get('verified'), False)

  def test_simple(self):
    bisector = bisector_cli.BisectorCommandLine(DummyDomain)
    bisector.main('init', '--num=20', '--old=3', '--new=15')
    bisector.main('config', 'switch', 'true')
    bisector.main('config', 'eval', 'sh', '-c', '[ "$BISECT_REV" -lt 7 ]')
    bisector.main('run')
    self.assertEqual(bisector.strategy.get_best_guess(), 7)

    with mock.patch('sys.stdout', new_callable=io.StringIO):
      # Only make sure no exceptions. No output verification.
      bisector.main('log')

  def test_switch_fail(self):
    bisector = bisector_cli.BisectorCommandLine(DummyDomain)
    bisector.main('init', '--num=20', '--old=3', '--new=15')
    bisector.main('config', 'switch', 'false')
    bisector.main('config', 'eval', 'sh', '-c', '[ "$BISECT_REV" -lt 7 ]')
    with self.assertRaises(errors.UnableToProceed):
      bisector.main('run')

    result = bisector.current_status()
    self.assertEqual(result.get('inited'), True)
    self.assertEqual(result.get('verified'), False)

  def do_evaluate(self, _cmd, _domain, rev, _log_file, capture_values=False):
    del capture_values  # unused
    if int(rev) < 42:
      return core.StepResult('old')
    return core.StepResult('new')

  def do_noisy_evaluate(self,
                        _cmd,
                        _domain,
                        rev,
                        _log_file,
                        capture_values=False):
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
    bisector = bisector_cli.BisectorCommandLine(DummyDomain)
    bisector.main('init', '--num=200', '--old=0', '--new=99')
    bisector.main('config', 'switch', 'true')
    bisector.main('config', 'eval', 'false')

    with mock.patch('bisect_kit.bisector_cli.do_evaluate', self.do_evaluate):
      # two verify
      with mock.patch('sys.stdout', new_callable=io.StringIO) as mock_stdout:
        bisector.main('next')
        self.assertEqual(mock_stdout.getvalue(), '99\n')

      bisector.main('run', '-1')
      with mock.patch('sys.stdout', new_callable=io.StringIO) as mock_stdout:
        bisector.main('next')
        self.assertEqual(mock_stdout.getvalue(), '0\n')

      bisector.main('run', '-1')
      self.assertEqual(bisector.strategy.get_range(), (0, 99))
      bisector.main('run', '-1')
      self.assertIn(bisector.strategy.get_range(), {(0, 49), (0, 50)})

      bisector.main('run')
      self.assertEqual(bisector.strategy.get_best_guess(), 42)
    with mock.patch('sys.stdout', new_callable=io.StringIO) as mock_stdout:
      bisector.main('next')
      self.assertEqual(mock_stdout.getvalue(), 'done\n')

  def test_run_classic_with_endpoint_verification(self):
    bisector = bisector_cli.BisectorCommandLine(DummyDomain)
    bisector.main('init', '--num=200', '--old=0', '--new=99',
                  '--endpoint_verification')
    bisector.main('config', 'switch', 'true')
    bisector.main('config', 'eval', 'false')

    with mock.patch('bisect_kit.bisector_cli.do_evaluate', self.do_evaluate):
      with mock.patch('sys.stdout', new_callable=io.StringIO) as mock_stdout:
        bisector.main('next')
        self.assertEqual(mock_stdout.getvalue(), '99\n')

      bisector.main('run', '-1')
      with mock.patch('sys.stdout', new_callable=io.StringIO) as mock_stdout:
        bisector.main('next')
        self.assertEqual(mock_stdout.getvalue(), '99\n')
      self.assertEqual(bisector.strategy.get_range(), (0, 99))

      bisector.main('run')
      self.assertEqual(bisector.strategy.get_best_guess(), 42)
    with mock.patch('sys.stdout', new_callable=io.StringIO) as mock_stdout:
      bisector.main('next')
      self.assertEqual(mock_stdout.getvalue(), 'done\n')

  def test_run_noisy(self):
    bisector = bisector_cli.BisectorCommandLine(DummyDomain)
    bisector.main('init', '--num=100', '--old=0', '--new=99',
                  '--noisy=old=1/10,new=9/10')
    bisector.main('config', 'switch', 'true')
    bisector.main('config', 'eval', 'false')

    self.old_rate = 0.1
    self.new_rate = 0.9

    with mock.patch('bisect_kit.bisector_cli.do_evaluate',
                    self.do_noisy_evaluate):
      bisector.main('run')
      self.assertEqual(bisector.strategy.get_best_guess(), 42)

    with mock.patch('sys.stdout', new_callable=io.StringIO) as mock_stdout:
      bisector.main('next')
      # There might be small math error near the boundary of confidence.
      self.assertIn(mock_stdout.getvalue(), ['done\n', '41\n', '42\n'])

  def test_run_noisy_with_endpoint_verification(self):
    bisector = bisector_cli.BisectorCommandLine(DummyDomain)
    bisector.main('init', '--num=100', '--old=0', '--new=99',
                  '--noisy=old=1/10,new=9/10', '--endpoint_verification')
    bisector.main('config', 'switch', 'true')
    bisector.main('config', 'eval', 'false')

    self.old_rate = 0.1
    self.new_rate = 0.9

    with mock.patch('bisect_kit.bisector_cli.do_evaluate',
                    self.do_noisy_evaluate):
      bisector.main('run')
      self.assertEqual(bisector.strategy.get_best_guess(), 42)

    with mock.patch('sys.stdout', new_callable=io.StringIO) as mock_stdout:
      bisector.main('next')
      self.assertIn(mock_stdout.getvalue(), ['done\n', '41\n', '42\n'])

  def test_cmd_old_and_new(self):
    """Tests cmd_old and cmd_new"""
    bisector = bisector_cli.BisectorCommandLine(DummyDomain)
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

    with self.assertRaises(errors.UnableToProceed):
      bisector.main('skip', '20*10')
    with self.assertRaises(errors.UnableToProceed):
      bisector.main('skip', '30*10')
    self.assertEqual(bisector.strategy.get_range(), (20, 30))

  def test_run_very_noisy(self):
    bisector = bisector_cli.BisectorCommandLine(DummyDomain)
    bisector.main('init', '--num=100', '--old=0', '--new=99',
                  '--noisy=old=3/10,new=7/10', '--endpoint_verification')
    bisector.main('config', 'switch', 'true')
    bisector.main('config', 'eval', 'false')

    self.old_rate = 0.3
    self.new_rate = 0.7

    with mock.patch('bisect_kit.bisector_cli.do_evaluate',
                    self.do_noisy_evaluate):
      bisector.main('run')
      self.assertEqual(bisector.strategy.get_best_guess(), 42)

  def test_run_noisy_many_times(self):
    """Test noisy cases with different ratio"""
    bisector = bisector_cli.BisectorCommandLine(DummyDomain)
    total_correct_count = 0
    total_wrong_count = 0

    cases = [
        dict(rate=(0.1, 0.9), times=100, expect_correct_rate=0.99),
        dict(rate=(0.2, 0.8), times=50, expect_correct_rate=0.99),
        dict(rate=(0.3, 0.7), times=50, expect_correct_rate=0.8),
    ]
    correct_rate_list = []

    with mock.patch('bisect_kit.bisector_cli.do_evaluate',
                    self.do_noisy_evaluate):
      for case in cases:
        self.old_rate, self.new_rate = case['rate']
        correct_count = 0
        wrong_count = 0
        noisy_flag = '--noisy=old=%d/10,new=%d/10' % (self.old_rate * 10,
                                                      self.new_rate * 10)

        for _ in range(case['times']):
          try:
            bisector.main('init', '--num=100', '--old=0', '--new=99',
                          noisy_flag, '--endpoint_verification')
            bisector.main('config', 'switch', 'true')
            bisector.main('config', 'eval', 'false')
            bisector.main('run')

            self.assertEqual(bisector.strategy.get_best_guess(), 42)
            correct_count += 1

          except errors.VerifyInitialRangeFailed:
            wrong_count += 1

        correct_rate = correct_count / (correct_count + wrong_count)
        self.assertGreaterEqual(correct_rate, case['expect_correct_rate'])

        correct_rate_list.append(correct_rate)
        total_correct_count += correct_count
        total_wrong_count += wrong_count

    total_correct_rate = total_correct_count / (
        total_correct_count + total_wrong_count)
    print('Correct rate of different test ratio: %s' % correct_rate_list)
    self.assertGreaterEqual(total_correct_rate, 0.95)

  def test_cmd_switch(self):
    """Test cmd_switch"""
    bisector = bisector_cli.BisectorCommandLine(DummyDomain)
    bisector.main('init', '--num=100', '--old=0', '--new=99')
    bisector.main('config', 'switch', 'true')
    bisector.main('config', 'eval', 'false')

    switched = []

    def do_switch(_cmd, _domain, rev, _log_file):
      switched.append(rev)

    with mock.patch('bisect_kit.bisector_cli.do_switch', do_switch):
      bisector.main('switch', 'next')
      bisector.main('switch', '3')
      bisector.main('switch', '5')
      bisector.main('switch', '4')
      self.assertEqual(switched, ['99', '3', '5', '4'])

  def test_cmd_view(self):
    bisector = bisector_cli.BisectorCommandLine(DummyDomain)
    bisector.main('init', '--num=100', '--old=10', '--new=90')
    with mock.patch.object(DummyDomain, 'fill_candidate_summary') as mock_view:
      bisector.main('view')
      mock_view.assert_called()

  def test_cmd_config_confidence(self):
    bisector = bisector_cli.BisectorCommandLine(DummyDomain)
    bisector.main('init', '--num=100', '--old=10', '--new=90',
                  '--confidence=0.75')

    with self.assertRaises(errors.ArgumentError):
      bisector.main('config', 'confidence', 'foo')
    with self.assertRaises(errors.ArgumentError):
      bisector.main('config', 'confidence', '0.9', '0.8')

    self.assertEqual(bisector.states.config['confidence'], 0.75)
    bisector.main('config', 'confidence', '0.875')
    self.assertEqual(bisector.states.config['confidence'], 0.875)

  def test_cmd_config_noisy(self):
    bisector = bisector_cli.BisectorCommandLine(DummyDomain)
    bisector.main('init', '--num=100', '--old=10', '--new=90',
                  '--noisy=new=9/10')

    with self.assertRaises(errors.ArgumentError):
      bisector.main('config', 'noisy', 'hello', 'world')

    self.assertEqual(bisector.states.config['noisy'], 'new=9/10')
    bisector.main('config', 'noisy', 'old=1/10,new=8/9')
    self.assertEqual(bisector.states.config['noisy'], 'old=1/10,new=8/9')

    with mock.patch('sys.stdout', new_callable=io.StringIO):
      # Only make sure no exceptions. No output verification.
      bisector.main('view')

  def test_current_status(self):
    common.init()
    bisector = bisector_cli.BisectorCommandLine(DummyDomain)
    result = bisector.current_status()
    self.assertEqual(result.get('inited'), False)

    bisector.main('init', '--num=100', '--old=10', '--new=90',
                  '--noisy=new=9/10')
    result = bisector.current_status()
    self.assertEqual(result.get('inited'), True)


if __name__ == '__main__':
  unittest.main()
