# Copyright 2017 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Utility functions and classes."""

from __future__ import annotations

import contextlib
import difflib
import ipaddress
import logging
import os
import pathlib
import queue
import re
import shutil
import socket
import subprocess
import threading
import time
import typing

from bisect_kit import core
from bisect_kit import errors
import psutil


logger = logging.getLogger(__name__)

PathLike = os.PathLike | str


class Popen:
    """Wrapper of subprocess.Popen. Support output logging.

    The default is text mode with utf8 encoding. This is different to
    subprocess.Popen, which is default binary.

    Attributes:
      duration: Wall time of program execution in seconds.
      returncode: The child return code.
    """

    def __init__(
        self,
        args: PathLike | list[PathLike],
        cwd: PathLike | None = None,
        stdout_callback: typing.Callable | None = None,
        stderr_callback: typing.Callable | None = None,
        log_stdout: bool = True,
        binary: bool = False,
        **kwargs,
    ):
        """Initializes Popen.

        Args:
          args: Command line arguments.
          cwd: The working directory to execute the command line.
          stdout_callback: Callback function for stdout. Called once per line.
          stderr_callback: Callback function for stderr. Called once per line.
          log_stdout: Whether write the stdout output of the child process to log.
          binary: binary mode; default is False
          **kwargs: Additional arguments passing to subprocess.Popen.
        """
        if 'stdout' in kwargs:
            raise ValueError(
                'stdout argument not allowed, it will be overridden.'
            )
        if 'stderr' in kwargs:
            raise ValueError(
                'stderr argument not allowed, it will be overridden.'
            )

        self.log_stdout = log_stdout
        self.binary_mode = binary
        if self.binary_mode:
            assert not kwargs.get('encoding')
            self.encoding = None
        else:
            self.encoding = kwargs.get('encoding', 'utf8')
        kwargs['encoding'] = self.encoding

        self.stdout_callback = stdout_callback
        self.stderr_callback = stderr_callback
        self.duration = -1.0
        self.start = time.time()
        self.queue: queue.Queue = queue.Queue(65536)

        if cwd is not None:
            cwd = str(cwd)

        if isinstance(args, PathLike):
            args = [args]
        args = [str(arg) for arg in args]

        logger.debug('cwd=%s, run %r', cwd, subprocess.list2cmdline(args))

        self.p = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
            **kwargs,
        )

        self.stdout_thread = self._start_reader_thread('stdout', self.p.stdout)
        self.stderr_thread = self._start_reader_thread('stderr', self.p.stderr)

    @property
    def returncode(self) -> int:
        return self.p.returncode

    def _start_reader_thread(
        self, fd_name: str, fd: typing.IO
    ) -> threading.Thread:
        """Creates and starts a reader thread to help reading stdout and stderr.

        Args:
          fd_name: 'stdout' or 'stderr'.
          fd: file object which producing output.
        """

        def _reader_thread(fd_name: str, fd: typing.IO) -> None:
            try:
                for line in fd:
                    self.queue.put((fd_name, line))
            except Exception:
                logger.exception('reader thread %s throws exception', fd_name)
            self.queue.put((fd_name, ''))
            fd.close()

        thread = threading.Thread(target=_reader_thread, args=(fd_name, fd))
        thread.daemon = True
        thread.start()
        return thread

    def wait(self, timeout: float | None = None) -> int:
        """Waits child process.

        Returns:
          return code.
        """
        logger.debug(
            '[%d] running %r', self.p.pid, subprocess.list2cmdline(self.p.args)
        )
        t0 = time.time()
        ended = 0
        while ended < 2:
            if timeout is not None:
                try:
                    remaining_time = timeout - (time.time() - t0)
                    if remaining_time > 0:
                        fd_name, line = self.queue.get(
                            block=True, timeout=remaining_time
                        )
                    else:
                        # We follow queue.get's behavior to raise queue.Empty, so it's
                        # always queue.Empty when time is up, no matter remaining_time is
                        # negative or positive.
                        raise queue.Empty
                except queue.Empty:
                    logger.debug(
                        'child process time out (%.1f seconds), kill it',
                        timeout,
                    )
                    self.p.kill()
                    raise errors.ExecutionTimeout from None
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
                logger.debug(
                    '[%d][%s] %s', self.p.pid, fd_name, line.rstrip('\n')
                )
        self.p.wait()
        self.duration = time.time() - self.start
        logger.debug('[%d] returncode %d', self.p.pid, self.returncode)
        return self.returncode

    def terminate(self) -> None:
        """Terminates child and descendant processes."""
        # Need to ignore failures because sometimes they are expected.
        # For example, the owner of child process is different to current and
        # unable to be killed by current process. 'cros_sdk' is one of such case.
        for proc in psutil.Process(self.p.pid).children(recursive=True):
            try:
                proc.terminate()
            except (psutil.Error, OSError) as exception:
                logger.warning(
                    'Unable to terminate pid=%d because of exception=%s; ignore',
                    proc.pid,
                    exception,
                )
        try:
            self.p.terminate()
        except (psutil.Error, OSError) as exception:
            logger.warning(
                'Unable to terminate pid=%d because of exception=%s; ignore',
                self.p.pid,
                exception,
            )
        time.sleep(0.1)
        try:
            self.p.kill()
        except (psutil.Error, OSError) as exception:
            logger.warning(
                'Unable to kill pid=%d because of exception=%s; ignore',
                self.p.pid,
                exception,
            )


def call(*args, timeout: float | None = None, **kwargs) -> int:
    """Run command.

    Modeled after subprocess.call.

    Returns:
      Exit code of sub-process.
    """
    p = Popen(args, **kwargs)
    return p.wait(timeout=timeout)


# Suppressing the`deprecated warning, because AnyStr is deprecated on Python 3.13+
# pylint: disable=deprecated-attribute
def _check_output(
    *args,
    timeout: float | None = None,
    retry: int = 1,
    stream_type: typing.Type[typing.AnyStr],
    **kwargs,
) -> typing.AnyStr:
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
            args, stdout_callback=stdout_lines.append, binary=binary, **kwargs
        )
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


def check_call(
    *args, timeout: float | None = None, retry: int = 1, **kwargs
) -> None:
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


def ssh_cmd(
    host: str,
    *args,
    connect_timeout: int | None = None,
    max_attempts: int = 1,
    retry_interval: int = 60,
) -> str:
    """Runs remote command using ssh.

    Args:
      host: remote host address
      *args: command and args running on the remote host
      connect_timeout: connection timeout in seconds
      max_attempts: maximum number of attempts to connect to the remote host
      retry_interval: retry interval in seconds

    Raises:
      subprocess.CalledProcessError if the exit code is non-zero.
      errors.SshConnectionError on connection failure
    """
    cmd = ['ssh']
    # Avoid keyboard-interactive to prevent hang forever.
    cmd += ['-oPreferredAuthentications=publickey']
    if connect_timeout:
        cmd += [f'-oConnectTimeout={connect_timeout}']
    cmd.append(host)
    cmd += list(args)

    max_attempts = max(max_attempts, 1)
    attempts = 0
    while True:
        attempts += 1
        try:
            return check_output(*cmd)
        except subprocess.CalledProcessError as e:
            # ssh's own error code is 255. For other codes, they are returned from
            # the remote command.
            if e.returncode != 255:
                raise
            if attempts >= max_attempts:
                raise errors.SshConnectionError(
                    f'ssh connection to {host!r} failed'
                )
        # ssh's ConnectionAttempts is not enough because we want to deal with
        # situations needing several seconds to recover.
        logger.warning(
            'ssh connection failed, will retry %d seconds later', retry_interval
        )
        time.sleep(retry_interval)


def scp_cmd(
    from_file: str,
    to_file: str,
    connect_timeout: int | None = None,
    max_attempts: int = 1,
    retry_interval: int = 60,
) -> str:
    """Copies a file through scp.

    Args:
      from_file: from file name
      to_file: target file name
      connect_timeout: connection timeout in seconds
      max_attempts: maximum number of attempts to connect to the remote host
      retry_interval: retry interval in seconds

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

    max_attempts = max(max_attempts, 1)
    attempts = 0
    while True:
        attempts += 1
        try:
            return check_output(*cmd)
        except subprocess.CalledProcessError as e:
            # ssh's own error code is 255. For other codes, they are returned from
            # the remote command.
            if e.returncode != 255:
                raise
            if attempts >= max_attempts:
                raise errors.SshConnectionError(
                    f'scp {from_file} to {to_file} failed'
                )
        # ssh's ConnectionAttempts is not enough because we want to deal with
        # situations needing several seconds to recover.
        logger.warning(
            'ssh connection failed, will retry %d seconds later', retry_interval
        )
        time.sleep(retry_interval)


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


def report_similar_candidates(
    key: str, value: str, candidates: list[str]
) -> typing.NoReturn:
    assert value not in candidates
    if not candidates:
        raise errors.ExecutionFatalError(
            f'no candidates of {key} at all, something wrong?'
        )
    similar_candidates = difflib.get_close_matches(value, candidates)
    if not similar_candidates:
        raise errors.ExecutionFatalError(
            f'no {key} candidates similar to {value}'
        )
    logger.error('incorrect %s: %r; possible candidates:', key, value)
    for candidate in similar_candidates:
        logger.error('    %s', candidate)
    raise errors.ExecutionFatalError(
        'incorrect %s: %r; possible candidates: %s'
        % (key, value, similar_candidates)
    )


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


def wait_until(
    func: typing.Callable, timeout: float, period: float, *args, **kwargs
) -> None:
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
    raise errors.ExecutionTimeout('wait_until failed')


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
        logger.debug(
            'creating ssh forwarding connection: from_host: %s, to_port: %d',
            from_host,
            to_port,
        )
        p = Popen(
            [
                'go',
                'run',
                os.path.expanduser(
                    '~/chromiumos/src/platform/dev/contrib/sshwatcher/sshwatcher.go'
                ),
                from_host,
                str(to_port),
            ]
        )
        wait_until(is_port_in_use, 360, 1, to_port)
        logger.debug(
            'ssh forwarding connection is established: from_host: %s, to_port: %d',
            from_host,
            to_port,
        )
        yield
    finally:
        if p:
            p.terminate()
            logger.debug(
                'ssh forwarding connection is closed: from_host: %s, to_port: %d',
                from_host,
                to_port,
            )
        assert not is_port_in_use(to_port)


def get_test_crash_step_result(
    test_crash_reason: str, fail_to_pass: bool | None
) -> core.StepResult:
    """Make core step result object for test crash

    Args:
        fail_to_pass: A flag indicating failure as old behavior
        test_crash_reason: The reason for the crash
    """
    if fail_to_pass:
        logger.info('failed')
        return core.StepResult('old', test_crash_reason)
    logger.info('failed')
    return core.StepResult('new', test_crash_reason)


def is_valid_ip(ip: str) -> bool:
    """Checks if an ip address is valid or not

    Args:
        ip: An ipv4 of ipv6 address

    Returns:
        True for valid ip address, False otherwise
    """
    try:
        ipaddress.ip_address(ip)
    except ValueError:
        return False
    return True


def append_to_file(src_path, dst_path):
    """Copies or appends a file to the destination, ensuring no overwrite.

    Args:
        src_path (str): Path to the source file.
        dst_path (str): Path to the destination file.
    """

    with open(src_path, "rb") as fsrc, open(dst_path, "ab") as fdst:
        shutil.copyfileobj(fsrc, fdst)
    logger.debug('copied %s to %s', src_path, dst_path)


def copy_file_to_log_folder(file_path: str) -> bool:
    """Copy a file to the log directory

    Copy a file to the log directory. Log directory is parsed by 'LOG_FILE' environment variable.

    Args:
        file_path: The path of the file to be copied

    Retruns:
        True for successful copy, False otherwise
    """
    env_copy = os.environ.copy()

    if 'LOG_FILE' not in env_copy:
        logger.debug(
            'LOG_FILE is not defined in env. No need to copy the result file.'
        )
        return False

    log_path = pathlib.Path(env_copy['LOG_FILE'])
    if not log_path.is_file():
        logger.debug(
            'Log file does not exist. No need to copy the result file.'
        )
        return False

    path = pathlib.Path(file_path)
    if not path.is_file():
        logger.debug('Result file deos not exist.')
        return False

    # Assuming, log_path = bisect.sessions/session_name/log/log_file_name.txt
    # and file_path = some_directory/results.json
    # destination_file_name will be log_file_name.results.json
    # destination_file_path will be bisect.sessions/session_name/log/log_file_name.results.json
    destination_file_name = log_path.stem + '.' + path.stem + path.suffix
    destination_file_path = log_path.parents[0] / destination_file_name
    append_to_file(file_path, destination_file_path)
    return True


class MethodTimer:
    """A tool to measure running time of a method.

    The class is a bit special which aims at measuring the running time of a method.
    It is parameterized with another method of the same class as a callback.
    When the measured method is done, the callback is called on the same instance as the one whose method is being measured.
    """

    def __init__(
        self, callback: typing.Callable[[typing.Any, float, float], None]
    ):
        """Initializer.

        Args:
          callback: a method which accepts two arguments - the start timestamp and end timestamp.
        """
        self._callback = callback

    def __call__(self, func):
        def wrapper(wrapped_self, *args, **kwargs):
            start_timestamp = time.time()
            try:
                func(wrapped_self, *args, **kwargs)
            finally:
                # The callback is called on the same instance as the one being
                # measured.
                self._callback(wrapped_self, start_timestamp, time.time())

        return wrapper


class InitOnce:
    """A helper class to run an init func only once."""

    def __init__(self, name: str, init_func: typing.Callable[[], None]):
        self._name = name
        self._init_func = init_func
        self._initialized = False

    def run(self):
        if self._initialized:
            logger.info('init once func "%s" has been run, skipped', self._name)
            return
        logger.info('running init once func "%s"', self._name)
        self._init_func()
        logger.info('init once func "%s" completed', self._name)
        self._initialized = True
