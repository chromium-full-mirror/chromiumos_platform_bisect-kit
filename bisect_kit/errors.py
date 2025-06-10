# Copyright 2018 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Exception classes."""

import argparse
from enum import StrEnum
import logging
import sys


logger = logging.getLogger(__name__)


# The enum should be in sync with the error types defined in
# google3/googleclient/chrome/chromeos_bisector/common/bisect.go
class ErrorType(StrEnum):
    """Defines error types."""

    Unknown = "Unknown"
    Retriable = "Retriable"
    Contradiction = "Contradiction"
    Fatal = "Fatal"


class BisectException(Exception):
    """Base exception."""

    def error_type(self) -> str:
        return ErrorType.Unknown


class ArgumentError(BisectException):
    """Bad command line argument."""

    def __init__(self, argument_name, message):
        self.argument_name = argument_name
        self.message = message
        super().__init__(str(self))

    def __str__(self):
        if self.argument_name:
            return 'argument %s: %s' % (self.argument_name, self.message)
        return self.message

    def error_type(self) -> str:
        return ErrorType.Fatal


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

    def error_type(self) -> str:
        return ErrorType.Fatal


class Uninitialized(BisectException):
    """'init' must succeed before other subcommands."""

    def error_type(self) -> str:
        return ErrorType.Fatal


class ExecutionFatalError(BisectException):
    """Fatal error and bisect should not continue.

    Switch or eval commands return fatal exit code.
    """

    def error_type(self) -> str:
        return ErrorType.Fatal


class BisectRetriableError(BisectException):
    """The top level class of exceptions which may be transient.

    An automatically retry is worthwhile.
    Note that here "retry" means a re-execution of the whole bisection process,
    the current bisection would be halted. It is different from
    BisectionTemporaryError where it doesn't halt the bisection unless retry
    limit is reached.
    """

    def error_type(self) -> str:
        return ErrorType.Retriable


class DutException(BisectRetriableError):
    """Top level class for DUT related exceptions."""


class NoDutAvailable(DutException):
    """There is no DUT which satisfy the DutAllocateSpec in lab."""


class DutLeaseTimeout(DutException):
    """Unable to allocate DUT in a predefined time period.

    The DutAllocateSpec is valid, but the lease takes too much time.
    """


class DutLeaseException(DutException):
    """Issues of skylab DUT lease."""


class BrokenDutException(DutException):
    """The DUT is broken."""


class DatastoreTransactionConflict(BisectRetriableError):
    """A warpper around Datastore transaction conflict exception.

    Make it a subclass of BisectRetriableError so the bisection can be retried.
    Note that it is usually raised only when an internal retry limit has been
    exceeded.
    """


class TooManyTemporaryErrors(BisectRetriableError):
    """Unable to narrow bisect range further due to too many temporary errors."""


class BisectionTemporaryError(BisectRetriableError):
    """Temporary error during bisection switch/eval step.

    This error might be temporary and would be recovered next time. In term of
    bisection decision, this is "skip", which means "the current candidate is
    undecidable to have old or new behavior, should skip the result of this
    run, retry may help".

    It is a subset of BisectRetriableError in that we expect this errors to be
    more transient. That is, an immediate retry may help. If an error is
    BisectRetriableError but not BisectionTemporaryError, we tend to halt this
    execution and retry later.
    Currently, some BisectionTemporaryError are retried in the same execution,
    but not all.
    """


class ExecutionTimeout(BisectionTemporaryError):
    """Timeout expired when executing a subprocess.

    This may be raised by blocking calls like Popen.wait(), check_call(),
    check_output(), etc.
    """


class ExternalError(BisectionTemporaryError):
    """Errors in external dependency.

    Like configuration errors, network errors, DUT issues, etc.
    """


class SshConnectionError(BisectionTemporaryError):
    """SSH connection error."""


class WrongAssumption(BisectException):
    """Wrong assumption.

    For non-noisy binary search, the assumption is all versions with old behavior
    occurs before all versions with new behavior. But the eval result contracted
    this ordering assumption.

    p.s. This only happens (could be detected) if users marked versions 'old' and
    'new' manually.

    Suggestion: try noisy search instead (--noisy).
    """

    def error_type(self) -> str:
        return ErrorType.Contradiction


class DiagnoseContradiction(BisectException):
    """Contradiction happened during diagnose.

    Test result of individual component/version is unreliable/untrustable
    (something wrong and/or flakiness out of control).
    """

    def error_type(self) -> str:
        return ErrorType.Contradiction


class VerificationFailed(BisectException):
    """Bisection range is verified false."""

    def error_type(self) -> str:
        return ErrorType.Contradiction


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


class TooFewRevisionsError(VerificationFailed):
    """Too few revisions to search."""


class InternalError(BisectException):
    """bisect-kit internal error.

    In general, it means something wrong or not implemented in bisect-kit and
    needs fix.
    """

    def error_type(self) -> str:
        return ErrorType.Fatal


class DutPreconditionNotMet(BisectException):
    """DUT precondition doesn't met so the bisection can not proceed."""

    def error_type(self) -> str:
        return ErrorType.Fatal


class DutAllocateSpecError(BisectException):
    """Raised when failed to create a DutAllocateSpec from the bisect database."""

    def error_type(self) -> str:
        return ErrorType.Fatal


class BuildError(BisectException):
    """Errors happen in build phase which are not in general recoverable.

    For example, spec errors, compile errors.
    """

    def error_type(self) -> str:
        return ErrorType.Fatal


class PatchFetchError(BisectException):
    """Raised when failed to fetch the given patch to to be applied to the code."""

    def error_type(self) -> str:
        return ErrorType.Fatal


class PatchApplyError(BisectException):
    """Raised when failed to apply the given patch to the code."""

    def error_type(self) -> str:
        return ErrorType.Fatal


def reconstruct_from_string(
    cls_name: str, error_msg: str
) -> BisectException | None:
    """Reconstruct an exception from its class name and error message.

    It can be useful when an exception is derived from a subprocess call where
    the exception information is available in the subprocess output.

    Args:
      cls_name: the name of the class. It must be one of the
        exceptions in this module.
      err_msg: the error message of the exception.

    Returns:
      The reconstructed exception or None if unable to reconstruct it.
    """
    try:
        cls = getattr(sys.modules[__name__], cls_name)
    except Exception:
        logger.error('Failed to find class by name %r', cls_name)
    else:
        return cls(error_msg)
    return None


def error_type(e: Exception) -> str:
    """Returns the type of an exception."""
    if isinstance(e, BisectException):
        return e.error_type()
    return ErrorType.Unknown
