# -*- coding: utf-8 -*-
# Copyright 2017 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Utility functions and classes."""

from __future__ import print_function
import difflib
import logging
import queue
import re
import subprocess
import threading
import time

import psutil

from bisect_kit import errors

logger = logging.getLogger(__name__)


class TimeoutExpired(Exception):
  """Timeout expired.

  This may be raised by blocking calls like Popen.wait(), check_call(),
  check_output(), etc.
  """


class Popen:
  """Wrapper of subprocess.Popen. Support output logging.

  The default is text mode with utf8 encoding. This is different to
  subprocess.Popen, which is default binary.

  Attributes:
    duration: Wall time of program execution in seconds.
    returncode: The child return code.
  """

  def __init__(self,
               args,
               stdout_callback=None,
               stderr_callback=None,
               log_stdout=True,
               binary=None,
               **kwargs):
    """Initializes Popen.

    Args:
      args: Command line arguments.
      stdout_callback: Callback function for stdout. Called once per line.
      stderr_callback: Callback function for stderr. Called once per line.
      binary: binary mode; default is False
      log_stdout: Whether write the stdout output of the child process to log.
      **kwargs: Additional arguments passing to subprocess.Popen.
    """
    if 'stdout' in kwargs:
      raise ValueError('stdout argument not allowed, it will be overridden.')
    if 'stderr' in kwargs:
      raise ValueError('stderr argument not allowed, it will be overridden.')

    if binary:
      assert not kwargs.get('encoding')
      self.binary_mode = True
      self.encoding = None
      self.log_stdout = False
    else:
      self.binary_mode = False
      self.encoding = kwargs.get('encoding', 'utf8')
      self.log_stdout = log_stdout
    kwargs['encoding'] = self.encoding

    self.stdout_callback = stdout_callback
    self.stderr_callback = stderr_callback
    self.stdout_lines = []
    self.stderr_lines = []
    self.duration = -1
    self.start = time.time()
    self.queue = queue.Queue(65536)
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
    child_file.close()

  def wait(self, timeout=None):
    """Waits child process.

    Returns:
      return code.
    """
    t0 = time.time()
    ended = 0
    while ended < 2:
      if timeout is not None:
        try:
          remaining_time = timeout - (time.time() - t0)
          if remaining_time > 0:
            where, line = self.queue.get(block=True, timeout=remaining_time)
          else:
            # We follow queue.get's behavior to raise queue.Empty, so it's
            # always queue.Empty when time is up, no matter remaining_time is
            # negative or positive.
            raise queue.Empty
        except queue.Empty:
          logger.debug('child process time out (%.1f seconds), kill it',
                       timeout)
          self.p.kill()
          raise TimeoutExpired
      else:
        where, line = self.queue.get(block=True)
      # line includes '\n', will be '' if EOF.
      if not line:
        ended += 1
        continue
      if self.stdout_callback and where == 'stdout':
        self.stdout_callback(line)
      if self.stderr_callback and where == 'stderr':
        self.stderr_callback(line)
      if self.log_stdout or where == 'stderr':
        if self.binary_mode:
          line = line.decode('utf8', errors='replace')
        logger.debug('[%s] %s', where, line.rstrip('\n'))
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


def call(*args, timeout=None, **kwargs):
  """Run command.

  Modeled after subprocess.call.

  Returns:
    Exit code of sub-process.
  """
  p = Popen(args, **kwargs)
  return p.wait(timeout=timeout)


def check_output(*args, timeout=None, **kwargs):
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
  p.wait(timeout=timeout)
  stdout = ''.join(stdout_lines)
  if p.returncode != 0:
    raise subprocess.CalledProcessError(p.returncode, args, stdout)

  return stdout


def check_call(*args, timeout=None, **kwargs):
  """Runs command and ensures it succeeded.

  Modeled after subprocess.check_call.

  Raises:
    subprocess.CalledProcessError if the exit code is non-zero.
  """
  p = Popen(args, **kwargs)
  p.wait(timeout=timeout)
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


def escape_rev(rev):
  """Escapes special characters in version string.

  Sometimes we save files whose name is related to version, e.g. cache file and
  log file. Version strings must be escaped properly in order to make them
  path-friendly.

  Args:
    rev: rev string

  Returns:
    escaped string
  """
  # TODO(kcwu): change infra rev format, avoid special characters
  # Assume they don't collision after escaping.
  # Don't use "#" because gsutil using it as version identifiers.
  return re.sub('[^a-zA-Z0-9~._-]', '_', rev)


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
