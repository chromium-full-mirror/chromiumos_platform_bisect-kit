# -*- coding: utf-8 -*-
# Copyright 2017 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Bisect command line interface."""

from __future__ import print_function
import argparse
import datetime
import json
import logging
import os
import re
import textwrap
import time

from bisect_kit import common
from bisect_kit import configure
from bisect_kit import core
from bisect_kit import errors
from bisect_kit import strategy
from bisect_kit import util

logger = logging.getLogger(__name__)

DEFAULT_SESSION_NAME = 'default'
DEFAULT_CONFIDENCE = 0.999


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
    full_msg = '%s (example value: %s)' % (self.msg, ', '.join(self.example))
    super(ArgTypeError, self).__init__(full_msg)


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
  except ValueError:
    raise ArgTypeError('should be a number', '123')


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
    raise ArgTypeError('should match "%s"' % pattern,
                       '"%s" like %s' % (pattern, example))

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
      raise ArgTypeError(e.msg, e.example + [e.example[0] + '*3'])

  return helper


def argtype_dir_path(s):
  """Validate argument is an existing directory.

  Args:
    s: string to validate.

  Raises:
    ArgTypeError if the path is not a directory.
  """
  if not os.path.exists(s):
    raise ArgTypeError('should be an existing directory', '/path/to/somewhere')
  if not os.path.isdir(s):
    raise ArgTypeError('should be a directory', '/path/to/somewhere')

  # Normalize, trim trailing path separators.
  if len(s) > 1 and s[-1] == os.path.sep:
    s = s[:-1]
  return s


def _collect_bisect_result_values(values, line):
  """Collect bisect result values from output line.

  Args:
    values: Collected values are appending to this list.
    line: One line of output string.
  """
  m = re.match(r'^BISECT_RESULT_VALUES=(.+)', line)
  if m:
    try:
      values.extend(map(float, m.group(1).split()))
    except ValueError:
      raise errors.InternalError(
          'BISECT_RESULT_VALUES should be list of floats: %r' % m.group(1))


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


def do_evaluate(evaluate_cmd, domain, rev):
  """Invokes evaluator command.

  The `evaluate_cmd` can get the target revision from the environment variable
  named 'BISECT_REV'.

  The result is determined according to the exit code of evaluator:
    0: 'old'
    1..124: 'new'
    125: 'skip'
    126, 127: fatal error
    terminated by signal: fatal error

  p.s. the definition of result is compatible with git-bisect(1).

  It also extracts additional values from evaluate_cmd's stdout lines which
  match the following format:
       BISECT_RESULT_VALUES=<float>[, <float>]*

  Args:
    evaluate_cmd: evaluator command.
    domain: a bisect_kit.core.Domain instance.
    rev: version to evaluate.

  Returns:
    (result, values):
      result is one of 'old', 'new', 'skip'.
      values are additional collected values, like performance score.

  Raises:
    errors.ExecutionFatalError if evaluator returned fatal error code.
  """
  env = os.environ.copy()
  env['BISECT_REV'] = rev
  domain.setenv(env, rev)

  values = []
  p = util.Popen(
      evaluate_cmd,
      env=env,
      stdout_callback=lambda line: _collect_bisect_result_values(values, line))
  returncode = p.wait()
  if returncode < 0 or returncode > 125:
    raise errors.ExecutionFatalError('eval failed: %s' % returncode)

  if returncode == 0:
    return 'old', values
  if returncode == 125:
    return 'skip', values
  return 'new', values


def do_switch(switch_cmd, domain, rev):
  """Invokes switcher command.

  The `switch_cmd` can get the target revision from the environment variable
  named 'BISECT_REV'.

  The result is determined according to the exit code of switcher:
    0: switch succeeded
    1..125: 'skip'
    126, 127: fatal error
    terminated by signal: fatal error

  In other words, any non-fatal errors are considered as 'skip'.

  Args:
    switch_cmd: switcher command.
    domain: a bisect_kit.core.Domain instance.
    rev: version to switch.

  Returns:
    None if switch successfully, 'skip' otherwise.

  Raises:
    errors.ExecutionFatalError if switcher returned fatal error code.
  """
  env = os.environ.copy()
  env['BISECT_REV'] = rev
  domain.setenv(env, rev)

  returncode = util.call(*switch_cmd, env=env)
  if returncode < 0 or returncode > 125:
    raise errors.ExecutionFatalError('switch failed: %s' % returncode)

  if returncode != 0:
    return 'skip'
  return None


class BisectorCommandLine(object):
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
    self.states = None
    self.strategy = None

  @property
  def config(self):
    return self.states.config

  def _format_status(self, status):
    if status in ('old', 'new'):
      return '%s behavior' % status
    return status

  def _add_sample(self, rev, status, **kwargs):
    idx = self.states.rev2idx(rev)
    self.states.add_sample(idx, status, **kwargs)
    self.strategy.update(idx, status)

  def cmd_reset(self, _opts):
    """Resets bisect session and clean up saved result."""
    self.states.reset()

  def cmd_init(self, opts):
    """Initializes bisect session.

    See init command's help message for more detail.
    """
    config, revlist = self.domain_cls.init(opts)
    logger.info('found %d revs to bisect', len(revlist))
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

    config.update(confidence=opts.confidence, noisy=opts.noisy)

    self.states.init(config, revlist)
    self.states.save()

  def _switch_and_eval(self, rev, prev_rev=None):
    """Switches and evaluates given version.

    If current version equals to target, switch step will be skip.

    Args:
      rev: Target version.
      prev_rev: Previous version.

    Returns:
      (step, status, values):
        step: Last step executed ('switch' or 'eval').
        status: Execution result ('old', 'new', or 'skip').
        values: Collected values from eval step. None if last step is 'switch'.
    """
    if prev_rev != rev:
      logger.debug('switch to rev=%s', rev)
      t0 = time.time()
      status = do_switch(self.config['switch'], self.domain, rev)
      t1 = time.time()
      if status == 'skip':
        logger.debug('switch failed => skip')
        return 'switch', status, None
      self.states.data['stats']['switch_count'] += 1
      self.states.data['stats']['switch_time'] += t1 - t0

    logger.debug('eval rev=%s', rev)
    t0 = time.time()
    status, values = do_evaluate(self.config['eval'], self.domain, rev)
    t1 = time.time()
    if status == 'skip':
      return 'eval', status, values
    self.states.data['stats']['eval_count'] += 1
    self.states.data['stats']['eval_time'] += t1 - t0

    return 'eval', status, values

  def _next_idx_iter(self, opts, force):
    if opts.revs:
      for rev in opts.revs:
        idx = self.states.rev2idx(rev)
        logger.info('try idx=%d rev=%s (command line specified)', idx, rev)
        yield idx, rev
        if opts.once:
          break
    else:
      while force or not self.strategy.is_done():
        idx = self.strategy.next_idx()
        rev = self.states.idx2rev(idx)
        logger.info('try idx=%d rev=%s', idx, rev)
        yield idx, rev
        force = False
        if opts.once:
          break

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
      self.strategy.rebuild()

      prev_rev = None
      force = opts.force
      for idx, rev in self._next_idx_iter(opts, force):
        if not force:
          # Bail out if bisection range is unlikely true in order to prevent
          # wasting time. This is necessary because some configurations (say,
          # confidence) may be changed before cmd_run() and thus the bisection
          # range becomes not acceptable.
          self.strategy.check_verification_range()

        step, status, values = self._switch_and_eval(rev, prev_rev=prev_rev)
        logger.info('rev=%s status => %s', rev, self._format_status(status))
        force = False

        self._add_sample(rev, status, values=values)
        self.states.save()

        # Bail out if bisection range is unlikely true.
        self.strategy.check_verification_range()

        if status == 'skip':
          current_state = self.states.get(idx)
          if current_state['skip'] > (
              current_state['old'] + current_state['new'] + 1) * 5:
            message = 'too much "skip" for rev=%r' % rev
            raise errors.UnableToProceed(message)

        self.strategy.show_summary()

        if step == 'switch' and status == 'skip':
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
          'failed', text='%s: %s' % (exception_name, e), index=idx, rev=rev)
      self.states.save()
      raise
    finally:
      if rev:
        # progress so far
        old_idx, new_idx = self.strategy.get_range()
        self.states.add_history(
            'range',
            old=self.states.idx2rev(old_idx),
            new=self.states.idx2rev(new_idx))
        self.states.save()

  def cmd_view(self, opts):
    """Shows remaining candidates."""
    try:
      self.strategy.rebuild()
      # Rebuild twice in order to re-estimate noise.
      self.strategy.rebuild()
    except errors.VerificationFailed:
      # Do nothing, go ahead to show existing information anyway.
      pass

    old_idx, new_idx = self.strategy.get_range()
    old, new = map(self.states.idx2rev, [old_idx, new_idx])
    highlight_old_idx, highlight_new_idx = self.strategy.get_range(
        self.strategy.confidence / 10.0)
    summary = {
        'rev_info': [vars(info).copy() for info in self.states.rev_info],
        'current_range': (old, new),
        'highlight_range':
            map(self.states.idx2rev, [highlight_old_idx, highlight_new_idx]),
        'prob':
            self.strategy.prob,
        'remaining_steps':
            self.strategy.remaining_steps(),
    }

    if opts.verbose or opts.json:
      interesting_indexes = set(range(len(summary['rev_info'])))
    else:
      interesting_indexes = set([old_idx, new_idx])
      if self.strategy.prob:
        for i, p in enumerate(self.strategy.prob):
          if p > 0.05:
            interesting_indexes.add(i)

    self.domain.fill_candidate_summary(summary, interesting_indexes)

    if opts.json:
      print(json.dumps(summary, indent=2, sort_keys=True))
    else:
      self.show_summary(summary, interesting_indexes, verbose=opts.verbose)

  def show_summary(self, summary, interesting_indexes, verbose=False):
    old, new = summary['current_range']
    old_idx, new_idx = map(self.states.data['revlist'].index, [old, new])

    for link in summary.get('links', []):
      print('%s: %s' % (link['name'], link['url']))
      if 'note' in link:
        print(link['note'])

    print('Range: (%s, %s], %s revs left' % (old, new, (new_idx - old_idx)))
    if summary.get('remaining_steps'):
      print('(roughly %d steps)' % summary['remaining_steps'])

    for i, rev_info in enumerate(summary['rev_info']):
      if (not verbose and not old_idx <= i <= new_idx and
          not rev_info['result_counter']):
        continue

      detail = []
      if self.strategy.is_noisy() and summary['prob']:
        detail.append('%.4f%%' % (summary['prob'][i] * 100))
      if rev_info['result_counter']:
        detail.append(str(rev_info['result_counter']))
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
          if 'text' in action:
            print('\t%s' % action['text'])
          if 'link' in action:
            print('\t%s' % action['link'])

  def current_status(self, session=None, session_base=None):
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
    self._create_states(session=session, session_base=session_base)
    if self.states.load():
      self.strategy = strategy.NoisyBinarySearch(
          self.states.rev_info,
          self.states.rev2idx(self.config['old']),
          self.states.rev2idx(self.config['new']),
          confidence=self.config['confidence'],
          observation=self.config['noisy'])
      try:
        self.strategy.rebuild()
      except errors.VerificationFailed:
        # Do nothing, go ahead to show existing information anyway.
        pass
      left, right = self.strategy.get_range()
      estimated_noise = self.strategy.get_noise_observation()

      result = dict(
          inited=True,
          old=self.states.idx2rev(left),
          new=self.states.idx2rev(right),
          verified=self.strategy.is_range_verified(),
          estimated_noise=estimated_noise,
          done=self.strategy.is_done())
    else:
      result = dict(inited=False)
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
        print('{datetime} {rev} {status} {values} {comment}'.format(
            datetime=entry_time,
            rev=entry['rev'],
            status=entry['status'] + ('*%d' % entry['times']
                                      if entry.get('times', 1) > 1 else ''),
            values=entry.get('values', ''),
            comment=entry.get('comment', '')))
      else:
        print('%s %r' % (entry_time, entry))

  def cmd_next(self, _opts):
    """Prints next suggested rev to bisect."""
    self.strategy.rebuild()
    if self.strategy.is_done():
      print('done')
      return

    idx = self.strategy.next_idx()
    rev = self.states.idx2rev(idx)
    print(rev)

  def cmd_switch(self, opts):
    """Switches to given rev without eval."""
    assert self.config.get('switch')

    self.strategy.rebuild()

    if opts.rev == 'next':
      idx = self.strategy.next_idx()
      rev = self.states.idx2rev(idx)
    else:
      rev = self.domain_cls.intra_revtype(opts.rev)
    assert rev

    logger.info('switch to %s', rev)
    status = do_switch(self.config['switch'], self.domain, rev)
    if status:
      print('switch failed')

  def _add_revs_status_helper(self, revs, status):
    self.strategy.rebuild()
    for rev, times in revs:
      self._add_sample(rev, status, times=times, comment='manual')
    self.states.save()

  def cmd_new(self, opts):
    """Tells bisect engine the said revs have "new" behavior."""
    logger.info('set [%s] as new', opts.revs)
    self._add_revs_status_helper(opts.revs, 'new')

  def cmd_old(self, opts):
    """Tells bisect engine the said revs have "old" behavior."""
    logger.info('set [%s] as old', opts.revs)
    self._add_revs_status_helper(opts.revs, 'old')

  def cmd_skip(self, opts):
    """Tells bisect engine the said revs have "skip" behavior."""
    logger.info('set [%s] as skip', opts.revs)
    self._add_revs_status_helper(opts.revs, 'skip')

  def _create_states(self, session=None, session_base=None):
    if not session:
      session = DEFAULT_SESSION_NAME
    if not session_base:
      session_base = configure.get('SESSION_BASE', common.DEFAULT_SESSION_BASE)

    session_file = os.path.join(session_base, session, self.domain_cls.__name__)

    if self.states:
      assert self.states.session_file == session_file
    else:
      self.states = core.BisectStates(session_file)

  def cmd_config(self, opts):
    """Configures additional setting.

    See config command's help message for more detail.
    """
    self.states.load()
    self.domain = self.domain_cls(self.states.config)
    if not opts.value:
      print(self.states.config[opts.key])
      return

    if opts.key in ['switch', 'eval']:
      result = check_executable(opts.value[0])
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
      except ValueError:
        raise errors.ArgumentError('confidence value',
                                   'invalid float value: %r' % opts.value[0])

    elif opts.key == 'noisy':
      if len(opts.value) != 1:
        raise errors.ArgumentError(
            'noisy value',
            'expected 1 value, %d values given' % len(opts.value))
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
    description += textwrap.dedent('''
        When running switcher and evaluator, it will set BISECT_REV environment
        variable, indicates current rev to switch/evaluate.
    ''')

    parser = argparse.ArgumentParser(
        prog=prog,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=description)
    common.add_common_arguments(parser)
    parser.add_argument(
        '--session_base',
        default=configure.get('SESSION_BASE', common.DEFAULT_SESSION_BASE),
        help='Directory to store sessions (default: %(default)r)')
    parser.add_argument(
        '--session',
        default=DEFAULT_SESSION_NAME,
        help='Session name (default: %(default)r)')
    subparsers = parser.add_subparsers(
        dest='command', title='commands', metavar='<command>')

    parser_reset = subparsers.add_parser(
        'reset', help='Reset bisect session and clean up saved result')
    parser_reset.set_defaults(func=self.cmd_reset)

    parser_init = subparsers.add_parser(
        'init',
        help='Initializes bisect session',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=textwrap.dedent('''
          Besides arguments for 'init' command, you also need to set 'switch'
          and 'eval' command line via 'config' command.
            $ bisector config switch <switch command and arguments>
            $ bisector config eval <eval command and arguments>

          The value of --noisy and --confidence could be changed by 'config'
          command after 'init' as well.
        '''))
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
        '--noisy',
        help='Enable noisy binary search and specify prior result. '
        'For example, "old=1/10,new=2/3" means old fail rate is 1/10 '
        'and new fail rate increased to 2/3. '
        'Skip if not flaky, say, "new=2/3" means old is always good.')
    parser_init.add_argument(
        '--confidence',
        type=float,
        default=DEFAULT_CONFIDENCE,
        help='Confidence level (default: %(default)r)')
    parser_init.set_defaults(func=self.cmd_init)
    self.domain_cls.add_init_arguments(parser_init)

    parser_config = subparsers.add_parser(
        'config', help='Configures additional setting')
    parser_config.add_argument(
        'key',
        choices=['switch', 'eval', 'confidence', 'noisy'],
        metavar='key',
        help='What config to change. choices=[%(choices)s]')
    parser_config.add_argument(
        'value', nargs=argparse.REMAINDER, help='New value')
    parser_config.set_defaults(func=self.cmd_config)

    parser_run = subparsers.add_parser(
        'run',
        help='Performs bisection',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=textwrap.dedent('''
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
        '''))
    parser_run.add_argument(
        '-1', '--once', action='store_true', help='Only run one step')
    parser_run.add_argument(
        '--force',
        action='store_true',
        help="Run at least once even it's already done")
    parser_run.add_argument(
        'revs',
        nargs='*',
        type=self.domain_cls.intra_revtype,
        help='revs to switch+eval; '
        'default is calculating automatically and run until done')
    parser_run.set_defaults(func=self.cmd_run)

    parser_switch = subparsers.add_parser(
        'switch', help='Switch to given rev without eval')
    parser_switch.add_argument(
        'rev',
        type=argtype_multiplexer(self.domain_cls.intra_revtype,
                                 argtype_re('next', 'next')))
    parser_switch.set_defaults(func=self.cmd_switch)

    parser_old = subparsers.add_parser(
        'old', help='Tells bisect engine the said revs have "old" behavior')
    parser_old.add_argument(
        'revs',
        nargs='+',
        type=argtype_multiplier(self.domain_cls.intra_revtype))
    parser_old.set_defaults(func=self.cmd_old)

    parser_new = subparsers.add_parser(
        'new', help='Tells bisect engine the said revs have "new" behavior')
    parser_new.add_argument(
        'revs',
        nargs='+',
        type=argtype_multiplier(self.domain_cls.intra_revtype))
    parser_new.set_defaults(func=self.cmd_new)

    parser_skip = subparsers.add_parser(
        'skip', help='Tells bisect engine the said revs have "skip" behavior')
    parser_skip.add_argument(
        'revs',
        nargs='+',
        type=argtype_multiplier(self.domain_cls.intra_revtype))
    parser_skip.set_defaults(func=self.cmd_skip)

    parser_view = subparsers.add_parser(
        'view', help='Shows current progress and candidates')
    parser_view.add_argument('--verbose', '-v', action='store_true')
    parser_view.add_argument('--json', action='store_true')
    parser_view.set_defaults(func=self.cmd_view)

    parser_log = subparsers.add_parser(
        'log', help='Prints what has been done so far')
    parser_log.add_argument('--before', type=float)
    parser_log.add_argument('--after', type=float)
    parser_log.add_argument(
        '--json', action='store_true', help='Machine readable output')
    parser_log.set_defaults(func=self.cmd_log)

    parser_next = subparsers.add_parser(
        'next', help='Prints next suggested rev to bisect')
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

    self._create_states(session=opts.session, session_base=opts.session_base)
    if opts.command not in ('init', 'reset', 'config'):
      self.states.load()
      self.domain = self.domain_cls(self.states.config)
      self.strategy = strategy.NoisyBinarySearch(
          self.states.rev_info,
          self.states.rev2idx(self.config['old']),
          self.states.rev2idx(self.config['new']),
          confidence=self.config['confidence'],
          observation=self.config['noisy'])

    return opts.func(opts)
