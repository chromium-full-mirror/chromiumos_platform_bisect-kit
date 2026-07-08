# Copyright 2026 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Test diagnose_cros_autotest.py script."""

import argparse
import unittest
from unittest import mock

from bisect_kit import core
import diagnose_cros_autotest


class TestDiagnoseCrosAutotest(unittest.TestCase):
    """Test diagnose_cros_autotest."""

    def test_init_hook_config_update(self):
        """Test that init_hook correctly sets attributes on DiagnoserConfig."""
        cmd = diagnose_cros_autotest.DiagnoseAutotestCommandLine()

        # Mock options
        opts = argparse.Namespace()
        opts.cts_revision = 'test_revision'
        opts.cts_abi = 'test_abi'
        opts.cts_prefix = 'test_prefix'
        opts.cts_module = 'test_module'
        opts.cts_test = 'test_test'
        opts.cts_timeout = 'test_timeout'

        # Mock states and config
        cmd.states = mock.Mock()
        cmd.states.config = core.DiagnoserConfig()

        # In old code: self.states.config.update(...)
        # This will raise an AttributeError on a dataclass if .update() is called,
        # but will succeed and set attributes if new code is used.
        cmd.init_hook(opts)

        # Verify config values
        self.assertEqual(cmd.states.config.cts_revision, 'test_revision')
        self.assertEqual(cmd.states.config.cts_abi, 'test_abi')
        self.assertEqual(cmd.states.config.cts_prefix, 'test_prefix')
        self.assertEqual(cmd.states.config.cts_module, 'test_module')
        self.assertEqual(cmd.states.config.cts_test, 'test_test')
        self.assertEqual(cmd.states.config.cts_timeout, 'test_timeout')


if __name__ == '__main__':
    unittest.main()
