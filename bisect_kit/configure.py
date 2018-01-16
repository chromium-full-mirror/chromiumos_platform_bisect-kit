# Copyright 2017 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Utility functions and classes."""

from __future__ import print_function
import argparse
import json
import logging
import os
import re
import sys

logger = logging.getLogger(__name__)

SURPRESS_CONFIG = 'none'
DEFAULT_CONFIG_FILENAME = 'bisect_kit.json'
CONFIG_ENV_NAME = 'BISECT_KIT_CONFIG'

root = None


def _parse_config_file(args):
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


class ConfigStore(object):
  """Content of configuration.

  Attributes:
    path: path of configure file. None for dummy store.
    children: nested ConfigStore objects.
    plugins: plugin list.
    options: dict of option values.
  """

  def __init__(self, path):
    self.path = path
    self.children = []
    self.plugins = []
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

    content = file(self.path).read()
    # strip comments as they are not allowed in standard json
    content = re.sub(r'^\s*//.*', '', content, 0, re.M)

    config = json.loads(content)

    for path in config.get('include', []):
      if not os.path.isabs(path):
        path = os.path.join(os.path.dirname(self.path), path)
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
      if not os.path.isabs(path):
        path = os.path.join(os.path.dirname(self.path), path)
      if not os.path.exists(path):
        raise ValueError('%s plugin %r: file not found' % (self.path, path))
      module = {}
      exec file(path) in module
      loaded_func = module.get('loaded', None)
      if loaded_func:
        loaded_func()


def search_config_file(args):
  """Searches config file.

  The search order of config file is:
    1. config file specified by command line --rc
    2. config file specified by environment variable BISECT_KIT_CONFIG
    3. <current working directory>/bisect_kit.json
    4. <bisect-kit's root>/bisect_kit.json

  Args:
    args: command line arguments. Note args[0] is program path.

  Returns:
    Path to config file. None if not found or surpressed.
  """
  config = _parse_config_file(args)
  if not config:
    config = os.environ.get(CONFIG_ENV_NAME, None)
  if config == SURPRESS_CONFIG:
    return None

  if config:
    if not os.path.exists(config):
      raise ValueError('Config file %r not found' % config)
    return config

  for path in [os.getcwd(), os.path.dirname(args[0])]:
    fullpath = os.path.join(path, DEFAULT_CONFIG_FILENAME)
    if os.path.exists(fullpath):
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
