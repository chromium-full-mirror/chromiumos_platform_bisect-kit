# Copyright 2017 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Utility functions and classes."""

import os


def get(name, default=None):
    """Gets configuration value.

    Args:
      name: Configuration key.
      default: Default value if the configuration is not available.

    Returns:
      Configuration value.
    """
    return os.environ.get(name, default)
