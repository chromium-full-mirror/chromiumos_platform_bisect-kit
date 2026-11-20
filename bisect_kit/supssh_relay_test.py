# Copyright 2026 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Tests for bisect_kit.supssh_relay and bisect_kit.supssh_relay_client."""

from __future__ import annotations

import array
import contextlib
import io
import os
import pathlib
import socket
import subprocess
import tempfile
import threading
import typing
import unittest
from unittest import mock
import uuid

import bisect_kit
from bisect_kit import supssh_relay
from bisect_kit import supssh_relay_client


FAKE_SUPSSH = str(bisect_kit.BISECT_KIT_ROOT / 'testdata' / 'fake_supssh.py')


def _unique_abstract_addr() -> str:
    return f'\x00bisect-kit-supssh-test-{os.getpid()}-{uuid.uuid4().hex[:12]}'


class TestSupsshRelayValidation(unittest.TestCase):
    """Unit tests for target validation, argument parsing, and file helpers."""

    def test_validate_target_accepts_valid_inputs(self) -> None:
        self.assertEqual(
            supssh_relay_client.validate_target(
                '70.32.141.158', '22', 'chromeos-ssh-proxy'
            ),
            22,
        )
        self.assertEqual(
            supssh_relay_client.validate_target('::1', '2222', ''),
            2222,
        )
        self.assertEqual(
            supssh_relay_client.validate_target(
                'ssh-dev-e.cr.sfo36.cr.dev', '65535', 'root'
            ),
            65535,
        )

    def test_validate_target_rejects_invalid_inputs(self) -> None:
        invalid_cases = [
            ('', '22', 'root'),
            ('-oProxyCommand=id', '22', 'root'),
            ('host;id', '22', 'root'),
            ('host name', '22', 'root'),
            ('::1%eth0', '22', 'root'),
            ('::1%-evil', '22', 'root'),
            ('70.32.141.158', '0', 'root'),
            ('70.32.141.158', '65536', 'root'),
            ('70.32.141.158', 'abc', 'root'),
            ('70.32.141.158', '22', '-proxy=evil'),
            ('70.32.141.158', '22', 'bad user'),
        ]
        for host, port, user in invalid_cases:
            with self.subTest(host=host, port=port, user=user):
                with self.assertRaises(ValueError):
                    supssh_relay_client.validate_target(host, port, user)

    def test_socket_address_resolution_and_formatting(self) -> None:
        with mock.patch.dict(os.environ, {}):
            os.environ.pop(supssh_relay_client.SOCKET_ENV, None)
            self.assertEqual(
                supssh_relay_client.get_socket_address(),
                f'\x00bisect-kit-supssh-{os.getuid()}',
            )
            self.assertEqual(
                supssh_relay_client.get_socket_address(uid=12345),
                '\x00bisect-kit-supssh-12345',
            )

        with mock.patch.dict(
            os.environ, {supssh_relay_client.SOCKET_ENV: '@custom-relay'}
        ):
            self.assertEqual(
                supssh_relay_client.get_socket_address(),
                '\x00custom-relay',
            )

        with mock.patch.dict(
            os.environ, {supssh_relay_client.SOCKET_ENV: '/tmp/custom.sock'}
        ):
            self.assertEqual(
                supssh_relay_client.get_socket_address(),
                '/tmp/custom.sock',
            )

        self.assertEqual(
            supssh_relay_client.format_socket_address('\x00custom-relay'),
            '@custom-relay',
        )
        self.assertEqual(
            supssh_relay_client.format_socket_address('/tmp/custom.sock'),
            '/tmp/custom.sock',
        )

    def test_parse_args_single_and_double_dash(self) -> None:
        double_dash = supssh_relay_client.parse_args(
            [
                '--user=chromeos-ssh-proxy',
                '--host=70.32.141.158',
                '--port=22',
            ]
        )
        self.assertEqual(double_dash.host, '70.32.141.158')
        self.assertEqual(double_dash.port, '22')
        self.assertEqual(double_dash.user, 'chromeos-ssh-proxy')

        single_dash = supssh_relay_client.parse_args(
            ['-host', '70.32.141.158', '-port', '2222', '-user', 'johndoe']
        )
        self.assertEqual(single_dash.host, '70.32.141.158')
        self.assertEqual(single_dash.port, '2222')
        self.assertEqual(single_dash.user, 'johndoe')

    def test_is_enabled_auto_detection_and_override(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            cfg = pathlib.Path(tempdir) / 'ssh_config'
            cfg.write_text(
                '# ProxyCommand supssh --host=%h\n'
                'Host *\n'
                '  ProxyCommand corp-ssh-helper %h %p # supssh\n',
                encoding='utf-8',
            )
            with mock.patch.dict(os.environ, {}):
                os.environ.pop(supssh_relay.ENABLE_ENV, None)
                self.assertFalse(
                    supssh_relay.is_enabled(
                        ssh_config_path=pathlib.Path(tempdir) / 'nonexistent',
                        supssh_bin=FAKE_SUPSSH,
                    )
                )
                self.assertFalse(
                    supssh_relay.is_enabled(
                        ssh_config_path=cfg, supssh_bin=FAKE_SUPSSH
                    )
                )

                cfg.write_text(
                    'Host ssh-dev-e-cr\n'
                    '  ProxyCommand supssh --user=%r --host=%h --port=%p\n',
                    encoding='utf-8',
                )
                rel_fake_supssh = os.path.relpath(FAKE_SUPSSH)
                self.assertTrue(
                    supssh_relay.is_enabled(
                        ssh_config_path=cfg, supssh_bin=rel_fake_supssh
                    )
                )
                self.assertFalse(
                    supssh_relay.is_enabled(
                        ssh_config_path=cfg,
                        supssh_bin=os.path.join(tempdir, 'missing_supssh'),
                    )
                )

            with mock.patch.dict(os.environ, {supssh_relay.ENABLE_ENV: 'off'}):
                self.assertFalse(
                    supssh_relay.is_enabled(
                        ssh_config_path=cfg, supssh_bin=FAKE_SUPSSH
                    )
                )

            with mock.patch.dict(os.environ, {supssh_relay.ENABLE_ENV: 'on'}):
                self.assertTrue(
                    supssh_relay.is_enabled(
                        ssh_config_path=pathlib.Path(tempdir) / 'nonexistent',
                        supssh_bin=os.path.join(tempdir, 'missing_supssh'),
                    )
                )

    def test_install_chroot_client_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            dst = supssh_relay.install_chroot_client(tempdir)
            expected_bytes = supssh_relay.CLIENT_SCRIPT_PATH.read_bytes()
            self.assertEqual(
                dst,
                pathlib.Path(tempdir) / 'out' / 'sdk' / 'bin' / 'supssh',
            )
            self.assertEqual(dst.stat().st_mode & 0o777, 0o755)
            self.assertEqual(dst.read_bytes(), expected_bytes)

            first_ino = dst.stat().st_ino
            supssh_relay.install_chroot_client(tempdir)
            self.assertEqual(dst.stat().st_ino, first_ino)

            # Mode-only drift (content unchanged) must still be repaired.
            os.chmod(dst, 0o644)
            supssh_relay.install_chroot_client(tempdir)
            self.assertEqual(dst.stat().st_mode & 0o777, 0o755)

            # Content corruption with 0o755 mode must be repaired.
            dst.write_text('corrupted', encoding='utf-8')
            os.chmod(dst, 0o755)
            supssh_relay.install_chroot_client(tempdir)
            self.assertEqual(dst.stat().st_mode & 0o777, 0o755)
            self.assertEqual(dst.read_bytes(), expected_bytes)

            # Symlink at dst must be atomically replaced with a regular file.
            symlink_target = pathlib.Path(tempdir) / 'target.py'
            symlink_target.write_bytes(expected_bytes)
            os.chmod(symlink_target, 0o755)
            dst.unlink()
            dst.symlink_to(symlink_target)
            supssh_relay.install_chroot_client(tempdir)
            self.assertFalse(dst.is_symlink())
            self.assertEqual(dst.stat().st_mode & 0o777, 0o755)
            self.assertEqual(dst.read_bytes(), expected_bytes)

    def test_clean_legacy_local_mounts(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            # Missing .local_mounts is a clean no-op.
            self.assertFalse(supssh_relay.clean_legacy_local_mounts(tempdir))

            scripts_dir = pathlib.Path(tempdir) / 'src' / 'scripts'
            scripts_dir.mkdir(parents=True)
            mounts_file = scripts_dir / '.local_mounts'
            initial_text = (
                '# Keep user config and custom mounts\n'
                '/home/cros-bisect-service/.config\n'
                '/tmp/relay/.cshh.sock /home/cros-bisect-service/.cshh.sock\n'
                '/opt/legacy/client.py /bin/supssh\n'
                '/opt/legacy/client.py /usr/bin/supssh\n'
                '/home/cros-bisect-service/supssh-helper-temporary/extra '
                '/mnt/extra\n'
                '/home/cros-bisect-service/.ssh/testing_rsa '
                '/home/cros-bisect-service/.ssh/testing_rsa\n'
                '/home/cros-bisect-service/.ssh/partner_testing_rsa '
                '/home/cros-bisect-service/.ssh/partner_testing_rsa\n'
                '/home/cros-bisect-service/.ssh/chromeos_ssh_proxy '
                '/home/cros-bisect-service/.ssh/chromeos_ssh_proxy\n'
                '/opt/custom /mnt/custom\n'
            )
            mounts_file.write_text(initial_text, encoding='utf-8')

            # drop_ssh_keys=False strips supssh/.cshh.sock mounts while keeping
            # .ssh/* key mounts intact.
            self.assertTrue(
                supssh_relay.clean_legacy_local_mounts(
                    tempdir, drop_ssh_keys=False
                )
            )
            self.assertEqual(
                mounts_file.read_text(encoding='utf-8'),
                '# Keep user config and custom mounts\n'
                '/home/cros-bisect-service/.config\n'
                '/home/cros-bisect-service/.ssh/testing_rsa '
                '/home/cros-bisect-service/.ssh/testing_rsa\n'
                '/home/cros-bisect-service/.ssh/partner_testing_rsa '
                '/home/cros-bisect-service/.ssh/partner_testing_rsa\n'
                '/home/cros-bisect-service/.ssh/chromeos_ssh_proxy '
                '/home/cros-bisect-service/.ssh/chromeos_ssh_proxy\n'
                '/opt/custom /mnt/custom\n',
            )

            # Default drop_ssh_keys=True also removes the redundant .ssh/* keys.
            self.assertTrue(supssh_relay.clean_legacy_local_mounts(tempdir))
            self.assertEqual(
                mounts_file.read_text(encoding='utf-8'),
                '# Keep user config and custom mounts\n'
                '/home/cros-bisect-service/.config\n'
                '/opt/custom /mnt/custom\n',
            )
            self.assertFalse(supssh_relay.clean_legacy_local_mounts(tempdir))


class TestSupsshRelayEndToEnd(unittest.TestCase):
    """Hermetic unit and end-to-end tests for server and standalone client."""

    @contextlib.contextmanager
    def _running_server(
        self, addr: str, expected_uid: int | None = None
    ) -> typing.Iterator[supssh_relay.RelayServer]:
        server = supssh_relay.RelayServer(
            socket_address=addr,
            supssh_bin=FAKE_SUPSSH,
            expected_uid=expected_uid,
        )
        ready_r, ready_w = os.pipe()
        srv_thread = threading.Thread(
            target=server.serve_forever, kwargs={'ready_fd': ready_w}
        )
        srv_thread.start()
        try:
            self.assertEqual(os.read(ready_r, 16), b'ok\n')
            os.close(ready_r)
            ready_r = -1
            yield server
        finally:
            if ready_r >= 0:
                os.close(ready_r)
            server.request_stop()
            srv_thread.join(timeout=5)
        self.assertFalse(srv_thread.is_alive())

    def test_end_to_end_stdio_stderr_exit_code_and_restart(self) -> None:
        addr = _unique_abstract_addr()
        printable_addr = supssh_relay_client.format_socket_address(addr)
        with tempfile.TemporaryDirectory() as tempdir:
            state_dir = pathlib.Path(tempdir) / 'state'
            chroot_root = pathlib.Path(tempdir) / 'chromeos'
            client_bin = supssh_relay.install_chroot_client(chroot_root)
            try:
                supssh_relay.ensure_running(
                    socket_address=addr,
                    state_dir=state_dir,
                    supssh_bin=FAKE_SUPSSH,
                )
                info = supssh_relay.ping(socket_address=addr)
                self.assertIsNotNone(info)
                assert info is not None
                self.assertEqual(info['version'], supssh_relay.PROTOCOL_VERSION)

                # Run installed client without PYTHONPATH to mimic chroot env.
                clean_env = {
                    'PATH': os.environ.get('PATH', '/usr/bin:/bin'),
                    supssh_relay_client.SOCKET_ENV: printable_addr,
                }
                proc = subprocess.run(
                    [
                        str(client_bin),
                        '--user=chromeos-ssh-proxy',
                        '--host=70.32.141.158',
                        '--port=22',
                    ],
                    input=b'SSH-2.0-test',
                    env=clean_env,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(proc.returncode, 0, msg=proc.stderr)
                self.assertEqual(
                    proc.stdout.decode('utf-8'),
                    'host=70.32.141.158 port=22 user=chromeos-ssh-proxy '
                    'stdin=SSH-2.0-test',
                )

                # Stop the daemon and verify ensure_running() brings it back.
                self.assertTrue(supssh_relay.stop_server(socket_address=addr))
                self.assertIsNone(supssh_relay.ping(socket_address=addr))

                with mock.patch.dict(
                    os.environ,
                    {
                        'FAKE_SUPSSH_STDERR': 'relay-diag\n',
                        'FAKE_SUPSSH_EXIT_CODE': '23',
                    },
                ):
                    supssh_relay.ensure_running(
                        socket_address=addr,
                        state_dir=state_dir,
                        supssh_bin=os.path.relpath(FAKE_SUPSSH),
                    )
                    proc2 = subprocess.run(
                        [
                            str(client_bin),
                            '-host',
                            '70.32.141.158',
                            '-port',
                            '22',
                        ],
                        input=b'ping',
                        env=clean_env,
                        capture_output=True,
                        check=False,
                    )
                self.assertEqual(proc2.returncode, 23)
                self.assertEqual(
                    proc2.stdout.decode('utf-8'),
                    'host=70.32.141.158 port=22 user= stdin=ping',
                )
                self.assertIn(b'relay-diag\n', proc2.stderr)
            finally:
                supssh_relay.stop_server(socket_address=addr)

    def test_filesystem_socket_unlink_and_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            sock_path = os.path.join(tempdir, 'relay.sock')
            state_dir = os.path.join(tempdir, 'state')
            try:
                supssh_relay.ensure_running(
                    socket_address=sock_path,
                    state_dir=state_dir,
                    supssh_bin=FAKE_SUPSSH,
                )
                self.assertTrue(os.path.exists(sock_path))
                self.assertTrue(
                    supssh_relay.stop_server(socket_address=sock_path)
                )
                self.assertFalse(os.path.exists(sock_path))

                # Restarting on the same path must not fail with EADDRINUSE.
                supssh_relay.ensure_running(
                    socket_address=sock_path,
                    state_dir=state_dir,
                    supssh_bin=FAKE_SUPSSH,
                )
                self.assertIsNotNone(
                    supssh_relay.ping(socket_address=sock_path)
                )
            finally:
                supssh_relay.stop_server(socket_address=sock_path)

    def test_ensure_running_state_transitions_and_errors(self) -> None:
        addr = _unique_abstract_addr()
        with tempfile.TemporaryDirectory() as tempdir:
            # Fast path when compatible version is already running.
            with (
                mock.patch(
                    'bisect_kit.supssh_relay.ping',
                    return_value={'version': supssh_relay.PROTOCOL_VERSION},
                ),
                mock.patch('subprocess.run') as mock_run,
            ):
                supssh_relay.ensure_running(
                    socket_address=addr,
                    state_dir=tempdir,
                    supssh_bin=FAKE_SUPSSH,
                )
                mock_run.assert_not_called()

            # Busy older version is kept without spawning a replacement.
            with (
                mock.patch(
                    'bisect_kit.supssh_relay.ping',
                    side_effect=[None, {'version': 0}],
                ),
                mock.patch(
                    'bisect_kit.supssh_relay.stop_server', return_value=False
                ) as mock_stop,
                mock.patch('subprocess.run') as mock_run,
            ):
                supssh_relay.ensure_running(
                    socket_address=addr,
                    state_dir=tempdir,
                    supssh_bin=FAKE_SUPSSH,
                )
                mock_stop.assert_called_once_with(
                    socket_address=addr, only_if_idle=True
                )
                mock_run.assert_not_called()

            # Idle older version is stopped and replaced.
            with (
                mock.patch(
                    'bisect_kit.supssh_relay.ping',
                    side_effect=[None, {'version': 0}],
                ),
                mock.patch(
                    'bisect_kit.supssh_relay.stop_server', return_value=True
                ) as mock_stop,
                mock.patch(
                    'subprocess.run',
                    return_value=subprocess.CompletedProcess([], 0),
                ) as mock_run,
            ):
                supssh_relay.ensure_running(
                    socket_address=addr,
                    state_dir=tempdir,
                    supssh_bin=FAKE_SUPSSH,
                )
                mock_stop.assert_called_once_with(
                    socket_address=addr, only_if_idle=True
                )
                mock_run.assert_called_once()

            # Missing supssh binary raises RuntimeError.
            with self.assertRaisesRegex(RuntimeError, 'not found'):
                supssh_relay.ensure_running(
                    socket_address=addr,
                    state_dir=tempdir,
                    supssh_bin=os.path.join(tempdir, 'missing_supssh'),
                )

            # Daemon startup failure raises RuntimeError.
            with (
                mock.patch(
                    'subprocess.run',
                    return_value=subprocess.CompletedProcess([], 1),
                ),
                self.assertRaisesRegex(RuntimeError, 'failed to start'),
            ):
                supssh_relay.ensure_running(
                    socket_address=addr,
                    state_dir=tempdir,
                    supssh_bin=FAKE_SUPSSH,
                )

    def test_main_cli_exit_codes(self) -> None:
        addr = _unique_abstract_addr()
        printable_addr = supssh_relay_client.format_socket_address(addr)
        with self.assertRaises(SystemExit) as ctx:
            supssh_relay.main([])
        self.assertEqual(ctx.exception.code, 2)

        with self.assertRaises(SystemExit) as ctx:
            supssh_relay.main(['--serve', '--stop'])
        self.assertEqual(ctx.exception.code, 2)

        with mock.patch.dict(
            os.environ,
            {
                supssh_relay_client.SOCKET_ENV: printable_addr,
                supssh_relay.SUPSSH_BIN_ENV: '/nonexistent/supssh',
            },
        ):
            self.assertEqual(supssh_relay.main(['--stop']), 1)
            with mock.patch('shutil.which', return_value=None):
                self.assertEqual(supssh_relay.main(['--serve']), 1)

        with self._running_server(addr):
            with mock.patch.dict(
                os.environ, {supssh_relay_client.SOCKET_ENV: printable_addr}
            ):
                self.assertEqual(supssh_relay.main(['--stop']), 0)

    def test_client_error_exit_codes(self) -> None:
        stderr_buf = io.StringIO()
        with contextlib.redirect_stderr(stderr_buf):
            self.assertEqual(supssh_relay_client.main(['--host=-bad-flag']), 2)
        self.assertIn('invalid host', stderr_buf.getvalue())

        addr = _unique_abstract_addr()
        printable_addr = supssh_relay_client.format_socket_address(addr)
        stderr_buf = io.StringIO()
        with (
            mock.patch.dict(
                os.environ, {supssh_relay_client.SOCKET_ENV: printable_addr}
            ),
            contextlib.redirect_stderr(stderr_buf),
        ):
            self.assertEqual(
                supssh_relay_client.main(['--host=70.32.141.158']), 1
            )
        self.assertIn('failed to connect', stderr_buf.getvalue())

        with tempfile.TemporaryDirectory() as tempdir:
            client_bin = supssh_relay.install_chroot_client(tempdir)
            for reply_bytes, expected_err in [
                (b'', b'without status'),
                (b'\xff', b'invalid reply'),
                (b'not-json', b'invalid reply'),
                (b'[]', b'invalid reply payload'),
                (b'{"error": "denied"}', b'supssh relay error: denied'),
                (b'{"returncode": "bad"}', b'invalid returncode'),
                (b'{"returncode": true}', b'invalid returncode'),
                (b'{"returncode": false}', b'invalid returncode'),
            ]:
                with self.subTest(reply_bytes=reply_bytes):
                    reply_addr = _unique_abstract_addr()
                    env = {
                        'PATH': os.environ.get('PATH', '/usr/bin:/bin'),
                        supssh_relay_client.SOCKET_ENV: (
                            supssh_relay_client.format_socket_address(
                                reply_addr
                            )
                        ),
                    }
                    with socket.socket(
                        socket.AF_UNIX, socket.SOCK_STREAM
                    ) as listener:
                        listener.settimeout(5.0)
                        listener.bind(reply_addr)
                        listener.listen(1)

                        def _serve_one(
                            srv: socket.socket = listener,
                            payload: bytes = reply_bytes,
                        ) -> None:
                            conn, _ = srv.accept()
                            fds = array.array('i')
                            with conn:
                                _, ancdata, _, _ = conn.recvmsg(
                                    4096,
                                    socket.CMSG_SPACE(4 * fds.itemsize),
                                )
                                for level, ctype, cdata in ancdata:
                                    if (
                                        level == socket.SOL_SOCKET
                                        and ctype == socket.SCM_RIGHTS
                                    ):
                                        fds.frombytes(cdata)
                                for fd in fds:
                                    os.close(fd)
                                if payload:
                                    conn.sendall(payload)

                        helper = threading.Thread(target=_serve_one)
                        helper.start()
                        proc = subprocess.run(
                            [str(client_bin), '--host=70.32.141.158'],
                            input=b'',
                            env=env,
                            capture_output=True,
                            check=False,
                        )
                        helper.join(timeout=5)
                        self.assertFalse(helper.is_alive())
                    self.assertEqual(proc.returncode, 1)
                    self.assertIn(expected_err, proc.stderr)
                    self.assertNotIn(b'Traceback', proc.stderr)

    def test_server_protocol_rejections_and_client_disconnect(self) -> None:
        addr = _unique_abstract_addr()
        with tempfile.TemporaryDirectory() as tempdir:
            fifo_path = os.path.join(tempdir, 'disconnect_gate.fifo')
            os.mkfifo(fifo_path)
            with self._running_server(addr) as server:
                baseline_fds = len(os.listdir('/proc/self/fd'))

                def _send_raw(payload: bytes, num_fds: int = 0) -> bytes:
                    pipes: list[tuple[int, int]] = []
                    try:
                        for _ in range(num_fds):
                            pipes.append(os.pipe())
                        with socket.socket(
                            socket.AF_UNIX, socket.SOCK_STREAM
                        ) as sock:
                            sock.settimeout(2.0)
                            sock.connect(addr)
                            if pipes:
                                fds = array.array(
                                    'i', [r_fd for r_fd, _ in pipes]
                                )
                                sock.sendmsg(
                                    [payload],
                                    [
                                        (
                                            socket.SOL_SOCKET,
                                            socket.SCM_RIGHTS,
                                            fds,
                                        )
                                    ],
                                )
                            elif payload:
                                sock.sendall(payload)
                            sock.shutdown(socket.SHUT_WR)
                            chunks: list[bytes] = []
                            while True:
                                chunk = sock.recv(4096)
                                if not chunk:
                                    break
                                chunks.append(chunk)
                            return b''.join(chunks)
                    finally:
                        for r_fd, w_fd in pipes:
                            os.close(r_fd)
                            os.close(w_fd)

                # Empty, invalid UTF-8, malformed JSON, and non-dict payloads
                # (with FDs attached) must not leak FDs in the server.
                self.assertEqual(_send_raw(b'', num_fds=3), b'')
                self.assertEqual(_send_raw(b'\xff', num_fds=3), b'')
                self.assertEqual(_send_raw(b'{bad', num_fds=3), b'')
                self.assertEqual(_send_raw(b'[1, 2]', num_fds=3), b'')
                # >4 FDs triggers MSG_CTRUNC rejection without leaking FDs.
                self.assertEqual(
                    _send_raw(
                        b'{"op":"connect","host":"70.32.141.158","port":22}',
                        num_fds=5,
                    ),
                    b'',
                )

                # Unsupported op, wrong FD count, bad JSON types, and invalid
                # target values return structured error replies.
                self.assertIn(b'unsupported op', _send_raw(b'{"op":"unknown"}'))
                self.assertIn(
                    b'3 stdio file descriptors',
                    _send_raw(
                        b'{"op":"connect","host":"70.32.141.158","port":22}',
                        num_fds=2,
                    ),
                )
                self.assertIn(
                    b'3 stdio file descriptors',
                    _send_raw(
                        b'{"op":"connect","host":"70.32.141.158","port":22}',
                        num_fds=4,
                    ),
                )
                for bad_msg in [
                    b'{"op":"connect","host":null,"port":22}',
                    b'{"op":"connect","host":123,"port":22}',
                    b'{"op":"connect","host":"70.32.141.158","port":true}',
                    (
                        b'{"op":"connect","host":"70.32.141.158",'
                        b'"port":22,"user":1}'
                    ),
                    b'{"op":"connect","host":"-bad","port":22}',
                    b'{"op":"connect","host":"70.32.141.158","port":0}',
                ]:
                    with self.subTest(bad_msg=bad_msg):
                        self.assertIn(b'"error"', _send_raw(bad_msg, num_fds=3))

                expected_idle = {
                    'ok': True,
                    'version': supssh_relay.PROTOCOL_VERSION,
                    'active': 0,
                }
                expected_busy = {
                    'ok': True,
                    'version': supssh_relay.PROTOCOL_VERSION,
                    'active': 1,
                }
                self.assertEqual(
                    supssh_relay.ping(socket_address=addr), expected_idle
                )
                self.assertLessEqual(
                    len(os.listdir('/proc/self/fd')), baseline_fds + 2
                )

                # Abrupt client disconnect while supssh is blocked must
                # terminate the child process and restore active count to 0.
                client_bin = supssh_relay.install_chroot_client(tempdir)
                with mock.patch.dict(
                    os.environ, {'FAKE_SUPSSH_GATE_FILE': fifo_path}
                ):
                    with subprocess.Popen(
                        [str(client_bin), '--host=70.32.141.158', '--port=22'],
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        env={
                            'PATH': os.environ.get('PATH', '/usr/bin:/bin'),
                            supssh_relay_client.SOCKET_ENV: (
                                supssh_relay_client.format_socket_address(addr)
                            ),
                        },
                    ) as client_proc:
                        try:
                            # Keep fifo_path open for writing while awaiting
                            # wait_for_idle() so fake_supssh can only exit if
                            # RelayServer terminates it on client disconnect.
                            with open(fifo_path, 'wb') as _gate_writer:
                                self.assertEqual(
                                    supssh_relay.ping(socket_address=addr),
                                    expected_busy,
                                )
                                client_proc.kill()
                                client_proc.wait(timeout=5)
                                self.assertTrue(
                                    server.wait_for_idle(timeout=5.0)
                                )
                        except BaseException:
                            client_proc.kill()
                            raise

                self.assertEqual(
                    supssh_relay.ping(socket_address=addr), expected_idle
                )
                self.assertTrue(
                    supssh_relay.stop_server(
                        socket_address=addr, only_if_idle=True
                    )
                )

    def test_peer_uid_verification_rejects_mismatch(self) -> None:
        s1, s2 = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        with s1, s2:
            supssh_relay_client.verify_peer_uid(s1, os.getuid())
            with self.assertRaises(PermissionError):
                supssh_relay_client.verify_peer_uid(s1, os.getuid() + 1)

        addr = _unique_abstract_addr()
        with self._running_server(addr, expected_uid=os.getuid() + 1):
            self.assertIsNone(supssh_relay.ping(socket_address=addr))

    def test_stop_only_if_idle_respects_active_connection(self) -> None:
        addr = _unique_abstract_addr()
        printable_addr = supssh_relay_client.format_socket_address(addr)
        with tempfile.TemporaryDirectory() as tempdir:
            client_bin = supssh_relay.install_chroot_client(tempdir)
            fifo_path = os.path.join(tempdir, 'gate.fifo')
            os.mkfifo(fifo_path)

            with mock.patch.dict(
                os.environ, {'FAKE_SUPSSH_GATE_FILE': fifo_path}
            ):
                with self._running_server(addr):
                    with subprocess.Popen(
                        [
                            str(client_bin),
                            '--host=70.32.141.158',
                            '--port=22',
                        ],
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        env={
                            'PATH': os.environ.get('PATH', '/usr/bin:/bin'),
                            supssh_relay_client.SOCKET_ENV: printable_addr,
                        },
                    ) as client_proc:
                        try:
                            # Opening the write end of the FIFO blocks until
                            # fake_supssh opens the read end, proving the
                            # connection is active without polling.
                            expected_busy = {
                                'ok': True,
                                'version': supssh_relay.PROTOCOL_VERSION,
                                'active': 1,
                            }
                            with open(fifo_path, 'wb') as gate_writer:
                                self.assertEqual(
                                    supssh_relay.ping(socket_address=addr),
                                    expected_busy,
                                )
                                self.assertFalse(
                                    supssh_relay.stop_server(
                                        socket_address=addr, only_if_idle=True
                                    )
                                )
                                gate_writer.write(b'1')

                            stdout_data, stderr_data = client_proc.communicate(
                                b'hello'
                            )
                        except BaseException:
                            client_proc.kill()
                            raise

                    self.assertEqual(client_proc.returncode, 0, msg=stderr_data)
                    self.assertIn(b'stdin=hello', stdout_data)
                    self.assertEqual(
                        supssh_relay.ping(socket_address=addr),
                        {
                            'ok': True,
                            'version': supssh_relay.PROTOCOL_VERSION,
                            'active': 0,
                        },
                    )

                    self.assertTrue(
                        supssh_relay.stop_server(
                            socket_address=addr, only_if_idle=True
                        )
                    )
                    self.assertIsNone(supssh_relay.ping(socket_address=addr))


if __name__ == '__main__':
    unittest.main()
