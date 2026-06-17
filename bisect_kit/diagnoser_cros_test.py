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

        def __init__(self, config: core.DiagnoserConfig, is_stateless: bool):
            # pylint: disable=super-init-not-called
            # Only initializes needed fields.
            self.config = config
            self.is_stateless = is_stateless

    def init(self, config: core.DiagnoserConfig, is_stateless: bool):
        self._diagnoser: TestDiagnose.FakeDiagnoser | None = self.FakeDiagnoser(
            config, is_stateless
        )

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
            switch_test_harness_cmd: list[str] | None = [
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


class TestDiagnoseCommandLine(unittest.TestCase):
    """Test DiagnoseCommandLineBase class."""

    class FakeDiagnoseCommandLine(diagnoser_cros.DiagnoseCommandLineBase):
        """A fake class for DiagnoseCommandLineBase."""

        def do_run(self):
            pass

        def cmd_run(self, opts):
            pass

    def setUp(self):
        self.cli = self.FakeDiagnoseCommandLine()
        self.parser = self.cli.create_argument_parser()

    def test_chrome_cfi_thinlto_build_arg(self):
        opts = self.parser.parse_args(
            [
                'init',
                '--old',
                'old_ver',
                '--new',
                'new_ver',
                '--chrome-cfi-thinlto-build',
                '--session',
                's',
            ]
        )
        self.assertTrue(opts.chrome_cfi_thinlto_build)


class TestCrosDiagnoser(unittest.TestCase):
    """Test CrosDiagnoser class."""

    class FakeDiagnoser(diagnoser_cros.CrosDiagnoser):
        """A mock class for CrosDiagnoser"""

        # pylint: disable=W0231
        # Not called the __init__method in the parent.
        def __init__(self, config: core.DiagnoserConfig):
            self.config = config
            self.states = mock.Mock()
            self.old_info = mock.Mock()
            self.new_info = mock.Mock()
            self.noisy: str | None = None
            self.is_stateless = True
            self.cros_old: str | None = '1.2.3'
            self.is_vm_board = False
            self.dut_allocate_spec = mock.Mock()
            self.dut_allocate_spec.boards = []
            self.pre_allocated_dut = 'dut'
            self._dut_manager = mock.Mock()

    def setUp(self):
        self.config: core.DiagnoserConfig = {
            'chrome_root': '/path/to/chrome',
            'chrome_mirror': '/path/to/mirror',
            'board': 'eve',
            'board_cpu_arch': 'amd64',
        }
        self.diagnoser = self.FakeDiagnoser(self.config)

    @mock.patch('bisect_kit.diagnoser_cros.cros_util.is_buildbucket_buildable')
    @mock.patch.object(diagnoser_cros.CrosDiagnoser, '_narrow_down_chrome')
    @mock.patch('bisect_kit.diagnoser_cros.cros_util.version_to_short')
    def test_diagnose_chrome_cfi_thinlto_build(
        self,
        mock_version_to_short,
        mock_narrow_down_chrome,
        mock_is_buildbucket_buildable,
    ):
        # pylint: disable=protected-access
        self.config['chrome_cfi_thinlto_build'] = True
        mock_version_to_short.return_value = '1.2.3'
        mock_is_buildbucket_buildable.return_value = False

        # We only want to test the part that sets gn_extra_args.
        # Since _diagnose is long, we might need to mock a lot of things.
        # Alternatively, we can test _narrow_down_chrome which is already tested.

        # Let's try to mock the steps before step 4.
        with mock.patch.object(
            self.diagnoser, 'narrow_down_chromeos_prebuilt'
        ), mock.patch.object(
            self.diagnoser, 'narrow_down_android'
        ) as mock_narrow_down_android, mock.patch.object(
            self.diagnoser, 'narrow_down_dlc_prebuilt'
        ) as mock_narrow_down_dlc, mock.patch(
            'bisect_kit.diagnoser_cros.run_switch_or_eval_cmd'
        ):
            mock_narrow_down_android.return_value = False
            mock_narrow_down_dlc.return_value = False
            mock_narrow_down_chrome.return_value = True

            self.diagnoser._diagnose(
                is_autotest=False,
                switch_test_harness_cmd=None,
                cros_prebuilt_eval_cmd=[],
                android_prebuilt_eval_cmd=[],
                chrome_localbuild_eval_cmd=['eval_chrome'],
                should_build_chrome_localbuild_with_tests=True,
                cros_localbuild_eval_cmd=[],
            )

        mock_narrow_down_chrome.assert_called_once()
        _, kwargs = mock_narrow_down_chrome.call_args
        gn_extra_args = kwargs['gn_extra_args']
        self.assertIn('use_thin_lto=true', gn_extra_args)
        self.assertIn('is_cfi=true', gn_extra_args)
        self.assertIn('use_cfi_cast=true', gn_extra_args)

    @mock.patch('bisect_kit.wrapper.BisectorWrapper')
    def test_bisect_with_chrome_localbuild_gn_extra_args(
        self, MockBisectorWrapper
    ):
        # pylint: disable=protected-access
        mock_dut_manager = mock.Mock()
        self.diagnoser._narrow_down_chrome(
            mock_dut_manager,
            eval_cmd=['eval.sh'],
            init_once=None,
            gn_extra_args='dcheck_always_on=true',
        )

        MockBisectorWrapper.return_value.init_if_necessary.assert_called()
        _, kwargs = MockBisectorWrapper.return_value.init_if_necessary.call_args
        switch_cmds = kwargs['switch_cmds']
        self.assertEqual(len(switch_cmds), 1)
        cmd = switch_cmds[0]
        self.assertIn('--gn-extra-args', cmd)
        self.assertIn('dcheck_always_on=true', cmd)

    @mock.patch('bisect_kit.diagnoser_cros.buildbucket_util.BuildbucketApi')
    @mock.patch('bisect_kit.diagnoser_cros.cros_util.is_buildbucket_buildable')
    @mock.patch.object(
        diagnoser_cros.CrosDiagnoser, 'narrow_down_chromeos_localbuild'
    )
    @mock.patch('bisect_kit.diagnoser_cros.cros_util.version_to_short')
    def test_diagnose_fallback_to_localbuild_when_no_builder(
        self,
        mock_version_to_short,
        mock_narrow_down_chromeos_localbuild,
        mock_is_buildbucket_buildable,
        mock_buildbucket_api_cls,
    ):
        # Setup
        self.config['enable_buildbucket_chrome'] = True
        self.config['disable_buildbucket_chromeos'] = False
        mock_version_to_short.return_value = '1.2.3'
        mock_is_buildbucket_buildable.return_value = True

        # Mock BuildbucketApi singleton instance
        mock_api_instance = mock_buildbucket_api_cls.return_value
        mock_api_instance.has_builder.return_value = False

        # Mock other steps to avoid running them
        with mock.patch.object(
            self.diagnoser, 'narrow_down_chromeos_prebuilt'
        ), mock.patch.object(
            self.diagnoser, 'narrow_down_android'
        ) as mock_narrow_down_android, mock.patch.object(
            self.diagnoser, 'narrow_down_dlc_prebuilt'
        ) as mock_narrow_down_dlc, mock.patch(
            'bisect_kit.diagnoser_cros.run_switch_or_eval_cmd'
        ):
            mock_narrow_down_android.return_value = False
            mock_narrow_down_dlc.return_value = False

            # pylint: disable=protected-access
            self.diagnoser._diagnose(
                is_autotest=False,
                switch_test_harness_cmd=None,
                cros_prebuilt_eval_cmd=[],
                android_prebuilt_eval_cmd=[],
                chrome_localbuild_eval_cmd=[],
                should_build_chrome_localbuild_with_tests=True,
                cros_localbuild_eval_cmd=['eval_cros'],
            )

        # Verify BuildbucketApi().has_builder was called with the board
        mock_api_instance.has_builder.assert_called_with('eve')

        # Verify ChromeOS bisection was called with buildbucket_build=False (3rd arg)
        mock_narrow_down_chromeos_localbuild.assert_called_once()
        args, _ = mock_narrow_down_chromeos_localbuild.call_args
        # args[0] is dut_manager, args[1] is eval_cmd, args[2] is buildbucket_build
        self.assertFalse(args[2])


if __name__ == '__main__':
    unittest.main()
