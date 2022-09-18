# -*- coding: utf-8 -*-
# Copyright 2017 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Utility functions and classes."""

from __future__ import annotations
from __future__ import print_function

import contextlib
import difflib
import logging
import os
import queue
import re
import socket
import subprocess
import threading
import time
import typing

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
               stdout_callback: typing.Callable | None = None,
               stderr_callback: typing.Callable | None = None,
               log_stdout: bool = True,
               binary: bool = False,
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

    self.binary_mode = binary
    if self.binary_mode:
      assert not kwargs.get('encoding')
      self.encoding = None
      self.log_stdout = False
    else:
      self.encoding = kwargs.get('encoding', 'utf8')
      self.log_stdout = log_stdout
    kwargs['encoding'] = self.encoding

    self.stdout_callback = stdout_callback
    self.stderr_callback = stderr_callback
    self.duration = -1.0
    self.start = time.time()
    self.queue: queue.Queue = queue.Queue(65536)
    if isinstance(args, str):
      logger.debug('cwd=%s, run %r', kwargs.get('cwd'), args)
    else:
      logger.debug('cwd=%s, run %r', kwargs.get('cwd'),
                   subprocess.list2cmdline(args))
    self.p = subprocess.Popen(
        args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs)

    self.stdout_thread = threading.Thread(
        target=self._reader_thread, args=('stdout', self.p.stdout))
    self.stdout_thread.daemon = True
    self.stdout_thread.start()

    self.stderr_thread = threading.Thread(
        target=self._reader_thread, args=('stderr', self.p.stderr))
    self.stderr_thread.daemon = True
    self.stderr_thread.start()

  @property
  def returncode(self) -> int:
    return self.p.returncode

  def _reader_thread(self, fd_name: str, fd: typing.IO) -> None:
    """Reader thread to help reading stdout and stderr.

    Args:
      fd_name: 'stdout' or 'stderr'.
      fd: file object which producing output.
    """
    for line in fd:
      self.queue.put((fd_name, line))
    self.queue.put((fd_name, ''))
    fd.close()

  def wait(self, timeout: float | None = None) -> int:
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
            fd_name, line = self.queue.get(block=True, timeout=remaining_time)
          else:
            # We follow queue.get's behavior to raise queue.Empty, so it's
            # always queue.Empty when time is up, no matter remaining_time is
            # negative or positive.
            raise queue.Empty
        except queue.Empty:
          logger.debug('child process time out (%.1f seconds), kill it',
                       timeout)
          self.p.kill()
          raise TimeoutExpired from None
      else:
        fd_name, line = self.queue.get(block=True)
      # line includes '\n', will be '' if EOF.
      if not line:
        ended += 1
        continue
      if self.stdout_callback and fd_name == 'stdout':
        self.stdout_callback(line)
      if self.stderr_callback and fd_name == 'stderr':
        self.stderr_callback(line)
      if self.log_stdout or fd_name == 'stderr':
        if self.binary_mode:
          line = line.decode('utf8', errors='replace')
        logger.debug('[%s] %s', fd_name, line.rstrip('\n'))
    self.p.wait()
    self.duration = time.time() - self.start
    logger.debug('returncode %d', self.returncode)
    return self.returncode

  def terminate(self) -> None:
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


def call(*args, timeout: float | None = None, **kwargs) -> int:
  """Run command.

  Modeled after subprocess.call.

  Returns:
    Exit code of sub-process.
  """
  p = Popen(args, **kwargs)
  return p.wait(timeout=timeout)


def _check_output(*args,
                  timeout: float | None = None,
                  retry: int = 1,
                  stream_type: typing.Type[typing.AnyStr],
                  **kwargs) -> typing.AnyStr:
  """Runs command and return output.

  Modeled after subprocess.check_output.

  Returns:
    stdout string of execution.

  Raises:
    subprocess.CalledProcessError if the exit code is non-zero.
  """

  empty_string = stream_type()
  binary = stream_type is bytes
  delay_duration = 1

  while True:
    stdout_lines: list[typing.AnyStr] = []
    p = Popen(
        args, stdout_callback=stdout_lines.append, binary=binary, **kwargs)
    p.wait(timeout=timeout)

    stdout = empty_string.join(stdout_lines)
    if p.returncode == 0:
      return stdout

    retry -= 1
    if retry <= 0:
      break
    time.sleep(delay_duration)
    delay_duration = min(delay_duration * 2, 100)

  raise subprocess.CalledProcessError(p.returncode, args, stdout)


def check_output(*args, **kwargs) -> str:
  return _check_output(*args, stream_type=str, **kwargs)


def check_output_in_bytes(*args, **kwargs) -> bytes:
  return _check_output(*args, stream_type=bytes, **kwargs)


def check_call(*args, timeout: float | None = None, retry: int = 1,
               **kwargs) -> None:
  """Runs command and ensures it succeeded.

  Modeled after subprocess.check_call.

  Raises:
    subprocess.CalledProcessError if the exit code is non-zero.
  """
  delay_duration = 1
  while retry > 0:
    retry -= 1
    p = Popen(args, **kwargs)
    p.wait(timeout=timeout)

    if p.returncode == 0:
      break
    if retry <= 0:
      raise subprocess.CalledProcessError(p.returncode, args)
    time.sleep(delay_duration)
    delay_duration = min(delay_duration * 2, 100)


def ssh_cmd(host: str,
            *args,
            connect_timeout: int | None = None,
            allow_retry: bool = False) -> str:
  """Runs remote command using ssh.

  Args:
    host: remote host address
    args: command and args running on the remote host
    connect_timeout: connection timeout in seconds (int)
    allow_retry: if True, retry when ssh connection failed

  Raises:
    subprocess.CalledProcessError if the exit code is non-zero.
  """
  cmd = ['ssh']
  # Avoid keyboard-interactive to prevent hang forever.
  cmd += ['-oPreferredAuthentications=publickey']
  if connect_timeout:
    cmd += [f'-oConnectTimeout={connect_timeout}']
  cmd.append(host)
  cmd += list(args)

  tries = 0
  while True:
    tries += 1
    try:
      return check_output(*cmd)
    except subprocess.CalledProcessError as e:
      # ssh's own error code is 255. For other codes, they are returned from
      # the remote command.
      if e.returncode != 255:
        raise
      if allow_retry and tries < 3:
        # ssh's ConnectionAttempts is not enough because we want to deal with
        # situations needing several seconds to recover.
        delay = 60
        logger.warning('ssh connection failed, will retry %d seconds later',
                       delay)
        time.sleep(delay)
        continue
      raise errors.SshConnectionError(f'ssh connection to {host!r} failed')


def scp_cmd(from_file: str,
            to_file: str,
            connect_timeout: int | None = None,
            allow_retry: bool = False) -> str:
  """Copies a file through scp.

  Args:
    from_file: from file name
    to_file: target file name
    connect_timeout: connection timeout in seconds (int)
    allow_retry: if True, retry when ssh connection failed

  Raises:
    subprocess.CalledProcessError if the exit code is non-zero.
  """
  cmd = ['scp']
  # Avoid keyboard-interactive to prevent hang forever.
  cmd += ['-oPreferredAuthentications=publickey']
  if connect_timeout:
    cmd += [f'-oConnectTimeout={connect_timeout}']
  cmd += [
      from_file,
      to_file,
  ]

  tries = 0
  while True:
    tries += 1
    try:
      return check_output(*cmd)
    except subprocess.CalledProcessError as e:
      # ssh's own error code is 255. For other codes, they are returned from
      # the remote command.
      if e.returncode != 255:
        raise
      if allow_retry and tries < 3:
        # ssh's ConnectionAttempts is not enough because we want to deal with
        # situations needing several seconds to recover.
        delay = 60
        logger.warning('ssh connection failed, will retry %d seconds later',
                       delay)
        time.sleep(delay)
        continue
      raise errors.SshConnectionError(f'scp {from_file} to {to_file} failed')


def escape_rev(rev: str) -> str:
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


def version_key_func(version: str) -> list[int | str]:
  """Splits version string into components.

  Split version number by '.', and convert to `int` if possible. After this
  conversion, version numbers can be compared ordering directly. Usually this is
  used with sort function together.

  Example,
    >>> version_key_func('1.a.3')
    [1, 'a', 3]

  Args:
    version: version string

  Returns:
    list of int or string
  """
  return [int(x) if x.isdigit() else x for x in version.split('.')]


def is_version_lesseq(a: str, b: str) -> bool:
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


def is_direct_relative_version(a: str, b: str) -> bool:
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

  Note, one version is direct-relative to itself.

  Args:
    a: version string
    b: version string

  Returns:
    bool: True if `a` and `b` are direct-relative.
  """
  ver_list_a = version_key_func(a)
  ver_list_b = version_key_func(b)
  assert len(ver_list_a) == len(ver_list_b)
  if ver_list_a > ver_list_b:
    ver_list_a, ver_list_b = ver_list_b, ver_list_a

  branched = False
  for x, y in zip(ver_list_a, ver_list_b):
    if branched:
      if x != 0:
        return False
    elif x != y:
      branched = True

  return True


def report_similar_candidates(key: str, value: str,
                              candidates: list[str]) -> typing.NoReturn:
  assert value not in candidates
  if not candidates:
    raise errors.ExecutionFatalError(
        f'no candidates of {key} at all, something wrong?')
  similar_candidates = difflib.get_close_matches(value, candidates)
  if not similar_candidates:
    raise errors.ExecutionFatalError(f'no {key} candidates similar to {value}')
  logger.error('incorrect %s: %r; possible candidates:', key, value)
  for candidate in similar_candidates:
    logger.error('    %s', candidate)
  raise errors.ExecutionFatalError('incorrect %s: %r; possible candidates: %s' %
                                   (key, value, similar_candidates))


def dict_get(element, *args):
  """Recursively get a deep attribute in dict.

  Args:
    element: A dict element or None.
    args: Attributes wanted to get.

  Returns:
    An attribute or None if attribute does not exist.
  """
  for arg in args:
    if element is None:
      break
    element = element.get(arg)
  return element


def wait_until(func: typing.Callable, timeout: float, period: float, *args,
               **kwargs) -> None:
  """Block until func(args, kwargs) becomes true or raise error.

  Args:
    func: A callable function.
    timeout: Timeout seconds.
    period: Function call period, in seconds.
  """
  until = time.time() + timeout
  while time.time() < until:
    if func(*args, **kwargs):
      return
    time.sleep(period)
  raise TimeoutExpired('wait_until failed')


def is_port_in_use(port: int) -> bool:
  """Check if some process is listening to localhost:port.

  Args:
    port: port number.

  Returns:
    True if some program is listening to the port.
  """
  with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
    return s.connect_ex(('localhost', port)) == 0


@contextlib.contextmanager
def forward_ssh(from_host: str, to_port: int):
  """Foward from_host:22 to localhost:to_port.

  Args:
    from_host: DUT host.
    to_port: port number.
  """
  assert not is_port_in_use(to_port)
  p = None
  try:
    p = Popen([
        'go', 'run',
        os.path.expanduser(
            '~/chromiumos/src/platform/dev/contrib/sshwatcher/sshwatcher.go'),
        from_host,
        str(to_port)
    ])
    wait_until(is_port_in_use, 60, 1, to_port)
    yield
  finally:
    if p:
      p.terminate()
    assert not is_port_in_use(to_port)
