# Copyright 2017 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Test bisect_list script."""

import io
import unittest
from unittest import mock

from bisect_kit import bisector_cli
from bisect_kit import dut_manager as dut_manager_module
from bisect_kit import testing
import bisect_list


@mock.patch('bisect_kit.common.config_logging', mock.Mock())
class TestListDomain(unittest.TestCase):
    """Test ListDomain class."""

    def setUp(self):
        self.session_base_patcher = testing.SessionBasePatcher()
        self.session_base_patcher.patch()

    def tearDown(self):
        self.session_base_patcher.reset()

    def test_basic(self):
        """Tests basic functionality."""
        # Testing ListDomain via BisectorCommandLineInterface, remember our purpose
        # is testing ListDomain, not bisector. Bisector logic should be tested
        # by its own unit test, not here.
        dut_manager = dut_manager_module.DutManager(
            'PreAllocate',
            states=None,
            dut_allocate_spec=None,
            pre_allocated_dut='pre_allocated_dut',
            should_auto_allocate=False,
        )
        bisector = bisector_cli.BisectorCommandLine(
            bisect_list.ListDomain, dut_manager
        )

        with mock.patch('sys.stdin', io.StringIO('S\nM\nL\nXL\nXXL\n')):
            bisector.main('init', '--old', 'S', '--new', 'XXL')

        bisector.main('config', 'switch', '/bin/false')
        bisector.main('config', 'eval', '/bin/false')
        bisector.main('run', '-1')
        bisector.main('view')


if __name__ == '__main__':
    unittest.main()
