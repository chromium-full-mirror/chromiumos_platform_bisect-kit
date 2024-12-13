# Copyright 2018 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Test eval_cros_autotest.py script"""

import unittest

from bisect_kit import errors
from bisect_kit import testing
import eval_cros_autotest


class TestEvalCrosAutotest(unittest.TestCase):
    """Test eval_cros_autotest."""

    def test_parse_tauto_results(self):
        self.assertEqual(
            eval_cros_autotest.parse_tauto_results(
                testing.get_testdata_path('tauto_test_result/pass'),
                'platform_FileSize',
            ),
            (True, None),
        )

        self.assertEqual(
            eval_cros_autotest.parse_tauto_results(
                testing.get_testdata_path('tauto_test_result/fail'),
                'platform_FileSize',
            ),
            (False, 'FAIL: /mnt/stateful_partition/tempfile file test failed.'),
        )
        passed, reason = eval_cros_autotest.parse_tauto_results(
            testing.get_testdata_path('tauto_test_result/fail.2'),
            'network_WiFi_Perf.vht80',
        )
        self.assertEqual(passed, False)
        self.assertEqual(
            reason[:50], 'FAIL: Throughput performance too low for test type'
        )

    def test_parse_cts_results(self):
        self.assertEqual(
            eval_cros_autotest.parse_cts_results(
                testing.get_testdata_path('cts_test_result/cts.pass'),
                'cheets_CTS_P.tradefed-run-test',
            ),
            (True, None),
        )

        self.assertEqual(
            eval_cros_autotest.parse_cts_results(
                testing.get_testdata_path(
                    'cts_test_result/cts.assumption-failure'
                ),
                'cheets_CTS_P.tradefed-run-test',
            ),
            (True, None),
        )

        self.assertEqual(
            eval_cros_autotest.parse_cts_results(
                testing.get_testdata_path('cts_test_result/cts.ignored'),
                'cheets_CTS_P.tradefed-run-test',
            ),
            (True, None),
        )

        passed, reason = eval_cros_autotest.parse_cts_results(
            testing.get_testdata_path('cts_test_result/gts.fail'),
            'cheets_GTS.tradefed-run-test',
        )
        self.assertEqual(passed, False)
        self.assertEqual(reason[:20], 'java.lang.Exception:')

        with self.assertRaisesRegex(
            errors.BisectionTemporaryError, r'Failed to login to Chrome'
        ):
            eval_cros_autotest.parse_cts_results(
                testing.get_testdata_path('cts_test_result/gts.chrome-fail'),
                'cheets_GTS.tradefed-run-test',
            )

        self.assertEqual(
            eval_cros_autotest.parse_cts_results(
                testing.get_testdata_path('cts_test_result/gts.test-not-found'),
                'cheets_GTS.tradefed-run-test',
            ),
            (False, 'No result found in the summary'),
        )


if __name__ == '__main__':
    unittest.main()
