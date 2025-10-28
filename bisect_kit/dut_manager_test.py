# Copyright 2023 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Test dut_manager module."""

import contextlib
import json
import logging
import os
import tempfile
import threading
import unittest
from unittest import mock

from bisect_kit import core
from bisect_kit import cros_lab_util
from bisect_kit import dut_allocate_spec as dut_allocate_spec_module
from bisect_kit import dut_allocator
from bisect_kit import dut_manager as dut_manager_module
from bisect_kit import errors
from bisect_kit import shared_dut_pool
from bisect_kit import vm_leaser


logger = logging.getLogger(__name__)


class DummyException(Exception):
    """A dummy exception class used for testing."""


class TestDutManagerAllocation(unittest.TestCase):
    """Test the dut allocation logic in DutManager."""

    def setUp(self):
        super().setUp()
        self.mock_states = self.dummy_mock_states()
        self.mock_dut_allocate_spec = dut_allocate_spec_module.DutAllocateSpec(
            boards=['test_board']
        )

        self.mock_update_states = self.enterContext(
            mock.patch.object(
                dut_manager_module.DutManager,
                '_update_states',
                autospec=True,
            )
        )
        self.mock_dut_lease_status_monitor = self.enterContext(
            mock.patch.object(
                cros_lab_util, 'dut_lease_status_monitor', autospec=True
            )
        )
        self.mock_allocate_dut = self.enterContext(
            mock.patch.object(dut_allocator, 'allocate_dut', autospec=True)
        )
        self.mock_wait_ssh_avaliable = self.enterContext(
            mock.patch.object(
                dut_manager_module.DutManager,
                '_wait_ssh_avaliable',
                autospec=True,
            )
        )
        self.mock_is_vm = self.enterContext(
            mock.patch.object(
                dut_manager_module.DutManager,
                'is_vm',
                new_callable=mock.PropertyMock,
            )
        )
        self.mock_search_vm_image = self.enterContext(
            mock.patch.object(
                dut_manager_module.DutManager, '_search_vm_image', autospec=True
            )
        )
        self.mock_allocate_vm_dut = self.enterContext(
            mock.patch.object(vm_leaser, 'allocate_dut')
        )
        self.mock_release_vm = self.enterContext(
            mock.patch.object(
                dut_manager_module.DutManager, '_release_vm', autospec=True
            )
        )
        self.mock_dut_init_func = mock.Mock()
        self.mock_dut_leave_context_check_func = mock.Mock()
        self.mock_delayed_dut_releaser_cls = self.enterContext(
            mock.patch.object(
                dut_manager_module, 'DelayedDutReleaser', autospec=True
            )
        )
        self.mock_dut_releaser = self.mock_delayed_dut_releaser_cls.return_value

    def reset_all_mocks(self):
        self.mock_update_states.reset_mock()
        self.mock_dut_lease_status_monitor.reset_mock()
        self.mock_allocate_dut.reset_mock()
        self.mock_wait_ssh_avaliable.reset_mock()
        self.mock_is_vm.reset_mock(return_value=True)
        self.mock_search_vm_image.reset_mock()
        self.mock_allocate_vm_dut.reset_mock(return_value=True)
        self.mock_release_vm.reset_mock()
        self.mock_dut_init_func.reset_mock()
        self.mock_dut_leave_context_check_func.reset_mock()
        self.mock_delayed_dut_releaser_cls.reset_mock()
        # By default self.mock_delayed_dut_releaser_cls.return_value is not
        # reset when calling reset_mock(), so self.mock_dut_releaser is still
        # valid.
        self.mock_dut_releaser.reset_mock()

    def dummy_mock_states(self):
        mock_states = core.DiagnoseStates('dummy_session_file')
        mock_states.init_states(
            config={
                'session': 'stateless',
                'experiments': [],
                'chromeos_root': 'PATH_TO_CHROMEOS_ROOT',
            },
        )
        return mock_states

    def dummy_allocate_vm_dut_result(self):
        return vm_leaser.VMLeaseResult(
            lease_id='lease_id',
            private_host='127.0.0.1',
            public_host='1.2.3.4',
            gce_project='mock_project',
            gce_region='mock_region',
            gce_image_project='mock_project',
            gce_image_name='mock_image',
        )

    def test_dut_pre_allocated_should_auto_alloc(self):
        self.mock_is_vm.return_value = False
        dut_manager = dut_manager_module.DutManager(
            'PreAllocate',
            self.mock_states,
            self.mock_dut_allocate_spec,
            'pre_allocated_dut',
            True,
            self.mock_dut_init_func,
            self.mock_dut_leave_context_check_func,
        )
        with dut_manager.provision() as dut:
            self.assertEqual(dut, 'pre_allocated_dut')
        self.mock_update_states.assert_not_called()
        self.mock_dut_lease_status_monitor.assert_not_called()
        self.mock_delayed_dut_releaser_cls.assert_not_called()
        self.mock_allocate_dut.assert_not_called()
        self.mock_dut_init_func.assert_called_once_with('pre_allocated_dut')
        self.mock_dut_leave_context_check_func.assert_called_once_with(
            'pre_allocated_dut'
        )

    def test_dut_pre_allocated_should_not_auto_alloc(self):
        self.mock_is_vm.return_value = False
        dut_manager = dut_manager_module.DutManager(
            'PreAllocate',
            self.mock_states,
            self.mock_dut_allocate_spec,
            'pre_allocated_dut',
            False,
            self.mock_dut_init_func,
            self.mock_dut_leave_context_check_func,
        )
        with dut_manager.provision() as dut:
            self.assertEqual(dut, 'pre_allocated_dut')
        self.mock_update_states.assert_not_called()
        self.mock_dut_lease_status_monitor.assert_not_called()
        self.mock_delayed_dut_releaser_cls.assert_not_called()
        self.mock_allocate_dut.assert_not_called()
        self.mock_dut_init_func.assert_called_once_with('pre_allocated_dut')
        self.mock_dut_leave_context_check_func.assert_called_once_with(
            'pre_allocated_dut'
        )

    def test_dut_pre_allocated_force_monitoring(self):
        self.mock_is_vm.return_value = False
        dut_manager = dut_manager_module.DutManager(
            'PreAllocate',
            self.mock_states,
            self.mock_dut_allocate_spec,
            'pre_allocated_dut',
            True,
            self.mock_dut_init_func,
            self.mock_dut_leave_context_check_func,
            should_force_monitoring=True,
        )
        with dut_manager.provision() as dut:
            self.assertEqual(dut, 'pre_allocated_dut')
        self.mock_update_states.assert_not_called()
        self.mock_dut_lease_status_monitor.assert_called_once_with(
            'pre_allocated_dut',
            cros_lab_util.make_lease_reason('stateless'),
            'stateless',
        )
        self.mock_delayed_dut_releaser_cls.assert_not_called()
        self.mock_allocate_dut.assert_not_called()
        self.mock_dut_init_func.assert_called_once_with('pre_allocated_dut')
        self.mock_dut_leave_context_check_func.assert_called_once_with(
            'pre_allocated_dut'
        )

    def test_dut_pre_allocated_only_initailize_once(self):
        self.mock_is_vm.return_value = False
        dut_manager = dut_manager_module.DutManager(
            'PreAllocate',
            self.mock_states,
            self.mock_dut_allocate_spec,
            'pre_allocated_dut',
            True,
            self.mock_dut_init_func,
            self.mock_dut_leave_context_check_func,
        )
        # First provision.
        with dut_manager.provision() as dut:
            self.assertEqual(dut, 'pre_allocated_dut')

        self.reset_all_mocks()

        # Second provision. dut_init_func should not be called again.
        self.mock_is_vm.return_value = False
        with dut_manager.provision() as dut:
            self.assertEqual(dut, 'pre_allocated_dut')

        self.mock_update_states.assert_not_called()
        self.mock_dut_lease_status_monitor.assert_not_called()
        self.mock_delayed_dut_releaser_cls.assert_not_called()
        self.mock_allocate_dut.assert_not_called()
        self.mock_dut_init_func.assert_not_called()
        self.mock_dut_leave_context_check_func.assert_called_once_with(
            'pre_allocated_dut'
        )

    def test_dut_pre_allocated_missing_dut_funcs(self):
        self.mock_is_vm.return_value = False
        dut_manager = dut_manager_module.DutManager(
            'PreAllocate',
            self.mock_states,
            self.mock_dut_allocate_spec,
            'pre_allocated_dut',
            True,
            # dut_init_func and dut_leave_context_check_func are missing.
        )
        with dut_manager.provision() as dut:
            self.assertEqual(dut, 'pre_allocated_dut')
        self.mock_update_states.assert_not_called()
        self.mock_dut_lease_status_monitor.assert_not_called()
        self.mock_delayed_dut_releaser_cls.assert_not_called()
        self.mock_allocate_dut.assert_not_called()
        self.mock_dut_init_func.assert_not_called()
        self.mock_dut_leave_context_check_func.assert_not_called()

    def test_no_pre_allocated_dut_should_auto_alloc(self):
        self.mock_is_vm.return_value = False
        dut_manager = dut_manager_module.DutManager(
            'NoPreAllocate',
            self.mock_states,
            self.mock_dut_allocate_spec,
            cros_lab_util.LAB_DUT,
            True,
            self.mock_dut_init_func,
            self.mock_dut_leave_context_check_func,
        )
        self.mock_allocate_dut.return_value = 'host', 'board'
        with dut_manager.provision() as dut:
            self.assertEqual(dut, 'host.cros')
        self.mock_update_states.assert_called_once_with(
            dut_manager, 'host.cros'
        )
        self.mock_dut_lease_status_monitor.assert_called_once_with(
            'host.cros',
            cros_lab_util.make_lease_reason('stateless'),
            'stateless',
        )
        self.mock_delayed_dut_releaser_cls.assert_called_once_with(
            'host.cros', False, False, mock.ANY
        )
        self.mock_dut_releaser.start.assert_called_once()
        self.mock_allocate_dut.assert_called_once_with(
            dut_manager.dut_allocate_spec,
            'PATH_TO_CHROMEOS_ROOT',
            enable_shared_dut_pool=False,
        )
        self.mock_dut_init_func.assert_called_once_with('host.cros')
        self.mock_dut_leave_context_check_func.assert_called_once_with(
            'host.cros'
        )

    def test_no_pre_allocated_dut_should_not_auto_alloc(self):
        self.mock_is_vm.return_value = False
        dut_manager = dut_manager_module.DutManager(
            'NoPreAllocate',
            self.mock_states,
            self.mock_dut_allocate_spec,
            cros_lab_util.LAB_DUT,
            False,
            self.mock_dut_init_func,
            self.mock_dut_leave_context_check_func,
        )
        with dut_manager.provision() as dut:
            self.assertEqual(dut, None)
        self.mock_update_states.assert_not_called()
        self.mock_dut_lease_status_monitor.assert_not_called()
        self.mock_delayed_dut_releaser_cls.assert_not_called()
        self.mock_allocate_dut.assert_not_called()
        self.mock_dut_init_func.assert_not_called()
        self.mock_dut_leave_context_check_func.assert_not_called()

    def test_no_pre_allocated_dut_auto_allocate_thorws_exception(self):
        self.mock_is_vm.return_value = False
        dut_manager = dut_manager_module.DutManager(
            'NoPreAllocate',
            self.mock_states,
            self.mock_dut_allocate_spec,
            cros_lab_util.LAB_DUT,
            True,
            self.mock_dut_init_func,
            self.mock_dut_leave_context_check_func,
        )
        self.mock_allocate_dut.side_effect = errors.DutLeaseTimeout('timeout')
        with self.assertRaises(errors.DutLeaseTimeout):
            with dut_manager.provision():
                pass
        self.mock_update_states.assert_not_called()
        self.mock_dut_lease_status_monitor.assert_not_called()
        self.mock_delayed_dut_releaser_cls.assert_not_called()
        self.mock_allocate_dut.assert_called_once_with(
            dut_manager.dut_allocate_spec,
            'PATH_TO_CHROMEOS_ROOT',
            enable_shared_dut_pool=False,
        )
        self.mock_dut_init_func.assert_not_called()
        self.mock_dut_leave_context_check_func.assert_not_called()

    def test_no_pre_allocated_dut_auto_allocate_no_host_returned(self):
        self.mock_is_vm.return_value = False
        dut_manager = dut_manager_module.DutManager(
            'NoPreAllocate',
            self.mock_states,
            self.mock_dut_allocate_spec,
            cros_lab_util.LAB_DUT,
            True,
            self.mock_dut_init_func,
            self.mock_dut_leave_context_check_func,
        )
        self.mock_allocate_dut.return_value = None, None
        with self.assertRaises(errors.InternalError):
            with dut_manager.provision():
                pass
        self.mock_update_states.assert_not_called()
        self.mock_dut_lease_status_monitor.assert_not_called()
        self.mock_delayed_dut_releaser_cls.assert_not_called()
        self.mock_allocate_dut.assert_called_once_with(
            dut_manager.dut_allocate_spec,
            'PATH_TO_CHROMEOS_ROOT',
            enable_shared_dut_pool=False,
        )
        self.mock_dut_init_func.assert_not_called()
        self.mock_dut_leave_context_check_func.assert_not_called()

    def test_consecutive_auto_allocate_restored_previous_dut(self):
        self.mock_is_vm.return_value = False
        dut_manager = dut_manager_module.DutManager(
            'NoPreAllocate',
            self.mock_states,
            self.mock_dut_allocate_spec,
            cros_lab_util.LAB_DUT,
            True,
            self.mock_dut_init_func,
            self.mock_dut_leave_context_check_func,
        )
        # First lease.
        self.mock_allocate_dut.return_value = 'host', 'board'
        with dut_manager.provision() as dut:
            self.assertEqual(dut, 'host.cros')

        # Reset all the mocks to test the second lease.
        self.reset_all_mocks()

        # Second lease.
        self.mock_is_vm.return_value = False
        self.mock_dut_releaser.cancel.return_value = True
        self.mock_dut_releaser.get_dut.return_value = 'host.cros'
        with dut_manager.provision() as dut:
            self.assertEqual(dut, 'host.cros')
            self.assertTrue(dut_manager.is_previous_dut())

        # Since the DUT is restored, _update_states() is not called again.
        self.mock_update_states.assert_not_called()
        self.mock_dut_lease_status_monitor.assert_called_once_with(
            'host.cros',
            cros_lab_util.make_lease_reason('stateless'),
            'stateless',
        )
        self.mock_delayed_dut_releaser_cls.assert_called_once_with(
            'host.cros', False, False, mock.ANY
        )
        self.mock_dut_releaser.start.assert_called_once()
        self.mock_allocate_dut.assert_not_called()
        self.mock_dut_init_func.assert_not_called()
        self.mock_dut_leave_context_check_func.assert_called_once_with(
            'host.cros'
        )

    def test_consecutive_auto_allocate_lease_new_dut(self):
        self.mock_is_vm.return_value = False
        dut_manager = dut_manager_module.DutManager(
            'NoPreAllocate',
            self.mock_states,
            self.mock_dut_allocate_spec,
            cros_lab_util.LAB_DUT,
            True,
            self.mock_dut_init_func,
            self.mock_dut_leave_context_check_func,
        )
        # First lease.
        self.mock_allocate_dut.return_value = 'host', 'board'
        with dut_manager.provision() as dut:
            self.assertEqual(dut, 'host.cros')

        # Reset all the mocks to test the second lease.
        self.reset_all_mocks()

        # Second lease.
        self.mock_is_vm.return_value = False
        self.mock_dut_releaser.cancel.return_value = False
        self.mock_allocate_dut.return_value = 'host2', 'board2'
        with dut_manager.provision() as dut:
            self.assertEqual(dut, 'host2.cros')
            self.assertFalse(dut_manager.is_previous_dut())

        self.mock_update_states.assert_called_once_with(
            dut_manager, 'host2.cros'
        )
        self.mock_dut_lease_status_monitor.assert_called_once_with(
            'host2.cros',
            cros_lab_util.make_lease_reason('stateless'),
            'stateless',
        )
        self.mock_delayed_dut_releaser_cls.assert_called_once_with(
            'host2.cros', False, False, mock.ANY
        )
        self.mock_dut_releaser.start.assert_called_once()
        self.mock_allocate_dut.assert_called_once_with(
            dut_manager.dut_allocate_spec,
            'PATH_TO_CHROMEOS_ROOT',
            enable_shared_dut_pool=False,
        )
        self.mock_dut_init_func.assert_called_once_with('host2.cros')
        self.mock_dut_leave_context_check_func.assert_called_once_with(
            'host2.cros'
        )

    def test_nested_provision_twice_with_restored_dut(self):
        self.mock_is_vm.return_value = False
        dut_manager = dut_manager_module.DutManager(
            'NoPreAllocate',
            self.mock_states,
            self.mock_dut_allocate_spec,
            cros_lab_util.LAB_DUT,
            True,
            self.mock_dut_init_func,
            self.mock_dut_leave_context_check_func,
        )

        # First lease.
        self.mock_allocate_dut.return_value = 'host', 'board'
        with dut_manager.provision() as dut:
            self.assertEqual(dut, 'host.cros')
            with dut_manager.provision() as dut:
                self.assertEqual(dut, 'host.cros')
                with dut_manager.provision() as dut:
                    self.assertEqual(dut, 'host.cros')

        self.mock_update_states.assert_called_once_with(
            dut_manager, 'host.cros'
        )
        self.mock_dut_lease_status_monitor.assert_called_once_with(
            'host.cros',
            cros_lab_util.make_lease_reason('stateless'),
            'stateless',
        )
        self.mock_delayed_dut_releaser_cls.assert_called_once_with(
            'host.cros', False, False, mock.ANY
        )
        self.mock_allocate_dut.assert_called_once_with(
            dut_manager.dut_allocate_spec,
            'PATH_TO_CHROMEOS_ROOT',
            enable_shared_dut_pool=False,
        )
        self.mock_dut_init_func.assert_called_once_with('host.cros')
        self.mock_dut_leave_context_check_func.assert_called_once_with(
            'host.cros'
        )

        # Reset all the mocks to test the second lease.
        self.reset_all_mocks()

        # Second lease.
        self.mock_is_vm.return_value = False
        self.mock_dut_releaser.cancel.return_value = True
        self.mock_dut_releaser.get_dut.return_value = 'host.cros'
        with dut_manager.provision() as dut:
            self.assertEqual(dut, 'host.cros')
            self.assertTrue(dut_manager.is_previous_dut())
            with dut_manager.provision() as dut:
                self.assertEqual(dut, 'host.cros')
                self.assertTrue(dut_manager.is_previous_dut())
                with dut_manager.provision() as dut:
                    self.assertEqual(dut, 'host.cros')
                    self.assertTrue(dut_manager.is_previous_dut())

        self.mock_update_states.assert_not_called()
        self.mock_dut_lease_status_monitor.assert_called_once_with(
            'host.cros',
            cros_lab_util.make_lease_reason('stateless'),
            'stateless',
        )
        self.mock_delayed_dut_releaser_cls.assert_called_once_with(
            'host.cros', False, False, mock.ANY
        )
        self.mock_dut_releaser.start.assert_called_once()
        self.mock_allocate_dut.assert_not_called()
        self.mock_dut_init_func.assert_not_called()
        self.mock_dut_leave_context_check_func.assert_called_once_with(
            'host.cros'
        )

    def test_nested_provision_twice_with_new_allocated_dut(self):
        self.mock_is_vm.return_value = False
        dut_manager = dut_manager_module.DutManager(
            'NoPreAllocate',
            self.mock_states,
            self.mock_dut_allocate_spec,
            cros_lab_util.LAB_DUT,
            True,
            self.mock_dut_init_func,
            self.mock_dut_leave_context_check_func,
        )

        # First lease.
        self.mock_allocate_dut.return_value = 'host', 'board'
        with dut_manager.provision() as dut:
            self.assertEqual(dut, 'host.cros')
            with dut_manager.provision() as dut:
                self.assertEqual(dut, 'host.cros')
                with dut_manager.provision() as dut:
                    self.assertEqual(dut, 'host.cros')

        self.mock_update_states.assert_called_once_with(
            dut_manager, 'host.cros'
        )
        self.mock_dut_lease_status_monitor.assert_called_once_with(
            'host.cros',
            cros_lab_util.make_lease_reason('stateless'),
            'stateless',
        )
        self.mock_delayed_dut_releaser_cls.assert_called_once_with(
            'host.cros', False, False, mock.ANY
        )
        self.mock_allocate_dut.assert_called_once_with(
            dut_manager.dut_allocate_spec,
            'PATH_TO_CHROMEOS_ROOT',
            enable_shared_dut_pool=False,
        )
        self.mock_dut_init_func.assert_called_once_with('host.cros')
        self.mock_dut_leave_context_check_func.assert_called_once_with(
            'host.cros'
        )

        # Reset all the mocks to test the second lease.
        self.reset_all_mocks()

        # Second lease.
        self.mock_is_vm.return_value = False
        self.mock_dut_releaser.cancel.return_value = False
        self.mock_allocate_dut.return_value = 'host2', 'board2'
        with dut_manager.provision() as dut:
            self.assertEqual(dut, 'host2.cros')
            self.assertFalse(dut_manager.is_previous_dut())
            with dut_manager.provision() as dut:
                self.assertEqual(dut, 'host2.cros')
                self.assertFalse(dut_manager.is_previous_dut())
                with dut_manager.provision() as dut:
                    self.assertEqual(dut, 'host2.cros')
                    self.assertFalse(dut_manager.is_previous_dut())

        self.mock_update_states.assert_called_once_with(
            dut_manager, 'host2.cros'
        )
        self.mock_dut_lease_status_monitor.assert_called_once_with(
            'host2.cros',
            cros_lab_util.make_lease_reason('stateless'),
            'stateless',
        )
        self.mock_delayed_dut_releaser_cls.assert_called_once_with(
            'host2.cros', False, False, mock.ANY
        )
        self.mock_dut_releaser.start.assert_called_once()
        self.mock_allocate_dut.assert_called_once_with(
            dut_manager.dut_allocate_spec,
            'PATH_TO_CHROMEOS_ROOT',
            enable_shared_dut_pool=False,
        )
        self.mock_dut_init_func.assert_called_once_with('host2.cros')
        self.mock_dut_leave_context_check_func.assert_called_once_with(
            'host2.cros'
        )

    def test_nested_provision_exception_raised(self):
        self.mock_is_vm.return_value = False
        dut_manager = dut_manager_module.DutManager(
            'NoPreAllocate',
            self.mock_states,
            self.mock_dut_allocate_spec,
            cros_lab_util.LAB_DUT,
            True,
            self.mock_dut_init_func,
            self.mock_dut_leave_context_check_func,
        )

        # First lease, exception raised at the second layer.
        self.mock_allocate_dut.return_value = 'host', 'board'
        with self.assertRaises(DummyException):
            with dut_manager.provision() as dut:
                self.assertEqual(dut, 'host.cros')
                with dut_manager.provision() as dut:
                    raise DummyException('some exception')

        self.mock_update_states.assert_called_once_with(
            dut_manager, 'host.cros'
        )
        self.mock_dut_lease_status_monitor.assert_called_once_with(
            'host.cros',
            cros_lab_util.make_lease_reason('stateless'),
            'stateless',
        )
        self.mock_delayed_dut_releaser_cls.assert_called_once_with(
            'host.cros', False, False, mock.ANY
        )
        self.mock_allocate_dut.assert_called_once_with(
            dut_manager.dut_allocate_spec,
            'PATH_TO_CHROMEOS_ROOT',
            enable_shared_dut_pool=False,
        )
        self.mock_dut_init_func.assert_called_once_with('host.cros')
        self.mock_dut_leave_context_check_func.assert_called_once_with(
            'host.cros'
        )

        # Reset all the mocks to test the second lease.
        self.reset_all_mocks()

        # Second lease.
        self.mock_is_vm.return_value = False
        self.mock_dut_releaser.cancel.return_value = False
        self.mock_allocate_dut.return_value = 'host2', 'board2'
        with dut_manager.provision() as dut:
            self.assertEqual(dut, 'host2.cros')

        self.mock_update_states.assert_called_once_with(
            dut_manager, 'host2.cros'
        )
        self.mock_dut_lease_status_monitor.assert_called_once_with(
            'host2.cros',
            cros_lab_util.make_lease_reason('stateless'),
            'stateless',
        )
        self.mock_delayed_dut_releaser_cls.assert_called_once_with(
            'host2.cros', False, False, mock.ANY
        )
        self.mock_dut_releaser.start.assert_called_once()
        self.mock_allocate_dut.assert_called_once_with(
            dut_manager.dut_allocate_spec,
            'PATH_TO_CHROMEOS_ROOT',
            enable_shared_dut_pool=False,
        )
        self.mock_dut_init_func.assert_called_once_with('host2.cros')
        self.mock_dut_leave_context_check_func.assert_called_once_with(
            'host2.cros'
        )

    def test_auto_release_on_exception(self):
        self.mock_is_vm.return_value = False
        self.mock_allocate_dut.return_value = 'host', 'board'
        dut_manager = dut_manager_module.DutManager(
            'NoPreAllocate',
            self.mock_states,
            self.mock_dut_allocate_spec,
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

        self.mock_update_states.assert_called_once_with(
            dut_manager, 'host.cros'
        )
        self.mock_dut_lease_status_monitor.assert_called_once_with(
            'host.cros',
            cros_lab_util.make_lease_reason('stateless'),
            'stateless',
        )
        self.mock_delayed_dut_releaser_cls.assert_called_once_with(
            'host.cros', False, False, mock.ANY
        )
        self.mock_dut_releaser.start.assert_called_once()
        self.mock_allocate_dut.assert_called_once_with(
            dut_manager.dut_allocate_spec,
            'PATH_TO_CHROMEOS_ROOT',
            enable_shared_dut_pool=False,
        )
        self.mock_dut_init_func.assert_called_once_with('host.cros')
        self.mock_dut_leave_context_check_func.assert_called_once_with(
            'host.cros'
        )

    def test_auto_release_on_broken_dut_exception(self):
        self.mock_is_vm.return_value = False
        self.mock_allocate_dut.return_value = 'host', 'board'
        dut_manager = dut_manager_module.DutManager(
            'NoPreAllocate',
            self.mock_states,
            self.mock_dut_allocate_spec,
            cros_lab_util.LAB_DUT,
            True,
            self.mock_dut_init_func,
            self.mock_dut_leave_context_check_func,
        )
        try:
            with dut_manager.provision() as dut:
                self.assertEqual(dut, 'host.cros')
                raise errors.BrokenDutException()
        except errors.BrokenDutException:
            pass

        self.mock_update_states.assert_called_once_with(
            dut_manager, 'host.cros'
        )
        self.mock_dut_lease_status_monitor.assert_called_once_with(
            'host.cros',
            cros_lab_util.make_lease_reason('stateless'),
            'stateless',
        )
        self.mock_delayed_dut_releaser_cls.assert_called_once_with(
            'host.cros', True, False, mock.ANY
        )
        self.mock_dut_releaser.start.assert_called_once()
        self.mock_allocate_dut.assert_called_once_with(
            dut_manager.dut_allocate_spec,
            'PATH_TO_CHROMEOS_ROOT',
            enable_shared_dut_pool=False,
        )
        self.mock_dut_init_func.assert_called_once_with('host.cros')
        self.mock_dut_leave_context_check_func.assert_called_once_with(
            'host.cros'
        )

    def test_dut_leave_context_check_func_failed(self):
        self.mock_is_vm.return_value = False
        self.mock_allocate_dut.return_value = 'host', 'board'
        dut_manager = dut_manager_module.DutManager(
            'NoPreAllocate',
            self.mock_states,
            self.mock_dut_allocate_spec,
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

        self.mock_update_states.assert_called_once_with(
            dut_manager, 'host.cros'
        )
        self.mock_dut_lease_status_monitor.assert_called_once_with(
            'host.cros',
            cros_lab_util.make_lease_reason('stateless'),
            'stateless',
        )
        self.mock_delayed_dut_releaser_cls.assert_called_once_with(
            'host.cros', False, False, mock.ANY
        )
        self.mock_dut_releaser.start.assert_called_once()
        self.mock_allocate_dut.assert_called_once_with(
            dut_manager.dut_allocate_spec,
            'PATH_TO_CHROMEOS_ROOT',
            enable_shared_dut_pool=False,
        )
        self.mock_dut_init_func.assert_called_once_with('host.cros')
        self.mock_dut_leave_context_check_func.assert_called_once_with(
            'host.cros'
        )

    def test_dut(self):
        self.mock_is_vm.return_value = False
        dut_manager = dut_manager_module.DutManager(
            'NoPreAllocate',
            self.mock_states,
            self.mock_dut_allocate_spec,
            cros_lab_util.LAB_DUT,
            True,
        )
        self.assertEqual(dut_manager.dut, None)

        dut_manager = dut_manager_module.DutManager(
            'PreAllocate',
            self.mock_states,
            self.mock_dut_allocate_spec,
            'pre_allocated_dut',
            True,
        )
        self.assertEqual(dut_manager.dut, 'pre_allocated_dut')

        dut_manager = dut_manager_module.DutManager(
            'NoPreAllocate',
            self.mock_states,
            self.mock_dut_allocate_spec,
            cros_lab_util.LAB_DUT,
            True,
        )
        self.assertEqual(dut_manager.dut, None)
        self.mock_allocate_dut.return_value = 'host', 'board'
        with dut_manager.provision():
            self.assertEqual(dut_manager.dut, 'host.cros')
        self.assertEqual(dut_manager.dut, None)

    def test_provision_invalid_vm_cros_version(self):
        # The provision version format is invalid.
        self.mock_is_vm.return_value = True
        dut_manager = dut_manager_module.DutManager(
            'VM',
            self.mock_states,
            self.mock_dut_allocate_spec,
            None,
            True,
            should_force_monitoring=False,
            vm_image_type=dut_manager_module.VmImageType.RELEASE_BUILD,
            # dut_init_func and dut_leave_context_check_func are missing.
        )
        with self.assertRaises(errors.ArgumentError):
            with dut_manager.provision(vm_cros_version='invalid_version'):
                pass

    def test_init_invalid_vm_cros_version(self):
        # The init version format is invalid.
        self.mock_is_vm.return_value = True
        with self.assertRaises(errors.ArgumentError):
            _dut_manager = dut_manager_module.DutManager(
                'VM',
                self.mock_states,
                self.mock_dut_allocate_spec,
                None,
                True,
                should_force_monitoring=False,
                vm_cros_version='invalid_version',
                vm_image_type=dut_manager_module.VmImageType.RELEASE_BUILD,
                # dut_init_func and dut_leave_context_check_func are missing.
            )

    def test_non_vm_init_with_vm_cros_version(self):
        # vm_cros_version should not be passed to init for non-VM case.
        self.mock_is_vm.return_value = False
        with self.assertRaises(AssertionError):
            _dut_manager = dut_manager_module.DutManager(
                'VM',
                self.mock_states,
                self.mock_dut_allocate_spec,
                None,
                True,
                should_force_monitoring=False,
                vm_cros_version='R102-19999.0.0-12345',
                # dut_init_func and dut_leave_context_check_func are missing.
            )

    def test_non_vm_provision_with_vm_cros_version(self):
        # vm_cros_version should not be passed to provision for non-VM case.
        self.mock_is_vm.return_value = False
        dut_manager = dut_manager_module.DutManager(
            'VM',
            self.mock_states,
            self.mock_dut_allocate_spec,
            None,
            True,
            should_force_monitoring=False,
            # dut_init_func and dut_leave_context_check_func are missing.
        )
        with self.assertRaises(AssertionError):
            with dut_manager.provision(vm_cros_version='R102-19999.0.0-12345'):
                pass

    def test_lease_vm_with_should_force_monitoring(self):
        # should_force_monitoring shouldn't be True.
        self.mock_is_vm.return_value = True
        with self.assertRaises(AssertionError):
            _dut_manager = dut_manager_module.DutManager(
                'VM',
                self.mock_states,
                self.mock_dut_allocate_spec,
                None,
                True,
                should_force_monitoring=True,
                # dut_init_func and dut_leave_context_check_func are missing.
            )

    def test_lease_vm_with_undefined_vm_cros_version(self):
        # vm_cros_version is not defined.
        self.mock_is_vm.return_value = True
        dut_manager = dut_manager_module.DutManager(
            'VM',
            self.mock_states,
            self.mock_dut_allocate_spec,
            None,
            True,
            should_force_monitoring=False,
            vm_image_type=dut_manager_module.VmImageType.RELEASE_BUILD,
            # dut_init_func and dut_leave_context_check_func are missing.
        )
        with dut_manager.provision() as dut:
            self.assertIsNone(dut)

    def test_lease_vm_with_vm_cros_version_defined_in_init(self):
        # Leases a VM by vm_cros_version in init.
        self.mock_is_vm.return_value = True
        self.mock_allocate_vm_dut.return_value = (
            self.dummy_allocate_vm_dut_result()
        )
        dut_manager = dut_manager_module.DutManager(
            'VM',
            self.mock_states,
            self.mock_dut_allocate_spec,
            None,
            True,
            should_force_monitoring=False,
            vm_cros_version='R102-19999.0.0-12345',
            vm_image_type=dut_manager_module.VmImageType.RELEASE_BUILD,
            # dut_init_func and dut_leave_context_check_func are missing.
        )
        self.mock_allocate_vm_dut.assert_not_called()
        with dut_manager.provision() as dut:
            self.assertEqual(
                dut, self.dummy_allocate_vm_dut_result().public_host
            )
            self.assertEqual(self.mock_allocate_vm_dut.call_count, 1)
            self.assertEqual(self.mock_release_vm.call_count, 0)

        self.mock_update_states.assert_called()
        self.mock_dut_lease_status_monitor.assert_not_called()
        self.mock_delayed_dut_releaser_cls.assert_not_called()
        self.mock_allocate_dut.assert_not_called()
        self.mock_dut_init_func.assert_not_called()
        self.mock_dut_leave_context_check_func.assert_not_called()
        self.mock_allocate_vm_dut.assert_called()
        self.assertEqual(self.mock_allocate_vm_dut.call_count, 1)
        self.mock_release_vm.assert_called()
        self.assertEqual(self.mock_release_vm.call_count, 1)

    def test_lease_vm_with_vm_cros_version_defined_in_provision(self):
        # Leases a VM by vm_cros_version in provision.
        self.mock_is_vm.return_value = True
        self.mock_allocate_vm_dut.return_value = (
            self.dummy_allocate_vm_dut_result()
        )
        dut_manager = dut_manager_module.DutManager(
            'VM',
            self.mock_states,
            self.mock_dut_allocate_spec,
            None,
            True,
            should_force_monitoring=False,
            vm_image_type=dut_manager_module.VmImageType.RELEASE_BUILD,
            # dut_init_func and dut_leave_context_check_func are missing.
        )
        self.mock_allocate_vm_dut.assert_not_called()
        with dut_manager.provision(
            vm_cros_version='R102-19999.0.0-12345'
        ) as dut:
            self.assertEqual(
                dut, self.dummy_allocate_vm_dut_result().public_host
            )
            self.assertEqual(self.mock_allocate_vm_dut.call_count, 1)
            self.assertEqual(self.mock_release_vm.call_count, 0)

        self.mock_allocate_vm_dut.assert_called()
        self.assertEqual(self.mock_allocate_vm_dut.call_count, 1)
        self.mock_release_vm.assert_called()
        self.assertEqual(self.mock_release_vm.call_count, 1)


class TestDutManagerLocalBuild(unittest.TestCase):
    """Test DutManager with LOCAL_BUILD image type."""

    def setUp(self):
        super().setUp()
        self.mock_states = core.DiagnoseStates('dummy_session_file')
        self.mock_states.init_states(
            config={
                'session': 'stateless',
                'experiments': [],
                'chromeos_root': 'PATH_TO_CHROMEOS_ROOT',
            },
        )
        self.mock_dut_allocate_spec = dut_allocate_spec_module.DutAllocateSpec(
            boards=['test_board']
        )

    def test_init_local_build_vm_cros_version(self):
        # LOCAL_BUILD should allow any version string.
        with mock.patch.object(
            dut_manager_module.DutManager,
            'is_vm',
            new_callable=mock.PropertyMock,
        ) as mock_is_vm:
            mock_is_vm.return_value = True
            _dut_manager = dut_manager_module.DutManager(
                'VM',
                self.mock_states,
                self.mock_dut_allocate_spec,
                None,
                True,
                should_force_monitoring=False,
                vm_cros_version='any_version_string',
                vm_image_type=dut_manager_module.VmImageType.LOCAL_BUILD,
            )

    def test_provision_local_build_vm_cros_version(self):
        # LOCAL_BUILD should allow any version string in provision.
        with mock.patch.object(
            dut_manager_module.DutManager,
            'is_vm',
            new_callable=mock.PropertyMock,
        ) as mock_is_vm:
            mock_is_vm.return_value = True
            dut_manager = dut_manager_module.DutManager(
                'VM',
                self.mock_states,
                self.mock_dut_allocate_spec,
                None,
                True,
                should_force_monitoring=False,
                vm_image_type=dut_manager_module.VmImageType.LOCAL_BUILD,
            )
            # Mock _search_vm_image to avoid actual GS call
            with mock.patch.object(
                dut_manager_module.DutManager, '_search_vm_image'
            ):
                with mock.patch.object(
                    dut_manager_module.DutManager, '_update_states'
                ):
                    with mock.patch.object(
                        dut_manager_module.DutManager, '_wait_ssh_avaliable'
                    ):
                        with mock.patch.object(vm_leaser, 'allocate_dut'):
                            with mock.patch.object(
                                dut_manager_module.DutManager, '_release_vm'
                            ):
                                with dut_manager.provision(
                                    vm_cros_version='any_version_string'
                                ):
                                    pass

    @mock.patch('bisect_kit.gs_util.ls')
    def test_search_vm_image_local_build(self, mock_gs_ls):
        # pylint: disable=protected-access
        with mock.patch.object(
            dut_manager_module.DutManager,
            'is_vm',
            new_callable=mock.PropertyMock,
        ) as mock_is_vm:
            mock_is_vm.return_value = True
            dut_manager = dut_manager_module.DutManager(
                'VM',
                self.mock_states,
                self.mock_dut_allocate_spec,
                None,
                True,
                should_force_monitoring=False,
                vm_cros_version='local:version/123',
                vm_image_type=dut_manager_module.VmImageType.LOCAL_BUILD,
            )

            expected_gs_path = 'gs://crosperf-chromeos-builds/test_board/local_version_123/chromiumos_test_image.tar.gz'
            mock_gs_ls.return_value = [expected_gs_path]

            path = dut_manager._search_vm_image()
            self.assertEqual(path, expected_gs_path)
            mock_gs_ls.assert_called_once_with(
                expected_gs_path, ignore_errors=True
            )


class TestDelayedDutReleaser(unittest.TestCase):
    """Test the dut release logic in DelayedDutReleaser."""

    def setUp(self):
        super().setUp()
        self.mock_crosfleet_release_dut = self.enterContext(
            mock.patch.object(
                cros_lab_util, 'crosfleet_release_dut', autospec=True
            )
        )
        self.mock_thread = self.enterContext(
            mock.patch.object(threading, 'Timer', autospec=True)
        )
        self.mock_dut_pool_manager_cls = self.enterContext(
            mock.patch.object(
                shared_dut_pool, 'SharedDutPoolManager', autospec=True
            )
        )
        self.mock_dut_pool_manager = self.mock_dut_pool_manager_cls.return_value

    def testCancelBeforeTimeUp(self):
        dut_releaser = dut_manager_module.DelayedDutReleaser('host.cros')
        dut_releaser.start()

        self.assertTrue(dut_releaser.cancel())

        # dut_releaser.run() is never called.

    def testCancelAfterTimeUp(self):
        dut_releaser = dut_manager_module.DelayedDutReleaser('host.cros')
        dut_releaser.start()

        # func is the function to be called when initializing the timer, which
        # is dut_releaser.run().
        func = self.mock_thread.call_args.args[1]
        func()

        self.assertFalse(dut_releaser.cancel())
        self.mock_crosfleet_release_dut.assert_called_once_with('host')

    def testCancelWhenTimeUpRaceCondition(self):
        dut_releaser = dut_manager_module.DelayedDutReleaser('host.cros')
        dut_releaser.start()

        self.assertTrue(dut_releaser.cancel())

        # Assuming dut_releaser.cancel() is too late. Though cancel() is
        # successful, run() gets executed nevertheless.
        func = self.mock_thread.call_args.args[1]
        func()

        self.mock_crosfleet_release_dut.assert_not_called()

    def testReturnToSharedDutPool(self):
        dut_releaser = dut_manager_module.DelayedDutReleaser(
            'host.cros', enable_shared_dut_pool=True
        )
        dut_releaser.start()

        self.mock_dut_pool_manager.mark_dut_as_idle.return_value = True

        # func is the function to be called when initializing the timer, which
        # is dut_releaser.run().
        func = self.mock_thread.call_args.args[1]
        func()

        self.assertFalse(dut_releaser.cancel())
        self.mock_dut_pool_manager.mark_dut_as_idle.assert_called_once_with(
            'host'
        )
        self.mock_crosfleet_release_dut.assert_not_called()

    def testReturnToSharedDutPoolFailed(self):
        dut_releaser = dut_manager_module.DelayedDutReleaser(
            'host.cros', enable_shared_dut_pool=True
        )
        dut_releaser.start()

        self.mock_dut_pool_manager.mark_dut_as_idle.return_value = False

        # func is the function to be called when initializing the timer, which
        # is dut_releaser.run().
        func = self.mock_thread.call_args.args[1]
        func()

        self.assertFalse(dut_releaser.cancel())
        # Returns False
        self.mock_dut_pool_manager.mark_dut_as_idle.assert_called_once_with(
            'host'
        )
        # Returned to the ChromeOS lab instead.
        self.mock_crosfleet_release_dut.assert_called_once_with('host')


class TestAddDutLeasesRecord(unittest.TestCase):
    """Test dut lease record is written when leaving DutManager context manager."""

    def setUp(self):
        self.mock_dut_allocate_spec = dut_allocate_spec_module.DutAllocateSpec(
            boards=['test_board']
        )

        self._mock_dut_leases_log_path = self.enterContext(
            mock.patch.object(
                dut_manager_module.DutManager,
                'dut_leases_log_path',
                autospec=True,
            )
        )

        self.mock_states = self.dummy_mock_states()
        self.mock_update_states = self.enterContext(
            mock.patch.object(
                dut_manager_module.DutManager,
                '_update_states',
                autospec=True,
            )
        )
        self.mock_dut_lease_status_monitor = self.enterContext(
            mock.patch.object(
                cros_lab_util, 'dut_lease_status_monitor', autospec=True
            )
        )
        self.mock_allocate_dut = self.enterContext(
            mock.patch.object(dut_allocator, 'allocate_dut', autospec=True)
        )
        self.mock_wait_ssh_avaliable = self.enterContext(
            mock.patch.object(
                dut_manager_module.DutManager,
                '_wait_ssh_avaliable',
                autospec=True,
            )
        )
        self.mock_delayed_dut_releaser_cls = self.enterContext(
            mock.patch.object(
                dut_manager_module, 'DelayedDutReleaser', autospec=True
            )
        )

        # time.time() is called indirectly via other modules, so we can not use
        # mock.patch.object(time, 'time', autospec=True) directly.
        # Instead, we only mock the usage via dut_manager_module.
        self.mock_time_module = self.enterContext(
            mock.patch.object(dut_manager_module, 'time', autospec=True)
        )

    def dummy_mock_states(self):
        mock_states = core.DiagnoseStates('dummy_session_file')
        mock_states.init_states(
            config={
                'session': 'stateless',
                'experiments': [],
                'chromeos_root': 'PATH_TO_CHROMEOS_ROOT',
            },
        )
        return mock_states

    def testWriteToNewFile(self):
        # The file does not exist yet. It only return a path name.
        log_file = tempfile.mktemp()
        self._mock_dut_leases_log_path.return_value = log_file

        self.mock_allocate_dut.return_value = 'host', 'board'
        # start_timestamp
        self.mock_time_module.time.return_value = 1689179900.1

        dut_manager = dut_manager_module.DutManager(
            'NoPreAllocate',
            self.mock_states,
            self.mock_dut_allocate_spec,
            cros_lab_util.LAB_DUT,
            True,
        )
        with dut_manager.provision() as dut:
            self.assertEqual(dut, 'host.cros')

        # Trigger the callback function manually.
        # end_timestamp
        self.mock_delayed_dut_releaser_cls.call_args.args[3](1689179950.1)

        with open(log_file) as f:
            records = json.load(f)
        self.assertEqual(
            records,
            [
                {
                    'name': 'NoPreAllocate',
                    'start_timestamp': 1689179900.1,
                    'end_timestamp': 1689179950.1,
                    'duration_secs': 50,
                }
            ],
        )

        os.unlink(log_file)

    def testAppendToExistingFile(self):
        # The file retains after closed.
        with tempfile.NamedTemporaryFile(mode='w', delete=False) as f:
            json.dump(
                [
                    {
                        'name': 'ExistingRecord',
                        'start_timestamp': 1689179874.5,
                        'end_timestamp': 1689179974.5,
                        'duration_secs': 100,
                    },
                ],
                f,
            )
            log_file = f.name

        self._mock_dut_leases_log_path.return_value = log_file

        self.mock_allocate_dut.return_value = 'host', 'board'
        # start_timestamp
        self.mock_time_module.time.return_value = 1689179900.1

        dut_manager = dut_manager_module.DutManager(
            'NoPreAllocate',
            self.mock_states,
            self.mock_dut_allocate_spec,
            cros_lab_util.LAB_DUT,
            True,
        )
        with dut_manager.provision() as dut:
            self.assertEqual(dut, 'host.cros')

        # Trigger the callback function manually.
        # end_timestamp
        self.mock_delayed_dut_releaser_cls.call_args.args[3](1689179950.1)

        with open(log_file) as f:
            records = json.load(f)
        self.assertEqual(
            records,
            [
                {
                    'name': 'ExistingRecord',
                    'start_timestamp': 1689179874.5,
                    'end_timestamp': 1689179974.5,
                    'duration_secs': 100,
                },
                {
                    'name': 'NoPreAllocate',
                    'start_timestamp': 1689179900.1,
                    'end_timestamp': 1689179950.1,
                    'duration_secs': 50,
                },
            ],
        )

        os.unlink(log_file)


class TestConfigUpdated(unittest.TestCase):
    """Test the states is updated correctly on provision."""

    @contextlib.contextmanager
    def fake_states(self, config):
        """Returns a fake DiagnoseStates() backed by a temporary file."""
        # The temporary file is removed automatically.
        with tempfile.NamedTemporaryFile() as f:
            fake_states = core.DiagnoseStates(f.name)
            fake_states.init_states(config=config)
            yield fake_states

    def setUp(self):
        super().setUp()

        self.mock_save_dut_allocate_spec = self.enterContext(
            mock.patch.object(
                dut_allocate_spec_module,
                'save',
                autospec=True,
            )
        )

        # Mocks relevant to the functionality under testing.
        self.mock_swarming_bot_list = self.enterContext(
            mock.patch.object(
                cros_lab_util,
                'swarming_bots_list',
                autospec=True,
            )
        )
        self.mock_swarming_bot_info = self.enterContext(
            mock.patch.object(
                cros_lab_util,
                'swarming_bot_info',
                autospec=True,
            )
        )
        self.mock_allocate_dut = self.enterContext(
            mock.patch.object(dut_allocator, 'allocate_dut', autospec=True)
        )
        self.mock_wait_ssh_avaliable = self.enterContext(
            mock.patch.object(
                dut_manager_module.DutManager,
                '_wait_ssh_avaliable',
                autospec=True,
            )
        )

        # Supplementary mocks which make provision() work.
        self.mock_dut_lease_status_monitor = self.enterContext(
            mock.patch.object(
                cros_lab_util, 'dut_lease_status_monitor', autospec=True
            )
        )
        self.mock_delayed_dut_releaser_cls = self.enterContext(
            mock.patch.object(
                dut_manager_module, 'DelayedDutReleaser', autospec=True
            )
        )
        self.mock_dut_releaser = self.mock_delayed_dut_releaser_cls.return_value

    def testFunctionalBisect(self):
        self.mock_swarming_bot_list.return_value = [
            {'botId': 'bot_id_1'},
        ]
        self.mock_swarming_bot_info.return_value = {
            'dimensions': {
                'label-model': [
                    'model1',
                    'model2',
                ]
            },
        }
        self.mock_allocate_dut.return_value = 'host', 'board1'

        config = {
            'session': 'stateless',
            'experiments': [],
            'sync_dut_allocate_spec_with_db': False,
        }
        dut_allocate_spec = dut_allocate_spec_module.DutAllocateSpec(
            boards=['board1']
        )
        with self.fake_states(config) as states:
            dut_manager = dut_manager_module.DutManager(
                'NoPreAllocate',
                states,
                dut_allocate_spec,
                cros_lab_util.LAB_DUT,
                True,
            )
            # _update_states() is called in provision().
            with dut_manager.provision():
                pass

            # 'board1' has been cleared.
            expected_dut_allocate_spec = (
                dut_allocate_spec_module.DutAllocateSpec(models=['model1'])
            )
            self.assertEqual(
                dut_manager.dut_allocate_spec, expected_dut_allocate_spec
            )

            expected_config = config | {
                'board': 'board1',
                'allocated_dut': 'host.cros',
            }
            self.assertEqual(states.config, expected_config)

        self.mock_save_dut_allocate_spec.assert_called_once_with(
            dut_allocate_spec,
            sync_with_db=False,
        )

        self.mock_swarming_bot_list.assert_called_once_with(['dut_name:host'])
        self.mock_swarming_bot_info.assert_called_once_with('bot_id_1')

    def testPerfBisect(self):
        self.mock_swarming_bot_list.return_value = [
            {'botId': 'bot_id_1'},
        ]
        self.mock_swarming_bot_info.return_value = {
            'dimensions': {
                'label-hwid_sku': [
                    'sku1',
                    'sku2',
                ]
            },
        }
        self.mock_allocate_dut.return_value = 'host', 'board1'

        config = {
            'session': 'stateless',
            'experiments': [],
            'metric': 'some_metric',
            'sync_dut_allocate_spec_with_db': False,
        }
        dut_allocate_spec = dut_allocate_spec_module.DutAllocateSpec(
            models=['model1']
        )
        with self.fake_states(config) as states:
            dut_manager = dut_manager_module.DutManager(
                'NoPreAllocate',
                states,
                dut_allocate_spec,
                cros_lab_util.LAB_DUT,
                True,
            )
            # _update_states() is called in provision().
            with dut_manager.provision():
                pass

            # 'model1' has been cleared.
            expected_dut_allocate_spec = (
                dut_allocate_spec_module.DutAllocateSpec(skus=['sku1'])
            )
            self.assertEqual(
                dut_manager.dut_allocate_spec, expected_dut_allocate_spec
            )

            expected_config = config | {
                'board': 'board1',
                'allocated_dut': 'host.cros',
            }
            self.assertEqual(states.config, expected_config)

        self.mock_save_dut_allocate_spec.assert_called_once_with(
            dut_allocate_spec,
            sync_with_db=False,
        )

        self.mock_swarming_bot_list.assert_called_once_with(['dut_name:host'])
        self.mock_swarming_bot_info.assert_called_once_with('bot_id_1')

    def testDutNameSpecified(self):
        self.mock_allocate_dut.return_value = 'dut_name', 'board1'

        config = {
            'session': 'stateless',
            'experiments': [],
            'sync_dut_allocate_spec_with_db': False,
        }
        dut_allocate_spec = dut_allocate_spec_module.DutAllocateSpec(
            dut_name='dut_name',
        )
        with self.fake_states(config) as states:
            dut_manager = dut_manager_module.DutManager(
                'NoPreAllocate',
                states,
                dut_allocate_spec,
                cros_lab_util.LAB_DUT,
                True,
            )
            # _update_states() is called in provision().
            with dut_manager.provision():
                pass

            # All fields are untouched since dut_name is present.
            expected_dut_allocate_spec = (
                dut_allocate_spec_module.DutAllocateSpec(dut_name='dut_name')
            )
            self.assertEqual(
                dut_manager.dut_allocate_spec, expected_dut_allocate_spec
            )

            expected_config = config | {
                'board': 'board1',
                'allocated_dut': 'dut_name.cros',
            }
            self.assertEqual(states.config, expected_config)

        self.mock_save_dut_allocate_spec.assert_called_once_with(
            dut_allocate_spec,
            sync_with_db=False,
        )

        self.mock_swarming_bot_list.assert_not_called()
        self.mock_swarming_bot_info.assert_not_called()

    def testNoAutoAllocate(self):
        config = {
            'session': 'stateless',
            'experiments': [],
            'board': 'board1',
            'sync_dut_allocate_spec_with_db': False,
        }
        dut_allocate_spec = dut_allocate_spec_module.DutAllocateSpec(
            boards=['board1']
        )
        with self.fake_states(config) as states:
            dut_manager = dut_manager_module.DutManager(
                'PreAllocate',
                states,
                dut_allocate_spec,
                'pre_allocated_dut',
                True,
            )
            # _update_states() is NOT called in provision().
            with dut_manager.provision():
                pass

            # Verify the configs are left intact.
            self.assertEqual(
                dut_manager.dut_allocate_spec,
                dut_allocate_spec_module.DutAllocateSpec(boards=['board1']),
            )
            self.assertEqual(states.config, config)

        self.mock_swarming_bot_list.assert_not_called()
        self.mock_swarming_bot_info.assert_not_called()
