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


class TestReleaseDutWrapper(unittest.TestCase):
    """Test release_dut_wrapper()."""

    def setUp(self):
        super().setUp()
        self._mock_shared_dut_pool_manager_cls = self.enterContext(
            mock.patch.object(
                shared_dut_pool,
                'SharedDutPoolManager',
                autospec=True,
            )
        )
        self._mock_clean_up_by_owner = (
            self._mock_shared_dut_pool_manager_cls.return_value.clean_up_duts_by_owner
        )
        self.maxDiff = None

    def test_shared_dut_pool_disalbed(self):
        class FakeDiagnoser:
            """Fake class for testing."""

            def __init__(self, config):
                self.config = config

            @diagnoser_cros.release_dut_wrapper
            def func(self):
                pass

        fake_diagnoser = FakeDiagnoser(config={'experiments': []})
        fake_diagnoser.func()
        self._mock_clean_up_by_owner.assert_not_called()

    def test_no_exception_raises(self):
        class FakeDiagnoser:
            """Fake class for testing."""

            def __init__(self, config):
                self.config = config
                self.is_stateless = True

            @diagnoser_cros.release_dut_wrapper
            def func(self):
                pass

        fake_diagnoser = FakeDiagnoser(
            config={
                'experiments': [
                    'shared_dut_pool',
                    'stateless',
                ],
                'session': 'session1',
            }
        )

        fake_diagnoser.func()

        self._mock_clean_up_by_owner.assert_called_once_with('session1')

    def test_retraible_error(self):
        class FakeDiagnoser:
            """Fake class for testing."""

            def __init__(self, config):
                self.config = config
                self.is_stateless = True

            @diagnoser_cros.release_dut_wrapper
            def func(self):
                raise errors.BisectRetriableError('retriable')

        fake_diagnoser = FakeDiagnoser(
            config={
                'experiments': [
                    'shared_dut_pool',
                    'stateless',
                ]
            }
        )
        with self.assertRaises(errors.BisectRetriableError):
            fake_diagnoser.func()

        self._mock_clean_up_by_owner.assert_not_called()

    def test_fatal_error(self):
        class FakeDiagnoser:
            """Fake class for testing."""

            def __init__(self, config):
                self.config = config
                self.is_stateless = True

            @diagnoser_cros.release_dut_wrapper
            def func(self):
                raise errors.ExecutionFatalError('fatal')

        fake_diagnoser = FakeDiagnoser(
            config={
                'experiments': [
                    'shared_dut_pool',
                    'stateless',
                ],
                'session': 'session1',
            }
        )

        with self.assertRaises(errors.ExecutionFatalError):
            fake_diagnoser.func()

        self._mock_clean_up_by_owner.assert_called_once_with('session1')
