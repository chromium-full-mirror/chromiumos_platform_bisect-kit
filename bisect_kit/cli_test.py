# Copyright 2017 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Test cli module."""

import argparse
import unittest

from bisect_kit import cli
from bisect_kit import errors


class TestCli(unittest.TestCase):
    """Test functions in cli module."""

    def test_argtype_int(self):
        self.assertEqual(cli.argtype_int('123456'), '123456')
        self.assertEqual(cli.argtype_int('-123456'), '-123456')
        with self.assertRaises(errors.ArgTypeError):
            cli.argtype_int('foobar')

    def test_argtype_notempty(self):
        self.assertEqual(cli.argtype_notempty('123456'), '123456')
        with self.assertRaises(errors.ArgTypeError):
            cli.argtype_notempty('')

    def test_argtype_re(self):
        argtype = cli.argtype_re(r'^r\d+$', 'r123')
        self.assertEqual(argtype('r123'), 'r123')

        with self.assertRaises(errors.ArgTypeError):
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

        with self.assertRaises(errors.ArgTypeError):
            argtype('hello')

    def test_argtype_multiplier(self):
        argtype = cli.argtype_multiplier(cli.argtype_int)
        self.assertEqual(argtype('123'), ('123', 1))
        self.assertEqual(argtype('123*5'), ('123', 5))

        # Make sure there is multiplier example in the message.
        with self.assertRaisesRegex(errors.ArgTypeError, r'\d+\*\d+'):
            argtype('hello')

    def test_argtype_path(self):
        self.assertEqual(cli.argtype_dir_path('/'), '/')
        self.assertEqual(cli.argtype_dir_path('/etc/'), '/etc')

        with self.assertRaises(errors.ArgTypeError):
            cli.argtype_dir_path('')
        with self.assertRaises(errors.ArgTypeError):
            cli.argtype_dir_path('/foo/bar/not/exist/path')
        with self.assertRaises(errors.ArgTypeError):
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

    def test_argtype_key_value(self):
        self.assertEqual(cli.argtype_key_value('key=value'), ('key', 'value'))
        self.assertEqual(cli.argtype_key_value('key='), ('key', ''))
        self.assertEqual(
            cli.argtype_key_value('aa_bb-cc.dd='), ('aa_bb-cc.dd', '')
        )

        with self.assertRaises(errors.ArgTypeError):
            cli.argtype_key_value('key')

        with self.assertRaises(errors.ArgTypeError):
            cli.argtype_key_value('key=value=value2')

        with self.assertRaises(errors.ArgTypeError):
            cli.argtype_key_value('?=value')

    def test_fatal_error_handler(self):
        @cli.fatal_error_handler
        def test_func():
            assert 0

        with self.assertRaises(SystemExit) as e:
            test_func()
        self.assertEqual(e.exception.code, cli.EXIT_CODE_FATAL)


class TestArgumentParser(unittest.TestCase):
    """Test ArgumentParser class in cli module"""

    def test_add_argument(self):
        parser = cli.ArgumentParser()
        with self.assertRaises(ValueError):
            parser.add_argument('--optional_underscore')
        parser.add_argument('--optional-hyphen')
        parser.add_argument('positional_underscore')

    def test_parse_args(self):
        parser = cli.ArgumentParser()
        parser.add_argument('--foo-bar')
        args = parser.parse_args(['--foo-bar', 'dummy1'])
        self.assertEqual(args.foo_bar, 'dummy1')
        with self.assertRaises(errors.ArgumentError):
            args = parser.parse_args(['--foo_bar', 'dummy1', '-baz', 'dummy2'])

        ns = argparse.Namespace()
        args = parser.parse_args(['--foo_bar', 'dummy1'], ns)
        self.assertEqual(args, ns)

    def test_parse_known_args(self):
        parser = cli.ArgumentParser()
        parser.add_argument('--foo-bar')
        args, remaining = parser.parse_known_args(['--foo-bar', 'dummy1'])
        self.assertEqual(args.foo_bar, 'dummy1')
        self.assertListEqual(remaining, [])
        args, remaining = parser.parse_known_args(
            ['--foo_bar', 'dummy1', '--baz', 'dummy2']
        )
        self.assertEqual(args.foo_bar, 'dummy1')
        self.assertListEqual(remaining, ['--baz', 'dummy2'])

        ns = argparse.Namespace()
        args, remaining = parser.parse_known_args(['--foo_bar', 'dummy1'], ns)
        self.assertEqual(args, ns)
        self.assertListEqual(remaining, [])

    def test_exit(self):
        parser = cli.ArgumentParser()
        parser.add_argument('--value', type=int)
        parser.add_argument('--necessary', required=True)

        # Nothing happened.
        parser.parse_args(['--necessary', 'foo'])

        with self.assertRaises(errors.ArgumentError):
            parser.parse_args(['foo'])

        with self.assertRaises(errors.ArgumentError):
            parser.parse_args(['--necessary', 'foo', '--value', 'bar'])

    def test_normalize_args(self):
        args = './cmd sub_cmd -a --arg_1 value1 --arg-2 value2 --arg_3=value_3 -- --arg_4 --arg-5'.split()
        expected = './cmd sub_cmd -a --arg-1 value1 --arg-2 value2 --arg-3=value_3 -- --arg_4 --arg-5'.split()
        self.assertSequenceEqual(
            cli.ArgumentParser.normalize_args(args), expected
        )


if __name__ == '__main__':
    unittest.main()
