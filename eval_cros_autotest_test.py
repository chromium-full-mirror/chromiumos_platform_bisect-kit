# -*- coding: utf-8 -*-
# Copyright 2018 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Test eval_cros_autotest.py script"""

from __future__ import print_function
import unittest

from bisect_kit import errors
from bisect_kit import testing

import eval_cros_autotest


class TestEvalCrosAutotest(unittest.TestCase):
  """Test eval_cros_autotest."""

  def test_parse_results_json(self):
    self.assertEqual(
        eval_cros_autotest.parse_results_json(
            testing.get_testdata_path('tauto_test_result/pass'),
            'platform_FileSize'), (True, None))

    self.assertEqual(
        eval_cros_autotest.parse_results_json(
            testing.get_testdata_path('tauto_test_result/fail'),
            'platform_FileSize'),
        (False, 'FAIL: /mnt/stateful_partition/tempfile file test failed.'))

    with self.assertRaises(errors.ExternalError):
      eval_cros_autotest.parse_results_json(
          testing.get_testdata_path('tauto_test_result/pass'), 'bad_testname')

  def test_parse_cts_results(self):
    self.assertEqual(
        eval_cros_autotest.parse_cts_results(
            testing.get_testdata_path('cts_test_result/cts.pass')),
        (True, None))

    passed, reason = eval_cros_autotest.parse_cts_results(
        testing.get_testdata_path('cts_test_result/gts.fail'))
    self.assertEqual(passed, False)
    self.assertEqual(reason[:20], 'java.lang.Exception:')

    with self.assertRaisesRegex(errors.BisectionTemporaryError,
                                r'Failed to login to Chrome'):
      eval_cros_autotest.parse_cts_results(
          testing.get_testdata_path('cts_test_result/gts.chrome-fail'))

    with self.assertRaises(errors.ExecutionFatalError):
      eval_cros_autotest.parse_cts_results(
          testing.get_testdata_path('cts_test_result/cts.test-not-found'))
    with self.assertRaises(errors.ExecutionFatalError):
      eval_cros_autotest.parse_cts_results(
          testing.get_testdata_path('cts_test_result/gts.test-not-found'))


if __name__ == '__main__':
  unittest.main()
