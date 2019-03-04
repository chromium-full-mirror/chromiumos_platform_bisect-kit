# -*- coding: utf-8 -*-
# Copyright 2018 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Exception classes."""


class ArgumentError(Exception):
  """Bad command line argument."""

  def __init__(self, argument_name, message):
    self.argument_name = argument_name
    self.message = message
    super(ArgumentError, self).__init__(str(self))

  def __str__(self):
    if self.argument_name:
      return 'argument %s: %s' % (self.argument_name, self.message)
    return self.message


class ExecutionFatalError(Exception):
  """Fatal error and bisect should not continue.

  Switch or eval commands return fatal exit code.
  """


class NoDutAvailable(Exception):
  """Unable to allocate DUT from lab."""


class WrongAssumption(Exception):
  """Wrong assumption.

  For non-noisy binary search, the assumption is all versions with old behavior
  occurs before all versions with new behavior. But the eval result contracted
  this ordering assumption.

  p.s. This only happens (could be detected) if users marked versions 'old' and
  'new' manually.

  Suggestion: try noisy search instead (--noisy).
  """


class VerificationFailed(Exception):
  """Bisection range is verified false."""

  def __init__(self, rev, expect, actual, bad_times=1):
    self.rev = rev
    self.expect = expect
    self.actual = actual
    self.bad_times = bad_times

    msg = 'rev=%s expect "%s" but got "%s"' % (self.rev, self.expect,
                                               self.actual)
    if self.bad_times > 1:
      msg += ' %d times' % self.bad_times
    super(VerificationFailed, self).__init__(msg)


class VerifyOldBehaviorFailed(VerificationFailed):
  """Old version does not behave as old."""

  def __init__(self, rev, bad_times=1):
    super(VerifyOldBehaviorFailed, self).__init__(rev, 'old', 'new', bad_times)


class VerifyNewBehaviorFailed(VerificationFailed):
  """New version does not behave as new."""

  def __init__(self, rev, bad_times=1):
    super(VerifyNewBehaviorFailed, self).__init__(rev, 'new', 'old', bad_times)


class UnableToProceed(Exception):
  """Unable to narrow bisect range further due to too many errors."""


class InternalError(Exception):
  """bisect-kit internal error.

  In general, it means something wrong or not implemented in bisect-kit and
  needs fix.
  """


class ExternalError(Exception):
  """Errors in external dependency.

  Like configuration errors, network errors, DUT issues, etc.
  """


class SshConnectionError(Exception):
  """SSH connection error."""
