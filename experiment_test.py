# Copyright 2023 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Test experiment module."""

from enum import StrEnum
import unittest
from unittest import mock

import experiment


class FakeID(StrEnum):
    """A fake class to mock out the real ID class."""

    EXP1 = 'exp1'
    EXP2 = 'exp2'
    EXP3 = 'exp3'
    EXP4 = 'exp4'


# A fake map to mock out the real _VERSION_MAP.
FAKE_VERSION_MAP = {
    FakeID.EXP1: 'dev',
    FakeID.EXP2: 'beta',
    FakeID.EXP3: 'beta',
}


class IsInExperimentTest(unittest.TestCase):
    """Test is_in_experiment."""

    def setUp(self):
        super().setUp()
        self._mock_id_class = self.enterContext(
            mock.patch.object(experiment, 'ID', FakeID)
        )

        self._mock_version_map = self.enterContext(
            mock.patch.object(experiment, '_VERSION_MAP', FAKE_VERSION_MAP)
        )

    def test_experiments_empty_list(self):
        self.assertFalse(experiment.is_in_experiment([], experiment.ID.EXP1))

    def test_experiments_single_experiment(self):
        self.assertTrue(
            experiment.is_in_experiment(
                [experiment.ID.EXP1], experiment.ID.EXP1
            )
        )
        self.assertFalse(
            experiment.is_in_experiment(
                [experiment.ID.EXP1], experiment.ID.EXP2
            )
        )

    def test_experiments_multiple_experiments(self):
        self.assertTrue(
            experiment.is_in_experiment(
                [
                    experiment.ID.EXP1,
                    experiment.ID.EXP2,
                    experiment.ID.EXP3,
                    experiment.ID.EXP4,
                ],
                experiment.ID.EXP2,
            )
        )
        self.assertFalse(
            experiment.is_in_experiment(
                [
                    experiment.ID.EXP1,
                    experiment.ID.EXP2,
                    experiment.ID.EXP3,
                ],
                experiment.ID.EXP4,
            )
        )
