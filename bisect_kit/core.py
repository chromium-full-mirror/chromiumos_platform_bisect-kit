# -*- coding: utf-8 -*-
# Copyright 2017 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Bisection core module."""

from __future__ import print_function
import json
import logging
import os
import shutil
import tempfile
import time

logger = logging.getLogger(__name__)


class VerificationFailed(Exception):
  """Verification of bisect range failed."""

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


class ExecutionFatalError(Exception):
  """Fatal error and bisect should not continue.

  Either:
    - Switch or eval commands return fatal exit code, or
    - Critical errors happened in bisect script itself.
  """


class RevInfo(object):
  """Aggregated evaluation result of one revision.

  The count of results can be easily accessed by using [] operator.

  Attributes:
    rev (str): revision id
    result_counter (dict): count of results, example: dict(new=3, old=2)
    values (list of numbers): additional values collected during evaluation,
                              like performance score.
  """

  def __init__(self, rev):
    self.rev = rev

    self.result_counter = {}
    self.values = []

  def __getitem__(self, key):
    return self.result_counter.get(key, 0)

  def __setitem__(self, key, value):
    self.result_counter[key] = value

  def append_values(self, values):
    self.values += values


class States(object):
  """Base class for serializing program state to disk.

  After instantiation, set_data() or load() should be invoked before access
  state values.
  """

  def __init__(self, session_file):
    """Initializes States.

    Args:
      session_file: path of session file.
    """
    self.session_file = session_file
    logger.debug('session file: %s', self.session_file)

    # Persistent data (dict). This is the canonical source of data. All other
    # fields are derived from this one. The whole dict will be serialized as
    # json to session file. Semantic of sub-fields are defined by subclasses.
    # Before initialization, its value is None.
    self.data = None

  def reset(self):
    """Resets state and deletes saved file."""
    self.data = None
    os.unlink(self.session_file)

  def set_data(self, data):
    """Sets state dict data.

    Subclass may override this method for post-processing.

    Args:
      data: program state data (dict)
    """
    self.data = data

  def load(self):
    """Loads saved data from file.

    Returns:
      True if loaded successfully.
    """
    if not os.path.exists(self.session_file):
      return False
    self.set_data(json.load(file(self.session_file)))
    return True

  def save(self):
    dirname = os.path.dirname(self.session_file)
    if not os.path.exists(dirname):
      os.makedirs(dirname)

    tmp_fn = tempfile.mktemp()
    with file(tmp_fn, 'w') as f:
      f.write(json.dumps(self.data, indent=4, sort_keys=True))
    # Move is an atomic operation, so the session file won't be corrupted due
    # to program terminated by any reason.
    shutil.move(tmp_fn, self.session_file)


class BisectStates(States):
  """Bisection states.

  After instantiation, init() or load() should be invoked before access state
  values.
  """

  def __init__(self, session_file):
    """Initializes BisectStates.

    Args:
      session_file: path of session file.
    """
    super(BisectStates, self).__init__(session_file)

    # Mapping of rev to idx; constructed from data['revlist'].
    self.rev_index = {}

    # List of RevInfo; constructed from data['revlist'] and data['history'].
    self.rev_info = []

  @property
  def config(self):
    return self.data['config']

  @property
  def stats(self):
    return self.data['stats']

  def init(self, config, revlist):
    """Initializes attributes data, rev_info and rev_index.

    Args:
      config: bisection configuration.
      revlist: version list.
    """
    # Statistics of bisector's action count (switch, eval) and their
    # accumulated duration.
    # With this, noisy bisector may run evaluator more times as time
    # optimization if evaluator is much faster than switcher.
    stats = dict(switch_count=0, eval_count=0, switch_time=0, eval_time=0)

    self.set_data(
        dict(
            # Bisection configurations (dict), values are determined by cmd_init
            # and each domain's init functions. There will be 'old' and 'new' at
            # least.
            config=config,
            # List of bisect candidates (version numbers).
            revlist=revlist,
            # What have been done so far. Each entry contains at least
            # timestamp, rev, and result.
            history=[],
            # Statistics values, see above.
            stats=stats))

  def set_data(self, data):
    super(BisectStates, self).set_data(data)

    self.rev_info = []
    self.rev_index = {}

    for i, rev in enumerate(self.data['revlist']):
      self.rev_info.append(RevInfo(rev))
      self.rev_index[rev] = i

    for entry in self.data['history']:
      idx = self.rev2idx(entry['rev'])
      self.rev_info[idx][entry['status']] += entry.get('times', 1)
      self.rev_info[idx].values += entry.get('values', [])

  def get(self, idx):
    """Get rev info by index.

    Args:
      idx: index of rev
    """
    return self.rev_info[idx]

  def idx2rev(self, idx):
    return self.rev_info[idx].rev

  def rev2idx(self, rev):
    return self.rev_index[rev]

  def add_status(self, idx, status, values=None, times=1, comment=''):
    self.rev_info[idx][status] += times
    if values:
      self.rev_info[idx].append_values(values)
    entry = dict(
        timestamp=int(time.time()), rev=self.idx2rev(idx), status=status)
    if times > 1:
      entry['times'] = times
    if comment:
      entry['comment'] = comment
    if values:
      entry['values'] = values
    self.data['history'].append(entry)


class BisectDomain(object):
  """Base class of bisection domain.

  "BisectDomain" is in the sense of "domain of math function". Mapping to
  specific problems, "domain" usually means version numbers, git hashes,
  timestamp, or any ordered strings. In other words, it means "what to bisect".

  The main purposes of this class are:
    - Takes care initial setup of bisection.
    - Enumerate version numbers need to bisect.
    - Provide users the information to difference of two version numbers.
  """

  # Bisector help message shown on command line --help.
  help = ''

  @staticmethod
  def revtype(rev):
    """Validates version string of two ends of bisect range.

    Args:
      rev: a version string from command line argument.

    Returns:
      The original or normalized version string if it is valid.

    Raises:
      TypeError or ValueError:
        Indicates rev is invalid.
      argparse.ArgumentTypeError:
        Indicates rev is invalid (with additional message.)
    """

  @classmethod
  def intra_revtype(cls, intra_rev):
    """Validates intra version string within bisect range.

    'rev' means the version string of two ends of bisect range. 'intra_rev'
    means other versions within the bisect range. intra_revtype equals to
    revtype by default.

    Args:
      intra_rev: a version string from command line argument.

    Returns:
      The original or normalized version string if it is valid.

    Raises:
      TypeError or ValueError:
        Indicates rev is invalid.
      argparse.ArgumentTypeError:
        Indicates rev is invalid (with additional message.)
    """
    return cls.revtype(intra_rev)

  @staticmethod
  def add_init_arguments(parser):
    """Adds additional arguments for init subcommand of bisector.

    Args:
      parser: An argparse.ArgumentParser instance.
    """

  @staticmethod
  def init(opts):
    """Initializes BisectDomain.

    This is called by bisector's "init" command.

    Args:
      opts: An argparse.Namespace to hold command line arguments.

    Returns:
      (config, revlist):
        config (dict): values saved to the per session storage. The bisection
          range could be adjusted by setting config['old'] and config['new'].
        revlist (list of string): list of version strings need to bisect. The
          bisect range `old` and `new` must be inside the list (but unnecessary
          to be the first and the last one).
    """

  def setenv(self, env, rev):
    """Sets environment variables needed by switchers and evaluators.

    Args:
      env: The dict to hold environment variables.
      rev: Current bisecting version.
    """

  def view(self, old, new):
    """Prints difference of the two versions.

    Args:
      old: The version string with old behavior.
      new: The version string with new behavior.
    """
