# -*- coding: utf-8 -*-
# Copyright 2019 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Test codechange module."""

from __future__ import print_function

import unittest

from bisect_kit import cli
from bisect_kit import codechange


class TestCodeChange(unittest.TestCase):
    """Test functions in codechange module."""

    def test_parse_intra_rev(self):
        rev = codechange.make_intra_rev('abc', 'def', 123)
        self.assertEqual(codechange.parse_intra_rev(rev), ('abc', 'def', 123))

        self.assertEqual(codechange.parse_intra_rev('foo'), ('foo', 'foo', 0))

    def test_argtype_intra_rev(self):
        arg_type = codechange.argtype_intra_rev(cli.argtype_re(r'^[a-f]$', 'a'))
        self.assertEqual(arg_type('a'), 'a')
        self.assertEqual(arg_type('a~b/10'), 'a~b/10')

        with self.assertRaises(cli.ArgTypeError):
            arg_type('a~b/c')

        with self.assertRaises(cli.ArgTypeError):
            arg_type('a~g/10')


if __name__ == '__main__':
    unittest.main()
