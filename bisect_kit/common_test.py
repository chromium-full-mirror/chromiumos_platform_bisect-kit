# -*- coding: utf-8 -*-
# Copyright 2017 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Test common module."""

from __future__ import print_function

import argparse
import logging
import os
import tempfile
import unittest
from unittest import mock

from bisect_kit import common
from bisect_kit import configure


logger = logging.getLogger(__name__)


class TestCommon(unittest.TestCase):
    """Test functions in common module."""

    def setUp(self):
        self.log_file = tempfile.mktemp()

    def tearDown(self):
        if os.path.exists(self.log_file):
            os.unlink(self.log_file)
        configure.reset()

    @mock.patch(
        'bisect_kit.common.config_logging.configured', False, create=True
    )
    def test_logging(self):
        common.init()
        parents = [common.common_argument_parser]
        parser = argparse.ArgumentParser(parents=parents)
        opts = parser.parse_args(['--log_file', self.log_file])
        common.config_logging(opts)

        logger.debug('test')
        with open(self.log_file) as f:
            self.assertIn('test', f.read())


if __name__ == '__main__':
    unittest.main()
