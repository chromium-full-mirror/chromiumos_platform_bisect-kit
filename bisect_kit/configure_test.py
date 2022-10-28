# -*- coding: utf-8 -*-
# Copyright 2017 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Configure module."""

from __future__ import print_function

import contextlib
import os
import shutil
import tempfile
import textwrap
import unittest
from unittest import mock

from bisect_kit import common
from bisect_kit import configure


@contextlib.contextmanager
def scoped_file(folder, filename, content=''):
    """Helper to create file and auto delete after use.

    This is with-context manager. It creates file with given content when at
    start. After the context finished, it will delete the created file.

    Args:
      folder: where to put the file.
      filename: filename to create.
      content: file content.

    Yields:
      full path of created file.
    """
    full_path = os.path.join(folder, filename)
    with open(full_path, 'w') as f:
        f.write(content)
    yield full_path
    os.unlink(full_path)


class TestConfigure(unittest.TestCase):
    """Test configure.Configure class."""

    def setUp(self):
        assert configure.CONFIG_ENV_NAME not in os.environ, (
            '"%s" should not be set during unittest' % configure.CONFIG_ENV_NAME
        )
        self.oldcwd = os.getcwd()
        self.cwddir = tempfile.mkdtemp()
        self.progdir = tempfile.mkdtemp()
        # Make sure current working directory and is clean.
        os.chdir(self.cwddir)

    def tearDown(self):
        os.chdir(self.oldcwd)
        shutil.rmtree(self.progdir)
        shutil.rmtree(self.cwddir)

    def test_scoped_file(self):
        with scoped_file(self.cwddir, 'foo', 'abc') as path:
            assert os.path.exists(path)
            with open(path) as f:
                self.assertEqual(f.read(), 'abc')
        assert not os.path.exists(path)

    def test_search_config_file(self):
        args = [os.path.join(self.progdir, 'foo.py')]

        self.assertEqual(configure.search_config_file(args), None)

        with scoped_file(self.cwddir, 'foo') as path:
            self.assertEqual(
                configure.search_config_file(args + ['--rc', path]), path
            )

        with scoped_file(self.cwddir, 'bar') as path:
            with mock.patch.dict(os.environ, {configure.CONFIG_ENV_NAME: path}):
                self.assertEqual(configure.search_config_file(args), path)

        with scoped_file(
            self.cwddir, configure.DEFAULT_CONFIG_FILENAME
        ) as path:
            self.assertEqual(configure.search_config_file(args), path)

        with scoped_file(
            self.progdir, configure.DEFAULT_CONFIG_FILENAME
        ) as path:
            self.assertEqual(configure.search_config_file(args), path)

    def test_search_config_file_precedence(self):
        args = [os.path.join(self.progdir, 'foo.py')]

        self.assertEqual(configure.search_config_file(args), None)

        with scoped_file(
            self.progdir, configure.DEFAULT_CONFIG_FILENAME
        ) as path1:
            self.assertEqual(configure.search_config_file(args), path1)

            with scoped_file(
                self.cwddir, configure.DEFAULT_CONFIG_FILENAME
            ) as path2:
                self.assertEqual(configure.search_config_file(args), path2)

                with scoped_file(self.cwddir, 'bar') as path3:
                    with mock.patch.dict(
                        os.environ, {configure.CONFIG_ENV_NAME: 'none'}
                    ):
                        self.assertEqual(
                            configure.search_config_file(args), None
                        )

                    with mock.patch.dict(
                        os.environ, {configure.CONFIG_ENV_NAME: path3}
                    ):
                        self.assertEqual(
                            configure.search_config_file(args), path3
                        )

                        with scoped_file(self.cwddir, 'foo') as path4:
                            self.assertEqual(
                                configure.search_config_file(
                                    args + ['--rc', path4]
                                ),
                                path4,
                            )
                            self.assertEqual(
                                configure.search_config_file(
                                    args + ['--rc', 'none']
                                ),
                                None,
                            )

    def test_search_config_file_not_found(self):
        args = [os.path.join(self.progdir, 'foo.py')]
        path = os.path.join(self.cwddir, 'foo')

        with self.assertRaises(ValueError):
            configure.search_config_file(args + ['--rc', path])

        with self.assertRaises(ValueError):
            with mock.patch.dict(os.environ, {configure.CONFIG_ENV_NAME: path}):
                configure.search_config_file(args)

    def test_load_sample_config(self):
        filename = 'bisect_kit.json.sample'
        path = os.path.join(common.BISECT_KIT_ROOT, filename)
        config = configure.ConfigStore(path)

        # Load succeeded, no exceptions.
        config.load_config()

    def test_include(self):
        subdir = os.path.join(self.progdir, 'subdir')
        os.mkdir(subdir)
        with scoped_file(
            self.progdir,
            'a',
            """
        {"include":["subdir/b"]}
        """,
        ) as path, scoped_file(
            subdir,
            'b',
            """
        {"options": {"foo": "bar"}}""",
        ):
            config = configure.ConfigStore(path)
            config.load_config()
            self.assertEqual(config.get_option('foo'), 'bar')

    def test_plugins(self):
        subdir = os.path.join(self.progdir, 'subdir')
        os.mkdir(subdir)

        with scoped_file(
            self.progdir,
            'a',
            """
        {"include": ["subdir/b.json"], "plugins": ["a.py"] }
        """,
        ) as path, scoped_file(
            self.progdir,
            'a.py',
            textwrap.dedent(
                """
        from bisect_kit import configure_test

        def loaded():
          configure_test.aaa = 111
        """
            ),
        ), scoped_file(
            subdir,
            'b.json',
            """
        {"plugins": ["b.py"]}
        """,
        ), scoped_file(
            subdir,
            'b.py',
            textwrap.dedent(
                """
        from bisect_kit import configure_test

        def loaded():
          configure_test.bbb = 222
        """
            ),
        ):
            config = configure.ConfigStore(path)
            config.load_config()
            config.load_plugins()
            self.assertEqual(globals().get('aaa'), 111)
            self.assertEqual(globals().get('bbb'), 222)


if __name__ == '__main__':
    unittest.main()
