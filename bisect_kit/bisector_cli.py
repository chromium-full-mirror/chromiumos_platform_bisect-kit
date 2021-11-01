# -*- coding: utf-8 -*-
# Copyright 2019 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Bisector command line interface."""

from __future__ import print_function
import argparse
import collections
import datetime
import json
import logging
import os
import re
import subprocess
import sys
import textwrap
import time

from bisect_kit import cli
from bisect_kit import common
from bisect_kit import core
from bisect_kit import errors
from bisect_kit import math_util
from bisect_kit import strategy
from bisect_kit import util

logger = logging.getLogger(__name__)

DEFAULT_CONFIDENCE = 0.999
END_OF_LOG_FILE_MARKER = '== end of log file ==\n'


def _collect_bisect_result_values(values, line):
  """Collect bisect result values from output line.

  Args:
    values: Collected values are appending to this list.
    line: One line of output string.
  """
  m = re.match(r'^BISECT_RESULT_VALUES=(.+)', line)
  if m:
    try:
      values.extend([float(v) for v in m.group(1).split()])
    except ValueError as e:
      raise errors.InternalError(
          'BISECT_RESULT_VALUES should be list of floats: %r' %
          m.group(1)) from e


def _is_fatal_returncode(returncode):
  return returncode < 0 or returncode >= 128


def _execute_command(args, env=None, stdout_callback=None):
  """Helper of do_evaluate() and do_switch().

  Args:
    args: command line arguments
    env: environment variables
    stdout_callback: Callback function for stdout. Called once per line.

  Returns:
    returncode
  """
  stderr_lines = []
  p = util.Popen(
      args,
      env=env,
      stdout_callback=stdout_callback,
      stderr_callback=stderr_lines.append)
  returncode = p.wait()
  if _is_fatal_returncode(returncode):
    # Only output error messages of child process if it is fatal error.
    print(cli.format_returncode(returncode))
    print(
        'Last stderr lines of "%s"' % subprocess.list2cmdline(args),
        file=sys.stderr)
    print('=' * 40, file=sys.stderr)
    for line in stderr_lines[-50:]:
      print(line, end='', file=sys.stderr)
    print('=' * 40, file=sys.stderr)
  return returncode


def do_evaluate(evaluate_cmd, domain, rev, log_file):
  """Invokes evaluator command.

  The `evaluate_cmd` can get the target revision from the environment variable
  named 'BISECT_REV'.

  The result is determined according to the exit code of evaluator:
    0: 'old'
    1..124, 126, 127: 'new'
    125: 'skip'
    128..255: fatal error
    terminated by signal: fatal error

  p.s. the definition of result is compatible with git-bisect(1).

  It also extracts additional values from evaluate_cmd's stdout lines which
  match the following format:
       BISECT_RESULT_VALUES=<float>[, <float>]*

  Args:
    evaluate_cmd: evaluator command.
    domain: a bisect_kit.core.Domain instance.
    rev: version to evaluate.
    log_file: hint sub-process where to output detail log

  Returns:
    (result, values):
      result is one of 'old', 'new', 'skip', 'fatal'.
      values are additional collected values, like performance score.
  """
  env = os.environ.copy()
  env['BISECT_REV'] = rev
  domain.setenv(env, rev)

  if log_file:
    logger.info('eval log file = %s', log_file)
    env['LOG_FILE'] = log_file

  values = []
  returncode = _execute_command(
      evaluate_cmd,
      env=env,
      stdout_callback=lambda line: _collect_bisect_result_values(values, line))
  if log_file and os.path.exists(log_file):
    with open(log_file, 'a') as f:
      f.write(END_OF_LOG_FILE_MARKER)
  if _is_fatal_returncode(returncode):
    return 'fatal', []
  if returncode == 0:
    return 'old', values
  if returncode == 125:
    return 'skip', values
  return 'new', values


def do_switch(switch_cmd, domain, rev, log_file):
  """Invokes switcher command.

  The `switch_cmd` can get the target revision from the environment variable
  named 'BISECT_REV'.

  The result is determined according to the exit code of switcher:
    0: switch succeeded
    1..127: 'skip'
    128..255: fatal error
    terminated by signal: fatal error

  In other words, any non-fatal errors are considered as 'skip'.

  Args:
    switch_cmd: switcher command.
    domain: a bisect_kit.core.Domain instance.
    rev: version to switch.
    log_file: hint sub-process where to output detail log

  Returns:
    None if switch successfully, otherwise 'skip' or 'fatal'.
  """
  env = os.environ.copy()
  env['BISECT_REV'] = rev
  domain.setenv(env, rev)

  if log_file:
    logger.info('switch log file = %s', log_file)
    env['LOG_FILE'] = log_file

  returncode = _execute_command(switch_cmd, env=env)
  if log_file and os.path.exists(log_file):
    with open(log_file, 'a') as f:
      f.write(END_OF_LOG_FILE_MARKER)
  if _is_fatal_returncode(returncode):
    return 'fatal'
  if returncode != 0:
    return 'skip'
  return None


class BisectorCommandLine:
  """Bisector command line interface.

  The typical usage pattern:

    if __name__ == '__main__':
        BisectorCommandLine(CustomDomain).main()

  where CustomDomain is a derived class of core.BisectDomain. See
  bisect_list.py as example.

  If you need to control the bisector using python code, the easier way is
  passing command line arguments to main() function. For example,
    bisector = Bisector(CustomDomain)
    bisector.main('init', '--old', '123', '--new', '456')
    bisector.main('config', 'switch', 'true')
    bisector.main('config', 'eval', 'true')
    bisector.main('run')
  """

  def __init__(self, domain_cls):
    self.domain_cls = domain_cls
    self.domain = None
    self.session_dir = None
    self.states = None
    self.strategy = None

    self.previous_strategy_state = None

  @property
  def config(self):
    return self.states.config

  def _get_term_map(self):
    return {
        'old': self.config['term_old'],
        'new': self.config['term_new'],
    }

  def _format_result_counter(self, result_counter):
    return core.RevInfo.format_result_counter(result_counter,
                                              self._get_term_map())

  def cmd_reset(self, _opts):
    """Resets bisect session and clean up saved result."""
    self.states.reset()

  def cmd_init(self, opts):
    """Initializes bisect session.

    See init command's help message for more detail.
    """
    try:
      if (opts.old_value is None) != (opts.new_value is None):
        raise errors.ArgumentError('--old_value and --new_value',
                                   'both should be specified')
      if opts.old_value is not None and opts.old_value == opts.new_value:
        raise errors.ArgumentError('--old_value and --new_value',
                                   'their values should be different')
      if opts.recompute_init_values and opts.old_value is None:
        raise errors.ArgumentError(
            '--recompute_init_values',
            '--old_value and --new_value must be specified '
            'when --recompute_init_values is present')

      config, candidates = self.domain_cls.init(opts)
      revlist = candidates['revlist']
      logger.info('found %d candidates to bisect', len(revlist))
      logger.debug('revlist %r', revlist)
      if 'new' not in config:
        config['new'] = opts.new
      if 'old' not in config:
        config['old'] = opts.old
      assert len(revlist) >= 2
      assert config['new'] in revlist
      assert config['old'] in revlist
      old_idx = revlist.index(config['old'])
      new_idx = revlist.index(config['new'])
      assert old_idx < new_idx

      config.update(
          confidence=opts.confidence,
          noisy=opts.noisy,
          term_old=opts.term_old,
          term_new=opts.term_new,
          old_value=opts.old_value,
          new_value=opts.new_value,
          recompute_init_values=opts.recompute_init_values)

      details = candidates.get('details', {})
      self.states.init_states(config, revlist, details)
    except Exception as e:
      exception_name = e.__class__.__name__
      self.states.add_history(
          'failed',
          text='%s: %s' % (exception_name, e),
          exception_name=exception_name)
      self.states.save()
      raise

    self.states.save()

  def _step_log_path(self, rev, step):
    # relative to session directory
    log_path = 'log/{bisector}.{timestamp}.{rev}.{step}.txt'.format(
        bisector=self.domain_cls.__name__,
        rev=util.escape_rev(rev),
        timestamp=time.strftime('%Y%m%d-%H%M%S'),
        step=step)
    return log_path

  def _switch_and_eval(self, rev, prev_rev=None):
    """Switches and evaluates given version.

    If current version equals to target, switch step will be skip.

    Args:
      rev: Target version.
      prev_rev: Previous version.

    Returns:
      (step, sample):
        step: Last step executed ('switch' or 'eval').
        sample (dict): sampling result of `rev`. The dict contains:
          status: Execution result ('old', 'new', 'fatal', or 'skip').
          values: For eval bisection, collected values from eval step.
          switch_time: how much time in switch step
          eval_time: how much time in eval step
    """
    # We treat 'rev' as source of truth. 'index' is redundant and just
    # informative.
    sample = {'rev': rev, 'index': self.states.rev2idx(rev)}
    if prev_rev != rev:
      switch_log = self._step_log_path(rev, 'switch')
      switch_log_fullpath = os.path.join(self.session_dir, switch_log)
      logger.debug('switch to rev=%s', rev)
      t0 = time.time()
      status = do_switch(self.config['switch'], self.domain, rev,
                         switch_log_fullpath)
      t1 = time.time()
      sample['switch_time'] = t1 - t0
      sample['status'] = status
      if os.path.exists(switch_log_fullpath):
        sample['switch_log'] = switch_log
      if status in ('skip', 'fatal'):
        logger.debug('switch failed => %s', status)
        return 'switch', sample

    eval_log = self._step_log_path(rev, 'eval')
    eval_log_fullpath = os.path.join(self.session_dir, eval_log)
    logger.debug('eval rev=%s', rev)
    t0 = time.time()
    status, values = do_evaluate(self.config['eval'], self.domain, rev,
                                 eval_log_fullpath)
    t1 = time.time()
    sample['eval_time'] = t1 - t0
    sample['status'] = status
    if os.path.exists(eval_log_fullpath):
      sample['eval_log'] = eval_log
    if status in ('skip', 'fatal'):
      return 'eval', sample

    if self.strategy.is_value_bisection():
      if not values:
        raise errors.ExecutionFatalError(
            'eval command (%s) terminated normally but did not output values' %
            self.config['eval'])
      sample['values'] = values
      sample['status'] = self.strategy.classify_result_from_values(values)

    return 'eval', sample

  def _estimate_cost(self, prev_rev):
    avg_switch_time = math_util.Averager()
    avg_eval_time = math_util.Averager()
    avg_eval_time_by_status = collections.defaultdict(math_util.Averager)
    for entry in self.states.data['history']:
      if entry['event'] != 'sample':
        continue
      if 'switch_time' in entry:
        avg_switch_time.add(entry['switch_time'])
      if 'eval_time' in entry:
        avg_eval_time.add(entry['eval_time'])
        avg_eval_time_by_status[entry['status']].add(entry['eval_time'])

    if avg_switch_time.count == 0:
      return None
    if avg_eval_time.count == 0:
      return None

    cost_table = []
    for info in self.strategy.rev_info:
      if info.rev == prev_rev:
        switch_time = 0
      else:
        # TODO(kcwu): estimate switch cost sophisticatedly
        switch_time = avg_switch_time.average()
      if (avg_eval_time_by_status['old'].count > 0 and
          avg_eval_time_by_status['new'].count > 0):
        cost = (
            switch_time + avg_eval_time_by_status['old'].average(),
            switch_time + avg_eval_time_by_status['new'].average(),
        )
      else:
        cost = (
            switch_time + avg_eval_time.average(),
            switch_time + avg_eval_time.average(),
        )
      cost_table.append(cost)
    return cost_table

  def _next_idx_iter(self, opts, force):
    prev_rev = None
    if opts.revs:
      for rev in opts.revs:
        idx = self.states.rev2idx(rev)
        logger.info('try idx=%d rev=%s (command line specified)', idx, rev)
        prev_rev = yield idx, rev
        if opts.once:
          break
    else:
      while force or not self.strategy.is_done():
        idx = self.strategy.next_idx(self._estimate_cost(prev_rev))
        rev = self.states.idx2rev(idx)
        logger.info('try idx=%d rev=%s', idx, rev)
        prev_rev = yield idx, rev
        force = False
        if opts.once:
          break

  def _make_decision(self, text):
    if sys.exc_info() != (None, None, None):
      logger.exception('decision: %s', text)
    else:
      logger.info('decision: %s', text)
    self.states.add_history('decision', text=text)

  def _show_step_summary(self):
    """Show current status after each bisect step."""
    current_state = self.strategy.state
    if self.config['recompute_init_values']:
      if (self.previous_strategy_state == self.strategy.INITED and
          current_state == self.strategy.STARTED):
        self._make_decision('After verifying with the initial revisions, '
                            'use %f as the bisect threshold.' %
                            self.strategy.threshold)

    self.previous_strategy_state = current_state

    self.strategy.show_summary()

  def cmd_run(self, opts):
    """Performs bisection.

    See run command's help message for more detail.

    Raises:
      errors.VerificationFailed: The bisection range is verified false. We
          expect 'old' at the first rev and 'new' at last rev.
      errors.UnableToProceed: Too many errors to narrow down further the
          bisection range.
    """
    # Set dummy values in case exception raised before loop.
    idx, rev = -1, None
    try:
      assert self.config.get('switch')
      assert self.config.get('eval')

      self.states.add_history(
          'start_range', old=self.config['old'], new=self.config['new'])
      term_map = self._get_term_map()
      prev_rev = opts.current_rev
      force = opts.force
      idx_gen = self._next_idx_iter(opts, force)
      while True:
        try:
          idx, rev = idx_gen.send(prev_rev)
        except StopIteration:
          break
        if not force:
          # Bail out if bisection range is unlikely true in order to prevent
          # wasting time. This is necessary because some configurations (say,
          # confidence) may be changed before cmd_run() and thus the bisection
          # range becomes not acceptable.
          self.strategy.check_verification_range()

        step, sample = self._switch_and_eval(rev, prev_rev=prev_rev)
        self.states.add_history('sample', **sample)
        self.states.save()
        if sample['status'] == 'fatal':
          raise errors.ExecutionFatalError('%s failed' % step)
        status = term_map.get(sample['status'], sample['status'])
        if 'values' in sample:
          logger.info('rev=%s status => %s: %s', rev, status, sample['values'])
        else:
          logger.info('rev=%s status => %s', rev, status)
        force = False

        # Bail out if bisection range is unlikely true.
        self.strategy.check_verification_range()

        self.strategy.add_sample(idx, **sample)
        self._show_step_summary()

        if step == 'switch' and sample['status'] == 'skip':
          # Previous switch failed and thus the current version is unknown. Set
          # it None, so next switch operation won't be bypassed (due to
          # optimization).
          prev_rev = None
        else:
          prev_rev = rev

      logger.info('done')
      old_idx, new_idx = self.strategy.get_range()
      self.states.add_history('done')
      self.states.save()
    except Exception as e:
      exception_name = e.__class__.__name__
      self.states.add_history(
          'failed',
          text='%s: %s' % (exception_name, e),
          index=idx,
          rev=rev,
          exception_name=exception_name)
      self.states.save()
      raise
    finally:
      if rev and self.strategy.state == self.strategy.STARTED:
        # progress so far
        old_idx, new_idx = self.strategy.get_range()
        self.states.add_history(
            'range',
            old=self.states.idx2rev(old_idx),
            new=self.states.idx2rev(new_idx))
        self.states.save()

  def cmd_view(self, opts):
    """Shows remaining candidates."""
    if not self.states.inited:
      if opts.json:
        print('{}')
      else:
        print('not init yet')
      return

    summary = {
        'rev_info': [],
    }

    for info in self.strategy.rev_info:
      info_dict = info.to_dict()
      detail = self.states.details.get(info.rev, {})
      info_dict.update(detail)
      summary['rev_info'].append(info_dict)

    try:
      old_idx, new_idx = self.strategy.get_range()
      highlight_old_idx, highlight_new_idx = self.strategy.get_range(
          self.strategy.confidence / 10.0)
    except errors.WrongAssumption:
      pass
    else:
      old = self.states.idx2rev(old_idx)
      new = self.states.idx2rev(new_idx)
      summary.update({
          'current_range': (old, new),
          'highlight_range': [
              self.states.idx2rev(highlight_old_idx),
              self.states.idx2rev(highlight_new_idx)
          ],
          'prob': self.strategy.get_prob(),
          'remaining_steps': self.strategy.remaining_steps(),
      })

    if opts.verbose or opts.json:
      interesting_indexes = set(range(len(summary['rev_info'])))
    elif 'current_range' not in summary:
      interesting_indexes = set()
    else:
      interesting_indexes = set([old_idx, new_idx])
      if self.strategy.prob:
        for i, p in enumerate(self.strategy.prob):
          if p > 0.05:
            interesting_indexes.add(i)

    self.domain.fill_candidate_summary(summary)

    if opts.json:
      print(json.dumps(summary, indent=2, sort_keys=True))
    else:
      self.show_bisect_summary(
          summary, interesting_indexes, verbose=opts.verbose)

  def show_bisect_summary(self, summary, interesting_indexes, verbose=False):
    for link in summary.get('links', []):
      if 'name' in link and 'url' in link:
        print('%s: %s' % (link['name'], link['url']))
      if 'note' in link:
        print(link['note'])

    if 'current_range' in summary:
      old, new = summary['current_range']
      old_idx = self.states.data['revlist'].index(old)
      new_idx = self.states.data['revlist'].index(new)
      print('Range: (%s, %s], %s revs left' % (old, new, (new_idx - old_idx)))
      if summary.get('remaining_steps'):
        print('(roughly %d steps)' % summary['remaining_steps'])
    else:
      old_idx, new_idx = None, None

    for i, rev_info in enumerate(summary['rev_info']):
      if not any([
          verbose,
          old_idx is not None and old_idx <= i <= new_idx,
          rev_info['result_counter'],
      ]):
        continue

      detail = []
      if self.strategy.is_noisy() and summary.get('prob'):
        detail.append('%.4f%%' % (summary['prob'][i] * 100))
      if rev_info['result_counter']:
        detail.append(self._format_result_counter(rev_info['result_counter']))
      values = sorted(rev_info['values'])
      if len(values) == 1:
        detail.append('%.3f' % values[0])
      elif len(values) > 1:
        detail.append('n=%d,avg=%.3f,median=%.3f,min=%.3f,max=%.3f' %
                      (len(values), sum(values) / len(values),
                       values[len(values) // 2], values[0], values[-1]))

      print('[%d] %s\t%s' % (i, rev_info['rev'], ' '.join(detail)))
      if i in interesting_indexes:
        if 'comment' in rev_info:
          print('\t%s' % rev_info['comment'])
        for action in rev_info.get('actions', []):
          if 'rev' in action and 'commit_summary' in action:
            print('%s %r' % (action['rev'][:10], action['commit_summary']))
          if 'link' in action:
            print('\t%s' % action['link'])

  def _strategy_factory(self):
    if not self.states.inited:
      return None
    term_map = {
        'old': self.config['term_old'],
        'new': self.config['term_new'],
    }
    rev_info = self.states.get_rev_info(term_map)
    assert rev_info
    return strategy.NoisyBinarySearch(
        rev_info,
        self.states.rev2idx(self.config['old']),
        self.states.rev2idx(self.config['new']),
        old_value=self.config['old_value'],
        new_value=self.config['new_value'],
        term_map=term_map,
        recompute_init_values=self.config['recompute_init_values'],
        confidence=self.config['confidence'],
        observation=self.config['noisy'])

  def current_status(self, session=None):
    """Gets current bisect status.

    Returns:
      A dict describing current status. It contains following items:
        inited: True iff the session file is initialized (init command has been
            invoked). If not, below items are omitted.
        old: Start of current estimated range.
        new: End of current estimated range.
        verified: The bisect range is already verified.
        estimated_noise: New estimated noise.
        done: True if bisection is done, otherwise False.
    """
    self._create_states(session=session)
    if not self.states.inited and not self.states.load_states():
      # Unable to load session file.
      return dict(inited=False)
    if not self.states.inited:
      # Session file loaded but the initialization is not successful.
      return dict(inited=False)

    self.strategy = self._strategy_factory()
    left, right = self.strategy.get_range()
    estimated_noise = self.strategy.get_noise_observation()

    result = dict(
        inited=True,
        old=self.states.idx2rev(left),
        new=self.states.idx2rev(right),
        verified=self.strategy.is_range_verified(),
        estimated_noise=estimated_noise,
        done=self.strategy.is_done())
    return result

  def cmd_log(self, opts):
    """Prints what has been done so far."""
    history = []
    for entry in self.states.data['history']:
      if opts.before and entry['timestamp'] >= opts.before:
        continue
      if opts.after and entry['timestamp'] <= opts.after:
        continue
      history.append(entry)

    if opts.json:
      print(json.dumps(history, indent=2))
      return

    for entry in history:
      entry_time = datetime.datetime.fromtimestamp(int(entry['timestamp']))
      if entry.get('event', 'sample') == 'sample':
        if entry.get('times', 1) > 1:
          status = '%s*%d' % (entry['status'], entry['times'])
        else:
          status = entry['status']
        print('{datetime} {rev} {status} {values} {comment}'.format(
            datetime=entry_time,
            rev=entry['rev'],
            status=status,
            values=entry.get('values', ''),
            comment=entry.get('comment', '')))
      else:
        print('%s %r' % (entry_time, entry))

  def cmd_next(self, opts):
    """Prints next suggested rev to bisect."""
    if self.strategy.is_done():
      print('done')
      return

    idx = self.strategy.next_idx(self._estimate_cost(opts.current_rev))
    rev = self.states.idx2rev(idx)
    print(rev)

  def cmd_switch(self, opts):
    """Switches to given rev without eval."""
    assert self.config.get('switch')

    if opts.rev == 'next':
      idx = self.strategy.next_idx(self._estimate_cost(opts.current_rev))
      rev = self.states.idx2rev(idx)
    else:
      rev = self.domain_cls.intra_revtype(opts.rev)
    assert rev

    switch_log = self._step_log_path(rev, 'switch')
    logger.info('switch to %s', rev)
    status = do_switch(self.config['switch'], self.domain, rev, switch_log)
    if status:
      print('switch failed')

  def _add_revs_status_helper(self, revs, status):
    if self.strategy.is_value_bisection():
      assert status not in ('old', 'new')
    for rev, times in revs:
      idx = self.states.rev2idx(rev)
      sample = {'rev': rev, 'status': status}
      # times=1 is default in the loader. Add 'times' entry only if necessary
      # in order to simplify the dict.
      if times > 1:
        sample['times'] = times
      self.states.add_history('sample', **sample)
      self.states.save()
      self.strategy.add_sample(idx, **sample)

  def cmd_new(self, opts):
    """Tells bisect engine the said revs have "new" behavior."""
    logger.info('set [%s] as %s', opts.revs, self.config['term_new'])
    self._add_revs_status_helper(opts.revs, 'new')

  def cmd_old(self, opts):
    """Tells bisect engine the said revs have "old" behavior."""
    logger.info('set [%s] as %s', opts.revs, self.config['term_old'])
    self._add_revs_status_helper(opts.revs, 'old')

  def cmd_skip(self, opts):
    """Tells bisect engine the said revs have "skip" behavior."""
    logger.info('set [%s] as skip', opts.revs)
    self._add_revs_status_helper(opts.revs, 'skip')

  def _create_states(self, session=None):
    self.session_dir = common.determine_session_dir(session)
    session_file = os.path.join(self.session_dir, self.domain_cls.__name__)

    if self.states:
      assert self.states.session_file == session_file
    else:
      self.states = core.BisectStates(session_file)

  def cmd_config(self, opts):
    """Configures additional setting.

    See config command's help message for more detail.
    """
    if not self.states.inited and not self.states.load_states():
      raise errors.Uninitialized
    self.domain = self.domain_cls(self.states.config)
    if not opts.value:
      print(self.states.config[opts.key])
      return

    if opts.key in ['switch', 'eval']:
      result = cli.check_executable(opts.value[0])
      if result:
        raise errors.ArgumentError('%s command' % opts.key, result)

      self.states.config[opts.key] = opts.value

    elif opts.key == 'confidence':
      if len(opts.value) != 1:
        raise errors.ArgumentError(
            'confidence value',
            'expected 1 value, %d values given' % len(opts.value))
      try:
        self.states.config[opts.key] = float(opts.value[0])
      except ValueError as e:
        raise errors.ArgumentError('confidence value',
                                   'invalid float value: %r' %
                                   opts.value[0]) from e

    elif opts.key == 'noisy':
      if len(opts.value) != 1:
        raise errors.ArgumentError(
            'noisy value',
            'expected 1 value, %d values given' % len(opts.value))
      self.states.config[opts.key] = opts.value[0]

    elif opts.key in ('term_old', 'term_new'):
      if len(opts.value) != 1:
        raise errors.ArgumentError(
            opts.key, 'expected 1 value, %d values given' % len(opts.value))
      self.states.config[opts.key] = opts.value[0]

    else:
      # unreachable
      assert 0

    self.states.save()

  def create_argument_parser(self, prog):
    if self.domain_cls.help:
      description = self.domain_cls.help
    else:
      description = 'Bisector for %s' % self.domain_cls.__name__
    description += textwrap.dedent("""
        When running switcher and evaluator, it will set BISECT_REV environment
        variable, indicates current rev to switch/evaluate.
    """)

    parents = [common.common_argument_parser, common.session_optional_parser]
    parser = argparse.ArgumentParser(
        prog=prog,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=description)
    subparsers = parser.add_subparsers(
        dest='command', title='commands', metavar='<command>', required=True)

    parser_reset = subparsers.add_parser(
        'reset',
        help='Reset bisect session and clean up saved result',
        parents=parents)
    parser_reset.set_defaults(func=self.cmd_reset)

    parser_init = subparsers.add_parser(
        'init',
        help='Initializes bisect session',
        parents=parents,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=textwrap.dedent("""
          Besides arguments for 'init' command, you also need to set 'switch'
          and 'eval' command line via 'config' command.
            $ bisector config switch <switch command and arguments>
            $ bisector config eval <eval command and arguments>

          The value of --noisy and --confidence could be changed by 'config'
          command after 'init' as well.
        """))
    parser_init.add_argument(
        '--old',
        required=True,
        type=self.domain_cls.revtype,
        help='Start of bisect range, which has old behavior')
    parser_init.add_argument(
        '--new',
        required=True,
        type=self.domain_cls.revtype,
        help='End of bisect range, which has new behavior')
    parser_init.add_argument(
        '--term_old',
        default='OLD',
        help='Alternative term for "old" state (default: %(default)r)')
    parser_init.add_argument(
        '--term_new',
        default='NEW',
        help='Alternative term for "new" state (default: %(default)r)')
    parser_init.add_argument(
        '--noisy',
        help='Enable noisy binary search and specify prior result. '
        'For example, "old=1/10,new=2/3" means old fail rate is 1/10 '
        'and new fail rate increased to 2/3. '
        'Skip if not flaky, say, "new=2/3" means old is always good.')
    parser_init.add_argument(
        '--old_value',
        type=float,
        help='For performance test, value of old behavior')
    parser_init.add_argument(
        '--new_value',
        type=float,
        help='For performance test, value of new behavior')
    parser_init.add_argument(
        '--recompute_init_values',
        action='store_true',
        help='For performance test, recompute initial values')
    parser_init.add_argument(
        '--confidence',
        type=float,
        default=DEFAULT_CONFIDENCE,
        help='Confidence level (default: %(default)r)')
    parser_init.set_defaults(func=self.cmd_init)
    self.domain_cls.add_init_arguments(parser_init)

    parser_config = subparsers.add_parser(
        'config', help='Configures additional setting', parents=parents)
    parser_config.add_argument(
        'key',
        choices=[
            'switch', 'eval', 'confidence', 'noisy', 'term_old', 'term_new'
        ],
        metavar='key',
        help='What config to change. choices=[%(choices)s]')
    parser_config.add_argument(
        'value', nargs=argparse.REMAINDER, help='New value')
    parser_config.set_defaults(func=self.cmd_config)

    parser_run = subparsers.add_parser(
        'run',
        help='Performs bisection',
        parents=parents,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=textwrap.dedent("""
        This command does switch and eval to determine candidates having old or
        new behavior.

        By default, it attempts to try versions in binary search manner until
        found the first version having new behavior.

        If version numbers are specified on command line, it just tries those
        versions and record the result.

        Example:
          Bisect automatically.
          $ %(prog)s

          Switch and run version "2.13" and "2.14" and then stop.
          $ %(prog)s 2.13 2.14
        """))
    parser_run.add_argument(
        '-1', '--once', action='store_true', help='Only run one step')
    parser_run.add_argument(
        '--force',
        action='store_true',
        help="Run at least once even it's already done")
    parser_run.add_argument(
        '--current_rev',
        type=self.domain_cls.intra_revtype,
        help='give hint the current rev')
    parser_run.add_argument(
        'revs',
        nargs='*',
        type=self.domain_cls.intra_revtype,
        help='revs to switch+eval; '
        'default is calculating automatically and run until done')
    parser_run.set_defaults(func=self.cmd_run)

    parser_switch = subparsers.add_parser(
        'switch', help='Switch to given rev without eval', parents=parents)
    parser_switch.add_argument(
        'rev',
        type=cli.argtype_multiplexer(self.domain_cls.intra_revtype,
                                     cli.argtype_re('next', 'next')))
    parser_switch.add_argument(
        '--current_rev',
        type=self.domain_cls.intra_revtype,
        help='if rev="next", give hint the current rev')
    parser_switch.set_defaults(func=self.cmd_switch)

    parser_old = subparsers.add_parser(
        'old',
        help='Tells bisect engine the said revs have "old" behavior',
        parents=parents)
    parser_old.add_argument(
        'revs',
        nargs='+',
        type=cli.argtype_multiplier(self.domain_cls.intra_revtype))
    parser_old.set_defaults(func=self.cmd_old)

    parser_new = subparsers.add_parser(
        'new',
        help='Tells bisect engine the said revs have "new" behavior',
        parents=parents)
    parser_new.add_argument(
        'revs',
        nargs='+',
        type=cli.argtype_multiplier(self.domain_cls.intra_revtype))
    parser_new.set_defaults(func=self.cmd_new)

    parser_skip = subparsers.add_parser(
        'skip',
        help='Tells bisect engine the said revs have "skip" behavior',
        parents=parents)
    parser_skip.add_argument(
        'revs',
        nargs='+',
        type=cli.argtype_multiplier(self.domain_cls.intra_revtype))
    parser_skip.set_defaults(func=self.cmd_skip)

    parser_view = subparsers.add_parser(
        'view', help='Shows current progress and candidates', parents=parents)
    parser_view.add_argument('--verbose', '-v', action='store_true')
    parser_view.add_argument('--json', action='store_true')
    parser_view.set_defaults(func=self.cmd_view)

    parser_log = subparsers.add_parser(
        'log', help='Prints what has been done so far', parents=parents)
    parser_log.add_argument('--before', type=float)
    parser_log.add_argument('--after', type=float)
    parser_log.add_argument(
        '--json', action='store_true', help='Machine readable output')
    parser_log.set_defaults(func=self.cmd_log)

    parser_next = subparsers.add_parser(
        'next', help='Prints next suggested rev to bisect', parents=parents)
    parser_next.add_argument(
        '--current_rev',
        type=self.domain_cls.intra_revtype,
        help='give hint the current rev')
    parser_next.set_defaults(func=self.cmd_next)

    return parser

  def main(self, *args, **kwargs):
    """Command line main function.

    Args:
      *args: Command line arguments.
      **kwargs: additional non command line arguments passed by script code.
        {
          'prog': Program name; optional.
        }
    """
    common.init()
    parser = self.create_argument_parser(kwargs.get('prog'))
    opts = parser.parse_args(args or None)
    common.config_logging(opts)

    self._create_states(session=opts.session)
    if opts.command not in ('init', 'reset', 'config'):
      if not self.states.inited and not self.states.load_states():
        raise errors.Uninitialized
      self.domain = self.domain_cls(self.states.config)
      self.strategy = self._strategy_factory()

    return opts.func(opts)
