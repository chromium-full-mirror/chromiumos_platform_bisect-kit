# Copyright 2023 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Test dut_manager module."""

import logging
import unittest
from unittest import mock

from bisect_kit import cros_lab_util
from bisect_kit import dut_manager as dut_manager_module
from bisect_kit import errors
import cros_helper


logger = logging.getLogger(__name__)


class DummyException(Exception):
    """A dummy exception class used for testing."""


class TestDutManagerAllocation(unittest.TestCase):
    """Test the dut allocation logic in DutManager."""

    def setUp(self):
        super().setUp()
        self.mock_update_dut_alloc_spec = self.enterContext(
            mock.patch.object(
                dut_manager_module.DutManager,
                'update_dut_alloc_spec',
                autospec=True,
            )
        )
        self.mock_dut_lease_status_monitor = self.enterContext(
            mock.patch.object(
                cros_lab_util, 'dut_lease_status_monitor', autospec=True
            )
        )
        self.mock_crosfleet_release_dut = self.enterContext(
            mock.patch.object(
                cros_lab_util, 'crosfleet_release_dut', autospec=True
            )
        )
        self.mock_allocate_dut = self.enterContext(
            mock.patch.object(cros_helper, 'allocate_dut', autospec=True)
        )
        self.mock_dut_init_func = mock.Mock()
        self.mock_dut_leave_context_check_func = mock.Mock()
        self.mock_states = self.dummy_mock_states()

    def dummy_mock_states(self):
        dummy_allocate_spec_dict = {
            'pool': None,
            'dimensions': None,
            'board': None,
            'model': None,
            'sku': None,
            'dut_name': None,
            'satlab_ip': None,
            'version_hint': None,
            'builder_hint': None,
            'time_limit': None,
            'duration': None,
            'parallel': None,
            'chromeos_root': None,
            'session': None,
        }
        mock_states = dut_manager_module.DiagnoseStates('dummy_session_file')
        mock_states.init(
            config={'session': 'stateless'},
            dut_allocate_spec_dict=dummy_allocate_spec_dict,
        )
        return mock_states

    def test_dut_pre_allocated_should_auto_alloc(self):
        dut_manager = dut_manager_module.DutManager(
            self.mock_states,
            'pre_allocated_dut',
            True,
            self.mock_dut_init_func,
            self.mock_dut_leave_context_check_func,
        )
        with dut_manager.provision() as dut:
            self.assertEqual(dut, 'pre_allocated_dut')
        self.mock_update_dut_alloc_spec.assert_not_called()
        self.mock_dut_lease_status_monitor.assert_not_called()
        self.mock_crosfleet_release_dut.assert_not_called()
        self.mock_allocate_dut.assert_not_called()
        self.mock_dut_init_func.assert_called_once_with('pre_allocated_dut')
        self.mock_dut_leave_context_check_func.assert_called_once_with(
            'pre_allocated_dut'
        )

    def test_dut_pre_allocated_should_not_auto_alloc(self):
        dut_manager = dut_manager_module.DutManager(
            self.mock_states,
            'pre_allocated_dut',
            False,
            self.mock_dut_init_func,
            self.mock_dut_leave_context_check_func,
        )
        with dut_manager.provision() as dut:
            self.assertEqual(dut, 'pre_allocated_dut')
        self.mock_update_dut_alloc_spec.assert_not_called()
        self.mock_dut_lease_status_monitor.assert_not_called()
        self.mock_crosfleet_release_dut.assert_not_called()
        self.mock_allocate_dut.assert_not_called()
        self.mock_dut_init_func.assert_called_once_with('pre_allocated_dut')
        self.mock_dut_leave_context_check_func.assert_called_once_with(
            'pre_allocated_dut'
        )

    def test_dut_pre_allocated_force_monitoring(self):
        dut_manager = dut_manager_module.DutManager(
            self.mock_states,
            'pre_allocated_dut',
            True,
            self.mock_dut_init_func,
            self.mock_dut_leave_context_check_func,
            force_monitoring=True,
        )
        with dut_manager.provision() as dut:
            self.assertEqual(dut, 'pre_allocated_dut')
        self.mock_update_dut_alloc_spec.assert_not_called()
        self.mock_dut_lease_status_monitor.assert_called_once_with(
            'pre_allocated_dut', cros_lab_util.make_lease_reason('stateless')
        )
        self.mock_crosfleet_release_dut.assert_not_called()
        self.mock_allocate_dut.assert_not_called()
        self.mock_dut_init_func.assert_called_once_with('pre_allocated_dut')
        self.mock_dut_leave_context_check_func.assert_called_once_with(
            'pre_allocated_dut'
        )

    def test_dut_pre_allocated_only_initailize_once(self):
        dut_manager = dut_manager_module.DutManager(
            self.mock_states,
            'pre_allocated_dut',
            True,
            self.mock_dut_init_func,
            self.mock_dut_leave_context_check_func,
        )
        with dut_manager.provision() as dut:
            self.assertEqual(dut, 'pre_allocated_dut')
        # provision is called twice, but the initialization function should not be
        # called again.
        with dut_manager.provision() as dut:
            self.assertEqual(dut, 'pre_allocated_dut')
        self.mock_update_dut_alloc_spec.assert_not_called()
        self.mock_dut_lease_status_monitor.assert_not_called()
        self.mock_crosfleet_release_dut.assert_not_called()
        self.mock_allocate_dut.assert_not_called()
        self.mock_dut_init_func.assert_called_once_with('pre_allocated_dut')
        self.mock_dut_leave_context_check_func.assert_has_calls(
            [mock.call('pre_allocated_dut'), mock.call('pre_allocated_dut')]
        )

    def test_dut_pre_allocated_missing_dut_funcs(self):
        dut_manager = dut_manager_module.DutManager(
            self.mock_states,
            'pre_allocated_dut',
            True
            # dut_init_func and dut_leave_context_check_func are missing.
        )
        with dut_manager.provision() as dut:
            self.assertEqual(dut, 'pre_allocated_dut')
        self.mock_update_dut_alloc_spec.assert_not_called()
        self.mock_dut_lease_status_monitor.assert_not_called()
        self.mock_crosfleet_release_dut.assert_not_called()
        self.mock_allocate_dut.assert_not_called()
        self.mock_dut_init_func.assert_not_called()
        self.mock_dut_leave_context_check_func.assert_not_called()

    def test_no_pre_allocated_dut_should_auto_alloc(self):
        self.mock_allocate_dut.return_value = 'host'
        dut_manager = dut_manager_module.DutManager(
            self.mock_states,
            cros_lab_util.LAB_DUT,
            True,
            self.mock_dut_init_func,
            self.mock_dut_leave_context_check_func,
        )
        with dut_manager.provision() as dut:
            self.assertEqual(dut, 'host.cros')
        self.mock_update_dut_alloc_spec.assert_called_once_with(
            dut_manager, 'host.cros'
        )
        self.mock_dut_lease_status_monitor.assert_called_once_with(
            'host.cros', cros_lab_util.make_lease_reason('stateless')
        )
        self.mock_crosfleet_release_dut.assert_called_once_with('host')
        self.mock_allocate_dut.assert_called_once_with(
            dut_manager.dut_allocate_spec
        )
        self.mock_dut_init_func.assert_called_once_with('host.cros')
        self.mock_dut_leave_context_check_func.assert_called_once_with(
            'host.cros'
        )

    def test_no_pre_allocated_dut_should_not_auto_alloc(self):
        self.mock_allocate_dut.return_value = 'host'
        dut_manager = dut_manager_module.DutManager(
            self.mock_states,
            cros_lab_util.LAB_DUT,
            False,
            self.mock_dut_init_func,
            self.mock_dut_leave_context_check_func,
        )
        with dut_manager.provision() as dut:
            self.assertEqual(dut, None)
        self.mock_update_dut_alloc_spec.assert_not_called()
        self.mock_dut_lease_status_monitor.assert_not_called()
        self.mock_crosfleet_release_dut.assert_not_called()
        self.mock_allocate_dut.assert_not_called()
        self.mock_dut_init_func.assert_not_called()
        self.mock_dut_leave_context_check_func.assert_not_called()

    def test_no_pre_allocated_dut_auto_allocate_failed(self):
        self.mock_allocate_dut.return_value = 'host'
        dut_manager = dut_manager_module.DutManager(
            self.mock_states,
            cros_lab_util.LAB_DUT,
            True,
            self.mock_dut_init_func,
            self.mock_dut_leave_context_check_func,
        )
        self.mock_allocate_dut.return_value = None
        with self.assertRaises(errors.NoDutAvailable):
            with dut_manager.provision():
                pass
        self.mock_update_dut_alloc_spec.assert_not_called()
        self.mock_dut_lease_status_monitor.assert_not_called()
        self.mock_crosfleet_release_dut.assert_not_called()
        self.mock_allocate_dut.assert_called_once_with(
            dut_manager.dut_allocate_spec
        )
        self.mock_dut_init_func.assert_not_called()
        self.mock_dut_leave_context_check_func.assert_not_called()

    def test_auto_release_on_exception(self):
        self.mock_allocate_dut.return_value = 'host'
        dut_manager = dut_manager_module.DutManager(
            self.mock_states,
            cros_lab_util.LAB_DUT,
            True,
            self.mock_dut_init_func,
            self.mock_dut_leave_context_check_func,
        )
        try:
            with dut_manager.provision() as dut:
                self.assertEqual(dut, 'host.cros')
                raise DummyException()
        except DummyException:
            pass

        self.mock_update_dut_alloc_spec.assert_called_once_with(
            dut_manager, 'host.cros'
        )
        self.mock_dut_lease_status_monitor.assert_called_once_with(
            'host.cros', cros_lab_util.make_lease_reason('stateless')
        )
        self.mock_crosfleet_release_dut.assert_called_once_with('host')
        self.mock_allocate_dut.assert_called_once_with(
            dut_manager.dut_allocate_spec
        )
        self.mock_dut_init_func.assert_called_once_with('host.cros')
        self.mock_dut_leave_context_check_func.assert_called_once_with(
            'host.cros'
        )

    def test_dut_leave_context_check_func_failed(self):
        self.mock_allocate_dut.return_value = 'host'
        dut_manager = dut_manager_module.DutManager(
            self.mock_states,
            cros_lab_util.LAB_DUT,
            True,
            self.mock_dut_init_func,
            self.mock_dut_leave_context_check_func,
        )

        def assert_failed_func(unused_dut):
            assert False, 'FAIL'

        self.mock_dut_leave_context_check_func.side_effect = assert_failed_func
        with self.assertRaises(AssertionError):
            with dut_manager.provision() as dut:
                self.assertEqual(dut, 'host.cros')

        self.mock_update_dut_alloc_spec.assert_called_once_with(
            dut_manager, 'host.cros'
        )
        self.mock_dut_lease_status_monitor.assert_called_once_with(
            'host.cros', cros_lab_util.make_lease_reason('stateless')
        )
        self.mock_crosfleet_release_dut.assert_called_once_with('host')
        self.mock_allocate_dut.assert_called_once_with(
            dut_manager.dut_allocate_spec
        )
        self.mock_dut_init_func.assert_called_once_with('host.cros')
        self.mock_dut_leave_context_check_func.assert_called_once_with(
            'host.cros'
        )

    def test_has_pre_allocated_dut(self):
        dut_manager = dut_manager_module.DutManager(
            self.mock_states, cros_lab_util.LAB_DUT, True
        )
        self.assertFalse(dut_manager.has_preallocated_dut())

        dut_manager = dut_manager_module.DutManager(
            self.mock_states, 'pre_allocated_dut', True
        )
        self.assertTrue(dut_manager.has_preallocated_dut())
