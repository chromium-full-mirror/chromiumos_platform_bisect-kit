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
import dataclasses
from enum import StrEnum
import logging


logger = logging.getLogger(__name__)


class ID(StrEnum):
    """Enum of all available experiments."""

    STATELESS = 'stateless'


_VERSION_MAP = {
    ID.STATELESS: 'alpha',
}


@dataclasses.dataclass
class Experiment:
    """Defines an experiment.

    name (ID) is mandatory while version is optional
    """

    name: ID
    version: str | None = None


def experiment_type(s: str):
    """Parse experiments from the command line.

    The command line expects the value of --experiments to be a list of
      - <exp_name> or
      - <exp_name:version>
    """
    name = None
    version = None

    parts = s.split(':')
    if len(parts) == 1:
        (name,) = parts
    elif len(parts) == 2:
        name, version = parts
    else:
        raise argparse.ArgumentTypeError('Invalid experiment format %s' % s)

    try:
        name = ID(name)
    except ValueError as e:
        raise argparse.ArgumentTypeError(e)

    return Experiment(name, version)


def is_in_experiment(experiments: list[Experiment], exp_in_query: ID) -> bool:
    """Returns whether a bisect is in a specific exepriment.

    For each experiment this bisect is in:
    If version is missing, we don't check experiment versions.
    If version is given,  we check whether current status of the experiment
    matches the specified version.

    For example, if the bisect is run with "--experiments exp1 exp2:beta exp3:alpha"
    and current experiments version is
    {
      exp1: "dev",
      exp2: "beta",
      exp3: "beta",
    }
    Then the bisect would be run with "exp1", "exp2" but not "exp3".

    Args:
      experiments: A list of Experiment object.
      exp_in_query: The Experiment ID in query.
    """
    for exp in experiments:
        if exp.name == exp_in_query:
            # If the version is not specified, current version doesn't matter.
            # If the version is specified, the experiment must specify its
            # current version in _VERSION_MAP and the two must match.
            if not exp.version or exp.version == _VERSION_MAP.get(exp.name):
                return True

    return False


def common_flags() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument(
        '--experiments',
        type=experiment_type,
        action="extend",
        nargs="+",
        metavar='EXPERIMENTS',
        default=[],
        help='Bisector experiments',
    )
    return parser
