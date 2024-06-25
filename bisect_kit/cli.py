# Copyright 2017 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Command line utility functions."""

import argparse
import logging
import os
import re
import signal
import sys

from bisect_kit import common
from bisect_kit import errors
from bisect_kit import util


logger = logging.getLogger(__name__)

# Exit code of bisect eval script. These values are chosen compatible with 'git
# bisect'.
EXIT_CODE_OLD = 0
EXIT_CODE_NEW = 1
EXIT_CODE_SKIP = 125
EXIT_CODE_FATAL = 128
# Doesn't matter for value output.
EXIT_CODE_VALUE = 0


class ArgumentParser(argparse.ArgumentParser):
    """A subclass of `argparse.ArgumentParser` that recommends using optional
    arguments with hyphens instead of underscores."""

    def __init__(self, *args, raise_bad_status=True, **kwargs):
        super().__init__(*args, **kwargs)
        self._raise_bad_status = raise_bad_status

    def parse_args(self, args=None, namespace=None):
        """Parses arguments and accepts both underscore-separated and
        hyphen-separated optional arguments."""
        if args is None:
            args = sys.argv[1:]
        return super().parse_args(self.normalize_args(args), namespace)

    def parse_known_args(self, args=None, namespace=None):
        """Parses arguments and accepts both underscore-separated and
        hyphen-separated optional arguments."""
        if args is None:
            args = sys.argv[1:]
        return super().parse_known_args(self.normalize_args(args), namespace)

    def add_argument(self, *names: str, **kwargs):
        """Adds optional arguments with hyphens instead of underscores.

        Please not that any internal `-` characters will be converted to `_`
        characters to make sure the string is a valid attribute name.
        For example:

        >>> parser.add_argument('--foo-bar')
        >>> parser.parse_args('--foo-bar baz'.split())
        Namespace(foo_bar='baz')
        """
        names_with_underscore = [
            name for name in names if name.startswith('--') and '_' in name
        ]
        if names_with_underscore:
            raise ValueError(
                'Please add optional arguments with names with hyphens '
                f'instead of underscores: {", ".join(names_with_underscore)}'
            )
        return super().add_argument(*names, **kwargs)

    def exit(self, status=0, message=None):
        if self._raise_bad_status and status != 0:
            if message:
                message = message.strip()
            raise errors.ArgumentError(None, message)
        return super().exit(status, message)

    @staticmethod
    def normalize_args(args: list[str]) -> list[str]:
        """Normalizes underscore-separated optional arguments to
        hyphen-separated for backward compatibility."""
        normalized = []
        preserved = False
        for arg in args:
            arg = str(arg)
            if arg == '--':
                preserved = True
                normalized.append(arg)
            elif not preserved and arg.startswith('--'):
                key, op, value = arg.partition('=')
                norm_arg = ''.join([key.replace('_', '-'), op, value])
                if norm_arg != arg:
                    logger.warning(
                        'Underscores in arguments will be deprecated. '
                        'Please use hyphens instead. (%s -> %s)',
                        arg,
                        norm_arg,
                    )
                normalized.append(norm_arg)
            else:  # preserved or not a key
                normalized.append(arg)
        return normalized


def argtype_notempty(s):
    """Validates argument is not an empty string.

    Args:
      s: string to validate.

    Raises:
      ArgTypeError if argument is empty string.
    """
    if not s:
        msg = 'should not be empty'
        raise errors.ArgTypeError(msg, 'foo')
    return s


def argtype_int(s):
    """Validate argument is a number.

    Args:
      s: string to validate.

    Raises:
      ArgTypeError if argument is not a number.
    """
    try:
        return str(int(s))
    except ValueError as e:
        raise errors.ArgTypeError('should be a number', '123') from e


def argtype_re(pattern, example):
    r"""Validate argument matches `pattern`.

    Args:
      pattern: regex pattern
      example: example string which matches `pattern`

    Returns:
      A new argtype function which matches regex `pattern`
    """
    assert re.match(pattern, example)

    def validate(s):
        if re.match(pattern, s):
            return s
        if re.escape(pattern) == pattern:
            raise errors.ArgTypeError(f'should be "{pattern}"', pattern)
        raise errors.ArgTypeError(
            f'should match "{pattern}"', f'"{pattern}" like {example}'
        )

    return validate


def argtype_multiplexer(*args):
    r"""argtype multiplexer

    This function takes a list of argtypes and creates a new function matching
    them. Moreover, it gives error message with examples.

    Examples:
      >>> argtype = argtype_multiplexer(argtype_int,
                                        argtype_re(r'^r\d+$', 'r123'))
      >>> argtype('123')
      123
      >>> argtype('r456')
      r456
      >>> argtype('hello')
      ArgTypeError: Invalid argument (example value: 123, "r\d+$" like r123)

    Args:
      *args: list of argtypes or regex pattern.

    Returns:
      A new argtype function which matches *args.
    """

    def validate(s):
        examples = []
        for t in args:
            try:
                return t(s)
            except errors.ArgTypeError as e:
                examples += e.example

        msg = 'Invalid argument'
        raise errors.ArgTypeError(msg, examples)

    return validate


def argtype_multiplier(argtype):
    """A new argtype that supports multiplier suffix of the given argtype.

    Examples:
      Supports the given argtype accepting "foo" as argument, this function
      generates a new argtype function which accepts argument like "foo*3".

    Returns:
      A new argtype function which returns (arg, times) where arg is accepted
      by input `argtype` and times is repeating count. Note that if multiplier is
      omitted, "times" is 1.
    """

    def helper(s):
        m = re.match(r'^(.+)\*(\d+)$', s)
        try:
            if m:
                return argtype(m.group(1)), int(m.group(2))
            return argtype(s), 1
        except errors.ArgTypeError as e:
            # It should be okay to gives multiplier example only for the first one
            # because it is just "example", no need to enumerate all possibilities.
            raise errors.ArgTypeError(
                e.msg, e.example + [e.example[0] + '*3']
            ) from e

    return helper


def argtype_dir_path(s):
    """Validate argument is an existing directory.

    Args:
      s: string to validate.

    Raises:
      ArgTypeError if the path is not a directory.
    """
    if not os.path.exists(s):
        raise errors.ArgTypeError(
            'should be an existing directory', '/path/to/somewhere'
        )
    if not os.path.isdir(s):
        raise errors.ArgTypeError('should be a directory', '/path/to/somewhere')

    # Normalize, trim trailing path separators.
    if len(s) > 1 and s[-1] == os.path.sep:
        s = s[:-1]
    return s


def argtype_key_value(s):
    """Validate argument is a valid `key=value` string.

    Args:
      s: string to validate.

    Returns:
      A (key, value) tuple, if valid.

    Raises:
      ArgTypeError if argument is invalid format.
    """
    split = s.split('=')
    if len(split) != 2:
        raise errors.ArgTypeError(f'Invalid key value string: {s}', 'key=value')
    if not re.match('^[A-Za-z0-9_.-]+$', split[0]):
        raise errors.ArgTypeError(f'Invalid key: {s}', 'key=value')
    return tuple(split)


def check_executable(program):
    """Checks whether a program is executable.

    Args:
      program: program path in question

    Returns:
      string as error message if `program` is not executable, or None otherwise.
      It will return None if unable to determine as well.
    """
    returncode = util.call('which', program)
    if returncode == 127:  # No 'which' on this platform, skip the check.
        return None
    if returncode == 0:  # is executable
        return None

    hint = ''
    if not os.path.exists(program):
        hint = 'Not in PATH?'
    elif not os.path.isfile(program):
        hint = 'Not a file'
    elif not os.access(program, os.X_OK):
        hint = 'Forgot to chmod +x?'
    elif '/' not in program:
        hint = 'Forgot to prepend "./" ?'
    return '%r is not executable. %s' % (program, hint)


def lookup_signal_name(signum):
    """Look up signal name by signal number.

    Args:
      signum: signal number

    Returns:
      signal name, like "SIGTERM". "Unknown" for unexpected number.
    """
    for k, v in vars(signal).items():
        if k.startswith('SIG') and '_' not in k and v == signum:
            return k
    return 'Unknown'


def format_returncode(returncode):
    # returncode is negative if the process is terminated by signal directly.
    if returncode < 0:
        signum = -returncode
        signame = lookup_signal_name(signum)
        return 'terminated by signal %d (%s)' % (signum, signame)
    # Some programs, e.g. shell, handled signal X and exited with (128 + X).
    if returncode > 128:
        signum = returncode - 128
        signame = lookup_signal_name(signum)
        return 'exited with code %d; may be signal %d (%s)' % (
            returncode,
            signum,
            signame,
        )

    return 'exited with code %d' % returncode


def fatal_error_handler(func):
    """Function decorator which exits with fatal code for fatal exceptions.

    This is a helper for switcher and evaluator. It catches fatal exceptions and
    exits with fatal exit code. The fatal exit code (128) is aligned with 'git
    bisect'.

    See also argparse_fatal_error().

    Args:
      func: wrapped function
    """

    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except (
            AssertionError,
            errors.ExecutionFatalError,
            errors.ArgumentError,
        ):
            logger.exception('fatal exception, bisection should stop')
            sys.exit(EXIT_CODE_FATAL)

    return wrapper


def create_common_argument_parser() -> ArgumentParser:
    common_argument_parser = ArgumentParser(add_help=False)
    common_argument_parser.add_argument(
        '--log-file',
        metavar='LOG_FILE',
        default=os.environ.get('LOG_FILE'),
        help='Override log filename',
    )
    common_argument_parser.add_argument(
        '--debug', action='store_true', help='Output DEBUG log to console'
    )
    common_argument_parser.add_argument(
        '--log-base-dir',
        metavar='LOG_BASE_DIR',
        default=common.DEFAULT_LOG_BASE,
        help='log base directory',
    )
    return common_argument_parser


def create_session_optional_parser() -> ArgumentParser:
    session_optional_parser = ArgumentParser(
        add_help=False, parents=[create_common_argument_parser()]
    )
    session_optional_parser.add_argument(
        '--session',
        default=common.DEFAULT_SESSION_NAME,
        help='Session name (default: %(default)r)',
    )
    return session_optional_parser


def create_session_required_parser() -> ArgumentParser:
    session_required_parser = ArgumentParser(
        add_help=False, parents=[create_common_argument_parser()]
    )
    session_required_parser.add_argument(
        '--session', required=True, help='Session name'
    )
    return session_required_parser
