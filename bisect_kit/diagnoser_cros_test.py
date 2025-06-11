# Copyright 2024 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Test diagnoser_cros module."""

import logging
import unittest
from unittest import mock

from bisect_kit import core
from bisect_kit import diagnoser_cros
from bisect_kit import dut_allocate_spec as dut_allocate_spec_module
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
        self._mock_load_dut_allocate_spec = self.enterContext(
            mock.patch.object(
                dut_allocate_spec_module,
                'load',
                autospec=True,
            )
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


class TestDiagnoseTastCommandLineLogic(unittest.TestCase):
    """
    Tests logic to check the command line is correctly parsed,
    """

    class MockableDiagnoseTastLogicCli(diagnoser_cros.DiagnoseCommandLineBase):
        """
        A helper class to mimic relevant parts of DiagnoseTastCommandLine
        for testing command generation logic.
        """

        def __init__(self, states_instance):
            super().__init__()

            # Override default None, instead of initializing it in DiagnoseCommandLineBase.main().
            self.states = states_instance

        def _build_cmds(self):
            switch_test_harness_cmd = [
                './switch_tast_prebuilt.py',
                '--rich-result',
                '--chromeos-root',
                self.config.get('chromeos_root'),
            ]
            common_eval_cmd = [
                './eval_cros_tast.py',
                '--rich-result',
                '--with-private-bundles',
                '--chromeos-root',
                self.config.get('chromeos_root'),
                '--test-name',
                self.config.get('test_name'),
            ]
            if self.config.get('tast_patch_cls'):
                for patch_cl in self.config.get('tast_patch_cls'):
                    common_eval_cmd += ['--tast-patch-cl', patch_cl]
                switch_test_harness_cmd = None

            return switch_test_harness_cmd, common_eval_cmd

        # Replicates do_run from DiagnoseTastCommandLine
        def do_run(self):
            diagnoser = diagnoser_cros.CrosDiagnoser(self.states, self.config)
            switch_test_harness_cmd, common_eval_cmd = self._build_cmds()

            # Just copy the command strings.
            cros_prebuilt_eval_cmd = common_eval_cmd[:]
            android_prebuilt_eval_cmd = common_eval_cmd[:]
            chrome_localbuild_eval_cmd = common_eval_cmd[:]
            cros_localbuild_eval_cmd = common_eval_cmd[:]

            diagnoser.diagnose(
                is_autotest=False,
                switch_test_harness_cmd=switch_test_harness_cmd,
                cros_prebuilt_eval_cmd=cros_prebuilt_eval_cmd,
                android_prebuilt_eval_cmd=android_prebuilt_eval_cmd,
                chrome_localbuild_eval_cmd=chrome_localbuild_eval_cmd,
                should_build_chrome_localbuild_with_tests=True,
                cros_localbuild_eval_cmd=cros_localbuild_eval_cmd,
            )

        # Defining the mock method for suppressing a pylint error
        def cmd_run(self, opts):
            raise NotImplementedError

    @mock.patch('bisect_kit.diagnoser_cros.cros_util.is_buildbucket_buildable')
    @mock.patch('bisect_kit.diagnoser_cros.CrosDiagnoser')
    def test_diagnoser_command_line(
        self, MockCrosDiagnoser, mock_is_buildbucket_buildable
    ):
        """Tests that --tast-patch-cl correctly modifies eval commands."""
        test_config = {
            'chromeos_root': '/path/to/chromeos',
            'test_name': 'tast.awesome.test',
            'tast_patch_cls': ['12345', '67890'],
        }
        mock_states = mock.Mock(spec=core.DiagnoseStates, config=test_config)
        # mock_states.config = test_config

        cli_runner = self.MockableDiagnoseTastLogicCli(mock_states)
        mock_diagnoser_instance = MockCrosDiagnoser.return_value
        mock_is_buildbucket_buildable.return_value = False

        cli_runner.do_run()

        MockCrosDiagnoser.assert_called_once_with(mock_states, test_config)
        mock_diagnoser_instance.diagnose.assert_called_once()
        _, kwargs = mock_diagnoser_instance.diagnose.call_args

        self.assertIsNone(kwargs['switch_test_harness_cmd'])
        expected_patch_args = [
            '--tast-patch-cl',
            '12345',
            '--tast-patch-cl',
            '67890',
        ]

        for cmd_key in [
            'cros_prebuilt_eval_cmd',
            'android_prebuilt_eval_cmd',
            'chrome_localbuild_eval_cmd',
            'cros_localbuild_eval_cmd',
        ]:
            self.assertIn('--test-name', kwargs[cmd_key])
            self.assertIn('tast.awesome.test', kwargs[cmd_key])
            for arg in expected_patch_args:
                self.assertIn(arg, kwargs[cmd_key])


if __name__ == '__main__':
    unittest.main()
