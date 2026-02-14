#!/usr/bin/env python3
# Copyright 2017 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""A simple bisector to bisect a list of arbitrary strings.

The strings to bisect are read from stdin, one item at a line.

Example:
  Let's play the number guessing game. You choice a number in your mind
  in range 1..99. You can lie within 20% probability. Let bisect-kit
  guess.

  Assume numbers less than your answer are considered as "old" and greater or
  equals to your answer are "new".
    $ seq 1 99 | ./bisect_list.py init --old 1 --new 99 --noisy=old=1/5,new=4/5
    $ ./bisect_list.py config switch /bin/true
    $ ./bisect_list.py config eval ./eval-manually.sh
    $ ./bisect_list.py run

  If you don't lie, just omit --noisy argument.

  p.s. seq(1) prints a sequence of numbers, one at a line.
"""

from __future__ import annotations

import argparse
import logging
import sys
import typing

from bisect_kit import bisector_cli
from bisect_kit import cli
from bisect_kit import core


logger = logging.getLogger(__name__)


class ListDomain(core.BisectDomain):
    """Enumerate list of string for bisection."""

    revtype = staticmethod(cli.argtype_notempty)

    @staticmethod
    def add_init_arguments(parser):
        # Do nothing because no additional arguments required for this bisector.
        pass

    @staticmethod
    def init(opts: argparse.Namespace) -> tuple[core.BisectConfig, dict]:
        config: dict[str, typing.Any] = {}
        revlist = [line.strip() for line in sys.stdin.readlines()]
        return config, {'revlist': revlist}

    def __init__(self, config: core.BisectConfig):
        self.config = config

    def setenv(self, env, rev, rev_details=None):
        pass

    def fill_candidate_summary(self, summary):
        # Do nothing.
        return


if __name__ == '__main__':
    bisector_cli.BisectorCommandLine(ListDomain).main()
