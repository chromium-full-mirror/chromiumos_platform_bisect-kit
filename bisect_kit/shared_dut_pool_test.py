# Copyright 2024 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Test shared_dut_pool module."""

import logging
import unittest
from unittest import mock

from bisect_kit import bisect_db
from bisect_kit import dut_allocate_spec_type
from bisect_kit import shared_dut_pool
from google.cloud import datastore


logger = logging.getLogger(__name__)


class TestDutMatchesSpec(unittest.TestCase):
    """Test dut_matches_spec()."""

    def setUp(self):
        super().setUp()
        self.maxDiff = None

    def test_dut_matches_dut_name(self):
        dut_name = 'chromeos8-row3-rack10-host39'

        dut = datastore.Entity()
        dut['dimensions'] = {'dut_name': [dut_name]}
        spec = dut_allocate_spec_type.DutAllocateSpec(dut_name=dut_name)
        self.assertTrue(shared_dut_pool.dut_matches_spec(dut, spec))

    def test_dut_not_match_dut_name(self):
        dut_name = 'chromeos8-row3-rack10-host39'

        dut = datastore.Entity()
        dut['dimensions'] = {'dut_name': [dut_name]}
        spec = dut_allocate_spec_type.DutAllocateSpec(dut_name='some_other_dut')
        self.assertFalse(shared_dut_pool.dut_matches_spec(dut, spec))

    def test_dut_matches_pool_board(self):
        dut = datastore.Entity()
        dut['dimensions'] = {
            'label-pool': ['healthmon-satlab'],
            'label-board': ['strongbad'],
        }
        spec = dut_allocate_spec_type.DutAllocateSpec(
            pools=['DUT_POOL_QUOTA', 'healthmon-satlab'],
            boards=['strongbad', 'dedede'],
        )
        self.assertTrue(shared_dut_pool.dut_matches_spec(dut, spec))

    def test_dut_not_match_board(self):
        dut = datastore.Entity()
        dut['dimensions'] = {
            'label-pool': ['healthmon-satlab'],
            'label-board': ['strongbad'],
        }
        spec = dut_allocate_spec_type.DutAllocateSpec(
            pools=['DUT_POOL_QUOTA', 'healthmon-satlab'],
            boards=['dedede'],
        )
        self.assertFalse(shared_dut_pool.dut_matches_spec(dut, spec))

    def test_dut_matches_pool_model(self):
        dut = datastore.Entity()
        dut['dimensions'] = {
            'label-pool': ['healthmon-satlab'],
            'label-model': ['quackingstick'],
        }
        spec = dut_allocate_spec_type.DutAllocateSpec(
            pools=['DUT_POOL_QUOTA', 'healthmon-satlab'],
            models=['quackingstick'],
        )
        self.assertTrue(shared_dut_pool.dut_matches_spec(dut, spec))

    def test_dut_not_match_pool_model(self):
        dut = datastore.Entity()
        dut['dimensions'] = {
            'label-pool': ['healthmon-satlab'],
            'label-model': ['quackingstick'],
        }
        spec = dut_allocate_spec_type.DutAllocateSpec(
            pools=['DUT_POOL_QUOTA', 'healthmon-satlab'],
            models=['coachz', 'homestar'],
        )
        self.assertFalse(shared_dut_pool.dut_matches_spec(dut, spec))

    def test_dut_matches_pool_sku(self):
        dut = datastore.Entity()
        dut['dimensions'] = {
            'label-pool': ['healthmon-satlab'],
            'label-hwid_sku': ['quackingstick_QSIP7180_4GB'],
        }
        spec = dut_allocate_spec_type.DutAllocateSpec(
            pools=['DUT_POOL_QUOTA', 'healthmon-satlab'],
            skus=['quackingstick_QSIP7180_4GB'],
        )
        self.assertTrue(shared_dut_pool.dut_matches_spec(dut, spec))

    def test_dut_matches_pool_model_dimensions(self):
        dut = datastore.Entity()
        dut['dimensions'] = {
            'label-pool': ['healthmon-satlab'],
            'label-model': ['quackingstick'],
            'label-touchscreen': ['True'],
        }
        spec = dut_allocate_spec_type.DutAllocateSpec(
            pools=['DUT_POOL_QUOTA', 'healthmon-satlab'],
            models=['quackingstick'],
            dimensions=['label-touchscreen:True'],
        )
        self.assertTrue(shared_dut_pool.dut_matches_spec(dut, spec))

    def test_dut_not_match_missing_dimensions(self):
        dut = datastore.Entity()
        dut['dimensions'] = {'label-touchscreen': ['True']}
        spec = dut_allocate_spec_type.DutAllocateSpec(
            dimensions=['label-touchscreen:True', 'label-servo:True'],
        )
        self.assertFalse(shared_dut_pool.dut_matches_spec(dut, spec))


class FakeSharedDutPoolManager(shared_dut_pool.SharedDutPoolManager):
    """Inherits SharedDutPoolManager to facilitate unittest."""

    def __init__(self, client):
        # pylint: disable=super-init-not-called
        # It is intentional to mock self._client
        self._client = client


class TestTransferOwner(unittest.TestCase):
    """Test transfer_owner()"""

    def setUp(self):
        super().setUp()
        self._mock_client = self.enterContext(
            mock.patch.object(
                datastore,
                'Client',
                autospec=True,
            )
        )
        self.maxDiff = None

    def testReceiverAlreadyOwnsDuts(self):
        dut = datastore.Entity()
        dut.update(
            {
                'name': 'dut_name_2',
                'owner': 'bisect_2',
            }
        )
        self._mock_client.query.return_value.fetch.return_value = iter([dut])

        pool_manager = FakeSharedDutPoolManager(self._mock_client)
        self.assertFalse(pool_manager.transfer_owner('dut_name_1', 'bisect_2'))

    def testDutNotFound(self):
        self._mock_client.get.return_value = None

        pool_manager = FakeSharedDutPoolManager(self._mock_client)
        self.assertFalse(pool_manager.transfer_owner('dut_name_1', 'bisect_2'))

    def testTransferred(self):
        dut = datastore.Entity()
        dut.update(
            {
                'name': 'dut_name_1',
                'owner': 'bisect_1',
            }
        )
        self._mock_client.get.return_value = dut

        pool_manager = FakeSharedDutPoolManager(self._mock_client)
        self.assertTrue(pool_manager.transfer_owner('dut_name_1', 'bisect_2'))
        self._mock_client.put.assert_called_once_with(
            {
                'name': 'dut_name_1',
                'owner': 'bisect_2',
                'state': shared_dut_pool.DutState.IDLE,
            }
        )


class TestYieldsDuts(unittest.TestCase):
    """Test yield_duts()"""

    def setUp(self):
        super().setUp()
        self._mock_client = self.enterContext(
            mock.patch.object(
                datastore,
                'Client',
                autospec=True,
            )
        )
        self._bisect_db_client_cls = self.enterContext(
            mock.patch.object(
                bisect_db,
                'Client',
                autospec=True,
            )
        )
        self._mock_bisect_db_client = self._bisect_db_client_cls.return_value

        self.maxDiff = None

    @staticmethod
    def _gen_mock_transfer_owner():
        """An bisect_id can only be transferred once."""
        transferred = set()

        def mock_transfer_owner(unused_dut_name, bisect_id) -> bool:
            if bisect_id in transferred:
                return False
            transferred.add(bisect_id)
            return True

        return mock_transfer_owner

    def testAllYielded(self):
        owned_duts = []
        for i in range(3):
            dut = datastore.Entity()
            dut.update(
                {
                    'name': 'dut_name_%s' % str(i + 1),
                    'owner': 'bisect_1',
                    'dimensions': {
                        'label-board': ['board1'],
                    },
                }
            )
            owned_duts.append(dut)
        self._mock_client.query.return_value.fetch.return_value = iter(
            owned_duts
        )

        self._mock_bisect_db_client.dut_alloc_spec_for_unfinished_bisects.return_value = {
            'bisect_id_1': dut_allocate_spec_type.DutAllocateSpec(
                boards=['board1']
            ),
            'bisect_id_2': dut_allocate_spec_type.DutAllocateSpec(
                boards=['board1', 'board2']
            ),
            'bisect_id_3': dut_allocate_spec_type.DutAllocateSpec(
                boards=['board3']
            ),
            'bisect_id_4': dut_allocate_spec_type.DutAllocateSpec(
                boards=['board4', 'board1']
            ),
            'bisect_id_5': dut_allocate_spec_type.DutAllocateSpec(
                boards=['board4']
            ),
        }

        pool_manager = FakeSharedDutPoolManager(self._mock_client)
        with mock.patch.object(
            pool_manager,
            'transfer_owner',
            new_callable=self._gen_mock_transfer_owner,
        ):
            yielded, not_yielded = pool_manager.yield_duts('session')
        self.assertEqual(['dut_name_1', 'dut_name_2', 'dut_name_3'], yielded)
        self.assertEqual([], not_yielded)

    def testPartiallyYielded(self):
        owned_duts = []
        for i in range(3):
            dut = datastore.Entity()
            dut.update(
                {
                    'name': 'dut_name_%s' % str(i + 1),
                    'owner': 'bisect_1',
                    'dimensions': {
                        'label-board': ['board1'],
                    },
                }
            )
            owned_duts.append(dut)
        self._mock_client.query.return_value.fetch.return_value = iter(
            owned_duts
        )

        self._mock_bisect_db_client.dut_alloc_spec_for_unfinished_bisects.return_value = {
            'bisect_id_1': dut_allocate_spec_type.DutAllocateSpec(
                boards=['board1']
            ),
            'bisect_id_2': dut_allocate_spec_type.DutAllocateSpec(
                boards=['board1', 'board2']
            ),
            'bisect_id_3': dut_allocate_spec_type.DutAllocateSpec(
                boards=['board13'],
            ),
        }

        pool_manager = FakeSharedDutPoolManager(self._mock_client)
        with mock.patch.object(
            pool_manager,
            'transfer_owner',
            new_callable=self._gen_mock_transfer_owner,
        ):
            yielded, not_yielded = pool_manager.yield_duts('session')
        self.assertEqual(['dut_name_1', 'dut_name_2'], yielded)
        self.assertEqual(['dut_name_3'], not_yielded)
