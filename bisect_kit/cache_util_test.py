# Copyright 2020 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Test cache_util module."""

import unittest

from bisect_kit import cache_util


class TestCache(unittest.TestCase):
    """Test cache_util.Cache."""

    def test_basic_cache(self):
        count = 0

        @cache_util.Cache
        def func(x):
            nonlocal count
            count += 1
            return x

        self.assertEqual(func.__name__, 'func')
        self.assertEqual(func(111), 111)
        self.assertEqual(count, 1)
        self.assertEqual(func(111), 111)
        self.assertEqual(count, 1)
        self.assertEqual(func(222), 222)
        self.assertEqual(count, 2)

    def test_default_disabled(self):
        count = 0

        @cache_util.Cache.default_disabled
        def func(x):
            nonlocal count
            count += 1
            return x

        self.assertEqual(func.__name__, 'func')
        self.assertEqual(func(123), 123)
        self.assertEqual(func(123), 123)
        self.assertEqual(count, 2)

    def test_enable_disable(self):
        count = 0

        @cache_util.Cache.default_disabled
        def func(x):
            nonlocal count
            count += 1
            return x

        self.assertEqual(func(123), 123)
        self.assertEqual(count, 1)
        func.enable_cache()
        self.assertEqual(func(123), 123)
        self.assertEqual(count, 2)

        func.disable_cache()
        self.assertEqual(func(123), 123)
        self.assertEqual(count, 3)
        self.assertEqual(func(123), 123)
        self.assertEqual(count, 4)

        func.enable_cache()
        self.assertEqual(func(123), 123)
        self.assertEqual(count, 5)
        self.assertEqual(func(123), 123)
        self.assertEqual(count, 5)


if __name__ == '__main__':
    unittest.main()
