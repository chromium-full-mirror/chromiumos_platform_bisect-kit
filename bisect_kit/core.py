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

from bisect_kit import math_util

logger = logging.getLogger(__name__)


class RevInfo:
  """Aggregated evaluation result of one revision.

  The count of results can be easily accessed by using [] operator.

  Attributes:
    rev (str): revision id
    result_counter (dict): count of results, example: dict(new=3, old=2)
    values (list of list of numbers):
        list of values collected during evaluations. There could be more than
        one value for a single evaluation.
    switch_time: total duration of switch step for such revision
    eval_time: total duration of eval step for such revision
  """

  def __init__(self, rev, term_map=None):
    self.rev = rev

    self.term_map = term_map or {}
    self.result_counter = {}
    self.values = []
    self.switch_time = 0
    self.eval_time = 0

  def to_dict(self):
    result = vars(self).copy()
    del result['term_map']
    result['averages'] = self.averages()
    # backward compatible with old behavior
    # TODO(kcwu): remove this after callers migrated
    result['values'] = self.averages()
    return result

  def __getitem__(self, key):
    return self.result_counter.get(key, 0)

  def __setitem__(self, key, value):
    """Shortcut of add_sample()."""
    self.result_counter[key] = value
    # Prune dead entries, so it looks good if we output 'result_counter'
    # directly.
    if value == 0:
      del self.result_counter[key]

  @classmethod
  def format_result_counter(cls, result_counter, term_map):
    result = []
    for status, count in sorted(result_counter.items()):
      status = term_map.get(status, status)
      result.append('%s:%s' % (status, count))
    return ', '.join(result)

  def counter_string(self):
    return self.format_result_counter(self.result_counter, self.term_map)

  def summary(self):
    """Summary of the result of this revision."""
    averages = sorted(self.averages())
    if not averages:
      return self.counter_string()
    if len(averages) == 1:
      return '%s %.3f' % (self.counter_string(), averages[0])

    return '%s n=%d,avg=%.3f,median=%.3f,min=%.3f,max=%.3f' % (
        self.counter_string(), len(averages), math_util.average(averages),
        averages[len(averages) // 2], averages[0], averages[-1])

  def averages(self):
    """Takes the average of sample values.

    In other words, one (average) value for each sample.
    """
    return [math_util.average(v) for v in self.values]

  def add_sample(self,
                 status=None,
                 values=None,
                 times=None,
                 switch_time=None,
                 eval_time=None,
                 **kwargs):
    if 'rev' in kwargs:
      assert kwargs['rev'] == self.rev
    assert status in (None, 'init', 'old', 'new', 'skip', 'fatal')

    if times is None:
      times = 1
    if values:
      assert isinstance(values, list)
      assert times == 1
      self.values.append(values)
    self[status] += times
    if switch_time:
      self.switch_time += switch_time
    if eval_time:
      self.eval_time += eval_time

  def reclassify(self, old_avg, threshold, new_avg):
    """Reclassify status by values."""
    assert self['init'] == len(self.values) and self.values
    assert self['old'] + self['new'] == 0
    for avg in self.averages():
      if old_avg < new_avg:
        status = 'old' if avg < threshold else 'new'
      else:
        status = 'new' if avg < threshold else 'old'
      self['init'] -= 1
      self[status] += 1


class States:
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
    with open(self.session_file) as f:
      self.set_data(json.load(f))
    return True

  def save(self):
    dirname = os.path.dirname(self.session_file)
    if not os.path.exists(dirname):
      os.makedirs(dirname)

    tmp_fn = tempfile.mktemp()
    with open(tmp_fn, 'w') as f:
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
    super().__init__(session_file)

    # Mapping of rev to idx; constructed from data['revlist'].
    self.rev_index = {}

  @property
  def config(self):
    return self.data['config']

  @property
  def details(self):
    return self.data['details']

  def init(self, config, revlist, details=None):
    """Initializes attributes data, rev_info and rev_index.

    Args:
      config: bisection configuration.
      revlist: version list.
      details: dict of rev details.
    """
    self.set_data(
        dict(
            # Bisection configurations (dict), values are determined by cmd_init
            # and each domain's init functions. There will be 'old' and 'new' at
            # least.
            config=config,
            # List of bisect candidates (version numbers).
            revlist=revlist,
            details=details or {},
            # What have been done so far. Each entry contains at least
            # timestamp, rev, and result.
            history=[]))

  def set_data(self, data):
    super().set_data(data)

    self.rev_index = {}
    for i, rev in enumerate(self.data['revlist']):
      self.rev_index[rev] = i

  def load_rev_info(self, term_map=None):
    rev_info = []
    for rev in self.data['revlist']:
      rev_info.append(RevInfo(rev, term_map=term_map))
    for entry in self.data['history']:
      if entry.get('event', 'sample') != 'sample':
        continue
      idx = self.rev2idx(entry['rev'])
      rev_info[idx].add_sample(**entry)
    return rev_info

  def idx2rev(self, idx):
    return self.data['revlist'][idx]

  def rev2idx(self, rev):
    return self.rev_index[rev]

  def add_history(self, event, **kwargs):
    entry = dict(event=event, timestamp=time.time(), **kwargs)
    self.data['history'].append(entry)


class BisectDomain:
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
      (config, revdata):
        config (dict): values saved to the per session storage. The bisection
            range could be adjusted by setting config['old'] and config['new'].
        revdata (dict):
          revlist: list of version strings need to bisect. The
              bisect range `old` and `new` must be inside the list (but
              unnecessary to be the first and the last one).
          details (dict): detail information for each rev
    """

  def setenv(self, env, rev):
    """Sets environment variables needed by switchers and evaluators.

    Args:
      env: The dict to hold environment variables.
      rev: Current bisecting version.
    """

  def fill_candidate_summary(self, summary):
    """Fill detail of candidates.

    This is for 'view' subcommand to display information of remaining
    candidates.

    Args:
      summary: dict of candidate details. It is prepopulated following fields:
          rev_info:
          current_range:
          highlight_range:
          prob:
          remaining_steps:
        This method can modify or fill more fields into the dict.
          links:
          rev_info:
    """
