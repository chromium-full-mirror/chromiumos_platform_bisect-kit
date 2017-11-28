# Copyright 2017 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Common functions for testing"""

from __future__ import print_function
import os


def get_testdata_path(filename=None):
  """Get path to test data

  Args:
    filename: path relative to the test data folder.

  Returns:
    path to the file inside test data folder. If filename is None, the path of
    test data folder is returned.
  """
  path = os.path.join(os.path.dirname(__file__), '..', 'testdata')
  if filename:
    path = os.path.join(path, filename)
  return path
