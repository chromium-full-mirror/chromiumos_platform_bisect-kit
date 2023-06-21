# Copyright 2023 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Unit tests for plugin module."""

import unittest

from bisect_kit import plugin_util


class TestPlugin(unittest.TestCase):
    """Test the functions in plugin module."""

    def test_register_patch_unregister(self):
        @plugin_util.register('dummy_key')
        def dummy_func_patched():
            return 'patched'

        # pylint: disable=protected-access
        self.assertEqual(
            dummy_func_patched, plugin_util._mapping.get('dummy_key')
        )

        @plugin_util.patch('dummy_key')
        def dummy_func():
            return 'not patched'

        self.assertEqual('patched', dummy_func())

        plugin_util.unregister('dummy_key')
        self.assertIsNone(plugin_util._mapping.get('dummy_key'))

        self.assertEqual('patched', dummy_func())

    def test_register_not_applied(self):
        @plugin_util.register('dummy_key', should_apply=False)
        def dummy_func_patched():
            return 'patched'

        # pylint: disable=protected-access
        self.assertIsNone(plugin_util._mapping.get('dummy_key'))

        @plugin_util.patch('dummy_key')
        def dummy_func():
            return 'not patched'

        self.assertEqual('not patched', dummy_func())

        plugin_util.unregister('dummy_key')
        self.assertIsNone(plugin_util._mapping.get('dummy_key'))

        self.assertEqual('not patched', dummy_func())

    def test_patch_not_applied(self):
        @plugin_util.register('dummy_key')
        def dummy_func_patched():
            return 'patched'

        # pylint: disable=protected-access
        self.assertEqual(
            dummy_func_patched, plugin_util._mapping.get('dummy_key')
        )

        @plugin_util.patch('dummy_key', should_apply=False)
        def dummy_func():
            return 'not patched'

        self.assertEqual('not patched', dummy_func())

        plugin_util.unregister('dummy_key')
        self.assertIsNone(plugin_util._mapping.get('dummy_key'))

        self.assertEqual('not patched', dummy_func())
