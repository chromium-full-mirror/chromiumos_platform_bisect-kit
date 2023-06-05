# Copyright 2023 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Bisector experiment module.

- To create a new experiment, define a new string in the Enum "ID"
- Setup experiment by running ./diagnose_cros_*.py init --experiments
  EXPERIMENT_ID_1 EXPERIMENT_ID_2 --experiments EXPERIMENT_ID_3 to
  assign arbitrary number of experiments to a bisection.
- Run ./diagnoser_cros*.py as usual. To check whether a bisection is in a
  certain experiments, check the list DiagnoseStates.config.experiments.
"""

import argparse
from enum import StrEnum


class ID(StrEnum):
    """Enum of all available experiments."""

    STATELESS = 'stateless'


def common_flags() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument(
        '--experiments',
        type=ID,
        action="extend",
        nargs="+",
        metavar='EXPERIMENTS',
        default=[],
        help='Bisector experiments',
    )
    return parser
