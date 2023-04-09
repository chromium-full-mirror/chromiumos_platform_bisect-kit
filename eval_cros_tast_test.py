# Copyright 2022 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Test eval_cros_tast.py script"""

from __future__ import print_function

import unittest

from bisect_kit import errors
from bisect_kit import testing
import eval_cros_tast


class TestEvalCrosTast(unittest.TestCase):
    """Test eval_cros_tast."""

    def test_parse_results_json(self):
        self.assertEqual(
            eval_cros_tast.parse_results_json(
                testing.get_testdata_path('tast_test_result/pass'),
                'example.Pass',
            ),
            (True, None),
        )

        self.assertEqual(
            eval_cros_tast.parse_results_json(
                testing.get_testdata_path('tast_test_result/fail'),
                'example.Fail',
            ),
            (False, 'Finally, a fatal error'),
        )

        with self.assertRaises(errors.ExternalError):
            eval_cros_tast.parse_results_json(
                testing.get_testdata_path('tast_test_result/pass'),
                'bad.testname',
            )


if __name__ == '__main__':
    unittest.main()
