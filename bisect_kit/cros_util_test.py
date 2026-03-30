# Copyright 2017 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Test cros_util module."""

import fnmatch
import logging
import os
import random
import subprocess
import tempfile
import textwrap
import unittest
from unittest import mock

from bisect_kit import cros_util
from bisect_kit import errors
from bisect_kit import gs_util
from bisect_kit import testing


logger = logging.getLogger(__name__)


def _fake_gs_util_ls(*args, **_kwargs):
    archive_base = 'gs://chromeos-image-archive/foo-release/'
    snapshot_base = 'gs://chromeos-image-archive/foo-snapshot/'

    gs_files = [
        snapshot_base + 'R63-9996.0.0-30103-000/image.zip',
        snapshot_base + 'R63-9997.0.0-30104-000/image.zip',
        snapshot_base + 'R63-9998.0.0-30105-000/image.zip',
        snapshot_base + 'R63-9999.0.0-30106-000/image.zip',
        snapshot_base + 'R63-9996.0.0-30103-000/image.zip',
        snapshot_base + 'R63-9996.0.0-30103-000/full_dev_part_KERN.bin.gz',
        snapshot_base + 'R63-9997.0.0-30104-000/image.zip',
        snapshot_base + 'R63-9998.0.0-30105-000/image.zip',
        snapshot_base + 'R63-9999.0.0-30106-000/image.zip',
        archive_base + 'R63-9995.0.0/image.zip',
        archive_base + 'R63-9995.0.0/chromiumos_test_image.tar.xz',
        archive_base + 'R63-9997.0.0/image.zip',
        archive_base + 'R63-9997.0.0/chromiumos_test_image.tar.xz',
        archive_base + 'R63-9999.0.0/image.zip',
        archive_base + 'R63-9999.1.0/image.zip',
        archive_base + 'R63-9998.0.0/chromiumos_test_image.tar.xz',
        archive_base + 'R63-9999.0.0/chromiumos_test_image.tar.xz',
        archive_base + 'R63-9999.1.0/chromiumos_test_image.tar.xz',
        archive_base + 'R63-10000.0.0/chromiumos_test_image.tar.xz',
        archive_base + 'R63-10001.0.0/chromiumos_test_image.tar.xz',
    ]

    assert len(args) == 1
    result = []
    for gs_path in gs_files:
        if fnmatch.fnmatchcase(gs_path, args[0]):
            result.append(gs_path)
        elif fnmatch.fnmatchcase(gs_path, args[0] + '*/'):
            result.append(gs_path)
    return result


def _fake_query_milestone_by_version(_1, _2):
    return '63'


# pylint: disable=unused-argument
def _fake_version_to_full(_board, version, spec_manager=None):
    if version.startswith('R'):
        return version
    return 'R63-' + version


def _fake_gs_util_ls_for_public_build(*args, **kwargs):
    archive_base = 'gs://chromiumos-image-archive/foo-public/'
    gs_files = [
        archive_base + 'R100-10000.0.0/chromiumos_test_image.tar.xz',
        archive_base + 'R100-10001.0.0/chromiumos_test_image.tar.xz',
    ]
    assert len(args) == 1
    result = []
    for gs_path in gs_files:
        if fnmatch.fnmatchcase(gs_path, args[0]):
            result.append(gs_path)
        elif fnmatch.fnmatchcase(gs_path, args[0] + '*/'):
            result.append(gs_path)
    return result


class TestCrosUtil(unittest.TestCase):
    """Test cros_util functions."""

    def test_is_cros_short_version(self):
        self.assertTrue(cros_util.is_cros_short_version('1234.0.0'))
        self.assertFalse(cros_util.is_cros_short_version('01234.0.0'))
        self.assertFalse(cros_util.is_cros_short_version('1234.00.0'))
        self.assertFalse(cros_util.is_cros_short_version('1234.0.00'))
        self.assertFalse(cros_util.is_cros_short_version('R99-1234.0.0'))
        self.assertFalse(
            cros_util.is_cros_short_version('1234.0.2018_01_01_1259')
        )

    def test_is_cros_full_version(self):
        self.assertTrue(cros_util.is_cros_full_version('R99-1234.0.0'))
        self.assertFalse(cros_util.is_cros_full_version('R099-1234.00.0'))
        self.assertFalse(cros_util.is_cros_full_version('R99-01234.00.0'))
        self.assertFalse(cros_util.is_cros_full_version('R99-1234.00.0'))
        self.assertFalse(cros_util.is_cros_full_version('R99-1234.0.00'))
        self.assertFalse(cros_util.is_cros_full_version('1234.0.0'))
        self.assertFalse(
            cros_util.is_cros_full_version('1234.0.2018_01_01_1259')
        )

    def test_is_cros_version(self):
        self.assertTrue(cros_util.is_cros_version('1234.0.0'))
        self.assertTrue(cros_util.is_cros_version('R99-1234.0.0'))
        self.assertFalse(cros_util.is_cros_version('1234.00.0'))
        self.assertFalse(cros_util.is_cros_version('R99-1234.00.0'))

    def test_is_ancestor_version(self):
        # One version is not ancestor of itself.
        self.assertFalse(
            cros_util.is_ancestor_version('12345.0.0', '12345.0.0')
        )
        self.assertFalse(
            cros_util.is_ancestor_version('12345.0.0', 'R10-12345.0.0')
        )
        self.assertFalse(
            cros_util.is_ancestor_version(
                'R10-12345.0.0-1000', 'R10-12345.0.0-1000'
            )
        )

        self.assertTrue(cros_util.is_ancestor_version('12345.0.0', '12346.0.0'))
        self.assertTrue(
            cros_util.is_ancestor_version('R10-12345.0.0', 'R10-12346.0.0')
        )
        self.assertTrue(
            cros_util.is_ancestor_version(
                'R10-12345.0.0-1000', 'R10-12345.0.0-2000'
            )
        )

        self.assertFalse(
            cros_util.is_ancestor_version('12347.0.0', '12346.0.0')
        )
        self.assertTrue(
            cros_util.is_ancestor_version('R12-12345.0.0', 'R10-12346.0.0')
        )
        self.assertFalse(
            cros_util.is_ancestor_version('R10-12346.0.0', 'R12-12345.0.0')
        )
        self.assertFalse(
            cros_util.is_ancestor_version(
                'R10-12345.0.0-3000', 'R10-12345.0.0-2000'
            )
        )

    def test_is_cros_snapshot_version(self):
        self.assertTrue(
            cros_util.is_cros_snapshot_version('R99-1234.0.0-23456')
        )

        self.assertFalse(cros_util.is_cros_snapshot_version('1234.0.0'))
        self.assertFalse(cros_util.is_cros_snapshot_version('R99-1234.0.0'))
        self.assertFalse(
            cros_util.is_cros_snapshot_version('1234.0.2018_01_01_1259')
        )
        self.assertFalse(cros_util.is_cros_snapshot_version('1234.0.0-23456'))

    def is_cros_or_snapshot_version(self):
        self.assertTrue(cros_util.is_cros_or_snapshot_version('1234.0.0'))
        self.assertTrue(cros_util.is_cros_or_snapshot_version('R99-1234.0.0'))
        self.assertTrue(
            cros_util.is_cros_or_snapshot_version('R99-1234.0.0-23456')
        )

        self.assertFalse(cros_util.is_cros_or_snapshot_version('1234.00.0'))
        self.assertFalse(cros_util.is_cros_or_snapshot_version('R99-1234.00.0'))
        self.assertFalse(cros_util.is_cros_or_snapshot_version('1234.0.0'))
        self.assertFalse(cros_util.is_cros_or_snapshot_version('R99-1234.0.0'))
        self.assertFalse(
            cros_util.is_cros_or_snapshot_version('1234.0.2018_01_01_1259')
        )
        self.assertFalse(
            cros_util.is_cros_or_snapshot_version('1234.0.0-23456')
        )

    def test_make_version_and_split(self):
        full_version = 'R33-1234.0.0'
        snapshot_version = 'R78-12345.3.4-3456'
        milestone, short_version = cros_util.version_split(full_version)

        self.assertEqual(milestone, '33')
        self.assertEqual(short_version, '1234.0.0')

        self.assertEqual(
            cros_util.make_cros_full_version(milestone, short_version),
            full_version,
        )

        snapshot_version = 'R78-12345.3.4-3456'
        (
            milestone,
            short_version,
            snapshot_id,
        ) = cros_util.snapshot_version_split(snapshot_version)

        self.assertEqual(milestone, '78')
        self.assertEqual(short_version, '12345.3.4')
        self.assertEqual(snapshot_id, '3456')
        self.assertEqual(
            cros_util.make_cros_snapshot_version(
                milestone, short_version, snapshot_id
            ),
            snapshot_version,
        )

    def test_argtype_cros_version(self):
        self.assertEqual(cros_util.argtype_cros_version('9876.0.0'), '9876.0.0')
        self.assertEqual(
            cros_util.argtype_cros_version('R99-9876.0.0'), 'R99-9876.0.0'
        )
        with self.assertRaises(errors.ArgTypeError):
            cros_util.argtype_cros_version('foobar')

    def test_lsb_release(self):
        with open(testing.get_testdata_path('lsb-release')) as f:
            sample = f.read()
        with mock.patch('bisect_kit.util.check_output', return_value=sample):
            dummy_dut = 'dummy'
            self.assertEqual(cros_util.query_dut_board(dummy_dut), 'samus')
            self.assertEqual(
                cros_util.query_dut_short_version(dummy_dut), '12345.0.0'
            )

    def test_os_release(self):
        with open(testing.get_testdata_path('os-release')) as f:
            sample = f.read()
        with mock.patch('bisect_kit.util.check_output', return_value=sample):
            dummy_dut = 'dummy'
            self.assertTrue(cros_util.is_dut(dummy_dut))

    def test_query_dut_prebuilt_version(self):
        dummy_dut = 'dummy'
        sample1 = {
            'CHROMEOS_RELEASE_VERSION': '13064.0.0',
            'CHROMEOS_RELEASE_BUILDER_PATH': 'nocturne-release/R84-13064.0.0',
        }
        sample2 = {
            'CHROMEOS_RELEASE_VERSION': '13064.0.0',
            'CHROMEOS_RELEASE_BUILDER_PATH': 'nocturne-release/R84-13064.0.0-12345',
        }
        sample3 = {
            'CHROMEOS_RELEASE_VERSION': '13059.0.0',
            'CHROMEOS_RELEASE_BUILDER_PATH': 'octopus-snapshot/R84-13059.0.0-31619-8882013661175561136',
        }
        sample4 = {
            'CHROMEOS_RELEASE_VERSION': '13059.0.0',
            'CHROMEOS_RELEASE_BUILDER_PATH': 'octopus-postsubmit/R84-13059.0.0-31619-8882013661175561136',
        }

        with mock.patch(
            'bisect_kit.cros_util.query_dut_lsb_release', return_value=sample1
        ):
            self.assertEqual(
                cros_util.query_dut_prebuilt_version(dummy_dut),
                '13064.0.0',
            )
        with mock.patch(
            'bisect_kit.cros_util.query_dut_lsb_release', return_value=sample2
        ):
            self.assertEqual(
                cros_util.query_dut_prebuilt_version(dummy_dut),
                '13064.0.0',
            )
        with mock.patch(
            'bisect_kit.cros_util.query_dut_lsb_release', return_value=sample3
        ):
            self.assertEqual(
                cros_util.query_dut_prebuilt_version(dummy_dut),
                'R84-13059.0.0-31619',
            )
        with mock.patch(
            'bisect_kit.cros_util.query_dut_lsb_release', return_value=sample4
        ):
            self.assertEqual(
                cros_util.query_dut_prebuilt_version(dummy_dut),
                'R84-13059.0.0-31619',
            )

    def test_query_prebuilt_gs_path(self):
        def fake_gs_util_ls(*args, **kwargs):
            logger.debug('fake_gs_util_ls %r %r', args, kwargs)
            if args in [
                (
                    '-d',
                    'gs://chromeos-image-archive/samus-release/R64-10123.0.0',
                ),
                (
                    '-d',
                    'gs://chromeos-image-archive/samus-release/R*-10123.0.0',
                ),
            ]:
                return [
                    'gs://chromeos-image-archive/samus-release/R64-10123.0.0/'
                ]
            if args in [
                (
                    '-d',
                    'gs://chromeos-image-archive/samus-factory/R64-10123.5.0',
                ),
                (
                    '-d',
                    'gs://chromeos-image-archive/samus-factory/R*-10123.5.0',
                ),
            ]:
                return [
                    'gs://chromeos-image-archive/samus-factory/R64-10123.5.0/'
                ]
            return []

        with mock.patch.object(gs_util, 'ls', fake_gs_util_ls):
            gs_path = cros_util.query_prebuilt_gs_path(
                'samus', 'R64-10123.0.0', is_public_build=False
            )
            self.assertEqual(
                gs_path,
                'gs://chromeos-image-archive/samus-release/R64-10123.0.0',
            )
            gs_path = cros_util.query_prebuilt_gs_path(
                'samus', '10123.0.0', is_public_build=False
            )
            self.assertEqual(
                gs_path,
                'gs://chromeos-image-archive/samus-release/R64-10123.0.0',
            )

            gs_path = cros_util.query_prebuilt_gs_path(
                'samus', 'R64-10123.5.0', is_public_build=False
            )
            self.assertEqual(
                gs_path,
                'gs://chromeos-image-archive/samus-factory/R64-10123.5.0',
            )
            gs_path = cros_util.query_prebuilt_gs_path(
                'samus', '10123.5.0', is_public_build=False
            )
            self.assertEqual(
                gs_path,
                'gs://chromeos-image-archive/samus-factory/R64-10123.5.0',
            )

            gs_path = cros_util.query_prebuilt_gs_path(
                'foo', 'R64-10123.5.0', is_public_build=False
            )
            self.assertEqual(gs_path, '')
            gs_path = cros_util.query_prebuilt_gs_path(
                'samus', '10123.6.0', is_public_build=False
            )
            self.assertEqual(gs_path, '')

    def test_has_release_prebuilt(self):
        def fake_gs_util_ls(*args, **kwargs):
            logger.debug('fake_gs_util_ls %r %r', args, kwargs)
            if args in [
                (
                    '-d',
                    'gs://chromeos-image-archive/samus-release/R64-10123.0.0',
                ),
                (
                    '-d',
                    'gs://chromeos-image-archive/samus-release/R*-10123.0.0',
                ),
            ]:
                return [
                    'gs://chromeos-image-archive/samus-release/R64-10123.0.0/'
                ]
            return []

        with mock.patch.object(gs_util, 'ls', fake_gs_util_ls):
            self.assertTrue(
                cros_util.has_release_prebuilt(
                    'samus', 'R64-10123.0.0', is_public_build=False
                )
            )
            self.assertTrue(
                cros_util.has_release_prebuilt(
                    'samus', '10123.0.0', is_public_build=False
                )
            )

            self.assertFalse(
                cros_util.has_release_prebuilt(
                    'samus', 'R65-10123.0.0', is_public_build=False
                )
            )
            self.assertFalse(
                cros_util.has_release_prebuilt(
                    'samus', 'R64-10123.5.0', is_public_build=False
                )
            )
            self.assertFalse(
                cros_util.has_release_prebuilt(
                    'samus', '10123.5.0', is_public_build=False
                )
            )

    def test_query_milestone_by_version(self):
        # Assume query_milestone_by_version is implemented via gsutil.
        def fake_gs_util_ls(*args, **kwargs):
            logger.debug('fake_gs_util_ls %r %r', args, kwargs)
            if args == (
                '-d',
                'gs://chromeos-image-archive/samus-release/R*-10123.0.0',
            ):
                return [
                    'gs://chromeos-image-archive/samus-release/R64-10123.0.0/'
                ]
            if args == (
                '-d',
                'gs://chromeos-image-archive/samus-factory/R*-10123.5.0',
            ):
                return [
                    'gs://chromeos-image-archive/samus-factory/R64-10123.5.0/'
                ]
            return []

        # First case, recent image, still exists in gs://chromeos-image-archive
        with mock.patch.object(gs_util, 'ls', fake_gs_util_ls):
            self.assertEqual(
                cros_util.query_milestone_by_version('samus', '10123.0.0'), '64'
            )
            self.assertEqual(
                cros_util.query_milestone_by_version('samus', '10123.5.0'), '64'
            )

    def test_version_operation(self):
        with mock.patch.object(
            cros_util, 'query_milestone_by_version', return_value='64'
        ):
            self.assertEqual(
                cros_util.version_to_short('R64-10123.0.0'), '10123.0.0'
            )
            self.assertEqual(
                cros_util.version_to_short('10123.0.0'), '10123.0.0'
            )
            self.assertEqual(
                cros_util.version_to_short('R64-10123.0.0-45678'), '10123.0.0'
            )

            self.assertEqual(
                cros_util.extract_major_version('R64-10123.0.0'), '10123'
            )
            self.assertEqual(
                cros_util.extract_major_version('10123.0.0'), '10123'
            )
            self.assertEqual(
                cros_util.extract_major_version('R64-10123.0.0-45678'), '10123'
            )
            self.assertEqual(
                cros_util.version_to_full('samus', 'R64-10123.0.0'),
                'R64-10123.0.0',
            )
            self.assertEqual(
                cros_util.version_to_full('samus', '10123.0.0'), 'R64-10123.0.0'
            )
            self.assertEqual(
                cros_util.version_to_full('samus', 'R64-10123.0.0-12345'),
                'R64-10123.0.0',
            )

    # pylint: disable=protected-access
    @mock.patch('bisect_kit.gs_util.ls')
    def test_find_files_from_image_archive(self, mock_gs_ls):
        def fake_gs_util_ls(path, ignore_errors=False):
            gs_files = [
                'gs://chromeos-image-archive/foo-release/R63-9995.0.0/image.zip',
                'gs://chromeos-image-archive/foo-release/R63-9997.0.0/image.zip',
                'gs://chromeos-image-archive/foo-release/R63-9997.0.1/image.zip',
                'gs://chromeos-image-archive/foo-factory/R63-9998.0.0/image.zip',
                'gs://chromeos-image-archive/foo-release/R63-9999.0.0/image.zip',
                'gs://chromeos-image-archive/foo-release/R63-9999.0.1/image.zip',
                'gs://chromeos-image-archive/foo-snapshot/R63-9996.0.0-30102-000/image.zip',
                'gs://chromeos-image-archive/foo-snapshot/R63-9996.0.0-30103-001/image.zip',
                'gs://chromeos-image-archive/foo-snapshot/R63-9996.0.0-30103-002/image.zip',
                'gs://chromeos-image-archive/foo-snapshot/R63-9996.0.0-30103-003/image.zip',
                'gs://chromeos-image-archive/foo-snapshot/R63-9996.0.0-30104-000/image.zip',
                'gs://chromeos-image-archive/foo-snapshot/R63-9996.0.0-30105-000/image.zip',
                'gs://chromeos-image-archive/foo-snapshot/R63-9993.0.0-20105-000/image.zip',
                'gs://chromiumos-image-archive/foo-public/R63-9991.0.0/image.zip',
                'gs://chromiumos-image-archive/foo-public-snapshot/R63-9993.0.0-20103-000/image.zip',
                'gs://chromiumos-image-archive/foo-public-snapshot/R63-9994.0.0-20104-000/image.zip',
                'gs://chromiumos-image-archive/foo-public-snapshot/R63-9995.0.0-20105-000/image.zip',
                # Unsupported paths
                'gs://chromeos-image-archive/foo-public/R63-9991.0.0/image.zip',
                'gs://chromeos-image-archive/foo-release-main/R63-9997.0.0/image.zip',
                'gs://chromiumos-image-archive/foo-snapshot/R63-9990.0.0-20100-000/image.zip',
                'gs://chromiumos-image-archive/foo-public-main/R63-9991.0.0/image.zip',
                'gs://chromeos-image-archive/foo-snapshot/R63-9996.0.0a-30103-002/image.zip',
            ]
            result = [p for p in gs_files if fnmatch.fnmatchcase(p, path)]
            random.seed(a=20250919, version=2)
            random.shuffle(result)
            return result

        mock_gs_ls.side_effect = fake_gs_util_ls

        class MockAsyncResult:
            """Mock for multiprocessing.pool.AsyncResult."""

            def __init__(self, result):
                self._result = result

            def get(self, timeout=None):
                return self._result

        class MockPool:
            """Mock for multiprocessing.Pool."""

            def apply_async(self, func, args, kwargs):
                return MockAsyncResult(func(*args, **kwargs))

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_val, exc_tb):
                pass

        with mock.patch('multiprocessing.Pool', MockPool):
            # Test case 1: non-snapshot, private build
            result = cros_util._find_files_from_image_archive(
                board='foo',
                filenames=['image.zip'],
                old='R63-9995.0.0',
                new='R63-9999.0.0',
                is_public_build=False,
                is_snapshot=False,
            )
            self.assertEqual(
                sorted(result),
                sorted(
                    [
                        (
                            'R63-9995.0.0',
                            'gs://chromeos-image-archive/foo-release/R63-9995.0.0/image.zip',
                        ),
                        (
                            'R63-9997.0.0',
                            'gs://chromeos-image-archive/foo-release/R63-9997.0.0/image.zip',
                        ),
                        # 'R63-9997.0.1' (Non-'Rxxx-xxx-0.0`) is ignored.
                        (
                            'R63-9998.0.0',
                            'gs://chromeos-image-archive/foo-factory/R63-9998.0.0/image.zip',
                        ),
                        (
                            'R63-9999.0.0',
                            'gs://chromeos-image-archive/foo-release/R63-9999.0.0/image.zip',
                        ),
                    ]
                ),
            )

            # Test case 2: snapshot, private build
            result = cros_util._find_files_from_image_archive(
                board='foo',
                filenames=['image.zip'],
                old='R63-9996.0.0-30103',
                new='R63-9996.0.0-30104',
                is_public_build=False,
                is_snapshot=True,
            )
            self.assertEqual(
                sorted(result),
                sorted(
                    [
                        (
                            'R63-9996.0.0-30103',
                            'gs://chromeos-image-archive/foo-snapshot/R63-9996.0.0-30103-001/image.zip',
                        ),
                        (
                            'R63-9996.0.0-30103',
                            'gs://chromeos-image-archive/foo-snapshot/R63-9996.0.0-30103-002/image.zip',
                        ),
                        (
                            'R63-9996.0.0-30103',
                            'gs://chromeos-image-archive/foo-snapshot/R63-9996.0.0-30103-003/image.zip',
                        ),
                        (
                            'R63-9996.0.0-30104',
                            'gs://chromeos-image-archive/foo-snapshot/R63-9996.0.0-30104-000/image.zip',
                        ),
                    ]
                ),
            )

            # Test case 3: snapshot, public build
            result = cros_util._find_files_from_image_archive(
                board='foo',
                filenames=['image.zip'],
                old='R63-9994.0.0',
                new='R63-9995.0.0',
                is_public_build=True,
                is_snapshot=True,
            )
            self.assertEqual(
                sorted(result),
                sorted(
                    [
                        (
                            'R63-9994.0.0-20104',
                            'gs://chromiumos-image-archive/foo-public-snapshot/R63-9994.0.0-20104-000/image.zip',
                        ),
                        # 'R63-9995.0.0-XXXX' is not matched since it's older than 'R63-9995.0.0'.
                    ]
                ),
            )

            # Test case 4: non-snapshot, public build
            result = cros_util._find_files_from_image_archive(
                board='foo',
                filenames=['image.zip'],
                old='R63-9990.0.0',
                new='R63-9999.0.0',
                is_public_build=True,
                is_snapshot=False,
            )
            self.assertEqual(
                sorted(result),
                sorted(
                    [
                        (
                            'R63-9991.0.0',
                            'gs://chromiumos-image-archive/foo-public/R63-9991.0.0/image.zip',
                        ),
                    ]
                ),
            )

            # Test case 5: no results
            result = cros_util._find_files_from_image_archive(
                board='foo',
                filenames=['image.zip'],
                old='R63-8000.0.0',
                new='R63-8009.0.0',
                is_public_build=True,
                is_snapshot=True,
            )
            self.assertEqual(
                result,
                [],
            )

    @mock.patch('bisect_kit.gs_util.ls')
    def test_query_version_info(self, mock_gs_util_ls):
        mock_gs_util_ls.return_value = ['fake_path']

        def create_fake_gsutil(metadata, partial_metadata, build_report):
            def fake_gsutil(*args, **unused_kargs):
                if args[0] == 'cat':
                    if args[1].endswith('/partial-metadata.json'):
                        return partial_metadata
                    if args[1].endswith('/metadata.json'):
                        return metadata
                    if args[1].endswith('/build_report.json'):
                        return build_report
                return ''

            return fake_gsutil

        with open(
            testing.get_testdata_path(
                'version_info_files/1/partial-metadata.json'
            )
        ) as f:
            partial_metadata = f.read()
        with mock.patch.object(
            gs_util,
            '_gsutil',
            side_effect=create_fake_gsutil('', partial_metadata, ''),
        ), mock.patch.object(
            cros_util,
            'fetch_buildbucket_version_info',
            return_value=cros_util.VersionInfo(),
        ):
            info = cros_util.query_version_info(
                'caroline', 'R89-13664.0.0', is_public_build=False
            )
            self.assertEqual(
                cros_util.VersionInfo(
                    buildbucket_id=8860981718532549296,
                    cros_short_version='13664.0.0',
                    cros_full_version='R89-13664.0.0',
                    milestone='89',
                    cr_version='89.0.4351.0',
                    android_build_id='7026951',
                    android_branch='git_pi-arc',
                    android_target='cheets',
                ),
                info,
            )

        with open(
            testing.get_testdata_path(
                'version_info_files/2/partial-metadata.json'
            )
        ) as f:
            partial_metadata = f.read()
        with open(
            testing.get_testdata_path('version_info_files/2/metadata.json')
        ) as f:
            metadata = f.read()
        with open(
            testing.get_testdata_path('version_info_files/2/build_report.json')
        ) as f:
            build_report = f.read()

        with mock.patch.object(
            gs_util,
            '_gsutil',
            side_effect=create_fake_gsutil(
                metadata, partial_metadata, build_report
            ),
        ), mock.patch.object(
            cros_util,
            'fetch_buildbucket_version_info',
            return_value=cros_util.VersionInfo(),
        ):
            info = cros_util.query_version_info(
                'stronbad', 'R115-15452.0.0', is_public_build=False
            )
            self.assertEqual(
                cros_util.VersionInfo(
                    buildbucket_id=8781974877551968897,
                    cros_short_version='15452.0.0',
                    cros_full_version='R115-15452.0.0',
                    milestone='115',
                    cr_version='115.0.5751.0',
                    # android_branch is missied
                    android_branch='git_rvc-arc',
                    android_target='bertha',
                ),
                info,
            )

    @mock.patch('bisect_kit.gs_util.ls', _fake_gs_util_ls)
    @mock.patch('bisect_kit.cros_util.version_to_full', _fake_version_to_full)
    @mock.patch(
        'bisect_kit.cros_util.query_milestone_by_version',
        _fake_query_milestone_by_version,
    )
    def test_list_chromeos_prebuilt_versions(self):
        # Both ends are in gs://chromeos-image-archive
        self.assertEqual(
            cros_util.list_chromeos_prebuilt_versions(
                'foo', '9998.0.0', '10001.0.0', is_public_build=False
            ),
            (
                [
                    'R63-9998.0.0',
                    'R63-9999.0.0',
                    'R63-10000.0.0',
                    'R63-10001.0.0',
                ],
                {
                    'R63-9998.0.0': {
                        'images': {
                            'DISK_IMAGE': 'gs://chromeos-image-archive/foo-release/R63-9998.0.0/chromiumos_test_image.tar.xz'
                        }
                    },
                    'R63-9999.0.0': {
                        'images': {
                            'DISK_IMAGE': 'gs://chromeos-image-archive/foo-release/R63-9999.0.0/chromiumos_test_image.tar.xz',
                            'ZIP_FILE': 'gs://chromeos-image-archive/foo-release/R63-9999.0.0/image.zip',
                        }
                    },
                    'R63-10000.0.0': {
                        'images': {
                            'DISK_IMAGE': 'gs://chromeos-image-archive/foo-release/R63-10000.0.0/chromiumos_test_image.tar.xz'
                        }
                    },
                    'R63-10001.0.0': {
                        'images': {
                            'DISK_IMAGE': 'gs://chromeos-image-archive/foo-release/R63-10001.0.0/chromiumos_test_image.tar.xz'
                        }
                    },
                },
            ),
        )

    @mock.patch('bisect_kit.cros_util.snapshot_cutover_version', '0.0.0')
    @mock.patch('bisect_kit.gs_util.ls', _fake_gs_util_ls)
    @mock.patch('bisect_kit.cros_util.version_to_full', _fake_version_to_full)
    @mock.patch(
        'bisect_kit.cros_util.query_milestone_by_version',
        _fake_query_milestone_by_version,
    )
    def test_list_chromeos_prebuilt_versions_with_snapshots(self):
        # snapshot 1: release ~ release
        self.assertEqual(
            cros_util.list_chromeos_prebuilt_versions(
                'foo',
                'R63-9995.0.0',
                'R63-9997.0.0',
                is_public_build=False,
                use_snapshot=True,
            ),
            (
                [
                    'R63-9995.0.0',
                    'R63-9996.0.0-30103',
                    'R63-9997.0.0',
                ],
                {
                    'R63-9995.0.0': {
                        'images': {
                            'DISK_IMAGE': 'gs://chromeos-image-archive/foo-release/R63-9995.0.0/chromiumos_test_image.tar.xz',
                            'ZIP_FILE': 'gs://chromeos-image-archive/foo-release/R63-9995.0.0/image.zip',
                        }
                    },
                    'R63-9996.0.0-30103': {
                        'images': {
                            'PARTITION_IMAGE': 'gs://chromeos-image-archive/foo-snapshot/R63-9996.0.0-30103-000/full_dev_part_KERN.bin.gz',
                            'ZIP_FILE': 'gs://chromeos-image-archive/foo-snapshot/R63-9996.0.0-30103-000/image.zip',
                        }
                    },
                    'R63-9997.0.0': {
                        'images': {
                            'DISK_IMAGE': 'gs://chromeos-image-archive/foo-release/R63-9997.0.0/chromiumos_test_image.tar.xz',
                            'ZIP_FILE': 'gs://chromeos-image-archive/foo-release/R63-9997.0.0/image.zip',
                        }
                    },
                },
            ),
        )

        self.assertEqual(
            cros_util.list_chromeos_prebuilt_versions(
                'foo',
                'R63-9996.0.0',
                'R63-9998.0.0',
                is_public_build=False,
                use_snapshot=True,
            )[0],
            [
                'R63-9996.0.0-30103',
                'R63-9997.0.0',
                'R63-9997.0.0-30104',
                'R63-9998.0.0',
            ],
        )

        # snapshot 2: release ~ snapshot or snapshot ~ release
        self.assertEqual(
            cros_util.list_chromeos_prebuilt_versions(
                'foo',
                'R63-9995.0.0-30100',
                'R63-9997.0.0',
                is_public_build=False,
                use_snapshot=True,
            )[0],
            ['R63-9996.0.0-30103', 'R63-9997.0.0'],
        )

        self.assertEqual(
            cros_util.list_chromeos_prebuilt_versions(
                'foo',
                'R63-9995.0.0-30103',
                'R63-9997.0.0',
                is_public_build=False,
                use_snapshot=True,
            )[0],
            ['R63-9996.0.0-30103', 'R63-9997.0.0'],
        )

        self.assertEqual(
            cros_util.list_chromeos_prebuilt_versions(
                'foo',
                'R63-9995.0.0',
                'R63-9997.0.0-30104',
                is_public_build=False,
                use_snapshot=True,
            )[0],
            [
                'R63-9995.0.0',
                'R63-9996.0.0-30103',
                'R63-9997.0.0',
                'R63-9997.0.0-30104',
            ],
        )

        # snapshot 3: branch case
        self.assertEqual(
            cros_util.list_chromeos_prebuilt_versions(
                'foo',
                'R63-9998.0.0',
                'R63-9999.1.0',
                is_public_build=False,
                use_snapshot=True,
            )[0],
            [
                'R63-9998.0.0',
                'R63-9998.0.0-30105',
                'R63-9999.0.0',
                'R63-9999.0.0-30106',
                'R63-9999.1.0',
            ],
        )

        self.assertEqual(
            cros_util.list_chromeos_prebuilt_versions(
                'foo',
                'R63-9998.0.0',
                'R63-10000.0.0',
                is_public_build=False,
                use_snapshot=True,
            )[0],
            [
                'R63-9998.0.0',
                'R63-9998.0.0-30105',
                'R63-9999.0.0',
                'R63-9999.0.0-30106',
                'R63-10000.0.0',
            ],
        )

    def test_extract_info_from_prebuilt_gs_path(self):
        info = cros_util.extract_info_from_prebuilt_gs_path(
            'gs://chromeos-image-archive/foo-release/R63-10001.0.0/image.zip'
        )
        self.assertDictEqual(
            info,
            {
                'gcp_bucket': 'chromeos-image-archive',
                'board': 'foo',
                'bucket': 'release',
                'version': 'R63-10001.0.0',
            },
        )

        info = cros_util.extract_info_from_prebuilt_gs_path(
            'gs://chromeos-image-archive/bar-factory/R71-10082.30.0/chromiumos_test_image.tar.xz'
        )
        self.assertDictEqual(
            info,
            {
                'gcp_bucket': 'chromeos-image-archive',
                'board': 'bar',
                'bucket': 'factory',
                'version': 'R71-10082.30.0',
            },
        )

        info = cros_util.extract_info_from_prebuilt_gs_path(
            'gs://chromeos-image-archive/bar-factory/R71-10082.30.0'
        )
        self.assertDictEqual(info, {})

        info = cros_util.extract_info_from_prebuilt_gs_path(
            'gs://chromiumos-image-archive/hoge-public/R141-12345.30.0/chromiumos_test_image.tar.xz'
        )
        self.assertDictEqual(
            info,
            {
                'gcp_bucket': 'chromiumos-image-archive',
                'board': 'hoge',
                'bucket': 'public',
                'version': 'R141-12345.30.0',
            },
        )

        info = cros_util.extract_info_from_prebuilt_gs_path(
            'gs://chromeos-image-archive/hoge-public/R141-12345.30.0'
        )
        self.assertDictEqual(info, {})

        info = cros_util.extract_info_from_prebuilt_gs_path(
            'gs://chromeos-image-archive/betty-arc-t-snapshot/R141-12345.30.0-12345-1234567/image.zip'
        )
        self.assertDictEqual(
            info,
            {
                'gcp_bucket': 'chromeos-image-archive',
                'board': 'betty-arc-t',
                'bucket': 'snapshot',
                'version': 'R141-12345.30.0-12345-1234567',
            },
        )

        info = cros_util.extract_info_from_prebuilt_gs_path(
            'gs://chromiumos-image-archive/amd64-generic-public-snapshot/R141-12345.30.0-12345-1234567/image.zip'
        )
        self.assertDictEqual(
            info,
            {
                'gcp_bucket': 'chromiumos-image-archive',
                'board': 'amd64-generic',
                'bucket': 'public-snapshot',
                'version': 'R141-12345.30.0-12345-1234567',
            },
        )

    def test_is_cros_version_lesseq(self):
        self.assertTrue(
            cros_util.is_cros_version_lesseq('R99-1.0.0', 'R99-1.0.0')
        )
        self.assertTrue(
            cros_util.is_cros_version_lesseq('R99-1.0.0', 'R99-1.1.0')
        )
        self.assertTrue(
            cros_util.is_cros_version_lesseq('R99-1.0.0', 'R99-1.0.1')
        )
        self.assertTrue(
            cros_util.is_cros_version_lesseq('R99-1.0.0', 'R98-1.1.0')
        )
        self.assertTrue(
            cros_util.is_cros_version_lesseq('R99-1.0.0', 'R98-1.0.1')
        )
        self.assertFalse(
            cros_util.is_cros_version_lesseq('R98-1.0.1', 'R99-1.0.0')
        )
        self.assertTrue(
            cros_util.is_cros_version_lesseq('R99-1.0.0', 'R99-1.0.0-123')
        )
        self.assertTrue(
            cros_util.is_cros_version_lesseq('R99-1.0.0-122', 'R99-1.0.0-123')
        )
        self.assertFalse(
            cros_util.is_cros_version_lesseq('R99-1.0.0-124', 'R99-1.0.0-123')
        )

        # equal to
        self.assertTrue(
            cros_util.is_cros_version_lesseq('10000.0.0', '10000.0.0')
        )
        self.assertTrue(
            cros_util.is_cros_version_lesseq(
                'R100-10000.0.0-1000', 'R100-10000.0.0-1000'
            )
        )

        # less than
        self.assertTrue(
            cros_util.is_cros_version_lesseq('10000.0.0', '20000.0.0')
        )
        self.assertTrue(
            cros_util.is_cros_version_lesseq('10000.0.0', '10000.2.0')
        )
        self.assertTrue(
            cros_util.is_cros_version_lesseq('10000.0.0', '10000.0.2')
        )
        self.assertTrue(
            cros_util.is_cros_version_lesseq('10000.0.0', 'R100-10000.0.0-1000')
        )
        self.assertTrue(
            cros_util.is_cros_version_lesseq(
                'R100-10000.0.0-1000', 'R100-10000.0.0-2000'
            )
        )

        # greater than
        self.assertFalse(
            cros_util.is_cros_version_lesseq('20000.0.0', '10000.0.0')
        )
        self.assertFalse(
            cros_util.is_cros_version_lesseq('10000.2.0', '10000.0.0')
        )
        self.assertFalse(
            cros_util.is_cros_version_lesseq('10000.0.2', '10000.0.0')
        )
        self.assertFalse(
            cros_util.is_cros_version_lesseq('R100-10000.0.0-1000', '10000.0.0')
        )
        self.assertFalse(
            cros_util.is_cros_version_lesseq(
                'R100-10000.0.0-2000', 'R100-10000.0.0-1000'
            )
        )

        # milestone should be ignored
        self.assertTrue(
            cros_util.is_cros_version_lesseq('R100-10000.0.0', 'R100-10000.0.0')
        )
        self.assertTrue(
            cros_util.is_cros_version_lesseq('R100-10000.0.0', 'R200-10000.0.0')
        )
        self.assertTrue(
            cros_util.is_cros_version_lesseq('R200-10000.0.0', 'R100-10000.0.0')
        )
        self.assertTrue(
            cros_util.is_cros_version_lesseq('R200-10000.0.0', 'R100-20000.0.0')
        )
        self.assertTrue(
            cros_util.is_cros_version_lesseq(
                'R200-10000.0.0', 'R100-10000.0.0-1000'
            )
        )
        self.assertTrue(
            cros_util.is_cros_version_lesseq(
                'R200-10000.0.0-1000', 'R100-10000.0.0-2000'
            )
        )

    def test_is_buildbucket_buildable(self):
        sample_cutover = ['10000.0.0', '9999.20.0', '9998.20.0']
        with mock.patch.object(
            cros_util, 'buildbucket_cutover_versions', sample_cutover
        ):
            self.assertTrue(
                cros_util.is_buildbucket_buildable('R77-10000.0.0-12345')
            )
            self.assertTrue(cros_util.is_buildbucket_buildable('10000.0.0'))
            self.assertFalse(
                cros_util.is_buildbucket_buildable('R77-9999.0.0-12345')
            )
            self.assertFalse(cros_util.is_buildbucket_buildable('9999.19.0'))
            self.assertTrue(cros_util.is_buildbucket_buildable('9999.20.0'))
            self.assertTrue(cros_util.is_buildbucket_buildable('9998.100.100'))
            self.assertFalse(cros_util.is_buildbucket_buildable('9997.100.100'))

    def test_parse_chromeos_overlays(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            overlays_dir = os.path.join(temp_dir, 'src', 'overlays')
            private_overlays_dir = os.path.join(
                temp_dir, 'src', 'private-overlays'
            )
            os.makedirs(overlays_dir)
            os.makedirs(private_overlays_dir)

            # Overlay 1: board1
            board1_dir = os.path.join(overlays_dir, 'overlay-board1')
            os.makedirs(os.path.join(board1_dir, 'metadata'))
            with open(
                os.path.join(board1_dir, 'metadata', 'layout.conf'), 'w'
            ) as f:
                f.write('repo-name = board1\n')
                f.write('masters = portage-stable chromiumos eclass-overlay\n')

            # Overlay 2: board2 in private
            board2_dir = os.path.join(private_overlays_dir, 'overlay-board2')
            os.makedirs(os.path.join(board2_dir, 'metadata'))
            with open(
                os.path.join(board2_dir, 'metadata', 'layout.conf'), 'w'
            ) as f:
                f.write('repo-name = board2\n')
                f.write('masters = board1\n')

            # Overlay 3: board3 (using profiles/repo_name)
            board3_dir = os.path.join(overlays_dir, 'overlay-board3')
            os.makedirs(os.path.join(board3_dir, 'profiles'))
            with open(
                os.path.join(board3_dir, 'profiles', 'repo_name'), 'w'
            ) as f:
                f.write('board3\n')
            os.makedirs(os.path.join(board3_dir, 'metadata'))
            with open(
                os.path.join(board3_dir, 'metadata', 'layout.conf'), 'w'
            ) as f:
                f.write('masters = board2\n')

            overlays = cros_util.parse_chromeos_overlays(temp_dir)

            expected = {
                'board1': ['portage-stable', 'chromiumos', 'eclass-overlay'],
                'board2': ['board1'],
                'board3': ['board2'],
            }
            self.assertEqual(overlays, expected)

    def test_resolve_basic_boards(self):
        overlays = {
            'foo': ['chromiumos'],
            'foo-private': ['chromiumos'],
            'foo-kernelnext-private': ['chromiumos', 'foo-private'],
            'foo64-private': ['chromiumos', 'foo-private'],
            'bar-private': ['foo-private', 'chromiumos'],
            'bar-kernelnext': ['chromiumos', 'bar-private'],
            'bar64': ['chromiumos', 'bar-private'],
            # Special case (see the method implementation):
            'nirva': ['nissa'],
        }
        self.assertDictEqual(
            cros_util.resolve_basic_boards(overlays),
            {
                'foo': 'foo',
                'foo-kernelnext': 'foo',
                'foo64': 'foo',
                'bar': 'foo',
                'bar-kernelnext': 'foo',
                'bar64': 'foo',
                # Special case (see the method implementation):
                'nirva': 'nirva',
            },
        )

    def test_detect_branch_level(self):
        ref_prefix = [
            '',
            'refs/head/',
            'refs/remotes/cros/',
            'refs/remotes/origin/',
        ]
        tests: list[tuple[str, int]] = [
            ('master', 1),
            ('release-R64-10176.B', 2),
            ('release-R56-9000.B', 2),
            ('stabilize-3881.0.B', 2),
            ('firmware-octopus-11297.83.B', 3),
            ('stabilize-10323.67.B', 3),
            ('stabilize-9000.87.0.B', 3),
            ('factory-atlas-11907.B-master', 0),
        ]

        for prefix in ref_prefix:
            for test in tests:
                self.assertEqual(
                    cros_util.detect_branch_level(prefix + test[0]), test[1]
                )

    def test_get_crosland_link(self):
        template = cros_util.CROSLAND_URL_TEMPLATE
        self.assertEqual(
            cros_util.get_crosland_link(
                'R82-12872.0.0-27636', 'R82-12872.0.0-27637'
            ),
            template % ('27636', '27637'),
        )
        self.assertEqual(
            cros_util.get_crosland_link('R82-12872.0.0', 'R82-12872.0.0-27637'),
            template % ('12872.0.0', '27637'),
        )
        self.assertEqual(
            cros_util.get_crosland_link('12872.0.0', 'R82-12872.0.0-27637'),
            template % ('12872.0.0', '27637'),
        )
        self.assertEqual(
            cros_util.get_crosland_link('R82-12872.0.0-27637', 'R82-12873.0.0'),
            template % ('27637', '12873.0.0'),
        )
        self.assertEqual(
            cros_util.get_crosland_link('R82-12872.0.0-27637', '12873.0.0'),
            template % ('27637', '12873.0.0'),
        )

    def test_convert_path_outside_chroot(self):
        with mock.patch.dict(os.environ, {'USER': 'foouser'}):
            self.assertEqual(
                cros_util.convert_path_outside_chroot(
                    '/path/to/root', '/path/to/file'
                ),
                '/path/to/root/chroot/path/to/file',
            )
            self.assertEqual(
                cros_util.convert_path_outside_chroot(
                    '/path/to/root', '~/path/to/file'
                ),
                '/path/to/root/out/home/foouser/path/to/file',
            )

    def test_parse_snapshot_path(self):
        self.assertDictEqual(
            cros_util.SnapshotStore.parse_snapshot_path(
                'gs://my-bucket/hatch-snapshot/R96-14171.0.0-53037-123456789/image.zip'
            ),
            {
                'board': 'hatch',
                'bucket': 'snapshot',
                'snapshot_version': 'R96-14171.0.0-53037',
                'snapshot_id': '53037',
                'buildbucket_id': '123456789',
                'filename': 'image.zip',
            },
        )

        self.assertDictEqual(
            cros_util.SnapshotStore.parse_snapshot_path(
                'gs://my-bucket/eve-kernelnext-release/R96-14171.0.0-53037-123456789/full_dev_part_KERN.bin.gz'
            ),
            {
                'board': 'eve-kernelnext',
                'bucket': 'release',
                'snapshot_version': 'R96-14171.0.0-53037',
                'snapshot_id': '53037',
                'buildbucket_id': '123456789',
                'filename': 'full_dev_part_KERN.bin.gz',
            },
        )

        self.assertDictEqual(
            cros_util.SnapshotStore.parse_snapshot_path(
                'gs://my-bucket/octopus-public-snapshot/R96-14171.0.0-53037-123456789/image.zip'
            ),
            {
                'board': 'octopus',
                'bucket': 'public-snapshot',
                'snapshot_version': 'R96-14171.0.0-53037',
                'snapshot_id': '53037',
                'buildbucket_id': '123456789',
                'filename': 'image.zip',
            },
        )

        self.assertDictEqual(
            cros_util.SnapshotStore.parse_snapshot_path(
                'gs://my-bucket/amd64-generic-public-snapshot/R96-14171.0.0-53037-123456789/image.zip'
            ),
            {
                'board': 'amd64-generic',
                'bucket': 'public-snapshot',
                'snapshot_version': 'R96-14171.0.0-53037',
                'snapshot_id': '53037',
                'buildbucket_id': '123456789',
                'filename': 'image.zip',
            },
        )

    @mock.patch('os.makedirs')
    @mock.patch('bisect_kit.git_util.is_ancestor_commit')
    @mock.patch('shutil.move')
    @mock.patch('bisect_kit.util.check_call')
    @mock.patch('bisect_kit.gs_util.cp')
    @mock.patch('tempfile.TemporaryDirectory')
    @mock.patch('os.path.exists')
    @mock.patch('bisect_kit.cros_util.extract_info_from_prebuilt_gs_path')
    def test_prepare_image_for_cros_flash(
        self,
        mock_extract,
        mock_exists,
        mock_tempdir,
        mock_cp,
        mock_check_call,
        mock_move,
        mock_is_ancestor,
        mock_makedirs,
    ):
        chromeos_root = '/path/to/root'
        image_info = cros_util.ImageInfo()

        with self.subTest('disk_image_release_bucket'):
            mock_extract.return_value = {
                'board': 'eve',
                'bucket': 'release',
                'version': 'R1-1.2.3',
            }
            image_info.clear()
            image_info[cros_util.ImageType.DISK_IMAGE] = (
                'gs://chromeos-image-archive/eve-release/R1-1.2.3/chromiumos_test_image.tar.xz'
            )
            result = cros_util.prepare_image_for_cros_flash(
                chromeos_root, image_info, is_public_build=False
            )
            self.assertEqual(result, 'xbuddy://remote/eve/R1-1.2.3/test')

        with self.subTest('disk_image_public_bucket'):
            mock_extract.return_value = {
                'board': 'eve',
                'bucket': 'public',
                'version': 'R1-1.2.3',
            }
            image_info.clear()
            image_info[cros_util.ImageType.DISK_IMAGE] = (
                'gs://chromiumos-image-archive/eve-public/R1-1.2.3/chromiumos_test_image.tar.xz'
            )
            result = cros_util.prepare_image_for_cros_flash(
                chromeos_root, image_info, is_public_build=True
            )
            self.assertEqual(
                result,
                'gs://chromiumos-image-archive/eve-public/R1-1.2.3',
            )

        with self.subTest('disk_image_public_snapshot_bucket'):
            mock_extract.return_value = {
                'board': 'amd64-generic',
                'bucket': 'public-snapshot',
                'version': 'R1-1.2.3-456-7890',
            }
            image_info.clear()
            image_info[cros_util.ImageType.DISK_IMAGE] = (
                'gs://chromiumos-image-archive/amd64-generic-public-snapshot/R1-1.2.3-456-7890/chromiumos_test_image.tar.xz'
            )
            result = cros_util.prepare_image_for_cros_flash(
                chromeos_root, image_info, is_public_build=True
            )
            self.assertEqual(
                result,
                'gs://chromiumos-image-archive/amd64-generic-public-snapshot/R1-1.2.3-456-7890',
            )

        with self.subTest('disk_image_snapshot_bucket'):
            mock_extract.return_value = {
                'board': 'betty-arc-t',
                'bucket': 'snapshot',
                'version': 'R1-1.2.3-456-7890',
            }
            image_info.clear()
            image_info[cros_util.ImageType.DISK_IMAGE] = (
                'gs://chromeos-image-archive/betty-arc-t-snapshot/R1-1.2.3-456-7890/chromiumos_test_image.tar.xz'
            )
            result = cros_util.prepare_image_for_cros_flash(
                chromeos_root, image_info, is_public_build=True
            )
            self.assertEqual(
                result,
                'gs://chromeos-image-archive/betty-arc-t-snapshot/R1-1.2.3-456-7890',
            )

        with self.subTest('partition_image_new_chromite'):
            mock_is_ancestor.return_value = True
            image_info.clear()
            image_info[cros_util.ImageType.PARTITION_IMAGE] = (
                'gs://chromeos-image-archive/eve-release/R1-1.2.3/full_dev_part_KERN.bin.gz'
            )
            result = cros_util.prepare_image_for_cros_flash(
                chromeos_root, image_info, is_public_build=False
            )
            self.assertEqual(
                result,
                'gs://chromeos-image-archive/eve-release/R1-1.2.3/full_dev_part_KERN.bin.gz',
            )

        with self.subTest('partition_image_old_chromite'):
            mock_is_ancestor.return_value = False
            image_info.clear()
            image_info[cros_util.ImageType.PARTITION_IMAGE] = (
                'gs://chromeos-image-archive/eve-release/R1-1.2.3/full_dev_part_KERN.bin.gz'
            )
            result = cros_util.prepare_image_for_cros_flash(
                chromeos_root, image_info, is_public_build=False
            )
            self.assertIsNone(result)

        with self.subTest('zip_file_cache_miss'):
            mock_exists.reset_mock()
            mock_exists.side_effect = [False, True]
            mock_tempdir.return_value.__enter__.return_value = '/tmp/dir'
            image_info.clear()
            image_info[cros_util.ImageType.ZIP_FILE] = (
                'gs://chromeos-image-archive/eve-release/R1-1.2.3/image.zip'
            )
            result = cros_util.prepare_image_for_cros_flash(
                chromeos_root, image_info, is_public_build=False
            )
            self.assertTrue(result.startswith('tmp/images/'))
            mock_cp.assert_called_with(
                'gs://chromeos-image-archive/eve-release/R1-1.2.3/image.zip',
                '/tmp/dir',
            )
            mock_check_call.assert_called_with(
                'unzip',
                '-j',
                '/tmp/dir/image.zip',
                'chromiumos_test_image.bin',
                cwd='/tmp/dir',
            )
            mock_move.assert_called()

        with self.subTest('zip_file_cache_hit'):
            mock_exists.reset_mock()
            mock_exists.side_effect = [True, True]
            image_info.clear()
            image_info[cros_util.ImageType.ZIP_FILE] = (
                'gs://chromeos-image-archive/eve-release/R1-1.2.3/image.zip'
            )
            result = cros_util.prepare_image_for_cros_flash(
                chromeos_root, image_info, is_public_build=False
            )
            self.assertTrue(result.startswith('tmp/images/'))

    def test_determine_image_type(self):
        self.assertEqual(
            cros_util.ImageType.determine(
                'gs://my-bucket/hatch-snapshot/R96-14171.0.0-53037-123456789/image.zip'
            ),
            cros_util.ImageType.ZIP_FILE,
        )
        self.assertEqual(
            cros_util.ImageType.determine(
                'gs://my-bucket/hatch-snapshot/R96-14171.0.0-53037-123456789/full_dev_part_KERN.bin.gz'
            ),
            cros_util.ImageType.PARTITION_IMAGE,
        )
        self.assertEqual(
            cros_util.ImageType.determine(
                'gs://my-bucket/hatch-snapshot/R96-14171.0.0-53037-123456789/chromiumos_test_image.tar.xz'
            ),
            cros_util.ImageType.DISK_IMAGE,
        )
        self.assertEqual(
            cros_util.ImageType.determine(
                'gs://my-bucket/hatch-snapshot/R96-14171.0.0-53037-123456789/chromiumos_test_image.bin'
            ),
            cros_util.ImageType.DISK_IMAGE,
        )
        self.assertEqual(
            cros_util.ImageType.determine(
                'gs://my-bucket/hatch-snapshot/R96-14171.0.0-53037-123456789/mystery.bin'
            ),
            None,
        )

    def test_repair_dut_retry_failed(self):
        error = subprocess.CalledProcessError(1, 'cmd')

        with mock.patch('shutil.rmtree'), mock.patch('shutil.copytree'):
            with mock.patch(
                'bisect_kit.cros_util.cros_sdk', side_effect=[error] * 3
            ):
                result = cros_util.repair_dut('root', 'dut')
                self.assertFalse(result)
                self.assertIsNotNone(
                    result
                )  # `None` passes the assertFalse() test

    def test_repair_dut_ok(self):
        error = subprocess.CalledProcessError(1, 'cmd')
        with mock.patch('shutil.rmtree'), mock.patch('shutil.copytree'):
            with mock.patch(
                'bisect_kit.cros_util.cros_sdk',
                side_effect=[error, error, 'ok'],
            ):
                self.assertTrue(cros_util.repair_dut('root', 'dut'))

    def test_normalize_test_name(self):
        self.assertEqual(
            cros_util.normalize_test_name('dummy_test1'), 'dummy_test1'
        )
        self.assertEqual(
            cros_util.normalize_test_name('tast.dummy_test2'),
            'tast.dummy_test2',
        )
        self.assertEqual(
            cros_util.normalize_test_name('tauto.dummy_test3'), 'dummy_test3'
        )

    @mock.patch('bisect_kit.util.ssh_cmd')
    def test_query_dut_root_partition(self, mock_ssh_cmd):
        # partition B, A
        mock_ssh_cmd.side_effect = ['/dev/mmcblk0', '/dev/mmcblk0p5']
        self.assertEqual(
            cros_util.query_dut_root_partition('foo'),
            cros_util.PartitionInfo(
                disk='/dev/mmcblk0',
                active_root_partition=5,
                active_kernel_partition=4,
            ),
        )
        mock_ssh_cmd.side_effect = ['/dev/sdb', '/dev/sdb3']
        self.assertEqual(
            cros_util.query_dut_root_partition('bar'),
            cros_util.PartitionInfo(
                disk='/dev/sdb',
                active_root_partition=3,
                active_kernel_partition=2,
            ),
        )
        # unknown partition
        mock_ssh_cmd.side_effect = ['/dev/sdb', '/dev/sdb2']
        self.assertRaises(
            errors.ExternalError, cros_util.query_dut_root_partition, 'moo'
        )
        # format incorrect
        mock_ssh_cmd.side_effect = ['/dev/sdb', '/dev/sdb']
        self.assertRaises(
            errors.ExternalError, cros_util.query_dut_root_partition, 'meow'
        )

    @mock.patch('bisect_kit.util.ssh_cmd')
    def test_query_dut_test_runner_availability(self, mock_ssh_cmd):
        mock_ssh_cmd.side_effect = ['/usr/local/bin/local_test_runner']
        self.assertTrue(
            cros_util.query_dut_test_runner_availability('dut_host')
        )
        mock_ssh_cmd.side_effect = ['wrong_output']
        self.assertFalse(
            cros_util.query_dut_test_runner_availability('dut_host')
        )

        mock_ssh_cmd.side_effect = subprocess.CalledProcessError(255, 'ssh_cmd')
        self.assertFalse(
            cros_util.query_dut_test_runner_availability('dut_host')
        )

    @mock.patch('bisect_kit.util.ssh_cmd')
    def test_is_dut_kernel_ready(self, mock_ssh_cmd):
        mock_ssh_cmd.side_effect = ['/dev/mmcblk0', '/dev/mmcblk0p5', '1']
        self.assertEqual(cros_util.is_dut_kernel_ready('foo'), True)
        mock_ssh_cmd.side_effect = ['/dev/mmcblk0', '/dev/mmcblk0p5', '0']
        self.assertEqual(cros_util.is_dut_kernel_ready('bar'), False)

    @mock.patch('os.path.exists')
    def test_query_repo_chromeos_version(self, mock_path_exists):
        mock_path_exists.return_value = True
        content = textwrap.dedent(
            '''
        ChromiumOS version information:
        CHROME_BRANCH=120
        CHROME_VERSION=
        CHROMEOS_BUILD=15651
        CHROMEOS_BRANCH=0
        CHROMEOS_PATCH=0
        CHROMEOS_VERSION_STRING=15651.0.0'''
        )
        with mock.patch('builtins.open', mock.mock_open(read_data=content)):
            self.assertEqual(
                cros_util.ChromeOSVersionParser.query_repo_chromeos_version(
                    'foo'
                ),
                cros_util.ChromeOSVersion(
                    chrome_branch=120,
                    chromeos_build=15651,
                    chromeos_branch=0,
                    chromeos_patch=0,
                ),
            )

    def test_parse_chromeos_version(self):
        with open(testing.get_testdata_path('chromeos_version.sh')) as f:
            contents = f.read()
        self.assertEqual(
            cros_util.ChromeOSVersionParser.parse_chromeos_version(contents),
            cros_util.ChromeOSVersion(
                chrome_branch=121,
                chromeos_build=15684,
                chromeos_branch=71,
                chromeos_patch=0,
            ),
        )
        with self.assertRaises(ValueError):
            cros_util.ChromeOSVersionParser.parse_chromeos_version('unparsable')

    @mock.patch('bisect_kit.util.ssh_cmd')
    def test_query_dut_cpu_arch(self, mock_ssh_cmd):
        self.assertEqual(
            cros_util.query_dut_cpu_arch('kukui_dut', 'kukui'), 'arm64'
        )
        self.assertEqual(
            cros_util.query_dut_cpu_arch('kukui_dut', 'kukui64'), 'arm'
        )

        mock_ssh_cmd.side_effect = ['28']
        self.assertEqual(cros_util.query_dut_cpu_arch('arm32_dut'), 'arm')

        mock_ssh_cmd.side_effect = ['b7']
        self.assertEqual(cros_util.query_dut_cpu_arch('arm64_dut'), 'arm64')

        mock_ssh_cmd.side_effect = ['3e']
        self.assertEqual(cros_util.query_dut_cpu_arch('x86_64_dut'), 'x86_64')

    def test_query_prebuilt_gs_path_public(self):
        def fake_gs_util_ls(*args, **kwargs):
            logger.debug('fake_gs_util_ls %r %r', args, kwargs)
            if args in [
                (
                    '-d',
                    'gs://chromiumos-image-archive/samus-public/R100-12345.0.0',
                ),
                (
                    '-d',
                    'gs://chromiumos-image-archive/samus-public/R*-12345.0.0',
                ),
            ]:
                return [
                    'gs://chromiumos-image-archive/samus-public/R100-12345.0.0/'
                ]
            return []

        with mock.patch.object(gs_util, 'ls', fake_gs_util_ls):
            gs_path = cros_util.query_prebuilt_gs_path(
                'samus', 'R100-12345.0.0', is_public_build=True
            )
            self.assertEqual(
                gs_path,
                'gs://chromiumos-image-archive/samus-public/R100-12345.0.0',
            )
            gs_path = cros_util.query_prebuilt_gs_path(
                'samus', '12345.0.0', is_public_build=True
            )
            self.assertEqual(
                gs_path,
                'gs://chromiumos-image-archive/samus-public/R100-12345.0.0',
            )
            gs_path = cros_util.query_prebuilt_gs_path(
                'foo', 'R100-12345.0.0', is_public_build=True
            )
            self.assertEqual(gs_path, '')

    def test_has_release_prebuilt_public(self):
        def fake_gs_util_ls(*args, **kwargs):
            return []

        with mock.patch.object(gs_util, 'ls', fake_gs_util_ls):
            self.assertFalse(
                cros_util.has_release_prebuilt(
                    'samus', 'R100-12345.0.0', is_public_build=True
                )
            )

    @mock.patch(
        'bisect_kit.cros_util.version_to_full',
        lambda _, v, **kwargs: f'R100-{v}',
    )
    def test_list_chromeos_prebuilt_versions_public(self):
        with mock.patch(
            'bisect_kit.gs_util.ls', _fake_gs_util_ls_for_public_build
        ):
            (
                versions,
                details,
            ) = cros_util.list_chromeos_prebuilt_versions(
                'foo', '10000.0.0', '10001.0.0', is_public_build=True
            )
            self.assertEqual(versions, ['R100-10000.0.0', 'R100-10001.0.0'])
            self.assertIn('R100-10000.0.0', details)
            self.assertIn('R100-10001.0.0', details)

    # pylint: disable=protected-access
    def test_generate_possible_bucket_list(self):
        # is_public_build = True
        self.assertEqual(
            cros_util._generate_possible_bucket_list(
                is_public_build=True, is_snapshot=True
            ),
            ['public-snapshot'],
        )
        self.assertEqual(
            cros_util._generate_possible_bucket_list(
                is_public_build=True, is_snapshot=False
            ),
            ['public'],
        )
        self.assertEqual(
            sorted(
                cros_util._generate_possible_bucket_list(
                    is_public_build=True, is_snapshot=None
                )
            ),
            sorted(['public', 'public-snapshot']),
        )

        # is_public_build = False
        self.assertEqual(
            cros_util._generate_possible_bucket_list(
                is_public_build=False, is_snapshot=True
            ),
            ['snapshot'],
        )
        self.assertEqual(
            sorted(
                cros_util._generate_possible_bucket_list(
                    is_public_build=False, is_snapshot=False
                )
            ),
            sorted(['release', 'factory']),
        )
        self.assertEqual(
            sorted(
                cros_util._generate_possible_bucket_list(
                    is_public_build=False, is_snapshot=None
                )
            ),
            sorted(['release', 'factory', 'snapshot']),
        )

        # is_public_build = None
        self.assertEqual(
            sorted(
                cros_util._generate_possible_bucket_list(
                    is_public_build=None, is_snapshot=True
                )
            ),
            sorted(['snapshot', 'public-snapshot']),
        )
        self.assertEqual(
            sorted(
                cros_util._generate_possible_bucket_list(
                    is_public_build=None, is_snapshot=False
                )
            ),
            sorted(['release', 'factory', 'public']),
        )
        self.assertEqual(
            sorted(
                cros_util._generate_possible_bucket_list(
                    is_public_build=None, is_snapshot=None
                )
            ),
            sorted(
                [
                    'release',
                    'factory',
                    'public',
                    'snapshot',
                    'public-snapshot',
                ]
            ),
        )

    @mock.patch('bisect_kit.gs_util.ls')
    @mock.patch('bisect_kit.gs_util.cat')
    def test_query_snapshot_image_fallback(self, mock_gs_cat, mock_gs_ls):
        cros_util.SnapshotStore.images = {}
        board = 'board'
        snapshot_version = 'R123-15678.0.0-9999'
        is_public_build = False

        def fake_ls(path, ignore_errors=False):
            if (
                path
                == 'gs://chromeos-image-archive/board-snapshot/R123-15678.0.0-9999-8888/image.zip'
            ):
                return [
                    'gs://chromeos-image-archive/board-snapshot/R123-15678.0.0-9999-8888/image.zip'
                ]
            return []

        mock_gs_ls.side_effect = fake_ls
        mock_gs_cat.return_value = 'R123-15678.0.0-9999-8888\n'

        with mock.patch('bisect_kit.cros_util.is_vm_board', return_value=False):
            path = cros_util.SnapshotStore.query_snapshot_image(
                board, snapshot_version, is_public_build
            )

        self.assertEqual(
            path,
            'gs://chromeos-image-archive/board-snapshot/R123-15678.0.0-9999-8888/image.zip',
        )

        # Verify calls
        mock_gs_ls.assert_any_call(
            'gs://chromeos-image-archive/board-snapshot/R123-15678.0.0-9999-*/image.zip',
            ignore_errors=True,
        )
        mock_gs_cat.assert_called_with(
            'gs://chromeos-image-archive/board-snapshot/LATEST-SNAPSHOT-9999',
            ignore_errors=True,
        )
        mock_gs_ls.assert_any_call(
            'gs://chromeos-image-archive/board-snapshot/R123-15678.0.0-9999-8888/image.zip',
            ignore_errors=True,
        )

    @mock.patch('bisect_kit.gs_util.ls')
    @mock.patch('bisect_kit.gs_util.cat')
    def test_query_snapshot_image_success(self, mock_gs_cat, mock_gs_ls):
        cros_util.SnapshotStore.images = {}
        board = 'board'
        snapshot_version = 'R123-15678.0.0-9999'
        is_public_build = False

        def fake_ls(path, ignore_errors=False):
            if (
                path
                == 'gs://chromeos-image-archive/board-snapshot/R123-15678.0.0-9999-*/image.zip'
            ):
                return [
                    'gs://chromeos-image-archive/board-snapshot/R123-15678.0.0-9999-1234/image.zip'
                ]
            return []

        mock_gs_ls.side_effect = fake_ls

        with mock.patch('bisect_kit.cros_util.is_vm_board', return_value=False):
            path = cros_util.SnapshotStore.query_snapshot_image(
                board, snapshot_version, is_public_build
            )

        self.assertEqual(
            path,
            'gs://chromeos-image-archive/board-snapshot/R123-15678.0.0-9999-1234/image.zip',
        )

        # Verify calls
        mock_gs_ls.assert_any_call(
            'gs://chromeos-image-archive/board-snapshot/R123-15678.0.0-9999-*/image.zip',
            ignore_errors=True,
        )
        mock_gs_cat.assert_not_called()

    @mock.patch('bisect_kit.gs_util.ls')
    @mock.patch('bisect_kit.gs_util.cat')
    def test_query_snapshot_image_not_found(self, mock_gs_cat, mock_gs_ls):
        cros_util.SnapshotStore.images = {}
        board = 'board'
        snapshot_version = 'R123-15678.0.0-9999'
        is_public_build = False

        mock_gs_ls.return_value = []
        mock_gs_cat.return_value = None

        with mock.patch('bisect_kit.cros_util.is_vm_board', return_value=False):
            path = cros_util.SnapshotStore.query_snapshot_image(
                board, snapshot_version, is_public_build
            )

        self.assertIsNone(path)

        # Verify calls
        mock_gs_ls.assert_any_call(
            'gs://chromeos-image-archive/board-snapshot/R123-15678.0.0-9999-*/image.zip',
            ignore_errors=True,
        )
        mock_gs_cat.assert_called_with(
            'gs://chromeos-image-archive/board-snapshot/LATEST-SNAPSHOT-9999',
            ignore_errors=True,
        )


if __name__ == '__main__':
    unittest.main()
