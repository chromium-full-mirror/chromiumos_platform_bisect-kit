# Copyright 2019 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Catapult utility.

The main functionality of this module is parsing performance numbers from
test results.

References:
  - Chrome Performance Dashboard Data Format
    https://chromium.googlesource.com/catapult/+/HEAD/dashboard/docs/data-format.md
  - (code) How catapult dashboard parses.
    https://chromium.googlesource.com/catapult/+/HEAD/dashboard/dashboard/add_point.py
"""

from __future__ import annotations

import json
import logging
import math
import os
import re

from bisect_kit import errors
from bisect_kit import util


logger = logging.getLogger(__name__)


def escape_trace_name(name):
    # This follows catapult's behavior.
    return re.sub(r'[\:|=/#&,]', '_', name)


def extract_values_from_trace(name, trace):
    """Extracts values from trace dict.

    Args:
      name: trace name
      trace: trace dict

    Returns:
      list of values; empty list if any value is NaN; None if parsing error or
      unsupported
    """
    if 'type' not in trace:
        logger.warning('trace %s: unknown type', name)
        return None

    if trace['type'] == 'scalar':
        values = [trace.get('value')]
    elif trace['type'] == 'list_of_scalar_values':
        values = trace.get('values')
    else:
        logger.warning(
            'trace %s: type=%s is not supported', name, trace['type']
        )
        return None

    if not values or values[0] is None:
        if trace.get('none_value_reason'):
            logger.debug(
                'trace %s: none value, reason: %s',
                name,
                trace['none_value_reason'],
            )
            return []
        logger.error('trace %s: bad trace dict, no values', name)
        return None

    if any(math.isnan(x) for x in values):
        logger.warning('trace %s: NaN in values; ignore: %s', name, values)
        return []

    return values


def parse_results_chart_json(content):
    """Parses results-chart.json.

    It accepts telemetry's format (with metadata) and autotest's format (without
    metadata).

    Args:
      content: file content of results-chart.json

    Returns:
      dict which maps from metric name to values
    """
    data = json.loads(content)

    # Telemetry's output contains other metadata.
    # Pure autotest's output contains charts data directly.
    # Unifies them here.
    if 'charts' in data:
        data = data['charts']

    result = {}
    for chart_name, chart in data.items():
        # special dict, stores cloud storage link of trace data, skip.
        if chart_name == 'trace':
            continue

        # Follows chromeperf's TIR data hack.
        chart_name = re.sub(r'^(.+)@@(.+)$', r'\2/\1', chart_name)

        for trace_name, trace in chart.items():
            if trace_name == 'summary':
                subtest_name = chart_name
            else:
                subtest_name = chart_name + '/' + escape_trace_name(trace_name)
            result[subtest_name] = extract_values_from_trace(trace_name, trace)

    return result


def match_trace_name(dashboard_name, result_name):
    """Matches metric name from user input and result file.

    Args:
      dashboard_name: user input, may be subtest name from chromeperf or metric
          name from crosbolt
      result_name: parsed name from benchmark result file

    Returns:
      True if both names refer to the same metric
    """
    # crosbolt uses '.' as separator, but chromeperf uses '/'.
    dashboard_name = dashboard_name.replace('.', '/')
    result_name = result_name.replace('.', '/')

    # Chromeperf may append "ref" for reference builds.
    # They are meaningful on dashboard, but should be skip when matching test
    # results.
    if dashboard_name == result_name + '_ref':
        return True
    if dashboard_name == result_name + '/ref':
        return True

    return dashboard_name == result_name


def get_benchmark_values(json_path: str, metric: str) -> list[float]:
    if not os.path.exists(json_path):
        raise errors.BisectionTemporaryError(
            'benchmark result file not found: %s '
            '(the test failed before result generated?)' % json_path
        )

    with open(json_path) as f:
        content = f.read()
    traces = parse_results_chart_json(content)
    for subtest_name, values in traces.items():
        if not match_trace_name(metric, subtest_name):
            continue
        if not values:
            raise errors.ExecutionFatalError(
                'found metric %s but values are bad or unsupported' % metric
            )
        return values
    logger.error('metric %s not found in %s', metric, json_path)
    util.report_similar_candidates('metric name', metric, list(traces.keys()))
    assert 0  # unreachable
