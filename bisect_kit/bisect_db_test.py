# Copyright 2024 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Test bisect_db module."""

import logging
import unittest

from bisect_kit import bisect_db
from bisect_kit import dut_allocate_spec as dut_allocate_spec_type
from bisect_kit import errors
from google.cloud import datastore


logger = logging.getLogger(__name__)


class TestToDutAllocateSpec(unittest.TestCase):
    """Test to_dut_allocate_spec."""

    def setUp(self):
        super().setUp()
        self.maxDiff = None

    def test_bisect_with_dut_name_ends_with_cros(self):
        bisect = datastore.Entity()
        bisect.update(
            {
                'ID': '00005bdd-e7fd-4a0a-be51-6f3d48547731',
                'Builders': ['asurada'],
                'Pools': ['healthmon-satlab', 'DUT_POOL_QUOTA'],
                'UserDUT': 'chromeos7-row6-rack4-host3.cros',
                'Old.Version': '12703.0.0',
                'New.Version': '12739.0.0',
            }
        )

        self.assertEqual(
            dut_allocate_spec_type.DutAllocateSpec(
                pools=['healthmon-satlab', 'DUT_POOL_QUOTA'],
                dut_name='chromeos7-row6-rack4-host3',
                dimensions=[],
                version_hints=['12703.0.0', '12739.0.0'],
                builder_hints=['asurada'],
                session='00005bdd-e7fd-4a0a-be51-6f3d48547731',
            ),
            bisect_db.to_dut_allocate_spec(bisect),
        )

    def test_bisect_with_dut_name_not_ends_with_cros(self):
        bisect = datastore.Entity()
        bisect.update(
            {
                'ID': '00005bdd-e7fd-4a0a-be51-6f3d48547731',
                'Builders': ['asurada'],
                'Pools': ['healthmon-satlab', 'DUT_POOL_QUOTA'],
                'UserDUT': 'chromeos7-row6-rack4-host3',
                'Old.Version': '12703.0.0',
                'New.Version': '12739.0.0',
            }
        )

        self.assertEqual(
            dut_allocate_spec_type.DutAllocateSpec(
                pools=['healthmon-satlab', 'DUT_POOL_QUOTA'],
                dut_name='chromeos7-row6-rack4-host3',
                dimensions=[],
                version_hints=['12703.0.0', '12739.0.0'],
                builder_hints=['asurada'],
                session='00005bdd-e7fd-4a0a-be51-6f3d48547731',
            ),
            bisect_db.to_dut_allocate_spec(bisect),
        )

    def test_bisect_with_dut_name_and_device_spec(self):
        bisect = datastore.Entity()
        bisect.update(
            {
                'ID': '00005bdd-e7fd-4a0a-be51-6f3d48547731',
                'Builders': ['asurada'],
                'DeviceSpecType': bisect_db.DeviceSpecType.BOARD,
                'DeviceSpecValue': ['asurada'],
                'Pools': ['healthmon-satlab', 'DUT_POOL_QUOTA'],
                'UserDUT': 'chromeos7-row6-rack4-host3',
                'Old.Version': '12703.0.0',
                'New.Version': '12739.0.0',
            }
        )

        self.assertEqual(
            dut_allocate_spec_type.DutAllocateSpec(
                pools=['healthmon-satlab', 'DUT_POOL_QUOTA'],
                # boards not set.
                dut_name='chromeos7-row6-rack4-host3',
                dimensions=[],
                version_hints=['12703.0.0', '12739.0.0'],
                builder_hints=['asurada'],
                session='00005bdd-e7fd-4a0a-be51-6f3d48547731',
            ),
            bisect_db.to_dut_allocate_spec(bisect),
        )

    def test_bisect_with_dut_name_and_retry_device_spec(self):
        bisect = datastore.Entity()
        bisect.update(
            {
                'ID': '00005bdd-e7fd-4a0a-be51-6f3d48547731',
                'Builders': ['asurada'],
                'RetryDeviceSpecType': bisect_db.DeviceSpecType.MODEL,
                'RetryDeviceSpecValue': 'hayato',
                'Pools': ['healthmon-satlab', 'DUT_POOL_QUOTA'],
                'UserDUT': 'chromeos7-row6-rack4-host3',
                'Old.Version': '12703.0.0',
                'New.Version': '12739.0.0',
            }
        )

        self.assertEqual(
            dut_allocate_spec_type.DutAllocateSpec(
                pools=['healthmon-satlab', 'DUT_POOL_QUOTA'],
                # model is not set.
                dut_name='chromeos7-row6-rack4-host3',
                dimensions=[],
                version_hints=['12703.0.0', '12739.0.0'],
                builder_hints=['asurada'],
                session='00005bdd-e7fd-4a0a-be51-6f3d48547731',
            ),
            bisect_db.to_dut_allocate_spec(bisect),
        )

    def test_bisect_with_board(self):
        bisect = datastore.Entity()
        bisect.update(
            {
                'ID': '00005bdd-e7fd-4a0a-be51-6f3d48547731',
                'Builders': ['asurada'],
                'DeviceSpecType': bisect_db.DeviceSpecType.BOARD,
                'DeviceSpecValue': ['asurada'],
                'Pools': ['healthmon-satlab', 'DUT_POOL_QUOTA'],
                'Old.Version': '12703.0.0',
                'New.Version': '12739.0.0',
            }
        )

        self.assertEqual(
            dut_allocate_spec_type.DutAllocateSpec(
                pools=['healthmon-satlab', 'DUT_POOL_QUOTA'],
                boards=['asurada'],
                dimensions=[],
                version_hints=['12703.0.0', '12739.0.0'],
                builder_hints=['asurada'],
                session='00005bdd-e7fd-4a0a-be51-6f3d48547731',
            ),
            bisect_db.to_dut_allocate_spec(bisect),
        )

    def test_bisect_with_model(self):
        bisect = datastore.Entity()
        bisect.update(
            {
                'ID': '00005bdd-e7fd-4a0a-be51-6f3d48547731',
                'Builders': ['asurada'],
                'DeviceSpecType': bisect_db.DeviceSpecType.MODEL,
                'DeviceSpecValue': ['hayato'],
                'Pools': ['healthmon-satlab', 'DUT_POOL_QUOTA'],
                'Old.Version': '12703.0.0',
                'New.Version': '12739.0.0',
            }
        )

        self.assertEqual(
            dut_allocate_spec_type.DutAllocateSpec(
                pools=['healthmon-satlab', 'DUT_POOL_QUOTA'],
                models=['hayato'],
                dimensions=[],
                version_hints=['12703.0.0', '12739.0.0'],
                builder_hints=['asurada'],
                session='00005bdd-e7fd-4a0a-be51-6f3d48547731',
            ),
            bisect_db.to_dut_allocate_spec(bisect),
        )

    def test_bisect_with_sku(self):
        bisect = datastore.Entity()
        bisect.update(
            {
                'ID': '00005bdd-e7fd-4a0a-be51-6f3d48547731',
                'Builders': ['asurada'],
                'DeviceSpecType': bisect_db.DeviceSpecType.SKU,
                'DeviceSpecValue': ['hayato_MT8192_4GB'],
                'Pools': ['healthmon-satlab', 'DUT_POOL_QUOTA'],
                'Old.Version': '12703.0.0',
                'New.Version': '12739.0.0',
            }
        )

        self.assertEqual(
            dut_allocate_spec_type.DutAllocateSpec(
                pools=['healthmon-satlab', 'DUT_POOL_QUOTA'],
                skus=['hayato_MT8192_4GB'],
                dimensions=[],
                version_hints=['12703.0.0', '12739.0.0'],
                builder_hints=['asurada'],
                session='00005bdd-e7fd-4a0a-be51-6f3d48547731',
            ),
            bisect_db.to_dut_allocate_spec(bisect),
        )

    def test_bisect_with_multiple_models(self):
        bisect = datastore.Entity()
        bisect.update(
            {
                'ID': '00005bdd-e7fd-4a0a-be51-6f3d48547731',
                'Builders': ['asurada'],
                'DeviceSpecType': bisect_db.DeviceSpecType.MODEL,
                'DeviceSpecValue': ['hayato', 'spherion'],
                'Pools': ['healthmon-satlab', 'DUT_POOL_QUOTA'],
                'Old.Version': '12703.0.0',
                'New.Version': '12739.0.0',
            }
        )

        self.assertEqual(
            dut_allocate_spec_type.DutAllocateSpec(
                pools=['healthmon-satlab', 'DUT_POOL_QUOTA'],
                models=['hayato', 'spherion'],
                dimensions=[],
                version_hints=['12703.0.0', '12739.0.0'],
                builder_hints=['asurada'],
                session='00005bdd-e7fd-4a0a-be51-6f3d48547731',
            ),
            bisect_db.to_dut_allocate_spec(bisect),
        )

    def test_bisect_with_base_cros_version(self):
        bisect = datastore.Entity()
        bisect.update(
            {
                'ID': '00005bdd-e7fd-4a0a-be51-6f3d48547731',
                'Builders': ['asurada'],
                'DeviceSpecType': bisect_db.DeviceSpecType.MODEL,
                'DeviceSpecValue': ['hayato'],
                'Pools': ['healthmon-satlab', 'DUT_POOL_QUOTA'],
                'BaseCrosVersion': '12739.0.0',
            }
        )

        self.assertEqual(
            dut_allocate_spec_type.DutAllocateSpec(
                pools=['healthmon-satlab', 'DUT_POOL_QUOTA'],
                models=['hayato'],
                dimensions=[],
                version_hints=['12739.0.0'],
                builder_hints=['asurada'],
                session='00005bdd-e7fd-4a0a-be51-6f3d48547731',
            ),
            bisect_db.to_dut_allocate_spec(bisect),
        )

    def test_bisect_with_dimensions(self):
        bisect = datastore.Entity()
        bisect.update(
            {
                'ID': '00005bdd-e7fd-4a0a-be51-6f3d48547731',
                'Builders': ['asurada'],
                'DeviceSpecType': bisect_db.DeviceSpecType.MODEL,
                'DeviceSpecValue': ['hayato'],
                'Pools': ['healthmon-satlab', 'DUT_POOL_QUOTA'],
                'Old.Version': '12703.0.0',
                'New.Version': '12739.0.0',
                'DUTDimensionsFilters': [
                    "label-chameleon: True",
                    "label-audio_board: True",
                    "label-test_usbaudio: True",
                ],
            }
        )

        self.assertEqual(
            dut_allocate_spec_type.DutAllocateSpec(
                pools=['healthmon-satlab', 'DUT_POOL_QUOTA'],
                models=['hayato'],
                # Spaces are removed.
                dimensions=[
                    'label-chameleon:True',
                    'label-audio_board:True',
                    'label-test_usbaudio:True',
                ],
                version_hints=['12703.0.0', '12739.0.0'],
                builder_hints=['asurada'],
                session='00005bdd-e7fd-4a0a-be51-6f3d48547731',
            ),
            bisect_db.to_dut_allocate_spec(bisect),
        )

    def test_bisect_with_retry_device_spec_type(self):
        bisect = datastore.Entity()
        bisect.update(
            {
                'ID': '00005bdd-e7fd-4a0a-be51-6f3d48547731',
                'Builders': ['asurada'],
                'DeviceSpecType': bisect_db.DeviceSpecType.BOARD,
                'DeviceSpecValue': ['asurada'],
                'RetryDeviceSpecType': bisect_db.DeviceSpecType.MODEL,
                'RetryDeviceSpecValue': 'hayato',
                'Pools': ['healthmon-satlab', 'DUT_POOL_QUOTA'],
                'Old.Version': '12703.0.0',
                'New.Version': '12739.0.0',
            }
        )

        self.assertEqual(
            dut_allocate_spec_type.DutAllocateSpec(
                pools=['healthmon-satlab', 'DUT_POOL_QUOTA'],
                models=['hayato'],
                dimensions=[],
                version_hints=['12703.0.0', '12739.0.0'],
                builder_hints=['asurada'],
                session='00005bdd-e7fd-4a0a-be51-6f3d48547731',
            ),
            bisect_db.to_dut_allocate_spec(bisect),
        )

    def test_bisect_with_bad_dimensions(self):
        bisect = datastore.Entity()
        bisect.update(
            {
                'ID': '00005bdd-e7fd-4a0a-be51-6f3d48547731',
                'Builders': ['asurada'],
                'DeviceSpecType': bisect_db.DeviceSpecType.MODEL,
                'DeviceSpecValue': ['hayato'],
                'Pools': ['healthmon-satlab', 'DUT_POOL_QUOTA'],
                'Old.Version': '12703.0.0',
                'New.Version': '12739.0.0',
                'DUTDimensionsFilters': [
                    "label-chameleon:True",
                    "label-audio_board",
                    "label-test_usbaudio:True",
                    "",
                ],
            }
        )

        self.assertEqual(
            dut_allocate_spec_type.DutAllocateSpec(
                pools=['healthmon-satlab', 'DUT_POOL_QUOTA'],
                models=['hayato'],
                dimensions=[
                    "label-chameleon:True",
                    "label-test_usbaudio:True",
                ],
                version_hints=['12703.0.0', '12739.0.0'],
                builder_hints=['asurada'],
                session='00005bdd-e7fd-4a0a-be51-6f3d48547731',
            ),
            bisect_db.to_dut_allocate_spec(bisect),
        )

    def test_bisect_misses_old_version(self):
        bisect = datastore.Entity()
        bisect.update(
            {
                'ID': '00005bdd-e7fd-4a0a-be51-6f3d48547731',
                'Builders': ['asurada'],
                'DeviceSpecType': bisect_db.DeviceSpecType.MODEL,
                'DeviceSpecValue': ['hayato'],
                'Pools': ['healthmon-satlab', 'DUT_POOL_QUOTA'],
                'New.Version': '12739.0.0',
            }
        )

        with self.assertRaises(errors.DutAllocateSpecError):
            bisect_db.to_dut_allocate_spec(bisect)

    def test_bisect_misses_new_version(self):
        bisect = datastore.Entity()
        bisect.update(
            {
                'ID': '00005bdd-e7fd-4a0a-be51-6f3d48547731',
                'Builders': ['asurada'],
                'DeviceSpecType': bisect_db.DeviceSpecType.MODEL,
                'DeviceSpecValue': ['hayato'],
                'Pools': ['healthmon-satlab', 'DUT_POOL_QUOTA'],
                'Old.Version': '12703.0.0',
            }
        )

        with self.assertRaises(errors.DutAllocateSpecError):
            bisect_db.to_dut_allocate_spec(bisect)

    def test_bisect_misses_required_specs(self):
        bisect = datastore.Entity()
        bisect.update(
            {
                'ID': '00005bdd-e7fd-4a0a-be51-6f3d48547731',
                'Builders': ['asurada'],
                'Pools': ['healthmon-satlab', 'DUT_POOL_QUOTA'],
                'Old.Version': '12703.0.0',
                'New.Version': '12739.0.0',
            }
        )

        with self.assertRaises(errors.DutAllocateSpecError):
            bisect_db.to_dut_allocate_spec(bisect)


class TestRetryDeviceSpec(unittest.TestCase):
    """Test to_dut_allocate_spec."""

    def setUp(self):
        self.maxDiff = None

    def test_functional_bisection(self):
        bisect = datastore.Entity()
        bisect.update(
            {
                'DeviceSpecType': bisect_db.DeviceSpecType.BOARD,
                'DeviceSpecValue': ['asurada'],
            }
        )
        spec = dut_allocate_spec_type.DutAllocateSpec(models=['hayato'])

        self.assertTrue(bisect_db.update_retry_device_spec(bisect, spec))
        self.assertEqual(
            {
                'RetryDeviceSpecType': bisect_db.DeviceSpecType.MODEL,
                'RetryDeviceSpecValue': 'hayato',
                'DeviceSpecType': bisect_db.DeviceSpecType.BOARD,
                'DeviceSpecValue': ['asurada'],
            },
            dict(bisect),
        )

    def test_perf_bisection(self):
        bisect = datastore.Entity()
        bisect.update(
            {
                'Autotest.Metric': 'metric_name',
                'DeviceSpecType': bisect_db.DeviceSpecType.BOARD,
                'DeviceSpecValue': ['asurada'],
            }
        )
        spec = dut_allocate_spec_type.DutAllocateSpec(
            skus=['hayato_MT8192_4GB']
        )

        self.assertTrue(bisect_db.update_retry_device_spec(bisect, spec))
        self.assertEqual(
            {
                'Autotest.Metric': 'metric_name',
                'RetryDeviceSpecType': bisect_db.DeviceSpecType.SKU,
                'RetryDeviceSpecValue': 'hayato_MT8192_4GB',
                'DeviceSpecType': bisect_db.DeviceSpecType.BOARD,
                'DeviceSpecValue': ['asurada'],
            },
            dict(bisect),
        )

    def test_functional_bisection_missing_model(self):
        bisect = datastore.Entity()
        bisect.update(
            {
                'DeviceSpecType': bisect_db.DeviceSpecType.BOARD,
                'DeviceSpecValue': ['asurada'],
            }
        )
        spec = dut_allocate_spec_type.DutAllocateSpec()

        self.assertFalse(bisect_db.update_retry_device_spec(bisect, spec))
        # Leaved intact.
        self.assertEqual(
            {
                'DeviceSpecType': bisect_db.DeviceSpecType.BOARD,
                'DeviceSpecValue': ['asurada'],
            },
            dict(bisect),
        )

    def test_perf_bisection_missing_sku(self):
        bisect = datastore.Entity()
        bisect.update(
            {
                'Autotest.Metric': 'metric_name',
                'DeviceSpecType': bisect_db.DeviceSpecType.BOARD,
                'DeviceSpecValue': ['asurada'],
            }
        )
        spec = dut_allocate_spec_type.DutAllocateSpec()

        self.assertFalse(bisect_db.update_retry_device_spec(bisect, spec))
        # Leaved intact.
        self.assertEqual(
            {
                'Autotest.Metric': 'metric_name',
                'DeviceSpecType': bisect_db.DeviceSpecType.BOARD,
                'DeviceSpecValue': ['asurada'],
            },
            dict(bisect),
        )
