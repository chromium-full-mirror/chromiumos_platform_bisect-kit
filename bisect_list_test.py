# -*- coding: utf-8 -*-
# Copyright 2017 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Test bisect_list script."""

from __future__ import print_function
import unittest
from unittest import mock

import six

from bisect_kit import bisector_cli
import bisect_list


@mock.patch('bisect_kit.common.config_logging', mock.Mock())
class TestListDomain(unittest.TestCase):
  """Test ListDomain class."""

  def test_basic(self):
    """Tests basic functionality."""
    # Testing ListDomain via BisectorCommandLineInterface, remember our purpose
    # is testing ListDomain, not bisector. Bisector logic should be tested
    # by its own unit test, not here.
    bisector = bisector_cli.BisectorCommandLine(bisect_list.ListDomain)

    with mock.patch('sys.stdin', six.StringIO('S\nM\nL\nXL\nXXL\n')):
      bisector.main('init', '--old', 'S', '--new', 'XXL')

    bisector.main('config', 'switch', '/bin/false')
    bisector.main('config', 'eval', '/bin/false')
    bisector.main('run', '-1')
    bisector.main('view')


if __name__ == '__main__':
  unittest.main()
