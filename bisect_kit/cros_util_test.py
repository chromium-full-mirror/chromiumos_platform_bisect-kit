# -*- coding: utf-8 -*-
# Copyright 2017 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Test cros_util module."""

from __future__ import print_function
import fnmatch
import logging
import os
import subprocess
import unittest

from unittest import mock

from bisect_kit import cli
from bisect_kit import cros_util
from bisect_kit import testing

logger = logging.getLogger(__name__)


class TestCrosUtil(unittest.TestCase):
  """Test cros_util functions."""

  def test_is_cros_short_version(self):
    self.assertTrue(cros_util.is_cros_short_version('1234.0.0'))
    self.assertFalse(cros_util.is_cros_short_version('R99-1234.0.0'))
    self.assertFalse(cros_util.is_cros_short_version('1234.0.2018_01_01_1259'))

  def test_is_cros_full_version(self):
    self.assertFalse(cros_util.is_cros_full_version('1234.0.0'))
    self.assertTrue(cros_util.is_cros_full_version('R99-1234.0.0'))
    self.assertFalse(cros_util.is_cros_full_version('1234.0.2018_01_01_1259'))

  def test_is_cros_version(self):
    self.assertTrue(cros_util.is_cros_version('1234.0.0'))
    self.assertTrue(cros_util.is_cros_version('R99-1234.0.0'))

  def test_is_ancestor_version(self):
    # One version is not ancestor of itself.
    self.assertFalse(cros_util.is_ancestor_version('12345.0.0', '12345.0.0'))
    self.assertFalse(
        cros_util.is_ancestor_version('12345.0.0', 'R10-12345.0.0'))
    self.assertFalse(
        cros_util.is_ancestor_version('R10-12345.0.0-1000',
                                      'R10-12345.0.0-1000'))

    self.assertTrue(cros_util.is_ancestor_version('12345.0.0', '12346.0.0'))
    self.assertTrue(
        cros_util.is_ancestor_version('R10-12345.0.0', 'R10-12346.0.0'))
    self.assertTrue(
        cros_util.is_ancestor_version('R10-12345.0.0-1000',
                                      'R10-12345.0.0-2000'))

    self.assertFalse(cros_util.is_ancestor_version('12347.0.0', '12346.0.0'))
    self.assertFalse(
        cros_util.is_ancestor_version('R12-12345.0.0', 'R10-12346.0.0'))
    self.assertFalse(
        cros_util.is_ancestor_version('R10-12345.0.0-3000',
                                      'R10-12345.0.0-2000'))

  def test_is_cros_snapshot_version(self):
    self.assertFalse(cros_util.is_cros_snapshot_version('1234.0.0'))
    self.assertFalse(cros_util.is_cros_snapshot_version('R99-1234.0.0'))
    self.assertFalse(
        cros_util.is_cros_snapshot_version('1234.0.2018_01_01_1259'))
    self.assertFalse(cros_util.is_cros_snapshot_version('1234.0.0-23456'))
    self.assertTrue(cros_util.is_cros_snapshot_version('R99-1234.0.0-23456'))

  def test_make_version_and_split(self):
    full_version = 'R33-1234.0.0'
    snapshot_version = 'R78-12345.3.4-3456'
    milestone, short_version = cros_util.version_split(full_version)

    self.assertEqual(milestone, '33')
    self.assertEqual(short_version, '1234.0.0')

    self.assertEqual(
        cros_util.make_cros_full_version(milestone, short_version),
        full_version)

    snapshot_version = 'R78-12345.3.4-3456'
    milestone, short_version, snapshot_id = cros_util.snapshot_version_split(
        snapshot_version)

    self.assertEqual(milestone, '78')
    self.assertEqual(short_version, '12345.3.4')
    self.assertEqual(snapshot_id, '3456')
    self.assertEqual(
        cros_util.make_cros_snapshot_version(milestone, short_version,
                                             snapshot_id), snapshot_version)

  def test_argtype_cros_version(self):
    self.assertEqual(cros_util.argtype_cros_version('9876.0.0'), '9876.0.0')
    self.assertEqual(
        cros_util.argtype_cros_version('R99-9876.0.0'), 'R99-9876.0.0')
    with self.assertRaises(cli.ArgTypeError):
      cros_util.argtype_cros_version('foobar')

  def test_lsb_release(self):
    with open(testing.get_testdata_path('lsb-release')) as f:
      sample = f.read()
    with mock.patch('bisect_kit.util.check_output', return_value=sample):
      dummy_dut = 'dummy'
      self.assertEqual(cros_util.query_dut_board(dummy_dut), 'samus')
      self.assertEqual(
          cros_util.query_dut_short_version(dummy_dut), '12345.0.0')

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
        'CHROMEOS_RELEASE_VERSION':
            '13059.0.0',
        'CHROMEOS_RELEASE_BUILDER_PATH':
            'octopus-snapshot/R84-13059.0.0-31619-8882013661175561136',
    }
    sample4 = {
        'CHROMEOS_RELEASE_VERSION':
            '13059.0.0',
        'CHROMEOS_RELEASE_BUILDER_PATH':
            'octopus-postsubmit/R84-13059.0.0-31619-8882013661175561136',
    }

    with mock.patch(
        'bisect_kit.cros_util.query_dut_lsb_release', return_value=sample1):
      self.assertEqual(
          cros_util.query_dut_prebuilt_version(dummy_dut),
          '13064.0.0',
      )
    with mock.patch(
        'bisect_kit.cros_util.query_dut_lsb_release', return_value=sample2):
      self.assertEqual(
          cros_util.query_dut_prebuilt_version(dummy_dut),
          '13064.0.0',
      )
    with mock.patch(
        'bisect_kit.cros_util.query_dut_lsb_release', return_value=sample3):
      self.assertEqual(
          cros_util.query_dut_prebuilt_version(dummy_dut),
          'R84-13059.0.0-31619',
      )
    with mock.patch(
        'bisect_kit.cros_util.query_dut_lsb_release', return_value=sample4):
      self.assertEqual(
          cros_util.query_dut_prebuilt_version(dummy_dut),
          'R84-13059.0.0-31619',
      )

  def test_gsutil(self):
    with mock.patch('bisect_kit.util.check_output', return_value='abc'):
      self.assertEqual(cros_util.gsutil('cat', 'foo'), 'abc')

    with mock.patch(
        'bisect_kit.util.check_output',
        side_effect=subprocess.CalledProcessError(1, 'cmd')):
      with self.assertRaises(subprocess.CalledProcessError):
        cros_util.gsutil('cat', 'bar')

  def test_query_milestone_by_version(self):
    # Assume query_milestone_by_version is implemented via gsutil.
    def gsutil_ls(*args, **kwargs):
      logger.debug('gsutil_ls %r %r', args, kwargs)
      if args == ('gs://chromeos-releases/dev-channel/samus/9300.0.0',):
        path = args[0]
        return [
            path + '/ChromeOS-R58-9300.0.0-samus.tar.xz',
            path + '/ChromeOS-test-R58-9300.0.0-samus.tar.xz',
            path + '/stateful.tgz',
        ]

      if args == ('-d',
                  'gs://chromeos-image-archive/samus-release/R*-10123.0.0'):
        return ['gs://chromeos-image-archive/samus-release/R64-10123.0.0/']

      return []

    # First case, recent image, still exists in gs://chromeos-image-archive
    with mock.patch.object(cros_util, 'gsutil_ls', gsutil_ls):
      self.assertEqual(
          cros_util.query_milestone_by_version('samus', '10123.0.0'), '64')

    # Second case, old image, only exists in gs://chromeos-releases
    with mock.patch.object(cros_util, 'gsutil_ls', gsutil_ls):
      self.assertEqual(
          cros_util.query_milestone_by_version('samus', '9300.0.0'), '58')

  def test_version_operation(self):
    with mock.patch.object(
        cros_util, 'query_milestone_by_version', return_value='64'):
      self.assertEqual(
          cros_util.recognize_version('samus', 'R64-10123.0.0'),
          ('64', '10123.0.0'))
      self.assertEqual(
          cros_util.recognize_version('samus', '10123.0.0'),
          ('64', '10123.0.0'))

      self.assertEqual(cros_util.version_to_short('R64-10123.0.0'), '10123.0.0')
      self.assertEqual(cros_util.version_to_short('10123.0.0'), '10123.0.0')
      self.assertEqual(
          cros_util.version_to_short('R64-10123.0.0-45678'), '10123.0.0')

      self.assertEqual(
          cros_util.extract_major_version('R64-10123.0.0'), '10123')
      self.assertEqual(cros_util.extract_major_version('10123.0.0'), '10123')
      self.assertEqual(
          cros_util.extract_major_version('R64-10123.0.0-45678'), '10123')

      self.assertEqual(
          cros_util.version_to_full('samus', 'R64-10123.0.0'), 'R64-10123.0.0')
      self.assertEqual(
          cros_util.version_to_full('samus', '10123.0.0'), 'R64-10123.0.0')
      self.assertEqual(
          cros_util.version_to_full('samus', 'R64-10123.0.0-12345'),
          'R64-10123.0.0')

  def test_version_info(self):
    with open(testing.get_testdata_path('partial-metadata.json')) as f:
      sample = f.read()
    with mock.patch.object(cros_util, 'gsutil', return_value=sample):
      info = cros_util.version_info('caroline', '13664.0.0')
      self.assertEqual(info[cros_util.VERSION_KEY_CROS_SHORT_VERSION],
                       '13664.0.0')

  def test_list_prebuilt_from_image_archive(self):
    gs_path = 'gs://chromeos-image-archive/foo-release/R59-9410.0.0/'
    with mock.patch.object(cros_util, 'gsutil_ls', return_value=[gs_path]):
      self.assertEqual(
          cros_util.list_prebuilt_from_image_archive('foo'),
          [('R59-9410.0.0', gs_path + 'chromiumos_test_image.tar.xz')])

    with mock.patch.object(cros_util, 'gsutil_ls', return_value=[]):
      self.assertEqual(
          cros_util.list_prebuilt_from_image_archive('wrong-board'), [])

  def test_list_prebuilt_from_chromeos_releases(self):
    gs_path = 'gs://chromeos-releases/dev-channel/foo/9402.0.0/'
    with mock.patch.object(cros_util, 'gsutil_ls', return_value=[gs_path]):
      self.assertEqual(
          cros_util.list_prebuilt_from_chromeos_releases('foo'),
          [('9402.0.0', gs_path + 'ChromeOS-test-R*-9402.0.0-foo.tar.xz')])

    with mock.patch.object(cros_util, 'gsutil_ls', return_value=[]):
      self.assertEqual(
          cros_util.list_prebuilt_from_chromeos_releases('wrong-board'), [])

  @staticmethod
  def _gsutil_ls(*args, **_kwargs):
    archive_base = 'gs://chromeos-image-archive/foo-release/'
    release_base = 'gs://chromeos-releases/stable-channel/foo/'
    snapshot_base = 'gs://chromeos-image-archive/foo-snapshot/'

    gs_files = [
        release_base + '9994.0.0/',
        release_base + '9995.0.0/',
        release_base + '9997.0.0/',
        release_base + '9994.0.0/ChromeOS-test-R63-9994.0.0-foo.tar.xz',
        release_base + '9995.0.0/ChromeOS-test-R63-9995.0.0-foo.tar.xz',
        release_base + '9997.0.0/ChromeOS-test-R63-9997.0.0-foo.tar.xz',
        snapshot_base + 'R63-9995.0.0-100-000/',
        snapshot_base + 'R63-9995.0.0-101-000/',
        snapshot_base + 'R63-9995.0.0-102-000/',
        snapshot_base + 'R63-9996.0.0-103-000/',
        snapshot_base + 'R63-9997.0.0-104-000/',
        snapshot_base + 'R63-9998.0.0-105-000/',
        snapshot_base + 'R63-9999.0.0-106-000/',
        snapshot_base + 'R63-9995.0.0-100-000/image.zip',
        snapshot_base + 'R63-9995.0.0-101-000/image.zip',
        snapshot_base + 'R63-9995.0.0-102-000/image.zip',
        snapshot_base + 'R63-9996.0.0-103-000/image.zip',
        snapshot_base + 'R63-9997.0.0-104-000/image.zip',
        snapshot_base + 'R63-9998.0.0-105-000/image.zip',
        snapshot_base + 'R63-9999.0.0-106-000/image.zip',
        archive_base + 'R63-9998.0.0/',
        archive_base + 'R63-9999.0.0/',
        archive_base + 'R63-9999.1.0/',
        archive_base + 'R63-10000.0.0/',
        archive_base + 'R63-10001.0.0/',
        # archive_base + 'R63-9998.0.0/chromiumos_test_image.tar.xz',
        archive_base + 'R63-9999.0.0/chromiumos_test_image.tar.xz',
        archive_base + 'R63-9999.1.0/chromiumos_test_image.tar.xz',
        # archive_base + 'R63-10000.0.0/chromiumos_test_image.tar.xz',
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

  @staticmethod
  def _query_milestone_by_version(_1, _2):
    return '63'

  @staticmethod
  def _version_to_full(_board, version):
    return 'R63-' + version

  @mock.patch('bisect_kit.cros_util.gsutil_ls', _gsutil_ls.__func__)
  @mock.patch('bisect_kit.cros_util.version_to_full', _version_to_full.__func__)
  @mock.patch('bisect_kit.cros_util.query_milestone_by_version',
              _query_milestone_by_version.__func__)
  def test_list_chromeos_prebuilt_versions(self):
    # Both ends are in gs://chromeos-image-archive
    self.assertEqual(
        cros_util.list_chromeos_prebuilt_versions(
            'foo', '9998.0.0', '10001.0.0', only_good_build=False),
        ['R63-9998.0.0', 'R63-9999.0.0', 'R63-10000.0.0', 'R63-10001.0.0'])

    # Both ends are in gs://chromeos-image-archive; only good build
    self.assertEqual(
        cros_util.list_chromeos_prebuilt_versions(
            'foo', '9998.0.0', '10001.0.0', only_good_build=True),
        ['R63-9999.0.0', 'R63-10001.0.0'])

    # Some are in gs://chromeos-releases
    self.assertEqual(
        cros_util.list_chromeos_prebuilt_versions(
            'foo', '9995.0.0', '9999.0.0', only_good_build=False),
        ['R63-9995.0.0', 'R63-9997.0.0', 'R63-9998.0.0', 'R63-9999.0.0'])

    # Some are in gs://chromeos-releases; only good build
    self.assertEqual(
        cros_util.list_chromeos_prebuilt_versions(
            'foo', '9995.0.0', '9999.0.0', only_good_build=True),
        ['R63-9995.0.0', 'R63-9997.0.0', 'R63-9999.0.0'])

  @mock.patch('bisect_kit.cros_util.snapshot_cutover_version', '0.0.0')
  @mock.patch('bisect_kit.cros_util.gsutil_ls', _gsutil_ls.__func__)
  @mock.patch('bisect_kit.cros_util.version_to_full', _version_to_full.__func__)
  @mock.patch('bisect_kit.cros_util.query_milestone_by_version',
              _query_milestone_by_version.__func__)
  def test_list_chromeos_prebuilt_versions_with_snapshots(self):
    # snapshot 1: release ~ release
    self.assertEqual(
        cros_util.list_chromeos_prebuilt_versions(
            'foo',
            'R63-9995.0.0',
            'R63-9997.0.0',
            only_good_build=True,
            use_snapshot=True), [
                'R63-9995.0.0', 'R63-9995.0.0-100', 'R63-9995.0.0-101',
                'R63-9995.0.0-102', 'R63-9996.0.0-103', 'R63-9997.0.0'
            ])

    self.assertEqual(
        cros_util.list_chromeos_prebuilt_versions(
            'foo',
            'R63-9995.0.0',
            'R63-9998.0.0',
            only_good_build=True,
            use_snapshot=True), [
                'R63-9995.0.0', 'R63-9995.0.0-100', 'R63-9995.0.0-101',
                'R63-9995.0.0-102', 'R63-9996.0.0-103', 'R63-9997.0.0',
                'R63-9997.0.0-104'
            ])

    # snapshot 2: release ~ snapshot or snapshot ~ release
    self.assertEqual(
        cros_util.list_chromeos_prebuilt_versions(
            'foo',
            'R63-9995.0.0-100',
            'R63-9997.0.0',
            only_good_build=True,
            use_snapshot=True), [
                'R63-9995.0.0-100', 'R63-9995.0.0-101', 'R63-9995.0.0-102',
                'R63-9996.0.0-103', 'R63-9997.0.0'
            ])

    self.assertEqual(
        cros_util.list_chromeos_prebuilt_versions(
            'foo',
            'R63-9995.0.0-103',
            'R63-9997.0.0',
            only_good_build=True,
            use_snapshot=True), ['R63-9996.0.0-103', 'R63-9997.0.0'])

    self.assertEqual(
        cros_util.list_chromeos_prebuilt_versions(
            'foo',
            'R63-9995.0.0',
            'R63-9997.0.0-104',
            only_good_build=True,
            use_snapshot=True), [
                'R63-9995.0.0', 'R63-9995.0.0-100', 'R63-9995.0.0-101',
                'R63-9995.0.0-102', 'R63-9996.0.0-103', 'R63-9997.0.0',
                'R63-9997.0.0-104'
            ])

    # snapshot 3: branch case
    self.assertEqual(
        cros_util.list_chromeos_prebuilt_versions(
            'foo',
            'R63-9998.0.0',
            'R63-9999.1.0',
            only_good_build=False,
            use_snapshot=True),
        ['R63-9998.0.0', 'R63-9998.0.0-105', 'R63-9999.0.0', 'R63-9999.1.0'])

    self.assertEqual(
        cros_util.list_chromeos_prebuilt_versions(
            'foo',
            'R63-9998.0.0',
            'R63-10000.0.0',
            only_good_build=False,
            use_snapshot=True), [
                'R63-9998.0.0', 'R63-9998.0.0-105', 'R63-9999.0.0',
                'R63-9999.0.0-106', 'R63-10000.0.0'
            ])

  def test_is_cros_version_lesseq(self):
    self.assertTrue(cros_util.is_cros_version_lesseq('R99-1.0.0', 'R99-1.0.0'))
    self.assertTrue(cros_util.is_cros_version_lesseq('R99-1.0.0', 'R99-1.1.0'))
    self.assertTrue(cros_util.is_cros_version_lesseq('R99-1.0.0', 'R99-1.0.1'))
    self.assertFalse(cros_util.is_cros_version_lesseq('R99-1.0.0', 'R98-1.1.0'))
    self.assertFalse(cros_util.is_cros_version_lesseq('R99-1.0.0', 'R98-1.0.1'))
    self.assertFalse(cros_util.is_cros_version_lesseq('R98-1.0.1', 'R99-1.0.0'))
    self.assertTrue(
        cros_util.is_cros_version_lesseq('R99-1.0.0', 'R99-1.0.0-123'))
    self.assertTrue(
        cros_util.is_cros_version_lesseq('R99-1.0.0-122', 'R99-1.0.0-123'))
    self.assertFalse(
        cros_util.is_cros_version_lesseq('R99-1.0.0-124', 'R99-1.0.0-123'))

  def test_is_buildbucket_buildable(self):
    sample_cutover = ['10000.0.0', '9999.20.0', '9998.20.0']
    with mock.patch.object(cros_util, 'buildbucket_cutover_versions',
                           sample_cutover):
      self.assertTrue(cros_util.is_buildbucket_buildable('R77-10000.0.0-12345'))
      self.assertTrue(cros_util.is_buildbucket_buildable('10000.0.0'))
      self.assertFalse(cros_util.is_buildbucket_buildable('R77-9999.0.0-12345'))
      self.assertFalse(cros_util.is_buildbucket_buildable('9999.19.0'))
      self.assertTrue(cros_util.is_buildbucket_buildable('9999.20.0'))
      self.assertTrue(cros_util.is_buildbucket_buildable('9998.100.100'))
      self.assertFalse(cros_util.is_buildbucket_buildable('9997.100.100'))

  def test_resolve_basic_boards(self):
    overlays = {
        'foo': ['chromiumos'],
        'foo-private': ['chromiumos'],
        'foo-kernelnext-private': ['chromiumos', 'foo-private'],
        'foo64-private': ['chromiumos', 'foo-private'],
        'bar-private': ['foo-private', 'chromiumos'],
        'bar-kernelnext': ['chromiumos', 'bar-private'],
        'bar64': ['chromiumos', 'bar-private'],
    }
    self.assertDictEqual(
        cros_util.resolve_basic_boards(overlays), {
            'foo': 'foo',
            'foo-kernelnext': 'foo',
            'foo64': 'foo',
            'bar': 'foo',
            'bar-kernelnext': 'foo',
            'bar64': 'foo',
        })

  def test_detect_branch_level(self):
    ref_prefix = [
        '',
        'refs/head/',
        'refs/remotes/cros/',
        'refs/remotes/origin/',
    ]
    tests = [
        ['master', 1],
        ['release-R64-10176.B', 2],
        ['release-R56-9000.B', 2],
        ['stabilize-3881.0.B', 2],
        ['firmware-octopus-11297.83.B', 3],
        ['stabilize-10323.67.B', 3],
        ['stabilize-9000.87.0.B', 3],
        ['factory-atlas-11907.B-master', 0],
    ]

    for prefix in ref_prefix:
      for test in tests:
        self.assertEqual(
            cros_util.detect_branch_level(prefix + test[0]), test[1])

  def test_get_crosland_link(self):
    template = cros_util.CROSLAND_URL_TEMPLATE
    self.assertEqual(
        cros_util.get_crosland_link('R82-12872.0.0-27636',
                                    'R82-12872.0.0-27637'),
        template % ('27636', '27637'))
    self.assertEqual(
        cros_util.get_crosland_link('R82-12872.0.0', 'R82-12872.0.0-27637'),
        template % ('12872.0.0', '27637'))
    self.assertEqual(
        cros_util.get_crosland_link('12872.0.0', 'R82-12872.0.0-27637'),
        template % ('12872.0.0', '27637'))
    self.assertEqual(
        cros_util.get_crosland_link('R82-12872.0.0-27637', 'R82-12873.0.0'),
        template % ('27637', '12873.0.0'))
    self.assertEqual(
        cros_util.get_crosland_link('R82-12872.0.0-27637', '12873.0.0'),
        template % ('27637', '12873.0.0'))

  def test_convert_path_outside_chroot(self):
    with mock.patch.dict(os.environ, {'USER': 'foouser'}):
      self.assertEqual(
          cros_util.convert_path_outside_chroot('/path/to/root',
                                                '/path/to/file'),
          '/path/to/root/chroot/path/to/file')
      self.assertEqual(
          cros_util.convert_path_outside_chroot('/path/to/root',
                                                '~/path/to/file'),
          '/path/to/root/chroot/home/foouser/path/to/file')


if __name__ == '__main__':
  unittest.main()
