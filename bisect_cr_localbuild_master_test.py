# Copyright 2017 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Test bisect_cr_localbuild_master script."""

import os
import shutil
import tempfile
import unittest

import bisect_cr_localbuild_master
from bisect_kit import testing


class TestBisectCrLocalbuildMaster(unittest.TestCase):
    """Test bisect_cr_localbuild_master.py."""

    def setUp(self):
        self.chrome_root = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.chrome_root)

    def test_verify_gclient_dep(self):
        shutil.copy(
            testing.get_testdata_path('gclient_chromium_src'),
            os.path.join(self.chrome_root, '.gclient'),
        )

        self.assertTrue(
            bisect_cr_localbuild_master.verify_gclient_dep(self.chrome_root)
        )


if __name__ == '__main__':
    unittest.main()
