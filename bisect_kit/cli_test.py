# Copyright 2017 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Test cli module."""

from __future__ import print_function

import argparse
import unittest

from bisect_kit import cli
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
        argtype = cli.argtype_multiplexer(
            cli.argtype_int,
            cli.argtype_re('foobar', 'foobar'),
            cli.argtype_re(r'^r\d+$', 'r123'),
        )
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
        with self.assertRaisesRegex(cli.ArgTypeError, r'\d+\*\d+'):
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

    def test_check_executable(self):
        self.assertEqual(cli.check_executable('/bin/true'), None)

        self.assertRegex(cli.check_executable('/'), r'Not a file')
        self.assertRegex(cli.check_executable('LICENSE'), r'chmod')
        self.assertRegex(cli.check_executable('what'), r'PATH')
        self.assertRegex(cli.check_executable('eval-manually.sh'), r'\./')

    def test_lookup_signal_name(self):
        self.assertEqual(cli.lookup_signal_name(15), 'SIGTERM')
        self.assertEqual(cli.lookup_signal_name(99), 'Unknown')

    def test_patching_argparser_exit(self):
        parser = argparse.ArgumentParser()
        cli.patching_argparser_exit(parser)
        parser.add_argument('--value', type=int)
        parser.add_argument('--necessary', required=True)

        # Nothing happened.
        parser.parse_args(['--necessary', 'foo'])

        with self.assertRaises(errors.ArgumentError):
            parser.parse_args(['foo'])

        with self.assertRaises(errors.ArgumentError):
            parser.parse_args(['--necessary', 'foo', '--value', 'bar'])

    def test_argtype_key_value(self):
        self.assertEqual(cli.argtype_key_value('key=value'), ('key', 'value'))
        self.assertEqual(cli.argtype_key_value('key='), ('key', ''))
        self.assertEqual(
            cli.argtype_key_value('aa_bb-cc.dd='), ('aa_bb-cc.dd', '')
        )

        with self.assertRaises(cli.ArgTypeError):
            cli.argtype_key_value('key')

        with self.assertRaises(cli.ArgTypeError):
            cli.argtype_key_value('key=value=value2')

        with self.assertRaises(cli.ArgTypeError):
            cli.argtype_key_value('?=value')

    def test_fatal_error_handler(self):
        @cli.fatal_error_handler
        def test_func():
            assert 0

        with self.assertRaises(SystemExit) as e:
            test_func()
        self.assertEqual(e.exception.code, cli.EXIT_CODE_FATAL)


if __name__ == '__main__':
    unittest.main()
