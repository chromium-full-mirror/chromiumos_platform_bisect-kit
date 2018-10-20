# -*- coding: utf-8 -*-
# Copyright 2018 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Test gclient_util module."""

from __future__ import print_function
import unittest
import textwrap

from bisect_kit import gclient_util


class TestDepsParser(unittest.TestCase):
  """Tests gclient_util.DepsParser."""

  def test_parse_single_deps(self):
    deps_content = textwrap.dedent('''\
        vars = {
          'chromium_git': 'https://chromium.googlesource.com',
          'buildtools_revision': 'refs/heads/master',
          'checkout_foo': False,
          'checkout_bar': True,
        }

        deps = {
          'src/buildtools':
            Var('chromium_git') + '/chromium/buildtools.git' + '@' +
                Var('buildtools_revision'),
          'src/foo': {
            'url': Var('chromium_git') + '/chromium/foo.git' + '@' +
                'refs/heads/master',
            'condition': 'checkout_foo',
          },
          'src/bar': {
            'url': Var('chromium_git') + '/chromium/bar.git' + '@' +
                'refs/heads/master',
            'condition': 'checkout_bar',
          },
        }

        recursedeps = [
          'src/buildtools',
          'src/foo',
          'src/bar',
        ]
    ''')

    parser = gclient_util.DepsParser('/dummy', None)
    deps = parser.parse_single_deps(deps_content)

    buildtools = deps.entries['src/buildtools']
    self.assertEqual(buildtools.dep_type, 'git')
    self.assertEqual(
        buildtools.url,
        'https://chromium.googlesource.com/chromium/buildtools.git'
        '@refs/heads/master')

    self.assertIn(('src/buildtools', 'DEPS'), deps.recursedeps)
    self.assertNotIn(('src/foo', 'DEPS'), deps.recursedeps)
    self.assertIn(('src/bar', 'DEPS'), deps.recursedeps)

  def test_parse_dep_type(self):
    deps_content = textwrap.dedent('''\
        deps = {
          'src/foo': {
            'packages': [
              {
                'package': 'foo',
                'version': 'version:1.0',
              }
            ],
            'dep_type': 'cipd',
            'condition': 'False',
          },
        }
    ''')

    parser = gclient_util.DepsParser('/dummy', None)
    deps = parser.parse_single_deps(deps_content)
    # We don't support cipd yet. This test just make sure parsing is not
    # broken.
    self.assertEqual(len(deps.entries), 0)

  def test_parse_recursedeps(self):
    deps_content = textwrap.dedent('''\
        deps = {
          'src/foo': 'http://example.com/foo.git@refs/heads/master',
          'src/bar': 'http://example.com/bar.git@refs/heads/master',
        }

        recursedeps = [
          'src/foo',
          ('src/bar', 'DEPS.bar'),
        ]
    ''')

    parser = gclient_util.DepsParser('/dummy', None)
    deps = parser.parse_single_deps(deps_content)
    self.assertIn(('src/foo', 'DEPS'), deps.recursedeps)
    self.assertIn(('src/bar', 'DEPS.bar'), deps.recursedeps)


if __name__ == '__main__':
  unittest.main()
