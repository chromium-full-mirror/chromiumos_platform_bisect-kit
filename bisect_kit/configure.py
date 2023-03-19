# -*- coding: utf-8 -*-
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


logger = logging.getLogger(__name__)

SUPPRESS_CONFIG = 'none'
DEFAULT_CONFIG_FILENAME = 'bisect_kit.json'
CONFIG_ENV_NAME = 'BISECT_KIT_CONFIG'

root = None


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

    def __init__(self, path):
        self.path = pathlib.Path(path) if path is not None else None
        self.children: list[ConfigStore] = []
        self.plugins: list[str] = []
        self.options = {}

    def get_option(self, key):
        if key in self.options:
            return self.options[key]

        for child in reversed(self.children):
            value = child.get_option(key)
            if value is not None:
                return value

        return None

    def load_config(self):
        """Load configure file recursively."""
        if not self.path:
            return

        with self.path.open() as f:
            content = f.read()
        # strip comments as they are not allowed in standard json
        content = re.sub(r'^\s*//.*', '', content, 0, re.M)

        config = json.loads(content)

        for path in config.get('include', []):
            path = pathlib.Path(path)
            if not path.is_absolute():
                path = self.path.parent / path
            child = ConfigStore(path)
            child.load_config()
            self.children.append(child)

        self.plugins = config.get('plugins', [])
        self.options = config.get('options', {})

    def load_plugins(self):
        """Load plugins."""
        for child in self.children:
            child.load_plugins()

        for path in self.plugins:
            path = pathlib.Path(path)
            if not path.is_absolute():
                path = self.path.parent / path
            if not path.exists():
                raise ValueError(
                    '%s plugin %r: file not found' % (self.path, path)
                )
            module = {}
            with path.open() as f:
                code = compile(f.read(), path, 'exec')
                # pylint: disable=exec-used
                exec(code, module)
            loaded_func = module.get('loaded', None)
            if loaded_func:
                loaded_func()


def search_config_file(args: list[str]) -> pathlib.Path | None:
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


def load_config(args=None):
    if args is None:
        args = sys.argv
    global root  # pylint: disable=global-statement
    if not root:
        root_config = search_config_file(args)
        root = ConfigStore(root_config)
        root.load_config()


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
