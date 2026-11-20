#!/usr/bin/env python3
# Copyright 2026 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Chroot-side supssh proxy client forwarding stdio to the host relay server.

Installed into `<chromeos_root>/out/sdk/bin/supssh` (`/usr/local/bin/supssh`
inside the SDK chroot) so SSH `ProxyCommand supssh ...` works inside the chroot
using only the Python standard library.
"""

from __future__ import annotations

import argparse
import array
import ipaddress
import json
import os
import re
import socket
import struct
import sys


SOCKET_ENV = 'BISECT_KIT_SUPSSH_SOCKET'
MAX_MSG_SIZE = 4096

# Linux `struct ucred { pid_t pid; uid_t uid; gid_t gid; }`.
_UCRED_STRUCT = struct.Struct('iII')
_HOST_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9.-]*$')
_USER_RE = re.compile(r'^[A-Za-z0-9._][A-Za-z0-9._-]*$')


def get_socket_address(uid: int | None = None) -> str:
    """Returns the AF_UNIX socket address for the relay server."""
    override = os.environ.get(SOCKET_ENV)
    if override:
        if override.startswith('@'):
            return '\x00' + override[1:]
        return override
    if uid is None:
        uid = os.getuid()
    return f'\x00bisect-kit-supssh-{uid}'


def format_socket_address(addr: str) -> str:
    """Formats an AF_UNIX socket address for logs and environment variables."""
    return '@' + addr[1:] if addr.startswith('\x00') else addr


def _is_ip_address(value: str) -> bool:
    # Python 3.9+ ipaddress accepts arbitrary IPv6 `%<scope_id>` suffixes.
    if '%' in value:
        return False
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


def validate_target(host: str, port_str: str, user: str) -> int:
    """Validates destination host, port, and username.

    Args:
        host: Destination hostname or IP literal.
        port_str: Destination TCP port string.
        user: Destination SSH username (may be empty).

    Returns:
        Validated integer port number.

    Raises:
        ValueError: If any parameter fails validation.
    """
    if not host or len(host) > 255:
        raise ValueError(f'invalid host: {host!r}')
    if not (_is_ip_address(host) or _HOST_RE.fullmatch(host)):
        raise ValueError(f'invalid host: {host!r}')

    if not (port_str.isascii() and port_str.isdigit()):
        raise ValueError(f'invalid port: {port_str!r}')
    port = int(port_str)
    if not 1 <= port <= 65535:
        raise ValueError(f'port out of range: {port}')

    if user and (len(user) > 128 or not _USER_RE.fullmatch(user)):
        raise ValueError(f'invalid user: {user!r}')

    return port


def verify_peer_uid(sock: socket.socket, expected_uid: int) -> None:
    """Verifies the peer UID of a connected AF_UNIX socket."""
    # Linux abstract Unix sockets bypass filesystem permissions, so check
    # SO_PEERCRED to prevent another local UID from squatting on the name.
    raw = sock.getsockopt(
        socket.SOL_SOCKET, socket.SO_PEERCRED, _UCRED_STRUCT.size
    )
    _pid, peer_uid, _gid = _UCRED_STRUCT.unpack(raw)
    if peer_uid != expected_uid:
        raise PermissionError(
            f'unexpected relay peer uid {peer_uid} (expected {expected_uid})'
        )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parses Go-flag-compatible `-flag` and `--flag` supssh arguments."""
    parser = argparse.ArgumentParser()
    parser.add_argument('-host', '--host', required=True, help='Target host.')
    parser.add_argument('-port', '--port', default='22', help='Target port.')
    parser.add_argument('-user', '--user', default='', help='Target username.')
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Forwards stdio FDs and target flags to the host supssh relay."""
    args = parse_args(argv)
    try:
        port = validate_target(args.host, args.port, args.user)
    except ValueError as err:
        print(f'supssh relay client: {err}', file=sys.stderr)
        return 2

    addr = get_socket_address()
    printable_addr = format_socket_address(addr)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        try:
            sock.connect(addr)
            verify_peer_uid(sock, os.getuid())
        except OSError as err:
            print(
                f'supssh relay client: failed to connect to {printable_addr}: '
                f'{err}',
                file=sys.stderr,
            )
            return 1

        msg = json.dumps(
            {
                'op': 'connect',
                'host': args.host,
                'port': port,
                'user': args.user,
            }
        ).encode('utf-8')
        stdin_fd = sys.stdin.fileno()
        stdout_fd = sys.stdout.fileno()
        stderr_fd = sys.stderr.fileno()
        fds = array.array('i', [stdin_fd, stdout_fd, stderr_fd])
        sock.sendmsg([msg], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, fds)])

        # Release stdin/stdout in the client stub so EOF from ssh propagates
        # directly to the host supssh process holding the duplicated FDs.
        if stdin_fd != stderr_fd:
            os.close(stdin_fd)
        if stdout_fd not in (stdin_fd, stderr_fd):
            os.close(stdout_fd)

        chunks: list[bytes] = []
        while True:
            chunk = sock.recv(MAX_MSG_SIZE)
            if not chunk:
                break
            chunks.append(chunk)

    if not chunks:
        print(
            'supssh relay client: relay closed connection without status',
            file=sys.stderr,
        )
        return 1

    try:
        reply = json.loads(b''.join(chunks).decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError) as err:
        print(f'supssh relay client: invalid reply: {err}', file=sys.stderr)
        return 1
    if not isinstance(reply, dict):
        print('supssh relay client: invalid reply payload', file=sys.stderr)
        return 1
    if 'error' in reply:
        print(f"supssh relay error: {reply['error']}", file=sys.stderr)
        return 1
    returncode = reply.get('returncode', 1)
    if not isinstance(returncode, int) or isinstance(returncode, bool):
        print('supssh relay client: invalid returncode', file=sys.stderr)
        return 1
    return returncode


if __name__ == '__main__':
    sys.exit(main())
