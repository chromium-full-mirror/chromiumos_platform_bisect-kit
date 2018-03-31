# Copyright 2017 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Test common module."""

from __future__ import print_function
import argparse
import logging
import os
import tempfile
import unittest

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

  def test_logging(self):
    common.init()
    parser = argparse.ArgumentParser()
    common.add_common_arguments(parser)
    opts = parser.parse_args(['--log_file', self.log_file])
    common.config_logging(opts)

    logger.debug('test')
    self.assertIn('test', open(self.log_file).read())


if __name__ == '__main__':
  unittest.main()
