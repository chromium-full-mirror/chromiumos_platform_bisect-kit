# Copyright 2018 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Exception classes."""

import argparse


class ArgumentError(Exception):
    """Bad command line argument."""

    def __init__(self, argument_name, message):
        self.argument_name = argument_name
        self.message = message
        super().__init__(str(self))

    def __str__(self):
        if self.argument_name:
            return 'argument %s: %s' % (self.argument_name, self.message)
        return self.message


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


class Uninitialized(Exception):
    """'init' must succeed before other subcommands."""


class ExecutionFatalError(Exception):
    """Fatal error and bisect should not continue.

    Switch or eval commands return fatal exit code.
    """


class NoDutAvailable(Exception):
    """Unable to allocate DUT from lab."""


class DutLeaseException(Exception):
    """Issues of skylab DUT lease."""


class BrokenDutException(Exception):
    """The DUT is broken."""


class WrongAssumption(Exception):
    """Wrong assumption.

    For non-noisy binary search, the assumption is all versions with old behavior
    occurs before all versions with new behavior. But the eval result contracted
    this ordering assumption.

    p.s. This only happens (could be detected) if users marked versions 'old' and
    'new' manually.

    Suggestion: try noisy search instead (--noisy).
    """


class DiagnoseContradiction(Exception):
    """Contradiction happened during diagnose.

    Test result of individual component/version is unreliable/untrustable
    (something wrong and/or flakiness out of control).
    """


class VerificationFailed(Exception):
    """Bisection range is verified false."""


class VerifyBehaviorFailed(VerificationFailed):
    """Either old version or new version behaves incorrectly."""

    def __init__(self, rev, expect, actual, bad_times=1):
        self.rev = rev
        self.expect = expect
        self.actual = actual
        self.bad_times = bad_times
        msg = 'rev=%s expect "%s" but got "%s"' % (
            self.rev,
            self.expect,
            self.actual,
        )
        if self.bad_times > 1:
            msg += ' %d times' % self.bad_times
        super().__init__(msg)


class VerifyOldBehaviorFailed(VerifyBehaviorFailed):
    """Old version does not behave as old."""

    def __init__(self, rev, bad_times=1, term_old='OLD', term_new='NEW'):
        super().__init__(rev, term_old, term_new, bad_times)


class VerifyNewBehaviorFailed(VerifyBehaviorFailed):
    """New version does not behave as new."""

    def __init__(self, rev, bad_times=1, term_old='OLD', term_new='NEW'):
        super().__init__(rev, term_new, term_old, bad_times)


class VerifyInitialRangeFailed(VerificationFailed):
    """Fail to pass the statistical tests."""

    def __init__(
        self, rounds: int, old_failure_rate: float, new_failure_rate: float
    ):
        self.rounds = rounds
        msg = (
            f'Fail to pass the Boschloo Exact Test in round {self.rounds}'
            f' (Failure rate {old_failure_rate:.2%} vs {new_failure_rate:.2%})'
        )
        super().__init__(msg)


class UnableToProceed(Exception):
    """Unable to narrow bisect range further due to too many errors."""


class InternalError(Exception):
    """bisect-kit internal error.

    In general, it means something wrong or not implemented in bisect-kit and
    needs fix.
    """


class BisectionTemporaryError(Exception):
    """Temporary error during bisection switch/eval step.

    This error might be temporary and would be recovered next time. In term of
    bisection decision, this is "skip", which means "the current candidate is
    undecidable to have old or new behavior, should skip the result of this
    run, retry may help".
    """


class ExternalError(BisectionTemporaryError):
    """Errors in external dependency.

    Like configuration errors, network errors, DUT issues, etc.
    """


class SshConnectionError(BisectionTemporaryError):
    """SSH connection error."""
