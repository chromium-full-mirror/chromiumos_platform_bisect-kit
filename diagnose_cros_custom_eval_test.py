# Copyright 2026 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Test diagnose_cros_custom_eval.py script."""

import argparse
import unittest
from unittest import mock

from bisect_kit import core
import diagnose_cros_custom_eval


class TestDiagnoseCrosCustomEval(unittest.TestCase):
    """Test diagnose_cros_custom_eval."""

    def test_init_hook_config_update(self):
        """Test that init_hook correctly sets attributes on DiagnoserConfig."""
        cmd = diagnose_cros_custom_eval.DiagnoseCustomEvalCommandLine()

        # Mock options
        opts = argparse.Namespace()
        opts.eval_script = 'test_script'

        # Mock states and config
        cmd.states = mock.Mock()
        cmd.states.config = core.DiagnoserConfig()

        # In old code: self.states.config.update(...)
        # This will raise an AttributeError on a dataclass if .update() is called,
        # but will succeed and set attributes if new code is used.
        cmd.init_hook(opts)

        # Verify config values
        self.assertEqual(cmd.states.config.eval_script, 'test_script')


if __name__ == '__main__':
    unittest.main()
