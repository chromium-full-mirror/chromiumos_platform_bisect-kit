# -*- coding: utf-8 -*-
# Copyright 2017 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Utility functions and classes."""

from __future__ import print_function
import difflib
import logging
import Queue
import subprocess
import threading
import time

import psutil

from bisect_kit import errors

logger = logging.getLogger(__name__)


class Popen(object):
  """Wrapper of subprocess.Popen. Support output logging.

  Attributes:
    duration: Wall time of program execution in seconds.
    returncode: The child return code.
  """

  def __init__(self,
               args,
               stdout_callback=None,
               stderr_callback=None,
               log_output=True,
               **kwargs):
    """Initializes Popen.

    Args:
      args: Command line arguments.
      stdout_callback: Callback function for stdout. Called once per line.
      stderr_callback: Callback function for stderr. Called once per line.
      log_output: Whether write the output of the child process to log.
      **kwargs: Additional arguments passing to subprocess.Popen.
    """
    if 'stdout' in kwargs:
      raise ValueError('stdout argument not allowed, it will be overridden.')
    if 'stderr' in kwargs:
      raise ValueError('stderr argument not allowed, it will be overridden.')

    self.stdout_callback = stdout_callback
    self.stderr_callback = stderr_callback
    self.log_output = log_output
    self.stdout_lines = []
    self.stderr_lines = []
    self.duration = -1
    self.start = time.time()
    self.queue = Queue.Queue(65536)
    if isinstance(args, str):
      logger.debug('cwd=%s, run %r', kwargs.get('cwd'), args)
    else:
      logger.debug('cwd=%s, run %r', kwargs.get('cwd'),
                   subprocess.list2cmdline(args))
    self.p = subprocess.Popen(
        args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs)

    self.stdout_thread = threading.Thread(
        target=self._reader_thread, args=('stdout', self.p.stdout))
    self.stdout_thread.setDaemon(True)
    self.stdout_thread.start()

    self.stderr_thread = threading.Thread(
        target=self._reader_thread, args=('stderr', self.p.stderr))
    self.stderr_thread.setDaemon(True)
    self.stderr_thread.start()

  @property
  def returncode(self):
    return self.p.returncode

  def _reader_thread(self, where, child_file):
    """Reader thread to help reading stdout and stderr.

    Args:
      where: 'stdout' or 'stderr'.
      child_file: file object which producing output.
    """
    for line in iter(child_file.readline, ''):
      self.queue.put((where, line))
    self.queue.put((where, ''))

  def wait(self):
    """Waits child process.

    Returns:
      return code.
    """
    ended = 0
    while ended < 2:
      where, line = self.queue.get()
      # line includes '\n', will be '' if EOF.
      if not line:
        ended += 1
        continue
      if self.log_output:
        logger.debug('[%s] %s', where, line.rstrip('\n'))
      if self.stdout_callback and where == 'stdout':
        self.stdout_callback(line)
      if self.stderr_callback and where == 'stderr':
        self.stderr_callback(line)
    self.p.wait()
    self.duration = time.time() - self.start
    logger.debug('returncode %d', self.returncode)
    return self.returncode

  def terminate(self):
    """Terminates child and descendant processes."""
    # Need to ignore failures because sometimes they are expected.
    # For example, the owner of child process is different to current and
    # unable to be killed by current process. 'cros_sdk' is one of such case.
    for proc in psutil.Process(self.p.pid).children(recursive=True):
      try:
        proc.terminate()
      except psutil.AccessDenied:
        logger.warning('Unable to terminate pid=%d; ignore', proc.pid)
    try:
      self.p.terminate()
    except OSError:
      logger.warning('Unable to terminate pid=%d; ignore', self.p.pid)
    time.sleep(0.1)
    try:
      self.p.kill()
    except OSError:
      logger.warning('Unable to kill pid=%d; ignore', self.p.pid)


def call(*args, **kwargs):
  """Run command.

  Modeled after subprocess.call.

  Returns:
    Exit code of sub-process.
  """
  p = Popen(args, **kwargs)
  return p.wait()


def check_output(*args, **kwargs):
  """Runs command and return output.

  Modeled after subprocess.check_output.

  Returns:
    stdout string of execution.

  Raises:
    subprocess.CalledProcessError if the exit code is non-zero.
  """
  stdout_lines = []

  def collect_stdout(line):
    stdout_lines.append(line)

  p = Popen(args, stdout_callback=collect_stdout, **kwargs)
  p.wait()
  stdout = ''.join(stdout_lines)
  if p.returncode != 0:
    raise subprocess.CalledProcessError(p.returncode, args, stdout)

  return stdout


def check_call(*args, **kwargs):
  """Runs command and ensures it succeeded.

  Modeled after subprocess.check_call.

  Raises:
    subprocess.CalledProcessError if the exit code is non-zero.
  """
  p = Popen(args, **kwargs)
  p.wait()
  if p.returncode != 0:
    raise subprocess.CalledProcessError(p.returncode, args)


def ssh_cmd(host, *args, **kwargs):
  """Runs remote command using ssh.

  Args:
    host: remote host address
    args: command and args running on the remote host
    kwargs:
      connect_timeout: connection timeout in seconds (int)

  Raises:
    subprocess.CalledProcessError if the exit code is non-zero.
  """
  cmd = ['ssh']
  if kwargs.get('connect_timeout'):
    cmd += ['-oConnectTimeout=%d' % kwargs['connect_timeout']]
  cmd.append(host)
  cmd += list(args)
  try:
    return check_output(*cmd)
  except subprocess.CalledProcessError as e:
    # ssh's own error code is 255.
    if e.returncode == 255:
      raise errors.SshConnectionError('ssh connection to %r failed' % host)
    raise


def version_key_func(v):
  """Splits version string into components.

  Split version number by '.', and convert to `int` if possible. After this
  conversion, version numbers can be compared ordering directly. Usually this is
  used with sort function together.

  Example,
    >>> version_key_func('1.a.3')
    [1, 'a', 3]

  Args:
    v: version string

  Returns:
    list of int or string
  """
  return [int(x) if x.isdigit() else x for x in v.split('.')]


def is_version_lesseq(a, b):
  """Compares whether version `a` is less or equal to version `b`.

  Note this only compares the numeric values component-wise. That is, '1.1' is
  less than '2.0', but '1.1' may or may not be older than '2.0' according to
  chromium version semantic.

  Args:
    a: version string
    b: version string

  Returns:
    bool: True if a <= b
  """
  return version_key_func(a) <= version_key_func(b)


def is_direct_relative_version(a, b):
  r"""Determines two versions are direct-relative.

  "Direct-relative" means "one is ancestor of the other".

  This follows chromium and chromiumos version semantic.
      https://www.chromium.org/developers/version-numbers

  That is, [Major+1].[Minor] is a descendant of [Major+1].1, which is branched
  from [Major+1].0, which is a child of [Major].0. Thus, [Major+1].[Minor] is
  not direct-relative to any [Major].[Minor>0].

  For example, in this chart, 3.3 is not direct-relative to 2.2.

  -> 2.0 ------------------> 3.0 -------------
      \                       \
       -> 2.1 -> 2.2 ....      -> 3.1 -> 3.2 -> 3.3 ....

  Args:
    a: version string
    b: version string

  Returns:
    bool: True if `a` and `b` are direct-relative.
  """
  a = version_key_func(a)
  b = version_key_func(b)
  assert len(a) == len(b)
  if a > b:
    a, b = b, a

  branched = False
  for x, y in zip(a, b):
    if branched:
      if x != 0:
        return False
    elif x != y:
      branched = True

  return True


def show_similar_candidates(key, value, candidates):
  logger.error('incorrect %s: %r; possible candidates:', key, value)
  if not candidates:
    logger.error('(no candidates at all)')
    return
  similar_candidates = difflib.get_close_matches(value, candidates)
  if not similar_candidates:
    logger.error('(no similar candidates)')
    return
  for candidate in similar_candidates:
    logger.error('    %s', candidate)
