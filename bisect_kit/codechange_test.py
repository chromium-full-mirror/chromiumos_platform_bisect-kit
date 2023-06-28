# Copyright 2019 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Test codechange module."""

import unittest

from bisect_kit import cli
from bisect_kit import codechange


class TestCodeChange(unittest.TestCase):
    """Test functions in codechange module."""

    def test_static_constructors(self):
        args = ['dummy_name', 1688000000, 'dummy_path', None, 'dummy_rev']
        self.assertEqual(
            codechange.Spec(codechange.Spec.FIXED, *args),
            codechange.Spec.new_fixed(*args),
        )
        self.assertEqual(
            codechange.Spec(codechange.Spec.FLOAT, *args),
            codechange.Spec.new_float(*args),
        )

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

    def test_normalized_repo_url(self):
        # pylint: disable=protected-access
        url1 = codechange.PathSpec(
            '',
            'https://chrome-internal.googlesource.com/chromeos/path/to/repo',
            '',
        )._normalized_repo_url

        url2 = codechange.PathSpec(
            '',
            'https://chrome-internal.googlesource.com/chromeos/path/to/repo.git',
            '',
        )._normalized_repo_url
        self.assertEqual(url1, url2)

        url3 = codechange.PathSpec(
            '',
            'https://chrome-internal.googlesource.com/a/chromeos/path/to/repo',
            '',
        )._normalized_repo_url
        self.assertEqual(url1, url3)

        url4 = codechange.PathSpec(
            '',
            'https://chrome-internal.googlesource.com/a/chromeos/path/to/repo.git',
            '',
        )._normalized_repo_url
        self.assertEqual(url1, url4)

        url5 = codechange.PathSpec(
            '',
            'https://chrome-internal.googlesource.com/a/chromeos/path/to.git/repo',
            '',
        )._normalized_repo_url
        self.assertNotEqual(url1, url5)

        url6 = codechange.PathSpec(
            '',
            'http://chrome-internal.googlesource.com/a/chromeos/path/to/repo',
            '',
        )._normalized_repo_url
        self.assertNotEqual(url1, url6)
        # pylint: enable=protected-access


if __name__ == '__main__':
    unittest.main()
