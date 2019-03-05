# -*- coding: utf-8 -*-
# Copyright 2017 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Test cros_util module."""

from __future__ import print_function
import fnmatch
import logging
import subprocess
import unittest

import mock

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

  def test_is_cros_localbuild_version(self):
    self.assertTrue(
        cros_util.is_cros_localbuild_version('1234.0.2018_01_01_1259'))
    self.assertFalse(cros_util.is_cros_localbuild_version('1234.0.0'))
    self.assertFalse(cros_util.is_cros_localbuild_version('R99-1234.0.0'))

  def test_is_cros_version(self):
    self.assertTrue(cros_util.is_cros_version('1234.0.0'))
    self.assertTrue(cros_util.is_cros_version('R99-1234.0.0'))

  def test_make_version_and_split(self):
    full_version = 'R33-1234.0.0'
    milestone, short_version = cros_util.version_split(full_version)

    self.assertEqual(milestone, '33')
    self.assertEqual(short_version, '1234.0.0')

    self.assertEqual(
        cros_util.make_cros_full_version(milestone, short_version),
        full_version)

  def test_argtype_cros_version(self):
    self.assertEqual(cros_util.argtype_cros_version('9876.0.0'), '9876.0.0')
    self.assertEqual(
        cros_util.argtype_cros_version('R99-9876.0.0'), 'R99-9876.0.0')
    with self.assertRaises(cli.ArgTypeError):
      cros_util.argtype_cros_version('foobar')

  def test_lsb_release(self):
    sample = open(testing.get_testdata_path('lsb-release')).read()
    with mock.patch('bisect_kit.util.check_output', return_value=sample):
      dummy_dut = 'dummy'
      self.assertTrue(cros_util.is_dut(dummy_dut))
      self.assertEqual(cros_util.query_dut_board(dummy_dut), 'samus')
      self.assertEqual(
          cros_util.query_dut_short_version(dummy_dut), '12345.0.0')

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
          cros_util.version_to_full('samus', 'R64-10123.0.0'), 'R64-10123.0.0')
      self.assertEqual(
          cros_util.version_to_full('samus', '10123.0.0'), 'R64-10123.0.0')

  def test_version_info(self):
    sample = open(testing.get_testdata_path('partial-metadata.json')).read()
    with mock.patch.object(cros_util, 'gsutil', return_value=sample):
      info = cros_util.version_info('caroline', '9462.0.0')
      self.assertEqual(info[cros_util.VERSION_KEY_CROS_SHORT_VERSION],
                       '9462.0.0')

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

  def test_chromeos_prebuilt_versions(self):
    archive_base = 'gs://chromeos-image-archive/foo-release/'
    release_base = 'gs://chromeos-releases/stable-channel/foo/'

    gs_files = [
        release_base + '9995.0.0/ChromeOS-test-R63-9994.0.0-foo.tar.xz',
        release_base + '9995.0.0/ChromeOS-test-R63-9995.0.0-foo.tar.xz',
        release_base + '9997.0.0/ChromeOS-test-R63-9997.0.0-foo.tar.xz',
        archive_base + 'R63-9999.0.0/chromiumos_test_image.tar.xz',
        archive_base + 'R63-9999.1.0/chromiumos_test_image.tar.xz',
        archive_base + 'R63-10001.0.0/chromiumos_test_image.tar.xz',
    ]

    def gsutil_ls(*args, **_kwargs):
      assert len(args) == 1
      result = []
      for gs_path in gs_files:
        if fnmatch.fnmatchcase(gs_path, args[0]):
          result.append(gs_path)
      return result

    def version_to_full(_board, version):
      return 'R63-' + version

    with mock.patch.object(
        cros_util,
        'list_prebuilt_from_chromeos_releases',
        return_value=[
            ('9994.0.0',
             release_base + '9995.0.0/ChromeOS-test-R63-9994.0.0-foo.tar.xz'),
            ('9995.0.0',
             release_base + '9995.0.0/ChromeOS-test-R63-9995.0.0-foo.tar.xz'),
            ('9997.0.0',
             release_base + '9997.0.0/ChromeOS-test-R63-9997.0.0-foo.tar.xz'),
        ]):
      with mock.patch.object(
          cros_util,
          'list_prebuilt_from_image_archive',
          return_value=[
              ('R63-9998.0.0',
               archive_base + 'R63-9998.0.0/chromiumos_test_image.tar.xz'),
              ('R63-9999.0.0',
               archive_base + 'R63-9999.0.0/chromiumos_test_image.tar.xz'),
              ('R63-9999.1.0',
               archive_base + 'R63-9999.1.0/chromiumos_test_image.tar.xz'),
              ('R63-10000.0.0',
               archive_base + 'R63-10000.0.0/chromiumos_test_image.tar.xz'),
              ('R63-10001.0.0',
               archive_base + 'R63-10001.0.0/chromiumos_test_image.tar.xz'),
          ]):
        with mock.patch.multiple(
            cros_util, gsutil_ls=gsutil_ls, version_to_full=version_to_full):
          # Both ends are in gs://chromeos-image-archive
          self.assertEqual(
              cros_util.list_chromeos_prebuilt_versions(
                  'foo', '9998.0.0', '10001.0.0', only_good_build=False), [
                      'R63-9998.0.0', 'R63-9999.0.0', 'R63-10000.0.0',
                      'R63-10001.0.0'
                  ])

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


if __name__ == '__main__':
  unittest.main()
