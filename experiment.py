# Copyright 2023 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Bisector experiment module.

- To create a new experiment, define a new string in the Enum "ID"
- Setup experiment by running ./diagnose_cros_*.py init --experiments
  EXPERIMENT_1 EXPERIMENT_2 --experiments EXPERIMENT_3 to
  assign arbitrary number of experiments to a bisection.
- Run ./diagnoser_cros*.py as usual. To check whether a bisection is in a
  certain experiments, check the list DiagnoseStates.config.experiments.
"""

import argparse
from enum import StrEnum
import logging


logger = logging.getLogger(__name__)


class ID(StrEnum):
    """Enum of all available experiments."""

    STATELESS = 'stateless'
    VM = 'vm'
    SHARED_DUT_POOL = 'shared_dut_pool'
    EXT4_CHROME_CHECKOUT = 'ext4_chrome_checkout'


# TODO b/328452015: either record the version when an experimental bisection
# runs, or remove it if the version doesn't matter.
_VERSION_MAP = {
    ID.STATELESS: '20230901',
    ID.VM: '20240207',
}


def is_in_experiment(experiments: list[ID], exp_in_query: ID) -> bool:
    """Returns whether a bisect is in a specific exepriment.

    Args:
      experiments: A list of experiment IDs.
      exp_in_query: The Experiment ID in query.
    """
    return exp_in_query in experiments


def common_flags() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument(
        '--experiments',
        type=str,
        action="extend",
        nargs="+",
        metavar='EXPERIMENTS',
        default=[],
        help='Bisector experiments',
    )
    return parser
