# Copyright 2019 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Test catapult_util module."""

import unittest

from bisect_kit import catapult_util
from bisect_kit import testing


class TestCatapultUtil(unittest.TestCase):
    """Test functions in catapult_util module."""

    def test_extract_values_from_trace(self):
        self.assertEqual(
            catapult_util.extract_values_from_trace(
                'foo',
                {
                    'type': 'scalar',
                    'value': 0,
                },
            ),
            [0],
        )

        self.assertEqual(
            catapult_util.extract_values_from_trace(
                'foo',
                {
                    'type': 'list_of_scalar_values',
                    'values': [0, 3, 2],
                },
            ),
            [0, 3, 2],
        )

    def test_extract_values_from_trace_nan(self):
        self.assertEqual(
            catapult_util.extract_values_from_trace(
                'foo',
                {
                    'type': 'scalar',
                    'value': float('nan'),
                    'none_value_reason': 'unknown',
                },
            ),
            [],
        )

        self.assertEqual(
            catapult_util.extract_values_from_trace(
                'foo',
                {
                    'type': 'list_of_scalar_values',
                    'values': [float('nan')],
                },
            ),
            [],
        )

    def test_parse_results_chart_json(self):
        pass

    def test_match_trace_name(self):
        # chromeperf uses / as separator.
        self.assertTrue(
            catapult_util.match_trace_name('Total/Score', 'Total/Score')
        )
        # crosbolt uses . as separator.
        self.assertTrue(
            catapult_util.match_trace_name('Total.Score', 'Total/Score')
        )

        # v8 test
        self.assertTrue(
            catapult_util.match_trace_name(
                'Emscripten/Box2d/ref', 'Emscripten/Box2d'
            )
        )
        # jetstream test
        self.assertTrue(
            catapult_util.match_trace_name(
                'box2d/http___browserbench.org_JetStream__ref',
                'box2d/http___browserbench.org_JetStream_',
            )
        )

    def test_get_benchmark_values(self):
        json_path = testing.get_testdata_path('results-chart.telemetry.json')
        self.assertTrue(
            catapult_util.get_benchmark_values(
                json_path, 'frame_time_discrepancy'
            )
        )

    def test_get_benchmark_values_list(self):
        json_path = testing.get_testdata_path(
            'results-chart.autotest-without-graph.json'
        )
        self.assertEqual(
            len(
                catapult_util.get_benchmark_values(
                    json_path, 'boot_progress_start'
                )
            ),
            10,
        )

    def test_get_benchmark_values_regressions(self):
        json_path = testing.get_testdata_path('results-chart.octane.json')
        self.assertTrue(
            catapult_util.get_benchmark_values(json_path, 'Total.Score')
        )

        json_path = testing.get_testdata_path(
            'results-chart.cros_ui_smoothness.json'
        )
        self.assertTrue(
            catapult_util.get_benchmark_values(
                json_path, 'frame_times_count/overview_js_poster_circle'
            )
        )

    def test_get_benchmark_values_tir(self):
        json_path = testing.get_testdata_path(
            'results-chart.page_cycler_v2.json'
        )
        self.assertTrue(
            catapult_util.get_benchmark_values(
                json_path, 'timeToFirstContentfulPaint_min'
            )
        )
        self.assertTrue(
            catapult_util.get_benchmark_values(
                json_path, 'timeToFirstContentfulPaint_min/warm'
            )
        )


if __name__ == '__main__':
    unittest.main()
