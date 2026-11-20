#!/usr/bin/env python3
# Copyright 2026 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Host-side supssh relay server and chroot setup helpers.

Commands inside the `cros_sdk` chroot do not have `/usr/bin/supssh` or access to
host GMC credentials, while sharing the host network namespace. This module
manages an on-demand host daemon listening on an abstract Unix domain socket and
installs `supssh_relay_client.py` into `<chromeos_root>/out/sdk/bin/supssh`.

Because `ensure_running()` spawns the daemon under system `/usr/bin/python3`
outside the virtualenv, keep this module limited to the Python standard library
and stdlib-only `bisect_kit` imports.
"""

from __future__ import annotations

import argparse
import array
import contextlib
import fcntl
import json
import logging
import os
import pathlib
import re
import selectors
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import typing

import bisect_kit
from bisect_kit import supssh_relay_client


logger = logging.getLogger(__name__)

PROTOCOL_VERSION = 1
ENABLE_ENV = 'BISECT_KIT_SUPSSH_RELAY'
SUPSSH_BIN_ENV = 'BISECT_KIT_SUPSSH_BIN'

CLIENT_SCRIPT_PATH = (
    pathlib.Path(__file__).resolve().with_name('supssh_relay_client.py')
)

_PROXY_SUPSSH_RE = re.compile(r'^\s*ProxyCommand\b.*\bsupssh\b', re.IGNORECASE)
_TRUTHY_VALUES = frozenset({'1', 'true', 'on', 'yes'})
_FALSY_VALUES = frozenset({'0', 'false', 'off', 'no'})
_LEGACY_MOUNT_DESTS = frozenset({'/bin/supssh', '/usr/bin/supssh'})
_LEGACY_SSH_KEY_SUFFIXES = (
    '/.ssh/testing_rsa',
    '/.ssh/partner_testing_rsa',
    '/.ssh/chromeos_ssh_proxy',
)
_HEADER_TIMEOUT_SECONDS = 5.0
# Accept up to 4 FDs (3 expected by connect + 1) so a 4-FD request gets a
# structured error from _handle_connect while 5+ FDs trigger MSG_CTRUNC.
_MAX_RECEIVED_FDS = 4


def _resolve_supssh_bin(supssh_bin: str | None = None) -> str | None:
    candidate = (
        supssh_bin or os.environ.get(SUPSSH_BIN_ENV) or shutil.which('supssh')
    )
    if (
        candidate
        and os.path.isfile(candidate)
        and os.access(candidate, os.X_OK)
    ):
        return os.path.abspath(candidate)
    return None


def _atomic_write_bytes(
    dst: pathlib.Path,
    data: bytes,
    prefix: str,
    mode: int | None = None,
) -> None:
    """Atomically writes `data` to `dst` via a sibling temporary file."""
    # Write to a sibling temp file and rename atomically so concurrent readers
    # never observe a partially written file.
    tmp_path: pathlib.Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=dst.parent, prefix=prefix, delete=False
        ) as tmp:
            tmp_path = pathlib.Path(tmp.name)
            tmp.write(data)
        if mode is not None:
            os.chmod(tmp_path, mode)
        os.replace(tmp_path, dst)
    except BaseException:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)
        raise


def is_enabled(
    ssh_config_path: os.PathLike[str] | str | None = None,
    supssh_bin: str | None = None,
) -> bool:
    """Returns whether the supssh chroot relay should be enabled."""
    env_override = os.environ.get(ENABLE_ENV, '').strip().lower()
    if env_override in _TRUTHY_VALUES:
        return True
    if env_override in _FALSY_VALUES:
        return False

    if not _resolve_supssh_bin(supssh_bin):
        return False

    cfg_path = (
        pathlib.Path(ssh_config_path)
        if ssh_config_path is not None
        else pathlib.Path('~/.ssh/config').expanduser()
    )
    try:
        content = cfg_path.read_text(encoding='utf-8')
    except OSError:
        return False

    for line in content.splitlines():
        uncommented = line.split('#', 1)[0]
        if _PROXY_SUPSSH_RE.search(uncommented):
            return True
    return False


def install_chroot_client(
    chromeos_root: os.PathLike[str] | str,
) -> pathlib.Path:
    """Copies the relay client to `<chromeos_root>/out/sdk/bin/supssh`.

    Args:
        chromeos_root: Path to the ChromeOS checkout root.

    Returns:
        Path to the installed `supssh` executable outside the chroot.
    """
    sdk_bin_dir = pathlib.Path(chromeos_root) / 'out' / 'sdk' / 'bin'
    dst = sdk_bin_dir / 'supssh'
    src_bytes = CLIENT_SCRIPT_PATH.read_bytes()

    if dst.is_file() and not dst.is_symlink():
        try:
            mode = dst.stat().st_mode & 0o777
            if mode == 0o755 and dst.read_bytes() == src_bytes:
                return dst
        except OSError:
            pass

    sdk_bin_dir.mkdir(parents=True, exist_ok=True)
    _atomic_write_bytes(dst, src_bytes, prefix='.supssh.', mode=0o755)
    return dst


def _is_legacy_local_mount_line(line: str, drop_ssh_keys: bool) -> bool:
    uncommented = line.split('#', 1)[0].strip()
    if not uncommented:
        return False
    parts = uncommented.split()
    src = parts[0]
    dst = parts[1] if len(parts) >= 2 else src
    if 'supssh-helper-temporary' in src or 'supssh-helper-temporary' in dst:
        return True
    if dst in _LEGACY_MOUNT_DESTS or dst.endswith('/.cshh.sock'):
        return True
    if drop_ssh_keys and dst.endswith(_LEGACY_SSH_KEY_SUFFIXES):
        return True
    return False


def clean_legacy_local_mounts(
    chromeos_root: os.PathLike[str] | str,
    drop_ssh_keys: bool = True,
) -> bool:
    """Strips manual `supssh-helper-temporary` entries from `.local_mounts`.

    Args:
        chromeos_root: Path to the ChromeOS checkout root.
        drop_ssh_keys: Also drop redundant `.ssh/*` key bind mounts when True.

    Returns:
        True if `.local_mounts` was modified, False otherwise.
    """
    mounts_file = (
        pathlib.Path(chromeos_root) / 'src' / 'scripts' / '.local_mounts'
    )
    if not mounts_file.is_file():
        return False

    original_lines = mounts_file.read_text(encoding='utf-8').splitlines()
    kept_lines = [
        line
        for line in original_lines
        if not _is_legacy_local_mount_line(line, drop_ssh_keys=drop_ssh_keys)
    ]
    if kept_lines == original_lines:
        return False

    new_text = '\n'.join(kept_lines) + ('\n' if kept_lines else '')
    _atomic_write_bytes(
        mounts_file, new_text.encode('utf-8'), prefix='.local_mounts.'
    )
    return True


def _request_json(
    payload: dict[str, typing.Any],
    socket_address: str | None = None,
    expected_uid: int | None = None,
    timeout: float = 2.0,
) -> dict[str, typing.Any] | None:
    addr = (
        socket_address
        if socket_address is not None
        else supssh_relay_client.get_socket_address()
    )
    uid = os.getuid() if expected_uid is None else expected_uid
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        try:
            sock.connect(addr)
            supssh_relay_client.verify_peer_uid(sock, uid)
            sock.sendall(json.dumps(payload).encode('utf-8'))
            chunks: list[bytes] = []
            while True:
                chunk = sock.recv(supssh_relay_client.MAX_MSG_SIZE)
                if not chunk:
                    break
                chunks.append(chunk)
        except (OSError, ValueError):
            return None
    if not chunks:
        return None
    try:
        reply = json.loads(b''.join(chunks).decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return reply if isinstance(reply, dict) else None


def ping(
    socket_address: str | None = None,
    expected_uid: int | None = None,
    timeout: float = 2.0,
) -> dict[str, typing.Any] | None:
    """Pings a running relay server, returning its status dict or None."""
    return _request_json(
        {'op': 'ping'},
        socket_address=socket_address,
        expected_uid=expected_uid,
        timeout=timeout,
    )


def stop_server(
    socket_address: str | None = None,
    only_if_idle: bool = False,
    timeout: float = 5.0,
) -> bool:
    """Requests the relay server to stop listening and release its socket."""
    reply = _request_json(
        {'op': 'stop', 'only_if_idle': only_if_idle},
        socket_address=socket_address,
        timeout=timeout,
    )
    return bool(reply and reply.get('stopped'))


def _select_host_python() -> str:
    # Prefer system Python over bisect-kit's virtualenv because bkr recreates
    # `venv` (`uv venv --clear`) at the start of every bisection session.
    system_python = '/usr/bin/python3'
    if os.path.isfile(system_python) and os.access(system_python, os.X_OK):
        return system_python
    return sys.executable


def ensure_running(
    socket_address: str | None = None,
    state_dir: os.PathLike[str] | str | None = None,
    supssh_bin: str | None = None,
) -> None:
    """Ensures a compatible relay server is listening on the relay socket."""
    addr = (
        socket_address
        if socket_address is not None
        else supssh_relay_client.get_socket_address()
    )
    info = ping(socket_address=addr)
    if info and info.get('version') == PROTOCOL_VERSION:
        return

    cache_dir = (
        pathlib.Path(state_dir)
        if state_dir is not None
        else pathlib.Path('~/.cache/bisect-kit').expanduser()
    )
    cache_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_path = cache_dir / 'supssh-relay.lock'
    log_path = cache_dir / 'supssh-relay.log'

    with open(lock_path, 'w', encoding='utf-8') as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)

        info = ping(socket_address=addr)
        if info and info.get('version') == PROTOCOL_VERSION:
            return
        if info is not None:
            if not stop_server(socket_address=addr, only_if_idle=True):
                logger.info(
                    'Keeping active supssh relay (version=%s) until idle',
                    info.get('version'),
                )
                return

        resolved_supssh = _resolve_supssh_bin(supssh_bin)
        if not resolved_supssh:
            raise RuntimeError('supssh binary not found on host')

        env = os.environ.copy()
        root_str = str(bisect_kit.BISECT_KIT_ROOT)
        existing_py_path = env.get('PYTHONPATH', '')
        env['PYTHONPATH'] = (
            f'{root_str}:{existing_py_path}' if existing_py_path else root_str
        )
        env[supssh_relay_client.SOCKET_ENV] = (
            supssh_relay_client.format_socket_address(addr)
        )
        env[SUPSSH_BIN_ENV] = resolved_supssh

        cmd = [
            _select_host_python(),
            '-m',
            'bisect_kit.supssh_relay',
            '--serve',
            '--daemonize',
        ]
        # `--daemonize` binds and listens before exiting the intermediate child,
        # so subprocess.run() returns only once the socket is ready to accept
        # connections and the daemon is reparented to init.
        with open(log_path, 'ab') as log_file:
            proc = subprocess.run(
                cmd,
                cwd='/',
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                check=False,
            )
        if proc.returncode != 0:
            raise RuntimeError(
                f'supssh relay server failed to start (see {log_path})'
            )


def _close_fds(fds: list[int]) -> None:
    """Closes all file descriptors in `fds` and clears the list."""
    while fds:
        fd = fds.pop()
        with contextlib.suppress(OSError):
            os.close(fd)


def _send_json_best_effort(
    conn: socket.socket, data: dict[str, typing.Any]
) -> None:
    try:
        conn.sendall(json.dumps(data).encode('utf-8'))
    except OSError:
        # OpenSSH terminates ProxyCommand on disconnect, which may close the
        # client socket before supssh finishes exiting.
        pass


def _recv_request(
    conn: socket.socket,
    received_fds: list[int],
) -> dict[str, typing.Any] | None:
    """Reads a JSON request and appends any SCM_RIGHTS FDs to `received_fds`."""
    fds = array.array('i')
    conn.settimeout(_HEADER_TIMEOUT_SECONDS)
    msg_bytes, ancdata, flags, _ = conn.recvmsg(
        supssh_relay_client.MAX_MSG_SIZE,
        socket.CMSG_SPACE(_MAX_RECEIVED_FDS * fds.itemsize),
    )
    conn.settimeout(None)
    for cmsg_level, cmsg_type, cmsg_data in ancdata:
        if cmsg_level == socket.SOL_SOCKET and cmsg_type == socket.SCM_RIGHTS:
            usable = len(cmsg_data) - (len(cmsg_data) % fds.itemsize)
            fds.frombytes(cmsg_data[:usable])
    # Record unpacked FDs immediately so `_handle_client`'s `finally` closes
    # them if truncation or payload validation fails below.
    received_fds.extend(fds)
    if flags & socket.MSG_CTRUNC:
        raise ValueError('ancillary data truncated')
    if not msg_bytes:
        return None
    msg = json.loads(msg_bytes.decode('utf-8'))
    if not isinstance(msg, dict):
        raise ValueError('request payload must be a JSON object')
    return msg


def _terminate_process(proc: subprocess.Popen[bytes]) -> int:
    proc.terminate()
    try:
        return proc.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        proc.kill()
        return proc.wait()


def _wait_for_process_or_disconnect(
    proc: subprocess.Popen[bytes],
    conn: socket.socket,
) -> int:
    """Waits for `proc` to exit, terminating it if `conn` disconnects first."""
    try:
        pidfd = os.pidfd_open(proc.pid, 0)
    except (AttributeError, OSError):
        return proc.wait()

    try:
        with selectors.DefaultSelector() as sel:
            sel.register(pidfd, selectors.EVENT_READ)
            sel.register(conn, selectors.EVENT_READ)
            while True:
                events = sel.select()
                if any(key.fileobj == pidfd for key, _ in events):
                    return proc.wait()
                try:
                    data = conn.recv(64)
                except OSError:
                    data = b''
                if not data:
                    break
    finally:
        os.close(pidfd)

    return _terminate_process(proc)


class RelayServer:
    """AF_UNIX server executing host `supssh` for chroot clients."""

    def __init__(
        self,
        socket_address: str,
        supssh_bin: str,
        expected_uid: int | None = None,
    ):
        self._socket_address = socket_address
        self._supssh_bin = supssh_bin
        self._expected_uid = (
            os.getuid() if expected_uid is None else expected_uid
        )
        self._lock = threading.Lock()
        self._idle_cond = threading.Condition(self._lock)
        self._active_connections = 0
        self._stopping = threading.Event()
        self._listen_closed = threading.Event()
        wakeup_r, wakeup_w = os.pipe()
        os.set_blocking(wakeup_r, False)
        os.set_blocking(wakeup_w, False)
        self._wakeup_r = wakeup_r
        self._wakeup_w: int | None = wakeup_w
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self._sock.bind(self._socket_address)
            self._sock.listen(64)
        except BaseException:
            self._sock.close()
            os.close(wakeup_r)
            os.close(wakeup_w)
            raise

    def _signal_stop_locked(self) -> None:
        if self._stopping.is_set():
            return
        self._stopping.set()
        if self._wakeup_w is not None:
            with contextlib.suppress(BlockingIOError):
                os.write(self._wakeup_w, b'x')

    def request_stop(self) -> None:
        """Signals `serve_forever()` to stop accepting new connections."""
        with self._lock:
            self._signal_stop_locked()

    def wait_for_idle(self, timeout: float = 5.0) -> bool:
        """Blocks until active connections drop to zero."""
        with self._idle_cond:
            return self._idle_cond.wait_for(
                lambda: self._active_connections == 0, timeout=timeout
            )

    def serve_forever(self, ready_fd: int | None = None) -> None:
        """Runs the accept loop until a stop request is processed.

        Args:
            ready_fd: Optional writable file descriptor signaled once the
                selector is registered; this method takes ownership and always
                closes it.
        """
        try:
            with self._sock, selectors.DefaultSelector() as sel:
                sel.register(self._sock, selectors.EVENT_READ)
                sel.register(self._wakeup_r, selectors.EVENT_READ)
                if ready_fd is not None:
                    os.write(ready_fd, b'ok\n')
                    os.close(ready_fd)
                    ready_fd = None

                while not self._stopping.is_set():
                    for key, _ in sel.select():
                        if key.fileobj is self._wakeup_r:
                            with contextlib.suppress(BlockingIOError):
                                os.read(self._wakeup_r, 64)
                            break
                        try:
                            conn, _ = self._sock.accept()
                        except OSError:
                            break
                        threading.Thread(
                            target=self._handle_client,
                            args=(conn,),
                        ).start()
        finally:
            if ready_fd is not None:
                os.close(ready_fd)
            if not self._socket_address.startswith('\x00'):
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(self._socket_address)
            # Signal stop callers only after `with self._sock:` has closed (and
            # unlinked, for filesystem paths) the listening socket.
            self._listen_closed.set()
            with self._lock:
                wakeup_w = self._wakeup_w
                self._wakeup_w = None
            if wakeup_w is not None:
                os.close(wakeup_w)
            os.close(self._wakeup_r)

    def _handle_client(self, conn: socket.socket) -> None:
        received_fds: list[int] = []
        with conn:
            try:
                supssh_relay_client.verify_peer_uid(conn, self._expected_uid)
                msg = _recv_request(conn, received_fds)
                if msg is None:
                    return

                op = msg.get('op')
                if op == 'ping':
                    with self._lock:
                        active = self._active_connections
                    _send_json_best_effort(
                        conn,
                        {
                            'ok': True,
                            'version': PROTOCOL_VERSION,
                            'active': active,
                        },
                    )
                    return

                if op == 'stop':
                    only_if_idle = bool(msg.get('only_if_idle'))
                    with self._lock:
                        active = self._active_connections
                        if only_if_idle and active > 0:
                            stopped = False
                        else:
                            self._signal_stop_locked()
                            stopped = True
                    if not stopped:
                        _send_json_best_effort(
                            conn, {'stopped': False, 'active': active}
                        )
                        return
                    self._listen_closed.wait()
                    _send_json_best_effort(
                        conn, {'stopped': True, 'active': active}
                    )
                    return

                if op != 'connect':
                    _send_json_best_effort(
                        conn, {'error': f'unsupported op: {op!r}'}
                    )
                    return

                self._handle_connect(conn, msg, received_fds)
            except (OSError, ValueError) as err:
                logger.warning('supssh relay request failed: %s', err)
            finally:
                # Close unpacked FDs before `with conn:` closes the client
                # socket so callers draining until EOF observe deterministic
                # server-side FD cleanup.
                _close_fds(received_fds)

    def _handle_connect(
        self,
        conn: socket.socket,
        msg: dict[str, typing.Any],
        received_fds: list[int],
    ) -> None:
        if len(received_fds) != 3:
            _send_json_best_effort(
                conn, {'error': 'connect requires 3 stdio file descriptors'}
            )
            return

        raw_host = msg.get('host')
        raw_port = msg.get('port')
        raw_user = msg.get('user', '')
        if (
            not isinstance(raw_host, str)
            or not isinstance(raw_port, (int, str))
            or isinstance(raw_port, bool)
            or (raw_user is not None and not isinstance(raw_user, str))
        ):
            _send_json_best_effort(
                conn, {'error': 'invalid connect field types'}
            )
            return

        user = raw_user or ''
        try:
            port = supssh_relay_client.validate_target(
                raw_host, str(raw_port), user
            )
        except ValueError as err:
            _send_json_best_effort(conn, {'error': str(err)})
            return

        stdin_fd, stdout_fd, stderr_fd = received_fds
        args = [self._supssh_bin, '-host', raw_host, '-port', str(port)]
        if user:
            args.extend(['-user', user])

        with self._lock:
            self._active_connections += 1
        try:
            with subprocess.Popen(
                args,
                stdin=stdin_fd,
                stdout=stdout_fd,
                stderr=stderr_fd,
            ) as proc:
                try:
                    # Close the server's duplicates immediately so half-close
                    # EOF propagates directly between chroot ssh and host
                    # supssh, and clear `received_fds` to avoid double-closing.
                    _close_fds(received_fds)
                    returncode = _wait_for_process_or_disconnect(proc, conn)
                except BaseException:
                    proc.kill()
                    raise
            _send_json_best_effort(conn, {'returncode': returncode})
        finally:
            with self._idle_cond:
                self._active_connections -= 1
                if self._active_connections == 0:
                    self._idle_cond.notify_all()


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)s %(message)s',
    )
    parser = argparse.ArgumentParser(prog='supssh_relay')
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--serve', action='store_true', help='Run relay daemon')
    mode.add_argument(
        '--stop', action='store_true', help='Stop running relay daemon'
    )
    parser.add_argument(
        '--daemonize',
        action='store_true',
        help='Fork into background after binding socket',
    )
    args = parser.parse_args(argv)

    if args.stop:
        return 0 if stop_server() else 1

    supssh_bin = _resolve_supssh_bin()
    if not supssh_bin:
        logger.error('supssh binary not found')
        return 1

    server = RelayServer(
        socket_address=supssh_relay_client.get_socket_address(),
        supssh_bin=supssh_bin,
    )
    if args.daemonize:
        # Fork before starting any threads so the caller sees exit 0 only after
        # bind()/listen() succeeded and the daemon is reparented to init.
        if os.fork() > 0:
            os._exit(0)
    server.serve_forever()
    return 0


if __name__ == '__main__':
    sys.exit(main())
