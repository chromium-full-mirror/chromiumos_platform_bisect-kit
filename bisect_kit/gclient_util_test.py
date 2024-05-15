# Copyright 2018 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Test gclient_util module."""

import copy
import os
import shutil
import tempfile
import textwrap
import unittest

from bisect_kit import gclient_util
from bisect_kit import git_util
from bisect_kit import git_util_test
from bisect_kit import testing


class TestDepsParser(unittest.TestCase):
    """Tests gclient_util.DepsParser."""

    def test_parse_single_deps(self):
        deps_content = textwrap.dedent(
            """\
        vars = {
          'chromium_git': 'https://chromium.googlesource.com',
          'buildtools_revision': 'refs/heads/main',
          'checkout_foo': False,
          'checkout_bar': True,
          'speedometer_3.0_revision':
              '5107c739c1b2a008e7293e3b489c4f80a8fb2e01',
        }

        deps = {
          'src/buildtools':
            Var('chromium_git') + '/chromium/buildtools.git' + '@' +
                Var('buildtools_revision'),
          'src/foo': {
            'url': Var('chromium_git') + '/chromium/foo.git' + '@' +
                'refs/heads/main',
            'condition': 'checkout_foo',
          },
          'src/bar': {
            'url': Var('chromium_git') + '/chromium/bar.git' + '@' +
                'refs/heads/main',
            'condition': 'checkout_bar',
          },
          'src/third_party/speedometer/v3.0':
              Var('chromium_git') +
              '/external/github.com/WebKit/Speedometer.git' + '@' +
              Var('speedometer_3.0_revision'),
        }

        recursedeps = [
          'src/buildtools',
          'src/foo',
          'src/bar',
        ]
    """
        )

        parser = gclient_util.DepsParser('/dummy', None)
        deps = parser.parse_single_deps(deps_content)

        buildtools = deps.entries['src/buildtools']
        self.assertEqual(buildtools.dep_type, 'git')
        self.assertEqual(
            buildtools.url,
            'https://chromium.googlesource.com/chromium/buildtools.git'
            '@refs/heads/main',
        )

        self.assertIn(('src/buildtools', 'DEPS'), deps.recursedeps)
        self.assertNotIn(('src/foo', 'DEPS'), deps.recursedeps)
        self.assertIn(('src/bar', 'DEPS'), deps.recursedeps)

    def test_parse_cipd_dep_type(self):
        deps_content = textwrap.dedent(
            """\
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
    """
        )

        parser = gclient_util.DepsParser('/dummy', None)
        deps = parser.parse_single_deps(deps_content)
        # We don't support cipd yet. This test just make sure parsing is not
        # broken.
        self.assertEqual(len(deps.entries), 0)

    def test_parse_gcs_dep_type(self):
        deps_content = textwrap.dedent(
            """\
        deps = {
          'src/foo': {
            'objects': [
                {
                    'object_name': 'e9ce994346c62f8c9fd6d0cecb2b2b0b93b4c2d8',
                    'sha256sum': '519019df16c628c6c0893df18928faeaa3150a9d8f26a787a16ce7c6b2cec2ad',
                    'size_bytes': 601672,
                    'generation': 1664794185950162,
                    'output_file': 'ecommerce.json',
                },
                {
                    'object_name': '756068da5e551516b23b0ba133e55c144f623d38',
                    'sha256sum': '84ef87a8163335a95111d9709306596f96742539da0b34fbe7397f799946a168',
                    'size_bytes': 2156935,
                    'generation': 1664794188995509,
                    'output_file': 'encyclopedia.json',
                },
            ],
            'dep_type': 'gcs',
            'bucket': 'dummy_bucket',
            'condition': 'non_git_source',
          },
        }
    """
        )

        parser = gclient_util.DepsParser('/dummy', None)
        deps = parser.parse_single_deps(deps_content)
        # We don't support gcs yet. This test just make sure parsing is not
        # broken.
        self.assertEqual(len(deps.entries), 0)

    def test_parse_recursedeps(self):
        deps_content = textwrap.dedent(
            """\
        deps = {
          'src/foo': 'http://example.com/foo.git@refs/heads/main',
          'src/bar': 'http://example.com/bar.git@refs/heads/main',
        }

        recursedeps = [
          'src/foo',
          ('src/bar', 'DEPS.bar'),
        ]
    """
        )

        parser = gclient_util.DepsParser('/dummy', None)
        deps = parser.parse_single_deps(deps_content)
        self.assertIn(('src/foo', 'DEPS'), deps.recursedeps)
        self.assertIn(('src/bar', 'DEPS.bar'), deps.recursedeps)

    def test_deps_to_string(self):
        with open(testing.get_testdata_path('DEPS_test/test1.DEPS')) as f:
            deps = f.read()
        parser = gclient_util.DepsParser('/dummy', None)
        deps_generated = parser.parse_single_deps(deps).to_string()
        self.assertDictEqual(
            parser.load_single_deps(deps),
            parser.load_single_deps(deps_generated),
        )

    def test_flatten_multiple_deps(self):
        # create three repos: GIT_PATH, GIT_PATH/A and GIT_PATH/B
        # and saves each deps to corresponding repo
        test_dir = testing.get_testdata_path('DEPS_test/test2_recursive')

        try:
            repos = tempfile.mkdtemp()
            for cur_dir, _, files in sorted(os.walk(test_dir)):
                repo = os.path.join(repos, os.path.relpath(cur_dir, test_dir))
                os.makedirs(repo, exist_ok=True)
                git = git_util_test.GitOperation(repo)
                git.init(initial_branch=gclient_util.DEFAULT_BRANCH_NAME)

                for filename in files:
                    with open(os.path.join(cur_dir, filename)) as f:
                        content = f.read().replace('GIT_PATH', repos)
                        git.add_commit(
                            '2018-01-01T00:00:00', 'add', filename, content
                        )

            parser = gclient_util.DepsParser(None, None)
            for _, f in parser.enumerate_gclient_solutions(
                1515024000, 1577923200, repos
            ):
                forest = copy.deepcopy(f)
            deps = parser.flatten(forest, repos)
        finally:
            shutil.rmtree(repos)

        with open(
            testing.get_testdata_path('DEPS_test/test2_expected.DEPS')
        ) as f:
            expected = f.read().replace('GIT_PATH', repos)
        self.assertDictEqual(
            parser.load_single_deps(deps.to_string()),
            parser.load_single_deps(expected),
        )


class TestDep(unittest.TestCase):
    """Test class for DEPS parsing."""

    def test_simple(self):
        url = 'http://example.com@12345'
        dep = gclient_util.Dep('src', {}, url)
        self.assertTrue(dep == dep)  # pylint: disable=comparison-with-itself
        self.assertEqual(dep.url, url)
        self.assertEqual(dep.parse_url(), ('http://example.com', '12345'))
        # True by default.
        self.assertTrue(dep.eval_condition())

        dep = gclient_util.Dep('src', {}, {'url': url})
        self.assertTrue(dep.eval_condition())

    def test_variable(self):
        url = 'http://example.com@{vars[var1]}'
        dep = gclient_util.Dep('src', {'var1': 'foobar'}, url)
        self.assertEqual(dep.url, 'http://example.com@foobar')

    def test_eval_condition(self):
        url = 'http://example.com@12345'
        dep = gclient_util.Dep('src', {}, {'url': url, 'condition': 'True'})
        self.assertTrue(dep.eval_condition())
        dep = gclient_util.Dep('src', {}, {'url': url, 'condition': 'False'})
        self.assertFalse(dep.eval_condition())

        # variable
        dep = gclient_util.Dep(
            'src', {'var1': True}, {'url': url, 'condition': 'var1'}
        )
        self.assertTrue(dep.eval_condition())
        dep = gclient_util.Dep(
            'src', {'var1': False}, {'url': url, 'condition': 'var1'}
        )
        self.assertFalse(dep.eval_condition())

        # simple expression
        dep = gclient_util.Dep(
            'src', {'var1': 'abc'}, {'url': url, 'condition': 'var1 == "abc"'}
        )
        self.assertTrue(dep.eval_condition())

        dep = gclient_util.Dep(
            'src',
            {'var1': 'a or b', 'a': False, 'b': False},
            {'url': url, 'condition': 'var1'},
        )
        self.assertFalse(dep.eval_condition())

        dep = gclient_util.Dep(
            'src',
            {'var1': 'abc', 'abc': 'foo'},
            {'url': url, 'condition': 'var1 == "foo"'},
        )
        self.assertTrue(dep.eval_condition())

        dep = gclient_util.Dep(
            'src', {'var1': 'abc'}, {'url': url, 'condition': 'var1 == "abc"'}
        )
        self.assertTrue(dep.eval_condition())


class TestTimeSeriesTree(unittest.TestCase):
    """Test class for TimeSeriesTree."""

    def test_trivial(self):
        base = 'http://example.com'
        deps = gclient_util.Deps()
        deps.entries['src'] = gclient_util.Dep('src', {}, base)
        tstree = gclient_util.TimeSeriesTree(None, 'root', 0, 1000)
        tstree.add_snapshot(0, deps, [])
        tstree.no_more_snapshot()

        self.assertEqual(tstree.subtrees, [])

    def test_simple(self):
        base = 'http://example.com'
        deps = gclient_util.Deps()
        deps.entries['src'] = gclient_util.Dep('src', {}, base)
        tstree = gclient_util.TimeSeriesTree(None, 'root', 0, 0)
        tstree.add_snapshot(0, deps, [])
        tstree.no_more_snapshot()

        self.assertEqual(tstree.subtrees, [])


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
        src_git.init(initial_branch=gclient_util.DEFAULT_BRANCH_NAME)

        src_internal_mirror = self.code_storage.cached_git_root(
            repo_url['src-internal']
        )
        src_internal_git = git_util_test.GitOperation(src_internal_mirror)
        src_internal_git.init(initial_branch=gclient_util.DEFAULT_BRANCH_NAME)

        assistant_mirror = self.code_storage.cached_git_root(
            repo_url['assistant']
        )
        assistant_git = git_util_test.GitOperation(assistant_mirror)
        assistant_git.init(initial_branch=gclient_util.DEFAULT_BRANCH_NAME)

        a_3dcc = assistant_git.add_commit(1545172424, '-1', 'foo', '1')
        a_df77 = assistant_git.add_commit(1547073422, 'target', 'foo', '2')
        self.target = a_df77

        src_internal_deps = textwrap.dedent(
            """\
        deps = {
          'src/assistant': {
            'url': 'http://example.com/assistant@%s',
            'condition': 'checkout_chromeos',
          }
        }
    """
        )
        i_b4d4 = src_internal_git.add_commit(
            1547153329, '-1', 'DEPS', src_internal_deps % a_3dcc
        )
        _i_96a5 = src_internal_git.add_commit(
            1547162031, 'target1', 'DEPS', src_internal_deps % a_df77
        )
        i_e58c = src_internal_git.add_commit(
            1547225331, 'target1', 'foo', 'bar'
        )

        src_deps = textwrap.dedent(
            """\
        deps = {
          'src-internal': 'http://example.com/src-internal@%s',
        }

        recursedeps = [
          'src-internal',
        ]
    """
        )
        src_git.add_commit(1500000000, 'foo', 'code', 'old')
        s_8ab4 = src_git.add_commit(
            1547157647, 'foo', 'DEPS', src_deps % i_b4d4
        )
        _s_e0d0 = src_git.add_commit(
            1547228986, 'foo', 'DEPS', src_deps % i_e58c
        )
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
            old_timestamp, new_timestamp, src_workdir
        ):
            if timestamp == 1547228986:
                self.assertEqual(path_specs['src/assistant'].rev, self.target)


if __name__ == '__main__':
    unittest.main()
