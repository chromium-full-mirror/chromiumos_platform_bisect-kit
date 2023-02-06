# -*- coding: utf-8 -*-
# Copyright 2017 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Command line utility functions."""

from __future__ import print_function

import argparse
import logging
import os
import re
import signal
import sys

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


class ArgTypeError(argparse.ArgumentTypeError):
    """An error for argument validation failure.

    This not only tells users the argument is wrong but also gives correct
    example. The main purpose of this error is for argtype_multiplexer, which
    cascades examples from multiple ArgTypeError.
    """

    def __init__(self, msg, example):
        self.msg = msg
        if isinstance(example, list):
            self.example = example
        else:
            self.example = [example]
        full_msg = '%s (example value: %s)' % (
            self.msg,
            ', '.join(self.example),
        )
        super().__init__(full_msg)


def argtype_notempty(s):
    """Validates argument is not an empty string.

    Args:
      s: string to validate.

    Raises:
      ArgTypeError if argument is empty string.
    """
    if not s:
        msg = 'should not be empty'
        raise ArgTypeError(msg, 'foo')
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
        raise ArgTypeError('should be a number', '123') from e


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
            raise ArgTypeError('should be "%s"' % pattern, pattern)
        raise ArgTypeError(
            'should match "%s"' % pattern, '"%s" like %s' % (pattern, example)
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
            except ArgTypeError as e:
                examples += e.example

        msg = 'Invalid argument'
        raise ArgTypeError(msg, examples)

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
        except ArgTypeError as e:
            # It should be okay to gives multiplier example only for the first one
            # because it is just "example", no need to enumerate all possibilities.
            raise ArgTypeError(e.msg, e.example + [e.example[0] + '*3']) from e

    return helper


def argtype_dir_path(s):
    """Validate argument is an existing directory.

    Args:
      s: string to validate.

    Raises:
      ArgTypeError if the path is not a directory.
    """
    if not os.path.exists(s):
        raise ArgTypeError(
            'should be an existing directory', '/path/to/somewhere'
        )
    if not os.path.isdir(s):
        raise ArgTypeError('should be a directory', '/path/to/somewhere')

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
        raise ArgTypeError('Invalid key value string: %s' % s, 'key=value')
    if not re.match('^[A-Za-z0-9_.-]+$', split[0]):
        raise ArgTypeError('Invalid key: %s' % s, 'key=value')
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


def patching_argparser_exit(parser):
    """Patching argparse.ArgumentParser.exit to raise exception for failures.

    Args:
      parser: argparse.ArgumentParser object
    """
    orig_exit = parser.exit

    def exit_hack(status=0, message=None):
        if status != 0:
            if message:
                message = message.strip()
            raise errors.ArgumentError(None, message)
        orig_exit(status, message)

    parser.exit = exit_hack


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
