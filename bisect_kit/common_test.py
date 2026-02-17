# Copyright 2017 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Test common module."""

import logging
import os
import pathlib
import tempfile
import unittest
from unittest import mock

from bisect_kit import cli
from bisect_kit import common


logger = logging.getLogger(__name__)


class TestCommon(unittest.TestCase):
    """Test functions in common module."""

    def setUp(self):
        self.log_file = tempfile.mktemp()

    def tearDown(self):
        pathlib.Path(self.log_file).unlink(missing_ok=True)

    @mock.patch('bisect_kit.common._logging_configured', False, create=True)
    def test_logging(self):
        parents = [cli.create_common_argument_parser()]
        parser = cli.ArgumentParser(parents=parents)
        opts = parser.parse_args(['--log-file', self.log_file])
        common.config_logging(opts)

        logger.debug('test')
        with open(self.log_file) as f:
            self.assertIn('test', f.read())

    @mock.patch('bisect_kit.common._logging_configured', True, create=True)
    def test_logging_configured(self):
        parents = [cli.create_common_argument_parser()]
        parser = cli.ArgumentParser(parents=parents)
        opts = parser.parse_args(['--log-file', self.log_file])
        # Should do nothing, since the logging is considered to be configured.
        common.config_logging(opts)

        logger.debug('test')
        # Log file should not have been created, since the logging is not
        # configured to route to the file.
        self.assertFalse(os.path.exists(self.log_file))

    def test_is_valid_uuid(self):
        self.assertTrue(
            common.is_valid_uuid('683b2c93-7c88-4ac5-b4ed-206137ac859b')
        )

        # not uuid version 4
        self.assertFalse(
            common.is_valid_uuid('683b2c93-7c88-3ac5-b4ed-206137ac859b')
        )
        # malformed
        self.assertFalse(
            common.is_valid_uuid('0683b2c93-7c88-4ac5-b4ed-206137ac859b')
        )

    def test_path_factory(self):
        path_facory = common.ProjectPathFactory(
            'dummy_session',
            '/dummy_work_base',
            '/dummy_mirror_base',
            '/dummy_chrome_work_base',
        )
        self.assertEqual(path_facory.work_base, '/dummy_work_base')
        self.assertEqual(path_facory.session_name, 'dummy_session')
        self.assertEqual(path_facory.mirror_base, '/dummy_mirror_base')
        self.assertEqual(
            path_facory.get_session_workdir('dummy_session'),
            '/dummy_work_base/dummy_session',
        )
        self.assertEqual(
            path_facory.get_session_file('dummy_file'),
            '/dummy_work_base/dummy_session/dummy_file',
        )
        self.assertEqual(
            path_facory.get_chromeos_mirror(),
            '/dummy_mirror_base/chromeos',
        )
        self.assertEqual(
            path_facory.get_chromeos_tree(),
            '/dummy_work_base/dummy_session/chromeos',
        )
        self.assertEqual(
            path_facory.get_android_mirror('dummy_branch'),
            '/dummy_mirror_base/android.dummy_branch',
        )
        self.assertEqual(
            path_facory.get_android_tree('dummy_branch'),
            '/dummy_work_base/dummy_session/android.dummy_branch',
        )
        self.assertEqual(
            path_facory.get_chrome_cache(),
            '/dummy_mirror_base/chrome',
        )
        self.assertEqual(
            path_facory.get_chrome_tree(),
            '/dummy_chrome_work_base/dummy_session/chrome',
        )

    def test_get_session_file(self):
        cwd = pathlib.Path.cwd()
        self.assertEqual(
            common.get_session_log_path('session_name', 'file'),
            str(cwd / common.DEFAULT_LOG_BASE / 'session_name' / 'file'),
        )
        self.assertEqual(
            common.get_session_log_path('session_name', 'subdir/file'),
            str(
                cwd
                / common.DEFAULT_LOG_BASE
                / 'session_name'
                / 'subdir'
                / 'file'
            ),
        )
        self.assertEqual(
            common.get_session_log_path('session_name'),
            str(cwd / common.DEFAULT_LOG_BASE / 'session_name'),
        )

    def test_get_session_cache_dir(self):
        cwd = pathlib.Path.cwd()
        self.assertEqual(
            common.get_session_cache_dir('session_name'),
            str(
                cwd
                / common.DEFAULT_LOG_BASE
                / 'session_name'
                / common.DEFAULT_CACHE_DIR
            ),
        )


if __name__ == '__main__':
    unittest.main()
