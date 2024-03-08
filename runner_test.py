# Copyright 2017 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Test runner.py script"""

import argparse
import unittest
from unittest import mock

import runner


@mock.patch('bisect_kit.common.config_logging', mock.Mock())
class TestRunner(unittest.TestCase):
    """Test runner."""

    def test_argtype_ratio(self):
        self.assertEqual(runner.argtype_ratio('=1'), ('=', 1.0))
        with self.assertRaises(argparse.ArgumentTypeError):
            runner.argtype_ratio('hello')
        with self.assertRaises(argparse.ArgumentTypeError):
            runner.argtype_ratio('>5')

    def test_is_meet_finished(self):
        self.assertEqual(
            runner.criteria_is_met(
                ('=', 0), {"new": 0, "old": 0, "skip": 1}, 0, 'new'
            ),
            None,
        )

        self.assertEqual(
            runner.criteria_is_met(('=', 0), {"new": 0, "old": 3}, 0, 'new'),
            True,
        )
        self.assertEqual(
            runner.criteria_is_met(('<', 0.4), {"new": 3, "old": 7}, 0, 'new'),
            True,
        )
        self.assertEqual(
            runner.criteria_is_met(('<=', 0.4), {"new": 3, "old": 7}, 0, 'new'),
            True,
        )
        self.assertEqual(
            runner.criteria_is_met(('>', 0.2), {"new": 3, "old": 7}, 0, 'new'),
            True,
        )
        self.assertEqual(
            runner.criteria_is_met(('>=', 0.2), {"new": 3, "old": 7}, 0, 'new'),
            True,
        )

    def test_is_meet_rest(self):
        self.assertEqual(
            runner.criteria_is_met(
                ('=', 0), {"new": 0, "old": 0, "skip": 1}, 1, 'new'
            ),
            None,
        )

        # Between 0.45 and 0.63
        self.assertEqual(
            runner.criteria_is_met(('>', 0.5), {"old": 4, "new": 5}, 2, 'new'),
            None,
        )
        # Between 0.5 and 0.6
        self.assertEqual(
            runner.criteria_is_met(('>', 0.5), {"old": 4, "new": 5}, 1, 'new'),
            None,
        )
        # Between 0.5 and 0.6
        self.assertEqual(
            runner.criteria_is_met(('>=', 0.5), {"old": 4, "new": 5}, 1, 'new'),
            True,
        )
        # Between 0.4 and 0.5
        self.assertEqual(
            runner.criteria_is_met(('>=', 0.5), {"old": 5, "new": 4}, 1, 'new'),
            None,
        )
        # Between 0.4 and 0.5
        self.assertEqual(
            runner.criteria_is_met(('<', 0.5), {"old": 5, "new": 4}, 1, 'new'),
            None,
        )
        # Between 0.5 and 0.6
        self.assertEqual(
            runner.criteria_is_met(('<=', 0.5), {"old": 4, "new": 5}, 1, 'new'),
            None,
        )
        # Between 0.83 and 1.0
        self.assertEqual(
            runner.criteria_is_met(('=', 1.0), {"old": 0, "new": 5}, 1, 'new'),
            None,
        )

        # It's certain because the result count is impossible non-integer.
        self.assertEqual(
            runner.criteria_is_met(('=', 0.5), {"old": 1, "new": 1}, 1, 'new'),
            False,
        )

    def test_run_once_returncode(self):
        parser = runner.create_argument_parser()
        opts = parser.parse_args(['--new', '--returncode=0', 'true'])
        self.assertEqual(runner.run_once(opts), runner.NEW)

        opts = parser.parse_args(['--new', '--returncode=0', 'false'])
        self.assertEqual(runner.run_once(opts), runner.OLD)

        opts = parser.parse_args(
            ['--new', '--returncode=0', 'program.not.found']
        )
        self.assertEqual(runner.run_once(opts), runner.OLD)

        opts = parser.parse_args(
            ['--new', '--returncode=0', 'sh', '-c', 'kill $$']
        )
        self.assertEqual(runner.run_once(opts), runner.FATAL)

    def test_run_once_output(self):
        parser = runner.create_argument_parser()
        opts = parser.parse_args(['--old', '--output', 'OLD', 'echo', 'OLD'])
        self.assertEqual(runner.run_once(opts), runner.OLD)

        opts = parser.parse_args(
            [
                '--old',
                '--output',
                'OLD',
                '--precondition-output',
                'foo',
                'echo',
                'foo OLD',
            ]
        )
        self.assertEqual(runner.run_once(opts), runner.OLD)

        opts = parser.parse_args(
            [
                '--old',
                '--output',
                'OLD',
                '--precondition-output',
                'foo',
                'echo',
                'bar OLD',
            ]
        )
        self.assertEqual(runner.run_once(opts), runner.SKIP)

    def test_run_once_timeout(self):
        parser = runner.create_argument_parser()
        opts = parser.parse_args(['--new', '--timeout=0.1', 'sleep', '0.11'])
        self.assertEqual(runner.run_once(opts), runner.NEW)

        opts = parser.parse_args(['--new', '--timeout=0.1', 'sleep', '0'])
        self.assertEqual(runner.run_once(opts), runner.OLD)

    def test_run_once_terminate_output(self):
        parser = runner.create_argument_parser()
        opts = parser.parse_args(
            [
                '--new',
                '--output=hello',
                '--terminate-output',
                'hello',
                'sh',
                '-c',
                'echo hello; sleep 100; echo world',
            ]
        )
        self.assertEqual(runner.run_once(opts), runner.NEW)

        opts = parser.parse_args(
            [
                '--new',
                '--output=world',
                '--terminate-output',
                'hello',
                'sh',
                '-c',
                'echo hello; sleep 100; echo world',
            ]
        )
        self.assertEqual(runner.run_once(opts), runner.OLD)

    def test_main(self):
        self.assertEqual(
            runner.main(['--new', '--output', 'hello', 'echo', 'hello']),
            runner.NEW,
        )
        self.assertEqual(
            runner.main(['--old', '--output', 'hello', 'echo', 'hello']),
            runner.OLD,
        )
        self.assertEqual(
            runner.main(['--new', '--output', 'hello', 'echo', 'world']),
            runner.OLD,
        )
        self.assertEqual(
            runner.main(
                [
                    '--new',
                    '--output',
                    'hello',
                    '--precondition-output',
                    'hello',
                    'echo',
                    'world',
                ]
            ),
            runner.SKIP,
        )

    def test_main_fatal(self):
        self.assertEqual(
            runner.main(['--new', '--output', 'hello', '/non/existing/path']),
            runner.OLD,
        )

    def test_main_shortcut(self):
        with mock.patch.object(
            runner, 'run_once', return_value=runner.OLD
        ) as mock_run_once:
            self.assertEqual(
                runner.main(['--new', '--output=hello', '--repeat=5', 'foo']),
                runner.OLD,
            )
            self.assertEqual(mock_run_once.call_count, 1)

        with mock.patch.object(
            runner, 'run_once', return_value=runner.OLD
        ) as mock_run_once:
            self.assertEqual(
                runner.main(
                    [
                        '--new',
                        '--output=hello',
                        '--repeat=10',
                        '--ratio=>0.3',
                        'foo',
                    ]
                ),
                runner.OLD,
            )
            self.assertEqual(mock_run_once.call_count, 7)

        with mock.patch.object(
            runner, 'run_once', return_value=runner.OLD
        ) as mock_run_once:
            self.assertEqual(
                runner.main(
                    [
                        '--new',
                        '--output=hello',
                        '--repeat=10',
                        '--ratio=>0.3',
                        '--noshortcut',
                        'foo',
                    ]
                ),
                runner.OLD,
            )
            self.assertEqual(mock_run_once.call_count, 10)


if __name__ == '__main__':
    unittest.main()
