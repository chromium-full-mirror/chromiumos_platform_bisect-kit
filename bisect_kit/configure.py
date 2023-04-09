# Copyright 2017 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Utility functions and classes."""

from __future__ import print_function

import argparse
import json
import logging
import os
import pathlib
import re
import sys
import typing


SUPPRESS_CONFIG = 'none'
DEFAULT_CONFIG_FILENAME = 'bisect_kit.json'
CONFIG_ENV_NAME = 'BISECT_KIT_CONFIG'

PathLike = os.PathLike | str

logger = logging.getLogger(__name__)
root = None


def _rebase_relative_path(path: PathLike, parent: PathLike) -> pathlib.Path:
    path = pathlib.Path(path)
    if path.is_absolute():
        return path
    return pathlib.Path(parent) / path


def _parse_rc_argument(args: list[str]) -> str | None:
    """Parses --rc from command line arguments.

    The purpose of this function is parsing --rc argument before full argument
    parser is constructed. This is necessary because the full parser need to
    know default option values from configuration file, which may be specified
    by --rc.

    Args:
      args: command line arguments.
    """
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('--rc')
    opts, _ = parser.parse_known_args(args)
    return opts.rc


class ConfigStore:
    """Content of configuration.

    Attributes:
      path: path of configure file. None for dummy store.
      children: nested ConfigStore objects.
      plugins: plugin list.
      options: dict of option values.
    """

    def __init__(self, path: PathLike | None):
        self.path = pathlib.Path(path) if path is not None else None
        self.children: list[ConfigStore] = []
        self.plugins: list[pathlib.Path] = []
        self.options = {}
        self._parse_config()

    def iter_plugins(self) -> typing.Iterator[pathlib.Path]:
        yield from self.plugins
        for child in self.children:
            yield from child.iter_plugins()

    def get_option(self, key):
        if key in self.options:
            return self.options[key]

        for child in reversed(self.children):
            value = child.get_option(key)
            if value is not None:
                return value

        return None

    def _parse_config(self) -> None:
        """Load configure file recursively."""
        if not self.path:
            return

        with self.path.open() as f:
            content = f.read()
        # strip comments as they are not allowed in standard json
        content = re.sub(r'^\s*//.*', '', content, 0, re.M)

        config = json.loads(content)

        for include_path in config.get('include', []):
            include_path = _rebase_relative_path(include_path, self.path.parent)
            child = ConfigStore(include_path)
            self.children.append(child)

        self.plugins = [
            _rebase_relative_path(plugin_path, self.path.parent)
            for plugin_path in config.get('plugins', [])
        ]
        self.options = config.get('options', {})


def load_plugins(plugin_list: typing.Iterable[PathLike]) -> None:
    """Load plugins."""
    for plugin_path in plugin_list:
        plugin_path = pathlib.Path(plugin_path)
        if not plugin_path.exists():
            raise ValueError(f'Plugin file "{plugin_path}" not found')
        module = {}
        with plugin_path.open() as f:
            code = compile(f.read(), plugin_path, 'exec')
            # pylint: disable=exec-used
            exec(code, module)
        loaded_func = module.get('loaded', None)
        if loaded_func:
            loaded_func()


def search_config_file(args: list[str]) -> PathLike | None:
    """Searches config file.

    The search order of config file is:
      1. config file specified by command line --rc
      2. config file specified by environment variable BISECT_KIT_CONFIG
      3. <current working directory>/bisect_kit.json
      4. <bisect-kit's root>/bisect_kit.json

    Args:
      args: command line arguments. Note args[0] is program path.

    Returns:
      Path to config file. None if not found or suppressed.
    """
    config = _parse_rc_argument(args)
    if not config:
        config = os.environ.get(CONFIG_ENV_NAME, None)
    if config == SUPPRESS_CONFIG:
        return None

    if config:
        config = pathlib.Path(config)
        if not config.exists():
            raise ValueError(f'Config file "{config}" not found')
        return config

    for path in [pathlib.Path.cwd(), pathlib.Path(args[0]).parent]:
        fullpath = path / DEFAULT_CONFIG_FILENAME
        if fullpath.exists():
            return fullpath

    return None


def load_config() -> ConfigStore:
    global root  # pylint: disable=global-statement
    if not root:
        root = ConfigStore(search_config_file(sys.argv))
    return root


def get(name, default=None):
    """Gets configuration value.

    Args:
      name: Configuration key.
      default: Default value if the configuration is not available.

    Returns:
      Configuration value.
    """
    assert root
    value = root.get_option(name)
    if value is not None:
        return value

    return os.environ.get(name, default)


def reset():
    """Resets global config store.

    Usually you don't need this function and keep the parsed config live during
    the script runs. On the other hand, unittests may need to reset the global
    state.
    """
    global root  # pylint: disable=global-statement
    root = None
