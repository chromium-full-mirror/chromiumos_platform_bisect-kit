# Copyright 2017 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Configure module."""

import contextlib
import os
import pathlib
import shutil
import tempfile
import textwrap
import typing
import unittest
from unittest import mock

import bisect_kit
from bisect_kit import configure


@contextlib.contextmanager
def _scoped_file(
    folder, filename, content=''
) -> typing.Generator[pathlib.Path, None, None]:
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
    full_path = pathlib.Path(folder) / filename
    with full_path.open('w') as f:
        f.write(content)
    yield full_path
    full_path.unlink()


@contextlib.contextmanager
def _scoped_config_file(
    folder, filename=configure.DEFAULT_CONFIG_FILENAME, content=''
) -> typing.Generator[pathlib.Path, None, None]:
    with _scoped_file(folder, filename, content) as path:
        yield path


@contextlib.contextmanager
def _patch_env(**kwargs):
    with mock.patch.dict(
        os.environ, {k: str(v) for k, v in kwargs.items()}
    ) as mock_env:
        yield mock_env


class TestConfigure(unittest.TestCase):
    """Test configure.Configure class."""

    def setUp(self):
        assert (
            configure.CONFIG_ENV_NAME not in os.environ
        ), f'"{configure.CONFIG_ENV_NAME}" should not be set during unittest'
        self.oldcwd = pathlib.Path.cwd()
        self.cwddir = pathlib.Path(tempfile.mkdtemp())
        self.progdir = pathlib.Path(tempfile.mkdtemp())
        # Make sure current working directory and is clean.
        os.chdir(self.cwddir)

    def tearDown(self):
        os.chdir(self.oldcwd)
        shutil.rmtree(self.progdir)
        shutil.rmtree(self.cwddir)

    def test_scoped_file(self):
        with _scoped_file(self.cwddir, 'foo', 'abc') as path:
            assert path.exists()
            with path.open() as f:
                self.assertEqual(f.read(), 'abc')
        assert not path.exists()

    def test_search_config_file(self):
        args = [str(self.progdir / 'foo.py')]

        self.assertIsNone(configure.search_config_file(args))

        with _scoped_file(self.cwddir, 'foo') as path:
            self.assertEqual(
                configure.search_config_file(args + ['--rc', str(path)]), path
            )

        with _scoped_file(self.cwddir, 'bar') as path:
            with _patch_env(**{configure.CONFIG_ENV_NAME: path}):
                self.assertEqual(configure.search_config_file(args), path)

        with _scoped_config_file(self.cwddir) as path:
            self.assertEqual(configure.search_config_file(args), path)

        with _scoped_config_file(self.progdir) as path:
            self.assertEqual(configure.search_config_file(args), path)

    def test_search_config_file_precedence(self):
        args = [str(self.progdir / 'foo.py')]

        self.assertIsNone(configure.search_config_file(args))

        with _scoped_config_file(self.progdir) as path1:
            self.assertEqual(configure.search_config_file(args), path1)

            with _scoped_config_file(self.cwddir) as path2:
                self.assertEqual(configure.search_config_file(args), path2)

                with _scoped_file(self.cwddir, 'bar') as path3:
                    with _patch_env(**{configure.CONFIG_ENV_NAME: 'none'}):
                        self.assertIsNone(configure.search_config_file(args))

                    with _patch_env(**{configure.CONFIG_ENV_NAME: path3}):
                        self.assertEqual(
                            configure.search_config_file(args), path3
                        )

                        with _scoped_file(self.cwddir, 'foo') as path4:
                            self.assertEqual(
                                configure.search_config_file(
                                    args + ['--rc', str(path4)]
                                ),
                                path4,
                            )
                            self.assertIsNone(
                                configure.search_config_file(
                                    args + ['--rc', 'none']
                                )
                            )

    def test_search_config_file_not_found(self):
        args = [str(self.progdir / 'foo.py')]
        path = self.cwddir / 'foo'

        with self.assertRaises(ValueError):
            configure.search_config_file(args + ['--rc', str(path)])

        with self.assertRaises(ValueError):
            with _patch_env(**{configure.CONFIG_ENV_NAME: path}):
                configure.search_config_file(args)

    def test_load_sample_config(self):
        filename = 'bisect_kit.json.sample'
        path = bisect_kit.BISECT_KIT_ROOT / filename

        # Load succeeded, no exceptions.
        config = configure.ConfigStore(path)
        self.assertIsNone(config.get_option('foo'))

    def test_include(self):
        subdir = self.progdir / 'subdir'
        subdir.mkdir(exist_ok=True)
        with _scoped_file(
            self.progdir,
            'a',
            """{"include":["subdir/b"]}""",
        ) as path, _scoped_file(
            subdir,
            'b',
            """{"options": {"foo": "bar"}}""",
        ):
            config = configure.ConfigStore(path)
            self.assertEqual(config.get_option('foo'), 'bar')

    def test_plugins(self):
        subdir = self.progdir / 'subdir'
        subdir.mkdir(exist_ok=True)

        with _scoped_file(
            self.progdir,
            'a',
            """{"include": ["subdir/b.json"], "plugins": ["a.py"] }""",
        ) as path, _scoped_file(
            self.progdir,
            'a.py',
            textwrap.dedent(
                """
                from bisect_kit import configure_test

                def loaded():
                    configure_test.aaa = 111
                """
            ),
        ), _scoped_file(
            subdir,
            'b.json',
            """{"plugins": ["b.py"]}""",
        ), _scoped_file(
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
            configure.load_plugins(config.iter_plugins())
            self.assertEqual(globals().get('aaa'), 111)
            self.assertEqual(globals().get('bbb'), 222)


if __name__ == '__main__':
    unittest.main()
