# Copyright 2017 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Test bisect_cr_localbuild_master script."""

from __future__ import print_function
import unittest
import shutil
import tempfile
import os

from bisect_kit import testing

import bisect_cr_localbuild_master


class TestBisectCrLocalbuildMaster(unittest.TestCase):

  def setUp(self):
    self.chrome_root = tempfile.mkdtemp()

  def tearDown(self):
    shutil.rmtree(self.chrome_root)

  def test_verify_gclient_dep(self):
    shutil.copy(
        testing.get_testdata_path('gclient_master'),
        os.path.join(self.chrome_root, '.gclient'))

    self.assertTrue(
        bisect_cr_localbuild_master.verify_gclient_dep(self.chrome_root))


if __name__ == '__main__':
  unittest.main()
