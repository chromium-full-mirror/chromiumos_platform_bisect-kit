# Copyright 2023 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Test experiment module."""

from enum import StrEnum
import unittest

import experiment


class FakeID(StrEnum):
    """A fake class to mock out the real ID class."""

    EXP1 = 'exp1'
    EXP2 = 'exp2'
    EXP3 = 'exp3'
    EXP4 = 'exp4'


class IsInExperimentTest(unittest.TestCase):
    """Test is_in_experiment."""

    def test_experiments_empty_list(self):
        self.assertFalse(experiment.is_in_experiment([], FakeID.EXP1))

    def test_experiments_single_experiment(self):
        self.assertTrue(experiment.is_in_experiment([FakeID.EXP1], FakeID.EXP1))
        self.assertFalse(
            experiment.is_in_experiment([FakeID.EXP1], FakeID.EXP2)
        )

    def test_experiments_multiple_experiments(self):
        self.assertTrue(
            experiment.is_in_experiment(
                [
                    FakeID.EXP1,
                    FakeID.EXP2,
                    FakeID.EXP3,
                    FakeID.EXP4,
                ],
                FakeID.EXP2,
            )
        )
        self.assertFalse(
            experiment.is_in_experiment(
                [
                    FakeID.EXP1,
                    FakeID.EXP2,
                    FakeID.EXP3,
                ],
                FakeID.EXP4,
            )
        )
