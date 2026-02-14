# Copyright 2024 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Test shared_dut_pool module."""

from __future__ import annotations

import logging
import typing
import unittest
from unittest import mock

from bisect_kit import bisect_db
from bisect_kit import cros_lab_util
from bisect_kit import dut_allocate_spec_type
from bisect_kit import errors
from bisect_kit import shared_dut_pool
from google.cloud import datastore
import google.cloud.exceptions


logger = logging.getLogger(__name__)


class DutData(typing.TypedDict):
    """A dict to represent a DUT entity in the shared pool."""

    name: str
    owner: str
    dimensions: dict[str, list[str]]
    state: shared_dut_pool.DutState


class TestDutMatchesSpec(unittest.TestCase):
    """Test dut_matches_spec()."""

    def setUp(self):
        super().setUp()
        self.maxDiff = None

    def test_dut_matches_dut_name(self):
        dut_name = 'chromeos8-row3-rack10-host39'

        dimensions = {'dut_name': [dut_name]}
        spec = dut_allocate_spec_type.DutAllocateSpec(dut_name=dut_name)
        self.assertTrue(shared_dut_pool.dut_matches_spec(dimensions, spec))

    def test_dut_not_match_dut_name(self):
        dut_name = 'chromeos8-row3-rack10-host39'

        dimensions = {'dut_name': [dut_name]}
        spec = dut_allocate_spec_type.DutAllocateSpec(dut_name='some_other_dut')
        self.assertFalse(shared_dut_pool.dut_matches_spec(dimensions, spec))

    def test_dut_matches_pool_board(self):
        dimensions = {
            'label-pool': ['healthmon-satlab'],
            'label-board': ['strongbad'],
        }
        spec = dut_allocate_spec_type.DutAllocateSpec(
            pools=['DUT_POOL_QUOTA', 'healthmon-satlab'],
            boards=['strongbad', 'dedede'],
        )
        self.assertTrue(shared_dut_pool.dut_matches_spec(dimensions, spec))

    def test_dut_not_match_board(self):
        dimensions = {
            'label-pool': ['healthmon-satlab'],
            'label-board': ['strongbad'],
        }
        spec = dut_allocate_spec_type.DutAllocateSpec(
            pools=['DUT_POOL_QUOTA', 'healthmon-satlab'],
            boards=['dedede'],
        )
        self.assertFalse(shared_dut_pool.dut_matches_spec(dimensions, spec))

    def test_dut_matches_pool_model(self):
        dimensions = {
            'label-pool': ['healthmon-satlab'],
            'label-model': ['quackingstick'],
        }
        spec = dut_allocate_spec_type.DutAllocateSpec(
            pools=['DUT_POOL_QUOTA', 'healthmon-satlab'],
            models=['quackingstick'],
        )
        self.assertTrue(shared_dut_pool.dut_matches_spec(dimensions, spec))

    def test_dut_not_match_pool_model(self):
        dimensions = {
            'label-pool': ['healthmon-satlab'],
            'label-model': ['quackingstick'],
        }
        spec = dut_allocate_spec_type.DutAllocateSpec(
            pools=['DUT_POOL_QUOTA', 'healthmon-satlab'],
            models=['coachz', 'homestar'],
        )
        self.assertFalse(shared_dut_pool.dut_matches_spec(dimensions, spec))

    def test_dut_matches_pool_sku(self):
        dimensions = {
            'label-pool': ['healthmon-satlab'],
            'label-hwid_sku': ['quackingstick_QSIP7180_4GB'],
        }
        spec = dut_allocate_spec_type.DutAllocateSpec(
            pools=['DUT_POOL_QUOTA', 'healthmon-satlab'],
            skus=['quackingstick_QSIP7180_4GB'],
        )
        self.assertTrue(shared_dut_pool.dut_matches_spec(dimensions, spec))

    def test_dut_matches_pool_model_dimensions(self):
        dimensions = {
            'label-pool': ['healthmon-satlab'],
            'label-model': ['quackingstick'],
            'label-touchscreen': ['True'],
        }
        spec = dut_allocate_spec_type.DutAllocateSpec(
            pools=['DUT_POOL_QUOTA', 'healthmon-satlab'],
            models=['quackingstick'],
            dimensions=['label-touchscreen:True'],
        )
        self.assertTrue(shared_dut_pool.dut_matches_spec(dimensions, spec))

    def test_dut_not_match_missing_dimensions(self):
        dimensions = {'label-touchscreen': ['True']}
        spec = dut_allocate_spec_type.DutAllocateSpec(
            dimensions=['label-touchscreen:True', 'label-servo:True'],
        )
        self.assertFalse(shared_dut_pool.dut_matches_spec(dimensions, spec))


class TestSuccessorFinder(unittest.TestCase):
    """Tests SuccessorFinder."""

    def setUp(self):
        super().setUp()
        self._mock_bisect_db_client_cls = self.enterContext(
            mock.patch.object(
                bisect_db,
                'Client',
                autospec=True,
            )
        )
        self._mock_specs = (
            self._mock_bisect_db_client_cls.return_value.dut_alloc_spec_for_unfinished_bisects
        )

        self._mock_specs.return_value = {
            'bisect_1': dut_allocate_spec_type.DutAllocateSpec(
                pools=['DUT_POOL_QUOTA', 'healthmon-satlab'],
                boards=['strongbad', 'dedede'],
            ),
            'bisect_2': dut_allocate_spec_type.DutAllocateSpec(
                pools=['DUT_POOL_QUOTA', 'healthmon-satlab'],
                models=['coachz', 'homestar'],
            ),
            'bisect_3': dut_allocate_spec_type.DutAllocateSpec(
                pools=['DUT_POOL_QUOTA'],
                boards=['strongbad'],
            ),
            'bisect_4': dut_allocate_spec_type.DutAllocateSpec(
                pools=['DUT_POOL_QUOTA', 'healthmon-satlab'],
                models=['coachz', 'homestar'],
                dimensions=['label-touchscreen:True'],
            ),
        }

    def test_matched(self):
        finder = shared_dut_pool.SuccessorFinder('bisect_5')
        matched = finder.match(
            {
                'label-pool': ['DUT_POOL_QUOTA'],
                'label-board': ['strongbad'],
            }
        )
        self.assertEqual(matched, ['bisect_1', 'bisect_3'])

        matched = finder.match(
            {
                'label-pool': ['healthmon-satlab'],
                'label-model': ['homestar'],
                'label-touchscreen': ['True'],
            }
        )
        self.assertEqual(matched, ['bisect_2', 'bisect_4'])

    def test_unmatched(self):
        finder = shared_dut_pool.SuccessorFinder('bisect_5')
        matched = finder.match(
            {
                'label-pool': ['DUT_POOL_QUOTA'],
                'label-model': ['quackingstick'],
            }
        )
        self.assertEqual(matched, [])

    def test_exclude_itself(self):
        finder = shared_dut_pool.SuccessorFinder('bisect_1')
        matched = finder.match(
            {
                'label-pool': ['DUT_POOL_QUOTA'],
                'label-board': ['strongbad'],
            }
        )
        self.assertEqual(matched, ['bisect_3'])

        matched = finder.match(
            {
                'label-pool': ['healthmon-satlab'],
                'label-model': ['homestar'],
                'label-touchscreen': ['True'],
            }
        )
        self.assertEqual(matched, ['bisect_2', 'bisect_4'])


class TestFilterDimensions(unittest.TestCase):
    """Tests SuccessorFinder."""

    def test_filtered(self):
        dimensions = [
            'label-touchscreen:True',
            'dut_state:ready',
            'label-servo:True',
        ]
        to_filter = ['dut_state']
        self.assertEqual(
            ['label-touchscreen:True', 'label-servo:True'],
            shared_dut_pool.filter_dimensions(dimensions, to_filter),
        )

    def test_nothing_to_filter(self):
        dimensions = [
            'label-touchscreen:True',
            'dut_state:ready',
            'label-servo:True',
        ]
        to_filter: list[str] = []
        self.assertEqual(
            ['label-touchscreen:True', 'dut_state:ready', 'label-servo:True'],
            shared_dut_pool.filter_dimensions(dimensions, to_filter),
        )

    def test_all_filtered(self):
        dimensions = [
            'label-touchscreen:True',
            'dut_state:ready',
            'label-servo:True',
        ]
        to_filter = ['label-touchscreen', 'dut_state', 'label-servo']
        self.assertEqual(
            [], shared_dut_pool.filter_dimensions(dimensions, to_filter)
        )


class FakeSharedDutPoolManager(shared_dut_pool.SharedDutPoolManager):
    """Inherits SharedDutPoolManager to facilitate unittest."""

    def __init__(self, client):
        # pylint: disable=super-init-not-called
        # It is intentional to mock self._client
        self._client = client


class TestInsertDut(unittest.TestCase):
    """Test insert_dut()"""

    def setUp(self):
        super().setUp()
        self._mock_client = self.enterContext(
            mock.patch.object(
                datastore,
                'Client',
                autospec=True,
            )
        )
        self._mock_client.get.return_value = None

        self._mock_bisect_db_client_cls = self.enterContext(
            mock.patch.object(
                bisect_db,
                'Client',
                autospec=True,
            )
        )
        self._mock_specs = (
            self._mock_bisect_db_client_cls.return_value.dut_alloc_spec_for_unfinished_bisects
        )

        self.maxDiff = None

    def test_already_owned_duts_found_successor(self):
        pool_manager = FakeSharedDutPoolManager(self._mock_client)
        owned_duts = ['dut_name_%s' % (i + 1) for i in range(3)]
        dimensions = {
            'label-pool': ['healthmon-satlab'],
            'label-model': ['quackingstick'],
        }
        self._mock_specs.return_value = {
            'bisect_1': dut_allocate_spec_type.DutAllocateSpec(
                pools=['DUT_POOL_QUOTA', 'healthmon-satlab'],
                boards=['strongbad', 'dedede'],
            ),
            # bisect_2 matches |dimensions|.
            'bisect_2': dut_allocate_spec_type.DutAllocateSpec(
                pools=['DUT_POOL_QUOTA', 'healthmon-satlab'],
                models=['quackingstick'],
            ),
            'bisect_3': dut_allocate_spec_type.DutAllocateSpec(
                pools=['DUT_POOL_QUOTA', 'healthmon-satlab'],
                models=['homestar'],
            ),
        }
        self._mock_client.entity.return_value = datastore.Entity()
        with mock.patch.object(
            pool_manager, 'query_by_owner', autospec=True
        ) as mock_query:
            mock_query.return_value = owned_duts
            self.assertEqual(
                'bisect_2',
                pool_manager.insert_dut('dut_name_4', 'bisect_4', dimensions),
            )

        self._mock_client.put.assert_called_once()
        updated_dut = self._mock_client.put.call_args.args[0]
        self.assertEqual('dut_name_4', updated_dut['name'])
        # Transferred to bisect_2.
        self.assertEqual('bisect_2', updated_dut['owner'])
        self.assertEqual(shared_dut_pool.DutState.IDLE, updated_dut['state'])
        self.assertEqual(dimensions, updated_dut['dimensions'])

    def test_already_owned_duts_no_successor(self):
        pool_manager = FakeSharedDutPoolManager(self._mock_client)
        owned_duts = ['dut_name_%s' % (i + 1) for i in range(3)]
        dimensions = {
            'label-pool': ['healthmon-satlab'],
            'label-model': ['quackingstick'],
        }
        self._mock_specs.return_value = {
            'bisect_1': dut_allocate_spec_type.DutAllocateSpec(
                pools=['DUT_POOL_QUOTA', 'healthmon-satlab'],
                boards=['strongbad', 'dedede'],
            ),
            'bisect_2': dut_allocate_spec_type.DutAllocateSpec(
                pools=['DUT_POOL_QUOTA', 'healthmon-satlab'],
                skus=['quackingstick_QSIP7180_4GB'],
            ),
            # The bisect itself, we can't yield to ourselves.
            'bisect_3': dut_allocate_spec_type.DutAllocateSpec(
                pools=['DUT_POOL_QUOTA', 'healthmon-satlab'],
                models=['quackingstick'],
            ),
        }
        with mock.patch.object(
            pool_manager, 'query_by_owner', autospec=True
        ) as mock_query:
            mock_query.return_value = owned_duts
            self.assertEqual(
                None,
                pool_manager.insert_dut('dut_name_3', 'bisect_3', dimensions),
            )

        self._mock_client.put.assert_not_called()

    def test_existing_dut(self):
        pool_manager = FakeSharedDutPoolManager(self._mock_client)

        existing_dut = datastore.Entity()
        existing_dut.update(
            {
                'name': 'dut_name_1',
                'owner': 'bisect_2',
            }
        )
        self._mock_client.get.return_value = existing_dut

        self._mock_client.entity.return_value = datastore.Entity()
        dimensions = {'dim1:value1', 'dim2:value2'}
        with mock.patch.object(
            pool_manager, 'query_by_owner', autospec=True
        ) as mock_query:
            mock_query.return_value = []
            self.assertEqual(
                'bisect_1',
                pool_manager.insert_dut('dut_name_1', 'bisect_1', dimensions),
            )

        self._mock_client.put.assert_called_once()
        updated_dut = self._mock_client.put.call_args.args[0]
        self.assertEqual('dut_name_1', updated_dut['name'])
        self.assertEqual('bisect_1', updated_dut['owner'])
        self.assertEqual(shared_dut_pool.DutState.IDLE, updated_dut['state'])
        self.assertEqual(dimensions, updated_dut['dimensions'])

    def test_no_existing_dut(self):
        pool_manager = FakeSharedDutPoolManager(self._mock_client)

        self._mock_client.entity.return_value = datastore.Entity()
        dimensions = {'dim1:value1', 'dim2:value2'}
        with mock.patch.object(
            pool_manager, 'query_by_owner', autospec=True
        ) as mock_query:
            mock_query.return_value = []
            self.assertEqual(
                'bisect_1',
                pool_manager.insert_dut('dut_name_1', 'bisect_1', dimensions),
            )

        self._mock_client.put.assert_called_once()
        updated_dut = self._mock_client.put.call_args.args[0]
        self.assertEqual('dut_name_1', updated_dut['name'])
        self.assertEqual('bisect_1', updated_dut['owner'])
        self.assertEqual(shared_dut_pool.DutState.IDLE, updated_dut['state'])
        self.assertEqual(dimensions, updated_dut['dimensions'])


class TestMarkDutState(unittest.TestCase):
    """Test mark_dut_as_idle() and mark_dut_as_busy()."""

    def setUp(self):
        super().setUp()
        self._mock_client = self.enterContext(
            mock.patch.object(
                datastore,
                'Client',
                autospec=True,
            )
        )

    def test_dut_not_exist(self):
        self._mock_client.get.return_value = None
        pool_manager = FakeSharedDutPoolManager(self._mock_client)
        self.assertFalse(pool_manager.mark_dut_as_idle('dut_name_1'))
        self.assertFalse(pool_manager.mark_dut_as_busy('dut_name_1'))

    def test_mark_dut_as_idle(self):
        dut = datastore.Entity()
        dut.update(
            {
                'name': 'dut_name_1',
                'owner': 'bisect_1',
            }
        )
        self._mock_client.get.return_value = dut
        pool_manager = FakeSharedDutPoolManager(self._mock_client)
        self.assertTrue(pool_manager.mark_dut_as_idle('dut_name_1'))

        updated_dut = self._mock_client.put.call_args.args[0]
        self.assertEqual(
            shared_dut_pool.DutState.IDLE, updated_dut.get('state')
        )

    def test_mark_dut_as_busy(self):
        dut = datastore.Entity()
        dut.update(
            {
                'name': 'dut_name_1',
                'owner': 'bisect_1',
            }
        )
        self._mock_client.get.return_value = dut
        pool_manager = FakeSharedDutPoolManager(self._mock_client)
        self.assertTrue(pool_manager.mark_dut_as_busy('dut_name_1'))

        updated_dut = self._mock_client.put.call_args.args[0]
        self.assertEqual(
            shared_dut_pool.DutState.BUSY, updated_dut.get('state')
        )


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

    def test_receiver_already_owns_duts(self):
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

    def test_dut_not_found(self):
        self._mock_client.get.return_value = None

        pool_manager = FakeSharedDutPoolManager(self._mock_client)
        self.assertFalse(pool_manager.transfer_owner('dut_name_1', 'bisect_2'))

    def test_transferred(self):
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

        updated_dut = self._mock_client.put.call_args.args[0]
        self.assertEqual('dut_name_1', updated_dut.get('name'))
        self.assertEqual('bisect_2', updated_dut.get('owner'))
        self.assertEqual(
            shared_dut_pool.DutState.IDLE, updated_dut.get('state')
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

    def test_all_yielded(self):
        duts: list[DutData] = []
        for i in range(3):
            # Entity is a dict-like object.
            dut = typing.cast(DutData, datastore.Entity())
            dut.update(
                {
                    'name': 'dut_name_%s' % str(i + 1),
                    'owner': 'bisect_1',
                    'dimensions': {
                        'label-board': ['board1'],
                    },
                    'state': shared_dut_pool.DutState.IDLE,
                }
            )
            duts.append(dut)
        self._mock_client.get_multi.return_value = duts

        self._mock_bisect_db_client.dut_alloc_spec_for_unfinished_bisects.return_value = {
            'bisect_1': dut_allocate_spec_type.DutAllocateSpec(
                boards=['board1']
            ),
            'bisect_2': dut_allocate_spec_type.DutAllocateSpec(
                boards=['board1', 'board2']
            ),
            'bisect_3': dut_allocate_spec_type.DutAllocateSpec(
                boards=['board3']
            ),
            'bisect_4': dut_allocate_spec_type.DutAllocateSpec(
                boards=['board4', 'board1']
            ),
            'bisect_5': dut_allocate_spec_type.DutAllocateSpec(
                boards=['board5']
            ),
        }

        pool_manager = FakeSharedDutPoolManager(self._mock_client)
        with mock.patch.object(
            pool_manager,
            'transfer_owner',
            new_callable=self._gen_mock_transfer_owner,
        ):
            yielded, not_yielded = pool_manager.yield_duts(
                'bisect_100', [d.get('name') for d in duts]
            )
        self.assertEqual(['dut_name_1', 'dut_name_2', 'dut_name_3'], yielded)
        self.assertEqual([], not_yielded)

    def test_partially_yielded(self):
        duts: list[DutData] = []
        for i in range(3):
            # Entity is a dict-like object.
            dut = typing.cast(DutData, datastore.Entity())
            dut.update(
                {
                    'name': 'dut_name_%s' % str(i + 1),
                    'owner': 'bisect_100',
                    'dimensions': {
                        'label-board': ['board1'],
                    },
                    'state': shared_dut_pool.DutState.IDLE,
                }
            )
            duts.append(dut)
        self._mock_client.get_multi.return_value = duts
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
            yielded, not_yielded = pool_manager.yield_duts(
                'bisect_100', [d.get('name') for d in duts]
            )
        self.assertEqual(['dut_name_1', 'dut_name_2'], yielded)
        self.assertEqual(['dut_name_3'], not_yielded)

    def test_not_yield_to_self(self):
        duts: list[DutData] = []
        for i in range(3):
            # Entity is a dict-like object.
            dut = typing.cast(DutData, datastore.Entity())
            dut.update(
                {
                    'name': 'dut_name_%s' % str(i + 1),
                    'owner': 'bisect_100',
                    'dimensions': {
                        'label-board': ['board1'],
                    },
                    'state': shared_dut_pool.DutState.IDLE,
                }
            )
            duts.append(dut)
        self._mock_client.get_multi.return_value = duts

        self._mock_bisect_db_client.dut_alloc_spec_for_unfinished_bisects.return_value = {
            'bisect_1': dut_allocate_spec_type.DutAllocateSpec(
                boards=['board1']
            ),
            'bisect_2': dut_allocate_spec_type.DutAllocateSpec(
                boards=['board2']
            ),
            'bisect_3': dut_allocate_spec_type.DutAllocateSpec(
                boards=['board3']
            ),
        }

        pool_manager = FakeSharedDutPoolManager(self._mock_client)
        with mock.patch.object(
            pool_manager,
            'transfer_owner',
            new_callable=self._gen_mock_transfer_owner,
        ):
            yielded, not_yielded = pool_manager.yield_duts(
                'bisect_1', [d.get('name') for d in duts]
            )
        self.assertEqual([], yielded)
        self.assertEqual(
            ['dut_name_1', 'dut_name_2', 'dut_name_3'], not_yielded
        )


class TestCleanDutsByOwner(unittest.TestCase):
    """Test clean_up_duts_by_owner()"""

    def setUp(self):
        super().setUp()
        self._mock_client = self.enterContext(
            mock.patch.object(
                datastore,
                'Client',
                autospec=True,
            )
        )
        # mock the "key" method to return the raw "name"
        self._mock_client.key.side_effect = (
            lambda self, name, *args, **kwargs: name
        )
        self._mock_query_by_owner = self.enterContext(
            mock.patch.object(
                shared_dut_pool.SharedDutPoolManager,
                'query_by_owner',
                autospec=True,
            )
        )
        self._mock_yield_duts = self.enterContext(
            mock.patch.object(
                shared_dut_pool.SharedDutPoolManager,
                'yield_duts',
                autospec=True,
            )
        )
        self._mock_crosfleet_release_dut = self.enterContext(
            mock.patch.object(
                cros_lab_util,
                'crosfleet_release_dut',
                autospec=True,
            )
        )
        self.maxDiff = None

    def test_ok(self):
        owned_duts = ['dut1', 'dut2', 'dut3', 'dut4']
        self._mock_query_by_owner.return_value = owned_duts
        self._mock_yield_duts.return_value = (
            ['dut1', 'dut2'],
            ['dut3', 'dut4'],
        )
        pool_manager = FakeSharedDutPoolManager(self._mock_client)
        pool_manager.clean_up_duts_by_owner('bisect_1')

        self._mock_yield_duts.assert_called_once_with(
            mock.ANY, 'bisect_1', owned_duts
        )
        self._mock_client.delete_multi.assert_called_once_with(['dut3', 'dut4'])
        self._mock_crosfleet_release_dut.assert_has_calls(
            [
                mock.call('dut3'),
                mock.call('dut4'),
            ]
        )

    def test_raise_exception(self):
        owned_duts = ['dut1', 'dut2', 'dut3', 'dut4']
        self._mock_query_by_owner.return_value = owned_duts
        self._mock_yield_duts.return_value = (
            ['dut1', 'dut2'],
            ['dut3', 'dut4'],
        )
        self._mock_crosfleet_release_dut.side_effect = [
            None,
            errors.DutLeaseException('failed to release DUT'),
        ]

        pool_manager = FakeSharedDutPoolManager(self._mock_client)
        pool_manager.clean_up_duts_by_owner('bisect_1')

        self._mock_yield_duts.assert_called_once()
        self._mock_client.delete_multi.assert_called_once_with(['dut3', 'dut4'])
        self._mock_crosfleet_release_dut.assert_has_calls(
            [
                mock.call('dut3'),
                mock.call('dut4'),
            ]
        )


class TestCleanUpStaleDuts(unittest.TestCase):
    """Test _clean_up_stale_duts()"""

    def setUp(self):
        super().setUp()
        self._mock_client = self.enterContext(
            mock.patch.object(
                datastore,
                'Client',
                autospec=True,
            )
        )
        # mock the "key" method to return the raw "name"
        self._mock_client.key.side_effect = (
            lambda self, name, *args, **kwargs: name
        )
        self._mock_query_lease_status = self.enterContext(
            mock.patch.object(
                cros_lab_util,
                'query_lease_status',
                autospec=True,
            )
        )

    def test_all(self):
        self._mock_query_lease_status.side_effect = [
            cros_lab_util.LeaseStatus(dut_name='dut_name_1', is_leased=False),
            cros_lab_util.LeaseStatus(dut_name='dut_name_2', is_leased=False),
            cros_lab_util.LeaseStatus(dut_name='dut_name_3', is_leased=True),
        ]
        pool_manager = FakeSharedDutPoolManager(self._mock_client)
        # pylint: disable=protected-access
        pool_manager._clean_up_stale_duts(
            ['dut_name_1', 'dut_name_2', 'dut_name_3']
        )
        self._mock_client.delete_multi.assert_called_once_with(
            ['dut_name_1', 'dut_name_2']
        )

    def test_failed_to_query_lease_status(self):
        self._mock_query_lease_status.side_effect = [
            cros_lab_util.LeaseStatus(dut_name='dut_name_1', is_leased=False),
            errors.DutLeaseException('failed to query'),
            cros_lab_util.LeaseStatus(dut_name='dut_name_3', is_leased=True),
        ]
        pool_manager = FakeSharedDutPoolManager(self._mock_client)
        # pylint: disable=protected-access
        pool_manager._clean_up_stale_duts(
            ['dut_name_1', 'dut_name_2', 'dut_name_3']
        )
        self._mock_client.delete_multi.assert_called_once_with(['dut_name_1'])


class TestLeaseForSession(unittest.TestCase):
    """Test lease_for_bisect()"""

    def setUp(self):
        super().setUp()
        self._mock_client = self.enterContext(
            mock.patch.object(
                datastore,
                'Client',
                autospec=True,
            )
        )
        self._mock_query_obj = self.enterContext(
            mock.patch.object(
                FakeSharedDutPoolManager,
                '_build_query',
                autospec=True,
            )
        )
        self._mock_fetch = self._mock_query_obj.return_value.fetch

        self._mock_clean_up_stale_duts = self.enterContext(
            mock.patch.object(
                FakeSharedDutPoolManager,
                '_clean_up_stale_duts',
                autospec=True,
            )
        )
        self._mock_clean_up_stale_duts.return_value = []

        self._mock_query_by_owner = self.enterContext(
            mock.patch.object(
                FakeSharedDutPoolManager,
                'query_by_owner',
                autospec=True,
            )
        )

        self._mock_crosfleet_release_dut = self.enterContext(
            mock.patch.object(
                cros_lab_util,
                'crosfleet_release_dut',
                autospec=True,
            )
        )

        # mock the "key" method to return the raw "name"
        self._mock_client.key.side_effect = (
            lambda self, name, *args, **kwargs: name
        )
        self.maxDiff = None

    def test_has_matched_dut_and_only_good_owned_dut(self):
        duts = []
        for i in range(3):
            dut = datastore.Entity()
            dut.update(
                {
                    'name': 'dut_name_%s' % str(i + 1),
                    'owner': 'bisect_%s' % str(i + 1),
                }
            )
            duts.append(dut)
        pool_manager = FakeSharedDutPoolManager(self._mock_client)
        self._mock_fetch.return_value = iter(duts)
        self._mock_query_by_owner.return_value = ['dut_name_2']
        dut_name, orig_owner = pool_manager.lease_for_bisect(
            'bisect_2', [], [], []
        )
        self.assertEqual('dut_name_2', dut_name)
        self.assertEqual('bisect_2', orig_owner)

        self._mock_client.put.assert_called_once()
        updated_dut = self._mock_client.put.call_args.args[0]
        self.assertEqual('dut_name_2', updated_dut['name'])
        self.assertEqual('bisect_2', updated_dut['owner'])
        self.assertEqual(shared_dut_pool.DutState.BUSY, updated_dut['state'])

    def test_has_matched_dut_and_both_good_bad_owned_dut(self):
        duts = []
        for i in range(3):
            dut = datastore.Entity()
            dut.update(
                {
                    'name': 'dut_name_%s' % str(i + 1),
                    'owner': 'bisect_%s' % str(i + 1),
                }
            )
            duts.append(dut)
        pool_manager = FakeSharedDutPoolManager(self._mock_client)
        self._mock_fetch.return_value = iter(duts)
        self._mock_query_by_owner.return_value = [
            'dut_name_2',
            'dut_name_4',
            'dut_name_5',
        ]
        dut_name, orig_owner = pool_manager.lease_for_bisect(
            'bisect_2', [], [], []
        )
        self.assertEqual('dut_name_2', dut_name)
        self.assertEqual('bisect_2', orig_owner)

        self._mock_crosfleet_release_dut.assert_has_calls(
            [
                mock.call('dut_name_4'),
                mock.call('dut_name_5'),
            ]
        )
        self._mock_client.delete_multi.assert_called_once_with(
            ['dut_name_4', 'dut_name_5']
        )

        self._mock_client.put.assert_called_once()
        updated_dut = self._mock_client.put.call_args.args[0]
        self.assertEqual('dut_name_2', updated_dut['name'])
        self.assertEqual('bisect_2', updated_dut['owner'])
        self.assertEqual(shared_dut_pool.DutState.BUSY, updated_dut['state'])

    def test_has_matched_dut_and_only_bad_owned_dut(self):
        duts = []
        for i in range(3):
            dut = datastore.Entity()
            dut.update(
                {
                    'name': 'dut_name_%s' % str(i + 1),
                    'owner': 'bisect_%s' % str(i + 1),
                }
            )
            duts.append(dut)
        pool_manager = FakeSharedDutPoolManager(self._mock_client)
        self._mock_fetch.return_value = iter(duts)
        self._mock_query_by_owner.return_value = ['dut_name_4', 'dut_name_5']
        dut_name, orig_owner = pool_manager.lease_for_bisect(
            'bisect_4', [], [], []
        )
        self.assertEqual('dut_name_1', dut_name)
        self.assertEqual('bisect_1', orig_owner)

        self._mock_crosfleet_release_dut.assert_has_calls(
            [
                mock.call('dut_name_4'),
                mock.call('dut_name_5'),
            ]
        )
        self._mock_client.delete_multi.assert_called_once_with(
            ['dut_name_4', 'dut_name_5']
        )

        self._mock_client.put.assert_called_once()
        updated_dut = self._mock_client.put.call_args.args[0]
        self.assertEqual('dut_name_1', updated_dut['name'])
        self.assertEqual('bisect_4', updated_dut['owner'])
        self.assertEqual(shared_dut_pool.DutState.BUSY, updated_dut['state'])

    def test_has_matched_dut_and_no_owned_dut(self):
        duts = []
        for i in range(3):
            dut = datastore.Entity()
            dut.update(
                {
                    'name': 'dut_name_%s' % str(i + 1),
                    'owner': 'bisect_%s' % str(i + 1),
                }
            )
            duts.append(dut)
        pool_manager = FakeSharedDutPoolManager(self._mock_client)
        self._mock_fetch.return_value = iter(duts)
        self._mock_query_by_owner.return_value = []
        dut_name, orig_owner = pool_manager.lease_for_bisect(
            'bisect_4', [], [], []
        )
        self.assertEqual('dut_name_1', dut_name)
        self.assertEqual('bisect_1', orig_owner)

        self._mock_client.put.assert_called_once()
        updated_dut = self._mock_client.put.call_args.args[0]
        self.assertEqual('dut_name_1', updated_dut['name'])
        self.assertEqual('bisect_4', updated_dut['owner'])
        self.assertEqual(shared_dut_pool.DutState.BUSY, updated_dut['state'])

    def test_no_matched_dut_and_no_owned_dut(self):
        pool_manager = FakeSharedDutPoolManager(self._mock_client)
        self._mock_fetch.return_value = iter([])
        self._mock_query_by_owner.return_value = []
        dut_name, orig_owner = pool_manager.lease_for_bisect(
            'bisect_1', [], [], []
        )
        self.assertEqual(None, dut_name)
        self.assertEqual(None, orig_owner)
        self._mock_client.put.assert_not_called()

    def test_no_matched_dut_and_only_bad_owned_dut(self):
        pool_manager = FakeSharedDutPoolManager(self._mock_client)
        self._mock_fetch.return_value = iter([])
        self._mock_query_by_owner.return_value = ['dut_name_1', 'dut_name_2']
        dut_name, orig_owner = pool_manager.lease_for_bisect(
            'bisect_1', [], [], []
        )
        self.assertEqual(None, dut_name)
        self.assertEqual(None, orig_owner)

        self._mock_crosfleet_release_dut.assert_has_calls(
            [
                mock.call('dut_name_1'),
                mock.call('dut_name_2'),
            ]
        )
        self._mock_client.delete_multi.assert_called_once_with(
            ['dut_name_1', 'dut_name_2']
        )

        self._mock_client.put.assert_not_called()

    def test_has_stale_duts(self):
        duts = []
        for i in range(3):
            dut = datastore.Entity()
            dut.update(
                {
                    'name': 'dut_name_%s' % str(i + 1),
                    'owner': 'bisect_%s' % str(i + 1),
                }
            )
            duts.append(dut)
        pool_manager = FakeSharedDutPoolManager(self._mock_client)
        self._mock_fetch.return_value = iter(duts)
        self._mock_query_by_owner.return_value = []
        self._mock_clean_up_stale_duts.return_value = [
            'dut_name_1',
            'dut_name_2',
        ]
        dut_name, orig_owner = pool_manager.lease_for_bisect(
            'bisect_2', [], [], []
        )
        self.assertEqual('dut_name_3', dut_name)
        self.assertEqual('bisect_3', orig_owner)

        self._mock_client.put.assert_called_once()
        updated_dut = self._mock_client.put.call_args.args[0]
        self.assertEqual('dut_name_3', updated_dut['name'])
        self.assertEqual('bisect_2', updated_dut['owner'])
        self.assertEqual(shared_dut_pool.DutState.BUSY, updated_dut['state'])

    def test_no_matched_duts_after_filtered_stale_duts(self):
        duts = []
        for i in range(3):
            dut = datastore.Entity()
            dut.update(
                {
                    'name': 'dut_name_%s' % str(i + 1),
                    'owner': 'bisect_%s' % str(i + 1),
                }
            )
            duts.append(dut)
        pool_manager = FakeSharedDutPoolManager(self._mock_client)
        self._mock_fetch.return_value = iter(duts)
        self._mock_query_by_owner.return_value = []
        self._mock_clean_up_stale_duts.return_value = [
            'dut_name_1',
            'dut_name_2',
            'dut_name_3',
        ]
        dut_name, orig_owner = pool_manager.lease_for_bisect(
            'bisect_1', [], [], []
        )
        self.assertEqual(None, dut_name)
        self.assertEqual(None, orig_owner)
        self._mock_client.put.assert_not_called()


class TestRetryTransactionConflictWrapper(unittest.TestCase):
    """Test retry_transaction_conflict()"""

    def test_no_retry(self):
        func = mock.Mock()
        func.return_value = 'ok'

        wrapped_func = shared_dut_pool.retry_transaction_conflict(func)

        self.assertEqual('ok', wrapped_func())

    def test_retry_within_limit(self):
        func = mock.Mock()
        func.side_effect = [
            google.cloud.exceptions.Conflict(1),
            google.cloud.exceptions.Conflict(2),
            google.cloud.exceptions.Conflict(3),
            google.cloud.exceptions.Conflict(4),
            'ok',
        ]

        wrapped_func = shared_dut_pool.retry_transaction_conflict(func)

        self.assertEqual('ok', wrapped_func())

    def test_retry_exceeds_limit(self):
        func = mock.Mock()
        func.side_effect = [
            google.cloud.exceptions.Conflict(1),
            google.cloud.exceptions.Conflict(2),
            google.cloud.exceptions.Conflict(3),
            google.cloud.exceptions.Conflict(4),
            google.cloud.exceptions.Conflict(5),
        ]

        wrapped_func = shared_dut_pool.retry_transaction_conflict(func)

        with self.assertRaises(errors.DatastoreTransactionConflict):
            wrapped_func()
