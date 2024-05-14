# Copyright 2024 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Test diagnoser_cros module."""

import logging
import unittest
from unittest import mock

from bisect_kit import diagnoser_cros
from bisect_kit import errors
from bisect_kit import shared_dut_pool


logger = logging.getLogger(__name__)


class TestDiagnose(unittest.TestCase):
    """Test diagnose()."""

    class FakeDiagnoser(diagnoser_cros.CrosDiagnoser):
        """A subclass of Diagnoser to facilitate testing."""

        def __init__(self, config: dict, is_stateless: bool):
            # pylint: disable=super-init-not-called
            # Only initializes needed fields.
            self.config = config
            self.is_stateless = is_stateless

    def init(self, config: dict, is_stateless: bool):
        self._diagnoser = self.FakeDiagnoser(config, is_stateless)

    def diagnose(self):
        self._diagnoser.diagnose(
            is_autotest=False,
            switch_test_harness_cmd=[],
            cros_prebuilt_eval_cmd=[],
            android_prebuilt_eval_cmd=[],
            lacros_prebuilt_eval_cmd=[],
            chrome_localbuild_eval_cmd=[],
            should_build_chrome_localbuild_with_tests=False,
            cros_localbuild_eval_cmd=[],
            is_custom_eval=False,
        )

    def setUp(self):
        super().setUp()
        self._mock_diagnose = self.enterContext(
            mock.patch.object(
                self.FakeDiagnoser,
                "_diagnose",
                autospec=True,
            )
        )
        self._mock_shared_dut_pool_manager_cls = self.enterContext(
            mock.patch.object(
                shared_dut_pool,
                'SharedDutPoolManager',
                autospec=True,
            )
        )
        self._mock_clean_up_duts_by_owner = (
            self._mock_shared_dut_pool_manager_cls.return_value.clean_up_duts_by_owner
        )
        self._mock_dut_manager_recycler_cls = self.enterContext(
            mock.patch.object(
                diagnoser_cros,
                'DutManagerRecycler',
                autospec=True,
            )
        )
        self._mock_dut_manager_reset = (
            self._mock_dut_manager_recycler_cls.return_value.reset
        )
        self._diagnoser = None

        self.maxDiff = None

    def test_shared_dut_pool_disalbed_is_stateless(self):
        self.init(config={'experiments': []}, is_stateless=True)
        self.diagnose()

        self._mock_clean_up_duts_by_owner.assert_not_called()
        self._mock_dut_manager_reset.assert_called_once()

    def test_shared_dut_pool_disalbed_is_not_stateless(self):
        self.init(config={'experiments': []}, is_stateless=False)
        self.diagnose()

        self._mock_dut_manager_reset.assert_called_once()
        self._mock_clean_up_duts_by_owner.assert_not_called()

    def test_no_exception_raises(self):
        self.init(
            config={
                'experiments': [
                    'shared_dut_pool',
                ],
                'session': 'session1',
            },
            is_stateless=True,
        )
        self.diagnose()

        self._mock_dut_manager_reset.assert_called_once()
        self._mock_clean_up_duts_by_owner.assert_called_once_with('session1')

    def test_retraible_error(self):
        self.init(
            config={
                'experiments': [
                    'shared_dut_pool',
                ]
            },
            is_stateless=True,
        )
        self._mock_diagnose.side_effect = errors.BisectRetriableError(
            'retriable'
        )

        with self.assertRaises(errors.BisectRetriableError):
            self.diagnose()

        self._mock_dut_manager_reset.assert_called_once()
        self._mock_clean_up_duts_by_owner.assert_not_called()

    def test_fatal_error(self):
        self.init(
            config={
                'experiments': [
                    'shared_dut_pool',
                    'stateless',
                ],
                'session': 'session1',
            },
            is_stateless=True,
        )
        self._mock_diagnose.side_effect = errors.ExecutionFatalError('fatal')

        with self.assertRaises(errors.ExecutionFatalError):
            self.diagnose()

        self._mock_dut_manager_reset.assert_called_once()
        self._mock_clean_up_duts_by_owner.assert_called_once_with('session1')

    def test_dut_manager_reset_raises(self):
        self.init(
            config={
                'experiments': [
                    'shared_dut_pool',
                    'stateless',
                ],
                'session': 'session1',
            },
            is_stateless=True,
        )
        self._mock_diagnose.side_effect = errors.ExecutionFatalError('fatal')
        self._mock_dut_manager_reset.side_effect = Exception('blah')

        # Assert that the raised exception is from _diagose() instead of reset().
        with self.assertRaises(errors.ExecutionFatalError):
            self.diagnose()

        self._mock_dut_manager_reset.assert_called_once()
        self._mock_clean_up_duts_by_owner.assert_not_called()

    def test_clean_up_duts_by_owner_raises(self):
        self.init(
            config={
                'experiments': [
                    'shared_dut_pool',
                    'stateless',
                ],
                'session': 'session1',
            },
            is_stateless=True,
        )

        self._mock_diagnose.side_effect = errors.ExecutionFatalError('fatal')
        self._mock_clean_up_duts_by_owner.side_effect = Exception('blah')

        # Assert that the raised exception is from _diagnose() instead of
        # clean_up_duts_by_owner().
        with self.assertRaises(errors.ExecutionFatalError):
            self.diagnose()

        self._mock_dut_manager_reset.assert_called_once()
        self._mock_clean_up_duts_by_owner.assert_called_once_with('session1')
