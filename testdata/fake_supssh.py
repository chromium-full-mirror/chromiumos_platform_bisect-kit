#!/usr/bin/env python3
# Copyright 2026 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Fake supssh executable for hermetic relay end-to-end unit tests."""

import argparse
import os
import sys


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('-host', '--host', required=True)
    parser.add_argument('-port', '--port', required=True)
    parser.add_argument('-user', '--user', default='')
    args = parser.parse_args()

    stderr_msg = os.environ.get('FAKE_SUPSSH_STDERR')
    if stderr_msg:
        sys.stderr.write(stderr_msg)
        sys.stderr.flush()

    gate_fifo = os.environ.get('FAKE_SUPSSH_GATE_FILE')
    if gate_fifo:
        with open(gate_fifo, 'rb') as gate:
            gate.read(1)

    payload = sys.stdin.buffer.read().decode('utf-8', errors='replace')
    sys.stdout.write(
        f'host={args.host} port={args.port} user={args.user} stdin={payload}'
    )
    sys.stdout.flush()
    return int(os.environ.get('FAKE_SUPSSH_EXIT_CODE', '0'))


if __name__ == '__main__':
    sys.exit(main())
