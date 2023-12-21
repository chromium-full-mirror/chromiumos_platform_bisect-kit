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
from bisect_kit import dut_allocator
from bisect_kit import dut_manager as dut_manager_module
from bisect_kit import errors


logger = logging.getLogger(__name__)


class DummyException(Exception):
    """A dummy exception class used for testing."""


class TestDutManagerAllocation(unittest.TestCase):
    """Test the dut allocation logic in DutManager."""

    def setUp(self):
        super().setUp()
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
        self.mock_dut_init_func.reset_mock()
        self.mock_dut_leave_context_check_func.reset_mock()
        self.mock_delayed_dut_releaser_cls.reset_mock()
        # By default self.mock_delayed_dut_releaser_cls.return_value is not
        # reset when calling reset_mock(), so self.mock_dut_releaser is still
        # valid.
        self.mock_dut_releaser.reset_mock()

    def dummy_mock_states(self):
        dummy_allocate_spec = core.DutAllocateSpec()
        mock_states = core.DiagnoseStates('dummy_session_file')
        mock_states.init_states(
            config={'session': 'stateless'},
            dut_allocate_spec=dummy_allocate_spec,
        )
        return mock_states

    def test_dut_pre_allocated_should_auto_alloc(self):
        dut_manager = dut_manager_module.DutManager(
            'PreAllocate',
            self.mock_states,
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
        dut_manager = dut_manager_module.DutManager(
            'PreAllocate',
            self.mock_states,
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
        dut_manager = dut_manager_module.DutManager(
            'PreAllocate',
            self.mock_states,
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
            'pre_allocated_dut', cros_lab_util.make_lease_reason('stateless')
        )
        self.mock_delayed_dut_releaser_cls.assert_not_called()
        self.mock_allocate_dut.assert_not_called()
        self.mock_dut_init_func.assert_called_once_with('pre_allocated_dut')
        self.mock_dut_leave_context_check_func.assert_called_once_with(
            'pre_allocated_dut'
        )

    def test_dut_pre_allocated_only_initailize_once(self):
        dut_manager = dut_manager_module.DutManager(
            'PreAllocate',
            self.mock_states,
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
        dut_manager = dut_manager_module.DutManager(
            'PreAllocate',
            self.mock_states,
            'pre_allocated_dut',
            True
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
        dut_manager = dut_manager_module.DutManager(
            'NoPreAllocate',
            self.mock_states,
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
            'host.cros', cros_lab_util.make_lease_reason('stateless')
        )
        self.mock_delayed_dut_releaser_cls.assert_called_once_with(
            'host.cros', mock.ANY
        )
        self.mock_dut_releaser.start.assert_called_once()
        self.mock_allocate_dut.assert_called_once_with(
            dut_manager.dut_allocate_spec
        )
        self.mock_dut_init_func.assert_called_once_with('host.cros')
        self.mock_dut_leave_context_check_func.assert_called_once_with(
            'host.cros'
        )

    def test_no_pre_allocated_dut_should_not_auto_alloc(self):
        dut_manager = dut_manager_module.DutManager(
            'NoPreAllocate',
            self.mock_states,
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
        dut_manager = dut_manager_module.DutManager(
            'NoPreAllocate',
            self.mock_states,
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
            dut_manager.dut_allocate_spec
        )
        self.mock_dut_init_func.assert_not_called()
        self.mock_dut_leave_context_check_func.assert_not_called()

    def test_no_pre_allocated_dut_auto_allocate_no_host_returned(self):
        dut_manager = dut_manager_module.DutManager(
            'NoPreAllocate',
            self.mock_states,
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
            dut_manager.dut_allocate_spec
        )
        self.mock_dut_init_func.assert_not_called()
        self.mock_dut_leave_context_check_func.assert_not_called()

    def test_consecutive_auto_allocate_restored_previous_dut(self):
        dut_manager = dut_manager_module.DutManager(
            'NoPreAllocate',
            self.mock_states,
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
        self.mock_dut_releaser.cancel.return_value = True
        self.mock_dut_releaser.get_dut.return_value = 'host.cros'
        with dut_manager.provision() as dut:
            self.assertEqual(dut, 'host.cros')
            self.assertTrue(dut_manager.is_restored_dut())

        self.mock_update_states.assert_called_once_with(
            dut_manager, 'host.cros'
        )
        self.mock_dut_lease_status_monitor.assert_called_once_with(
            'host.cros', cros_lab_util.make_lease_reason('stateless')
        )
        self.mock_delayed_dut_releaser_cls.assert_called_once_with(
            'host.cros', mock.ANY
        )
        self.mock_dut_releaser.start.assert_called_once()
        self.mock_allocate_dut.assert_not_called()
        self.mock_dut_init_func.assert_not_called()
        self.mock_dut_leave_context_check_func.assert_called_once_with(
            'host.cros'
        )

    def test_consecutive_auto_allocate_lease_new_dut(self):
        dut_manager = dut_manager_module.DutManager(
            'NoPreAllocate',
            self.mock_states,
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
        self.mock_dut_releaser.cancel.return_value = False
        self.mock_allocate_dut.return_value = 'host2', 'board2'
        with dut_manager.provision() as dut:
            self.assertEqual(dut, 'host2.cros')
            self.assertFalse(dut_manager.is_restored_dut())

        self.mock_update_states.assert_called_once_with(
            dut_manager, 'host2.cros'
        )
        self.mock_dut_lease_status_monitor.assert_called_once_with(
            'host2.cros', cros_lab_util.make_lease_reason('stateless')
        )
        self.mock_delayed_dut_releaser_cls.assert_called_once_with(
            'host2.cros', mock.ANY
        )
        self.mock_dut_releaser.start.assert_called_once()
        self.mock_allocate_dut.assert_called_once_with(
            dut_manager.dut_allocate_spec
        )
        self.mock_dut_init_func.assert_called_once_with('host2.cros')
        self.mock_dut_leave_context_check_func.assert_called_once_with(
            'host2.cros'
        )

    def test_auto_release_on_exception(self):
        self.mock_allocate_dut.return_value = 'host', 'board'
        dut_manager = dut_manager_module.DutManager(
            'NoPreAllocate',
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

        self.mock_update_states.assert_called_once_with(
            dut_manager, 'host.cros'
        )
        self.mock_dut_lease_status_monitor.assert_called_once_with(
            'host.cros', cros_lab_util.make_lease_reason('stateless')
        )
        self.mock_delayed_dut_releaser_cls.assert_called_once_with(
            'host.cros', mock.ANY
        )
        self.mock_dut_releaser.start.assert_called_once()
        self.mock_allocate_dut.assert_called_once_with(
            dut_manager.dut_allocate_spec
        )
        self.mock_dut_init_func.assert_called_once_with('host.cros')
        self.mock_dut_leave_context_check_func.assert_called_once_with(
            'host.cros'
        )

    def test_dut_leave_context_check_func_failed(self):
        self.mock_allocate_dut.return_value = 'host', 'board'
        dut_manager = dut_manager_module.DutManager(
            'NoPreAllocate',
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

        self.mock_update_states.assert_called_once_with(
            dut_manager, 'host.cros'
        )
        self.mock_dut_lease_status_monitor.assert_called_once_with(
            'host.cros', cros_lab_util.make_lease_reason('stateless')
        )
        self.mock_delayed_dut_releaser_cls.assert_called_once_with(
            'host.cros', mock.ANY
        )
        self.mock_dut_releaser.start.assert_called_once()
        self.mock_allocate_dut.assert_called_once_with(
            dut_manager.dut_allocate_spec
        )
        self.mock_dut_init_func.assert_called_once_with('host.cros')
        self.mock_dut_leave_context_check_func.assert_called_once_with(
            'host.cros'
        )

    def test_dut(self):
        dut_manager = dut_manager_module.DutManager(
            'NoPreAllocate', self.mock_states, cros_lab_util.LAB_DUT, True
        )
        self.assertEqual(dut_manager.dut, None)

        dut_manager = dut_manager_module.DutManager(
            'PreAllocate', self.mock_states, 'pre_allocated_dut', True
        )
        self.assertEqual(dut_manager.dut, 'pre_allocated_dut')

        dut_manager = dut_manager_module.DutManager(
            'NoPreAllocate',
            self.mock_states,
            cros_lab_util.LAB_DUT,
            True,
        )
        self.assertEqual(dut_manager.dut, None)
        self.mock_allocate_dut.return_value = 'host', 'board'
        with dut_manager.provision():
            self.assertEqual(dut_manager.dut, 'host.cros')
        self.assertEqual(dut_manager.dut, None)


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


class TestAddDutLeasesRecord(unittest.TestCase):
    """Test dut lease record is written when leaving DutManager context manager."""

    def setUp(self):
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
        dummy_allocate_spec = core.DutAllocateSpec()
        mock_states = core.DiagnoseStates('dummy_session_file')
        mock_states.init_states(
            config={'session': 'stateless'},
            dut_allocate_spec=dummy_allocate_spec,
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
            cros_lab_util.LAB_DUT,
            True,
        )
        with dut_manager.provision() as dut:
            self.assertEqual(dut, 'host.cros')

        # Trigger the callback function manually.
        # end_timestamp
        self.mock_delayed_dut_releaser_cls.call_args.args[1](1689179950.1)

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
            cros_lab_util.LAB_DUT,
            True,
        )
        with dut_manager.provision() as dut:
            self.assertEqual(dut, 'host.cros')

        # Trigger the callback function manually.
        # end_timestamp
        self.mock_delayed_dut_releaser_cls.call_args.args[1](1689179950.1)

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
    def fake_states(self, config, dut_allocate_spec):
        """Returns a fake DiagnoseStates() backed by a temporary file."""
        # The temporary file is removed automatically.
        with tempfile.NamedTemporaryFile() as f:
            fake_states = core.DiagnoseStates(f.name)
            fake_states.init_states(
                config=config,
                dut_allocate_spec=dut_allocate_spec,
            )
            yield fake_states

    def setUp(self):
        super().setUp()
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
        }
        dut_allocate_spec = core.DutAllocateSpec(board='board1')
        with self.fake_states(config, dut_allocate_spec) as states:
            dut_manager = dut_manager_module.DutManager(
                'NoPreAllocate',
                states,
                cros_lab_util.LAB_DUT,
                True,
            )
            # _update_states() is called in provision().
            with dut_manager.provision():
                pass

            # 'board1' has been cleared.
            expected_dut_allocate_spec = core.DutAllocateSpec(model='model1')
            expected_config = config | {
                'board': 'board1',
                'allocated_dut': 'host.cros',
            }
            self.assertEqual(
                states.dut_allocate_spec, expected_dut_allocate_spec
            )
            self.assertEqual(states.config, expected_config)

            # Verify the states is saved to file.
            loaded_states = core.DiagnoseStates(states.session_file)
            loaded_states.load_states()
            self.assertEqual(
                loaded_states.dut_allocate_spec, expected_dut_allocate_spec
            )
            self.assertEqual(loaded_states.config, expected_config)

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
            'metric': 'some_metric',
        }
        dut_allocate_spec = core.DutAllocateSpec(model='model1')
        with self.fake_states(config, dut_allocate_spec) as states:
            dut_manager = dut_manager_module.DutManager(
                'NoPreAllocate',
                states,
                cros_lab_util.LAB_DUT,
                True,
            )
            # _update_states() is called in provision().
            with dut_manager.provision():
                pass

            # 'model1' has been cleared.
            expected_dut_allocate_spec = core.DutAllocateSpec(sku='sku1')
            expected_config = config | {
                'board': 'board1',
                'allocated_dut': 'host.cros',
            }
            self.assertEqual(
                states.dut_allocate_spec, expected_dut_allocate_spec
            )
            self.assertEqual(states.config, expected_config)

            # Verify the states is saved to file.
            loaded_states = core.DiagnoseStates(states.session_file)
            loaded_states.load_states()
            self.assertEqual(
                loaded_states.dut_allocate_spec, expected_dut_allocate_spec
            )
            self.assertEqual(loaded_states.config, expected_config)

        self.mock_swarming_bot_list.assert_called_once_with(['dut_name:host'])
        self.mock_swarming_bot_info.assert_called_once_with('bot_id_1')

    def testNoAutoAllocate(self):
        config = {
            'session': 'stateless',
            'board': 'board1',
        }
        dut_allocate_spec = core.DutAllocateSpec(model='model1')
        with self.fake_states(config, dut_allocate_spec) as states:
            dut_manager = dut_manager_module.DutManager(
                'PreAllocate',
                states,
                'pre_allocated_dut',
                True,
            )
            # _update_states() is NOT called in provision().
            with dut_manager.provision():
                pass

            # Verify the configs are left intact.
            self.assertEqual(states.dut_allocate_spec, dut_allocate_spec)
            self.assertEqual(states.config, config)

        self.mock_swarming_bot_list.assert_not_called()
        self.mock_swarming_bot_info.assert_not_called()
