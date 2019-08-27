# -*- coding: utf-8 -*-
# Copyright 2018 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Test gclient_util module."""

from __future__ import print_function
import os
import shutil
import tempfile
import textwrap
import unittest

from bisect_kit import gclient_util
from bisect_kit import git_util
from bisect_kit import git_util_test


class TestDepsParser(unittest.TestCase):
  """Tests gclient_util.DepsParser."""

  def test_parse_single_deps(self):
    deps_content = textwrap.dedent("""\
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
    """)

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
    deps_content = textwrap.dedent("""\
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
    """)

    parser = gclient_util.DepsParser('/dummy', None)
    deps = parser.parse_single_deps(deps_content)
    # We don't support cipd yet. This test just make sure parsing is not
    # broken.
    self.assertEqual(len(deps.entries), 0)

  def test_parse_recursedeps(self):
    deps_content = textwrap.dedent("""\
        deps = {
          'src/foo': 'http://example.com/foo.git@refs/heads/master',
          'src/bar': 'http://example.com/bar.git@refs/heads/master',
        }

        recursedeps = [
          'src/foo',
          ('src/bar', 'DEPS.bar'),
        ]
    """)

    parser = gclient_util.DepsParser('/dummy', None)
    deps = parser.parse_single_deps(deps_content)
    self.assertIn(('src/foo', 'DEPS'), deps.recursedeps)
    self.assertIn(('src/bar', 'DEPS.bar'), deps.recursedeps)


# TODO(kcwu): refactor into normal unittest.
class TestBug123161903(unittest.TestCase):
  """Reproduce b/123161903.

  Between 73.0.3668.0 and 73.0.3669.0, chrome compile failed because wrong
  commit in assistant repo.
  """

  def setUp(self):
    self.git_mirror = tempfile.mkdtemp()
    self.workdir = tempfile.mkdtemp()
    self.code_storage = gclient_util.GclientCache(self.git_mirror)
    self.target = None

  def tearDown(self):
    shutil.rmtree(self.git_mirror)
    shutil.rmtree(self.workdir)

  def setup_between_3668_3669(self):
    repo_url = {
        'src': 'http://example.com/src',
        'src-internal': 'http://example.com/src-internal',
        'assistant': 'http://example.com/assistant',
    }

    src_mirror = self.code_storage.cached_git_root(repo_url['src'])
    src_git = git_util_test.GitOperation(src_mirror)
    src_git.init()

    src_internal_mirror = self.code_storage.cached_git_root(
        repo_url['src-internal'])
    src_internal_git = git_util_test.GitOperation(src_internal_mirror)
    src_internal_git.init()

    assistant_mirror = self.code_storage.cached_git_root(repo_url['assistant'])
    assistant_git = git_util_test.GitOperation(assistant_mirror)
    assistant_git.init()

    a_3dcc = assistant_git.add_commit(1545172424, '-1', 'foo', '1')
    a_df77 = assistant_git.add_commit(1547073422, 'target', 'foo', '2')
    self.target = a_df77

    src_internal_deps = textwrap.dedent("""\
        deps = {
          'src/assistant': {
            'url': 'http://example.com/assistant@%s',
            'condition': 'checkout_chromeos',
          }
        }
    """)
    i_b4d4 = src_internal_git.add_commit(1547153329, '-1', 'DEPS',
                                         src_internal_deps % a_3dcc)
    _i_96a5 = src_internal_git.add_commit(1547162031, 'target1', 'DEPS',
                                          src_internal_deps % a_df77)
    i_e58c = src_internal_git.add_commit(1547225331, 'target1', 'foo', 'bar')

    src_deps = textwrap.dedent("""\
        deps = {
          'src-internal': 'http://example.com/src-internal@%s',
        }

        recursedeps = [
          'src-internal',
        ]
    """)
    src_git.add_commit(1500000000, 'foo', 'code', 'old')
    s_8ab4 = src_git.add_commit(1547157647, 'foo', 'DEPS', src_deps % i_b4d4)
    _s_e0d0 = src_git.add_commit(1547228986, 'foo', 'DEPS', src_deps % i_e58c)
    _s_f101 = src_git.add_commit(1547233639, 'foo', 'code', 'code')
    src_git.add_commit(1548000000, 'foo', 'code', 'new')

    src_workdir = os.path.join(self.workdir, 'src')
    git_util.clone(src_workdir, src_mirror)
    git_util.checkout_version(src_workdir, s_8ab4)

    return src_workdir

  def test_3668_3669(self):
    src_workdir = self.setup_between_3668_3669()

    parser = gclient_util.DepsParser(self.workdir, self.code_storage)
    old_timestamp, new_timestamp = 1547094405, 1547267292
    for timestamp, path_specs in parser.enumerate_path_specs(
        old_timestamp, new_timestamp, src_workdir):
      if timestamp == 1547228986:
        self.assertEqual(path_specs['src/assistant'].at, self.target)


if __name__ == '__main__':
  unittest.main()
