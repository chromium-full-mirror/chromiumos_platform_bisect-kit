# Copyright 2018 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Test cros_lab_util module."""

from __future__ import annotations

import queue
import subprocess
import threading
import time
import unittest
from unittest import mock

from bisect_kit import common
from bisect_kit import cros_lab_util
from bisect_kit import cros_util
from bisect_kit import errors
from bisect_kit import util


class TestCrosLab(unittest.TestCase):
    """Test functions in cros_lab_util module."""

    def test_normalize_sku_name(self):
        self.assertEqual(
            cros_lab_util.normalize_sku_name('foo_16GB'), 'foo_16GB'
        )
        self.assertEqual(
            cros_lab_util.normalize_sku_name('foo_16Gb'), 'foo_16GB'
        )
        self.assertEqual(
            cros_lab_util.normalize_sku_name('something_else'), 'something_else'
        )

    def test_is_lab_dut(self):
        self.assertEqual(cros_lab_util.is_lab_dut('abc'), False)
        self.assertEqual(cros_lab_util.is_lab_dut('abc.cros'), True)
        self.assertEqual(cros_lab_util.is_lab_dut('chromeos8-foo-host1'), True)
        self.assertEqual(
            cros_lab_util.is_lab_dut('chromeos8-foo-labstation1'), True
        )
        self.assertEqual(cros_lab_util.is_lab_dut('cre123'), True)
        self.assertEqual(
            cros_lab_util.is_lab_dut('satlab-dummy'),
            True,
        )

    def test_dut_host_name(self):
        with self.assertRaises(AssertionError):
            cros_lab_util.dut_host_name('abc')
        self.assertEqual(cros_lab_util.dut_host_name('abc.cros'), 'abc')
        self.assertEqual(
            cros_lab_util.dut_host_name('chromeos8-foo-host1'),
            'chromeos8-foo-host1',
        )
        self.assertEqual(
            cros_lab_util.dut_host_name('chromeos8-foo-labstation1'),
            'chromeos8-foo-labstation1',
        )
        self.assertEqual(cros_lab_util.dut_host_name('cre123'), 'cre123')

    def test_dut_name_to_address(self):
        with self.assertRaises(AssertionError):
            cros_lab_util.dut_name_to_address('abc.cros')
        self.assertEqual(cros_lab_util.dut_name_to_address('abc'), 'abc.cros')
        self.assertEqual(
            cros_lab_util.dut_name_to_address('chromeos8-foo-host1'),
            'chromeos8-foo-host1',
        )
        self.assertEqual(
            cros_lab_util.dut_name_to_address('chromeos8-foo-labstation1'),
            'chromeos8-foo-labstation1',
        )
        self.assertEqual(cros_lab_util.dut_name_to_address('cre123'), 'cre123')

    def test_convert_to_known_satlab_ip(self):
        self.assertIsNotNone(
            cros_lab_util.convert_to_known_satlab_ip('satlab_prefix-dummy')
        )

        self.assertEqual(
            cros_lab_util.convert_to_known_satlab_ip('satlab-unknown'),
            None,
        )

    @mock.patch('bisect_kit.cros_lab_util.crosfleet_cmd')
    def test_crosfleet_dut_leases(self, mock_cmd):
        mock_cmd.return_value = '''
DUT_HOSTNAME=chromeos8-row13-rack17-host2
MODEL=willow
BOARD=jacuzzi
SERVO_HOSTNAME=chromeos8-row13-rack17-labstation1
SERVO_PORT=9998
SERVO_SERIAL=SERVOV4P1-S-2204211229
MINS_REMAINING=1438.64
LEASE_ID=73967319

DUT_HOSTNAME=chromeos8-row13-rack17-host3
MODEL=willow
BOARD=jacuzzi
SERVO_HOSTNAME=chromeos8-row13-rack17-labstation2
SERVO_PORT=9998
SERVO_SERIAL=SERVOV4P1-S-2204211230
MINS_REMAINING=1438.64
LEASE_ID=73967320
'''
        info = cros_lab_util.crosfleet_dut_leases()
        self.assertEqual(len(info), 2)
        self.assertIn('chromeos8-row13-rack17-host2', info)
        self.assertIn('chromeos8-row13-rack17-host3', info)
        self.assertEqual(
            info['chromeos8-row13-rack17-host2']['dut_hostname'],
            'chromeos8-row13-rack17-host2',
        )
        self.assertEqual(
            info['chromeos8-row13-rack17-host2']['lease_id'], '73967319'
        )
        self.assertEqual(
            info['chromeos8-row13-rack17-host3']['lease_id'], '73967320'
        )

    def test_extract_lease_info(self):
        info = cros_lab_util.extract_lease_info(
            'Internal Scheduke lase ID (can be used for cancellation): 1234567'
        )
        self.assertIsNotNone(info)
        self.assertEqual(info['scheduke_lease_id'], '1234567')

    def test_extract_lease_property(self):
        key, value = cros_lab_util.extract_lease_property(
            'DUT_HOSTNAME=a1-b2-c3-d4'
        )
        self.assertEqual(key, 'DUT_HOSTNAME')
        self.assertEqual(value, 'a1-b2-c3-d4')

        key, value = cros_lab_util.extract_lease_property('BOARD=foo')
        self.assertEqual(key, 'BOARD')
        self.assertEqual(value, 'foo')

        key, value = cros_lab_util.extract_lease_property(
            'Not a lease property line.'
        )
        self.assertIsNone(key)
        self.assertIsNone(value)

    def test_enumerate_dimension_combinations(self):
        self.assertEqual(
            cros_lab_util.enumerate_dimension_combinations(
                ['dut-state:ready'],
                ['label-board:foo', 'label-board:bar'],
                None,
            ),
            [
                ['dut-state:ready', 'label-board:foo'],
                ['dut-state:ready', 'label-board:bar'],
            ],
        )
        self.assertEqual(
            cros_lab_util.enumerate_dimension_combinations(
                ['dut-state:ready'],
                ['label-board:foo', 'label-board:bar'],
                ['label-pool:DUT_POOL_QUOTA', 'label-pool:healthmon-satlab'],
            ),
            [
                [
                    'dut-state:ready',
                    'label-board:foo',
                    'label-pool:DUT_POOL_QUOTA',
                ],
                [
                    'dut-state:ready',
                    'label-board:bar',
                    'label-pool:DUT_POOL_QUOTA',
                ],
                [
                    'dut-state:ready',
                    'label-board:foo',
                    'label-pool:healthmon-satlab',
                ],
                [
                    'dut-state:ready',
                    'label-board:bar',
                    'label-pool:healthmon-satlab',
                ],
            ],
        )

    def test_is_rootfs_verification_on(self):
        with mock.patch.object(util, 'ssh_cmd', return_value='/dev/dm-0'):
            self.assertTrue(
                cros_lab_util.is_rootfs_verification_on('dummy_DUT')
            )

        with mock.patch.object(util, 'ssh_cmd', return_value='/dev/nvme0n1p3'):
            self.assertFalse(
                cros_lab_util.is_rootfs_verification_on('dummy_DUT')
            )

        with self.assertRaises(errors.SshConnectionError):
            with mock.patch.object(
                util, 'ssh_cmd', side_effect=errors.SshConnectionError
            ):
                cros_lab_util.is_rootfs_verification_on('dummy_DUT')

        with self.assertRaises(subprocess.CalledProcessError):
            with mock.patch.object(
                util,
                'ssh_cmd',
                side_effect=subprocess.CalledProcessError(255, 'ssh_cmd'),
            ):
                cros_lab_util.is_rootfs_verification_on('dummy_DUT')

    def test_enable_rootfs_verification(self):
        with mock.patch.object(
            cros_lab_util, 'is_rootfs_verification_on', return_value=True
        ):
            self.assertTrue(
                cros_lab_util.enable_rootfs_verification(
                    'dummy_DUT', common.get_default_chromeos_root()
                )
            )

        with mock.patch.object(
            util, 'ssh_cmd', side_effect=errors.SshConnectionError
        ):
            self.assertFalse(
                cros_lab_util.enable_rootfs_verification(
                    'dummy_DUT', common.get_default_chromeos_root()
                )
            )

        with mock.patch.object(
            cros_lab_util, 'is_rootfs_verification_on', return_value=True
        ), mock.patch.object(
            cros_util, 'query_dut_short_version', side_effect='short_version'
        ), mock.patch.object(
            cros_util, 'query_dut_board', side_effect='board'
        ), mock.patch.object(
            cros_util, 'version_to_full', side_effect='full_version'
        ), mock.patch.object(
            cros_util, 'search_image', side_effect=cros_util.ImageInfo()
        ), mock.patch.object(
            cros_util, 'provision_image', side_effect=[]
        ):
            self.assertTrue(
                cros_lab_util.enable_rootfs_verification(
                    'dummy_DUT', common.get_default_chromeos_root()
                )
            )


class LeaseKeeperTest(unittest.TestCase):
    """Test lease_keeper function."""

    def setUp(self):
        self.mock_query_lease_status = self.enterContext(
            mock.patch.object(cros_lab_util, 'query_lease_status')
        )
        self.mock_extend_lease = self.enterContext(
            mock.patch.object(cros_lab_util, '_extend_lease')
        )
        self.mock_time = self.enterContext(mock.patch.object(time, 'time'))
        self.mock_notify_error = self.enterContext(
            mock.patch.object(cros_lab_util, '_notify_error_from_lease_keeper')
        )
        self.quit_event = threading.Event()
        self.error_queue: queue.Queue[Exception] = queue.Queue()
        self.session_id = 'test_session_id'

    def test_quit_event(self):
        self.mock_time.return_value = 1000000000
        self.mock_query_lease_status.return_value = cros_lab_util.LeaseStatus(
            dut_name='dut1',
            is_leased=True,
            end_time=1000000101,
        )

        thread = threading.Thread(
            target=cros_lab_util.lease_keeper,
            args=(
                'dut1',
                'reason',
                self.quit_event,
                self.error_queue,
                self.session_id,
            ),
        )
        thread.start()

        # Let the thread run for a bit
        time.sleep(0.1)

        self.quit_event.set()
        thread.join(timeout=2)

        self.assertFalse(thread.is_alive())
        self.assertTrue(self.error_queue.empty())

    def test_lease_lost(self):
        self.mock_time.return_value = 1000000000
        self.mock_query_lease_status.side_effect = [
            cros_lab_util.LeaseStatus(
                dut_name='dut1',
                is_leased=True,
                end_time=1000001000,
                task_id='task1',
            ),
            cros_lab_util.LeaseStatus(dut_name='dut1', is_leased=False),
        ]
        self.quit_event.wait = mock.Mock()  # type: ignore

        cros_lab_util.lease_keeper(
            'dut1', 'reason', self.quit_event, self.error_queue, self.session_id
        )

        self.mock_notify_error.assert_called_once()
        self.assertIsInstance(
            self.mock_notify_error.call_args[0][0], errors.DutLeaseException
        )

    def test_lease_extension(self):
        self.mock_time.return_value = 1000000000
        self.mock_query_lease_status.side_effect = [
            cros_lab_util.LeaseStatus(
                dut_name='dut1',
                is_leased=True,
                end_time=1000000050,
                lease_id_in_dls='lease1',
            ),
            cros_lab_util.LeaseStatus(
                dut_name='dut1',
                is_leased=True,
                end_time=1000000000 + 3600,  # extended
                lease_id_in_dls='lease2',
            ),
        ]
        self.mock_extend_lease.return_value = {'leaseId': 'lease2'}

        # Let the loop run twice and then quit
        wait_count = 0

        def wait_and_quit(_t):
            nonlocal wait_count
            wait_count += 1
            if wait_count >= 2:
                self.quit_event.set()

        self.quit_event.wait = wait_and_quit  # type: ignore

        cros_lab_util.lease_keeper(
            'dut1', 'reason', self.quit_event, self.error_queue, self.session_id
        )

        self.mock_extend_lease.assert_called_once_with(
            'lease1', self.session_id
        )
        self.assertTrue(self.error_queue.empty())


class QueryLeaseStatusTest(unittest.TestCase):
    """Test query_lease_status function."""

    SAMPLE_GET_TASK_STATES_FROM_DEVICE_LEASE_SERVICE_RESPONSE: dict[
        str, str | dict[str, str]
    ] = {
        'id': 'lease1',
        'deviceId': 'chromeos8-row10-rack9-host2',
        'dutId': 'C298832',
        'deviceAddress': {},
        'deviceType': 'DEVICE_TYPE_PHYSICAL',
        'leasedTime': '2025-10-06T04:33:30.677219Z',
        'releasedTime': '2025-10-07T06:35:01.713224Z',
        'expirationTime': '2025-10-07T10:00:00Z',
        'lastUpdatedTime': '2025-10-07T06:35:01.713224Z',
        'userPayload': {
            'cros_bisect': 'https://crosperf.googleplex.com/test_session_id'
        },
    }

    def setUp(self):
        self.mock_get_task_states_from_device_lease_service = self.enterContext(
            mock.patch.object(
                cros_lab_util, '_get_task_states_from_device_lease_service'
            )
        )
        self.mock_get_task_states_from_frontdoor = self.enterContext(
            mock.patch.object(
                cros_lab_util.scheduke_util, 'get_task_states_from_frontdoor'
            )
        )
        self.mock_get_active_gcloud_user = self.enterContext(
            mock.patch.object(
                cros_lab_util.scheduke_util, 'get_active_gcloud_user'
            )
        )

    def test_not_leased(self):
        self.mock_get_task_states_from_device_lease_service.return_value = None
        self.mock_get_task_states_from_frontdoor.return_value = mock.Mock(
            tasks=[]
        )

        status = cros_lab_util.query_lease_status('dut1')
        self.assertFalse(status.is_leased)

    def test_leased_by_me_from_dls_and_exipired_on_scheduke(self):
        """DUT was expired on Scheduke but is extended on DLS."""
        self.mock_get_task_states_from_device_lease_service.return_value = {
            **self.SAMPLE_GET_TASK_STATES_FROM_DEVICE_LEASE_SERVICE_RESPONSE,
            # Overriding to simulate an on-going lease state in DLS record.
            'releasedTime': '0001-01-01T00:00:00Z',
        }
        self.mock_get_task_states_from_frontdoor.return_value = mock.Mock(
            tasks=[]
        )

        status = cros_lab_util.query_lease_status('dut1')

        self.assertTrue(status.is_leased)
        self.assertEqual(status.lease_id_in_dls, 'lease1')

    def test_leased_by_me_from_dls(self):
        """DUT isn't expired on Scheduke yet and is extended on DLS."""
        self.mock_get_task_states_from_device_lease_service.return_value = {
            **self.SAMPLE_GET_TASK_STATES_FROM_DEVICE_LEASE_SERVICE_RESPONSE,
            # Overriding to simulate an on-going lease state in DLS record.
            'releasedTime': '0001-01-01T00:00:00Z',
        }
        self.mock_get_task_states_from_frontdoor.return_value = mock.Mock(
            tasks=[]
        )

        status = cros_lab_util.query_lease_status('dut1')

        self.assertTrue(status.is_leased)
        self.assertEqual(status.lease_id_in_dls, 'lease1')

    def test_leased_by_me_from_scheduke(self):
        """DUT isn't expired on Scheduke yet and is not extended on DLS."""
        self.mock_get_task_states_from_device_lease_service.return_value = {
            **self.SAMPLE_GET_TASK_STATES_FROM_DEVICE_LEASE_SERVICE_RESPONSE,
            # Overriding to simulate an on-going lease state in DLS record.
            'releasedTime': '0001-01-01T00:00:00Z',
        }
        self.mock_get_task_states_from_frontdoor.return_value = mock.Mock(
            tasks=[
                mock.Mock(
                    task_state_id='task1',
                    end_time=1728300000000000,  # 2025-10-07 10:00:00
                )
            ]
        )
        self.mock_get_active_gcloud_user.return_value = 'test_user'

        status = cros_lab_util.query_lease_status('dut1')

        self.assertTrue(status.is_leased)

    def test_leased_by_other(self):
        response = (
            self.SAMPLE_GET_TASK_STATES_FROM_DEVICE_LEASE_SERVICE_RESPONSE
        )
        response['userPayload'].pop('cros_bisect')
        self.mock_get_task_states_from_device_lease_service.return_value = (
            response
        )
        self.mock_get_task_states_from_frontdoor.return_value = mock.Mock(
            tasks=[]
        )

        status = cros_lab_util.query_lease_status('dut1')

        self.assertFalse(status.is_leased)


if __name__ == '__main__':
    unittest.main()
