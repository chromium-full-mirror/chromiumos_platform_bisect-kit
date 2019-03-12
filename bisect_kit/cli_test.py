# -*- coding: utf-8 -*-
# Copyright 2017 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Test cli module."""

from __future__ import print_function
import random
import shutil
import tempfile
import unittest
import StringIO

import mock

from bisect_kit import cli
from bisect_kit import common
from bisect_kit import configure
from bisect_kit import core
from bisect_kit import errors


class TestCli(unittest.TestCase):
  """Test functions in cli module."""

  def test_argtype_int(self):
    self.assertEqual(cli.argtype_int('123456'), '123456')
    self.assertEqual(cli.argtype_int('-123456'), '-123456')
    with self.assertRaises(cli.ArgTypeError):
      cli.argtype_int('foobar')

  def test_argtype_notempty(self):
    self.assertEqual(cli.argtype_notempty('123456'), '123456')
    with self.assertRaises(cli.ArgTypeError):
      cli.argtype_notempty('')

  def test_argtype_re(self):
    argtype = cli.argtype_re(r'^r\d+$', 'r123')
    self.assertEqual(argtype('r123'), 'r123')

    with self.assertRaises(cli.ArgTypeError):
      argtype('hello')

  def test_argtype_multiplexer(self):
    argtype = cli.argtype_multiplexer(cli.argtype_int,
                                      cli.argtype_re('foobar', 'foobar'),
                                      cli.argtype_re(r'^r\d+$', 'r123'))
    self.assertEqual(argtype('123456'), '123456')
    self.assertEqual(argtype('foobar'), 'foobar')
    self.assertEqual(argtype('r123'), 'r123')

    with self.assertRaises(cli.ArgTypeError):
      argtype('hello')

  def test_argtype_multiplier(self):
    argtype = cli.argtype_multiplier(cli.argtype_int)
    self.assertEqual(argtype('123'), ('123', 1))
    self.assertEqual(argtype('123*5'), ('123', 5))

    # Make sure there is multiplier example in the message.
    with self.assertRaisesRegexp(cli.ArgTypeError, r'\d+\*\d+'):
      argtype('hello')

  def test_argtype_path(self):
    self.assertEqual(cli.argtype_dir_path('/'), '/')
    self.assertEqual(cli.argtype_dir_path('/etc/'), '/etc')

    with self.assertRaises(cli.ArgTypeError):
      cli.argtype_dir_path('')
    with self.assertRaises(cli.ArgTypeError):
      cli.argtype_dir_path('/foo/bar/not/exist/path')
    with self.assertRaises(cli.ArgTypeError):
      cli.argtype_dir_path('/etc/passwd')

  def test_collect_bisect_result_values(self):
    # pylint: disable=protected-access
    values = []
    cli._collect_bisect_result_values(values, '')
    self.assertEqual(values, [])

    values = []
    cli._collect_bisect_result_values(values, 'foo\n')
    cli._collect_bisect_result_values(values, 'bar\n')
    self.assertEqual(values, [])

    values = []
    cli._collect_bisect_result_values(values, 'BISECT_RESULT_VALUES=1 2\n')
    cli._collect_bisect_result_values(values, 'fooBISECT_RESULT_VALUES=3 4\n')
    cli._collect_bisect_result_values(values, 'BISECT_RESULT_VALUES=5\n')
    self.assertEqual(values, [1, 2, 5])

    with self.assertRaises(errors.InternalError):
      cli._collect_bisect_result_values(values, 'BISECT_RESULT_VALUES=hello\n')

  def test_check_executable(self):
    self.assertEqual(cli.check_executable('/bin/true'), None)

    self.assertRegexpMatches(cli.check_executable('/'), r'Not a file')
    self.assertRegexpMatches(cli.check_executable('LICENSE'), r'chmod')
    self.assertRegexpMatches(cli.check_executable('what'), r'PATH')
    self.assertRegexpMatches(cli.check_executable('eval-manually.sh'), r'\./')


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

    revlist = map(str, range(opts.num))
    return config, revlist

  def __init__(self, config):
    self.config = config

  def setenv(self, env, rev):
    env['ANS'] = str(self.config['ans'])
    env['OLD_P'] = str(self.config['old_p'])
    env['NEW_P'] = str(self.config['new_p'])
    env['REV'] = rev


@mock.patch('bisect_kit.common.config_logging', mock.Mock())
class TestBisectorCommandLine(unittest.TestCase):
  """Test cli.BisectorCommandLine class."""

  def setUp(self):
    self.session_base = tempfile.mkdtemp()
    self.patcher = mock.patch.object(common, 'DEFAULT_SESSION_BASE',
                                     self.session_base)
    self.patcher.start()

  def tearDown(self):
    shutil.rmtree(self.session_base)
    self.patcher.stop()
    configure.reset()

  def test_run_true(self):
    bisector = cli.BisectorCommandLine(DummyDomain)
    bisector.main('init', '--num=10', '--old=0', '--new=9')
    bisector.main('config', 'switch', 'true')
    bisector.main('config', 'eval', 'true')
    with self.assertRaises(errors.VerificationFailed):
      bisector.main('run')

    result = bisector.current_status()
    self.assertEqual(result.get('inited'), True)
    self.assertEqual(result.get('verified'), False)

  def test_run_false(self):
    bisector = cli.BisectorCommandLine(DummyDomain)
    bisector.main('init', '--num=10', '--old=0', '--new=9')
    bisector.main('config', 'switch', 'true')
    bisector.main('config', 'eval', 'false')
    with self.assertRaises(errors.VerificationFailed):
      bisector.main('run')

    result = bisector.current_status()
    self.assertEqual(result.get('inited'), True)
    self.assertEqual(result.get('verified'), False)

  def test_simple(self):
    bisector = cli.BisectorCommandLine(DummyDomain)
    bisector.main('init', '--num=20', '--old=3', '--new=15')
    bisector.main('config', 'switch', 'true')
    bisector.main('config', 'eval', 'sh', '-c', '[ "$BISECT_REV" -lt 7 ]')
    bisector.main('run')
    self.assertEqual(bisector.strategy.get_best_guess(), 7)

    with mock.patch('sys.stdout', new_callable=StringIO.StringIO):
      # Only make sure no exceptions. No output verification.
      bisector.main('log')

  def test_switch_fail(self):
    bisector = cli.BisectorCommandLine(DummyDomain)
    bisector.main('init', '--num=20', '--old=3', '--new=15')
    bisector.main('config', 'switch', 'false')
    bisector.main('config', 'eval', 'sh', '-c', '[ "$BISECT_REV" -lt 7 ]')
    with self.assertRaises(errors.UnableToProceed):
      bisector.main('run')

    result = bisector.current_status()
    self.assertEqual(result.get('inited'), True)
    self.assertEqual(result.get('verified'), False)

  def test_run_classic(self):
    bisector = cli.BisectorCommandLine(DummyDomain)
    bisector.main('init', '--num=200', '--old=0', '--new=99')
    bisector.main('config', 'switch', 'true')
    bisector.main('config', 'eval', 'false')

    def do_evaluate(_cmd, _domain, rev):
      if int(rev) < 42:
        return 'old', []
      return 'new', []

    with mock.patch('bisect_kit.cli.do_evaluate', do_evaluate):
      # two verify
      with mock.patch(
          'sys.stdout', new_callable=StringIO.StringIO) as mock_stdout:
        bisector.main('next')
        self.assertEqual(mock_stdout.getvalue(), '99\n')
      bisector.main('run', '-1')
      with mock.patch(
          'sys.stdout', new_callable=StringIO.StringIO) as mock_stdout:
        bisector.main('next')
        self.assertEqual(mock_stdout.getvalue(), '0\n')
      bisector.main('run', '-1')
      self.assertEqual(bisector.strategy.get_range(), (0, 99))

      bisector.main('run', '-1')
      self.assertEqual(bisector.strategy.get_range(), (0, 49))

      bisector.main('run')
      self.assertEqual(bisector.strategy.get_best_guess(), 42)
      self.assertEqual(bisector.states.stats['eval_count'], 2 + 7)

    with mock.patch(
        'sys.stdout', new_callable=StringIO.StringIO) as mock_stdout:
      bisector.main('next')
      self.assertEqual(mock_stdout.getvalue(), 'done\n')

  def test_run_noisy(self):
    bisector = cli.BisectorCommandLine(DummyDomain)
    bisector.main('init', '--num=100', '--old=0', '--new=99',
                  '--noisy=old=1/10,new=9/10')
    bisector.main('config', 'switch', 'true')
    bisector.main('config', 'eval', 'false')

    def do_evaluate(_cmd, _domain, rev):
      if int(rev) < 42:
        p = 0.1
      else:
        p = 0.9
      if random.random() < p:
        return 'new', []
      return 'old', []

    with mock.patch('bisect_kit.cli.do_evaluate', do_evaluate):
      bisector.main('run')
      self.assertEqual(bisector.strategy.get_best_guess(), 42)

    with mock.patch(
        'sys.stdout', new_callable=StringIO.StringIO) as mock_stdout:
      bisector.main('next')
      # There might be small math error near the boundary of confidence.
      self.assertIn(mock_stdout.getvalue(), ['done\n', '41\n', '42\n'])

  def test_cmd_old_and_new(self):
    """Tests cmd_old and cmd_new"""
    bisector = cli.BisectorCommandLine(DummyDomain)
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

  def test_cmd_switch(self):
    """Test cmd_switch"""
    bisector = cli.BisectorCommandLine(DummyDomain)
    bisector.main('init', '--num=100', '--old=0', '--new=99')
    bisector.main('config', 'switch', 'true')
    bisector.main('config', 'eval', 'false')

    switched = []

    def do_switch(_cmd, _domain, rev):
      switched.append(rev)

    with mock.patch('bisect_kit.cli.do_switch', do_switch):
      bisector.main('switch', 'next')
      bisector.main('switch', '3')
      bisector.main('switch', '5')
      bisector.main('switch', '4')
      self.assertEqual(switched, ['99', '3', '5', '4'])

  def test_cmd_view(self):
    bisector = cli.BisectorCommandLine(DummyDomain)
    bisector.main('init', '--num=100', '--old=10', '--new=90')
    with mock.patch.object(DummyDomain, 'fill_candidate_summary') as mock_view:
      bisector.main('view')
      mock_view.assert_called()

  def test_cmd_config_confidence(self):
    bisector = cli.BisectorCommandLine(DummyDomain)
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
    bisector = cli.BisectorCommandLine(DummyDomain)
    bisector.main('init', '--num=100', '--old=10', '--new=90',
                  '--noisy=new=9/10')

    with self.assertRaises(errors.ArgumentError):
      bisector.main('config', 'noisy', 'hello', 'world')

    self.assertEqual(bisector.states.config['noisy'], 'new=9/10')
    bisector.main('config', 'noisy', 'old=1/10,new=8/9')
    self.assertEqual(bisector.states.config['noisy'], 'old=1/10,new=8/9')

    with mock.patch('sys.stdout', new_callable=StringIO.StringIO):
      # Only make sure no exceptions. No output verification.
      bisector.main('view')

  def test_current_status(self):
    common.init()
    bisector = cli.BisectorCommandLine(DummyDomain)
    result = bisector.current_status()
    self.assertEqual(result.get('inited'), False)

    bisector.main('init', '--num=100', '--old=10', '--new=90',
                  '--noisy=new=9/10')
    result = bisector.current_status()
    self.assertEqual(result.get('inited'), True)


if __name__ == '__main__':
  unittest.main()
