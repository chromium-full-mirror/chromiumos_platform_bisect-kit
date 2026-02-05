#!/usr/bin/env python3
# Copyright 2025 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Tests for gerrit_util.py."""

import io
import json
import logging
import unittest
from unittest.mock import MagicMock
from unittest.mock import patch
import urllib.error
import urllib.request

from bisect_kit import gerrit_util


class TestGerritUtil(unittest.TestCase):
    """Tests for gerrit_util.py."""

    def setUp(self):
        self.logger_to_test = logging.getLogger(gerrit_util.__name__)

        # Store original handlers and level to restore them after each test
        self.original_handlers = self.logger_to_test.handlers[:]
        self.original_level = self.logger_to_test.level

        self.logger_to_test.setLevel(logging.ERROR)
        self.log_capture_string = io.StringIO()
        self.string_io_handler = logging.StreamHandler(self.log_capture_string)
        self.logger_to_test.addHandler(self.string_io_handler)

    def tearDown(self):
        self.logger_to_test.removeHandler(self.string_io_handler)
        self.log_capture_string.close()

        # Restore original handlers and level
        self.logger_to_test.handlers = self.original_handlers
        self.logger_to_test.level = self.original_level

    def get_captured_logs(self):
        return self.log_capture_string.getvalue()

    def test_get_gerrit_cl_info_success(self):
        sample_data = {
            "subject": "Test CL",
            "project": "test/project",
            "_number": 123,
        }
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.read.return_value = (
            gerrit_util.GERRIT_JSON_PREFIX + json.dumps(sample_data)
        ).encode('utf-8')

        mock = MagicMock()
        mock.__enter__ = MagicMock(return_value=mock_response)

        with patch('urllib.request.urlopen', return_value=mock) as mock_urlopen:
            result = gerrit_util.get_gerrit_cl_info("123")
            self.assertEqual(result, sample_data)
            mock_urlopen.assert_called_once()
            request_arg = mock_urlopen.call_args[0][0]
            self.assertEqual(
                request_arg.full_url,
                "https://chromium-review.googlesource.com/changes/123/detail?o=CURRENT_REVISION&o=DETAILED_LABELS&o=MESSAGES",
            )

    def test_get_gerrit_cl_info_success_url_with_slash(self):
        sample_data = {"subject": "Test CL"}
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.read.return_value = (
            gerrit_util.GERRIT_JSON_PREFIX + json.dumps(sample_data)
        ).encode('utf-8')

        mock = MagicMock()
        mock.__enter__ = MagicMock(return_value=mock_response)

        with patch('urllib.request.urlopen', return_value=mock) as mock_urlopen:
            result = gerrit_util.get_gerrit_cl_info("123")  # URL ends with /
            self.assertEqual(result, sample_data)
            request_arg = mock_urlopen.call_args[0][0]
            self.assertEqual(
                request_arg.full_url,
                "https://chromium-review.googlesource.com/changes/123/detail?o=CURRENT_REVISION&o=DETAILED_LABELS&o=MESSAGES",
            )

    def test_get_gerrit_cl_info_http_error(self):
        http_error = urllib.error.HTTPError("url", 404, "Not Found", {}, None)
        # Mock the read method for the HTTPError object itself
        http_error.read = MagicMock(return_value=b"Error content")  # type: ignore

        with patch('urllib.request.urlopen', side_effect=http_error):
            result = gerrit_util.get_gerrit_cl_info("123")
            self.assertIsNone(result)

            log_contents = self.get_captured_logs()
            self.assertIn("HTTP error occurred: 404 Not Found", log_contents)
            self.assertIn("Response content: Error content", log_contents)

    def test_get_gerrit_cl_info_json_decode_error(self):
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.read.return_value = (
            gerrit_util.GERRIT_JSON_PREFIX + "invalid json"
        ).encode('utf-8')

        mock = MagicMock()
        mock.__enter__ = MagicMock(return_value=mock_response)

        with patch('urllib.request.urlopen', return_value=mock):
            result = gerrit_util.get_gerrit_cl_info("123")
            self.assertIsNone(result)

            log_contents = self.get_captured_logs()
            self.assertIn("Error decoding JSON response", log_contents)
            self.assertIn(
                "Response content that failed to parse: invalid json",
                log_contents,
            )

    def test_get_gerrit_cl_info_non_200_status(self):
        mock_response = MagicMock()
        mock_response.status = 500
        mock_response.reason = "Internal Server Error"
        mock_response.read.return_value = b"Server error details"

        mock = MagicMock()
        mock.__enter__ = MagicMock(return_value=mock_response)

        with patch('urllib.request.urlopen', return_value=mock):
            result = gerrit_util.get_gerrit_cl_info("123")
            self.assertIsNone(result)

            log_contents = self.get_captured_logs()
            self.assertIn(
                "HTTP error occurred: 500 Internal Server Error", log_contents
            )
            self.assertIn(
                "Response content: Server error details", log_contents
            )


if __name__ == '__main__':
    unittest.main()
