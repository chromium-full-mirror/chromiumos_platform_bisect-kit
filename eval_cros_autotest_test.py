# -*- coding: utf-8 -*-
# Copyright 2018 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Test eval_cros_autotest.py script"""

from __future__ import print_function
import unittest

from bisect_kit import testing

import eval_cros_autotest


class TestEvalCrosAutotest(unittest.TestCase):
  """Test eval_cros_autotest."""

  def test_parse_test_report_log(self):
    result = open(testing.get_testdata_path('test_report.log')).read()
    self.assertEqual(
        eval_cros_autotest.parse_test_report_log(result,
                                                 'seconds_kernel_to_login'),
        (True, [4.13, 4.08, 4.07, 4.11, 4.09, 4.0, 4.1, 4.07, 4.14, 4.1]))

    self.assertEqual(
        eval_cros_autotest.parse_test_report_log(result, None), (True, None))


if __name__ == '__main__':
  unittest.main()
