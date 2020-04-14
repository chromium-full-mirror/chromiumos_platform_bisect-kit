# -*- coding: utf-8 -*-
# Copyright 2017 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Common functions for testing"""

from __future__ import print_function
import contextlib
import os
import shutil
import tempfile

from unittest import mock

from bisect_kit import common


def get_testdata_path(filename=None):
  """Gets path to test data

  Args:
    filename: path relative to the test data folder.

  Returns:
    path to the file inside test data folder. If filename is None, the path of
    test data folder is returned.
  """
  path = os.path.join(os.path.dirname(__file__), '..', 'testdata')
  assert os.path.exists(path)
  if filename:
    path = os.path.join(path, filename)
  return path


@contextlib.contextmanager
def mock_function_by_file(module, name, filename):
  """Context manager to mock function by test data

  Args:
    module: module of function to mock
    name: function name to mock
    filename: path relative to the test data folder

  Returns:
    context manager, which mocks the said function within its scope.
  """
  with open(get_testdata_path(filename)) as f:
    data = f.read()
  with mock.patch.object(module, name, return_value=data):
    yield


class mock_session_base(contextlib.ContextDecorator):
  """Context decorator to mock session base and clean up."""

  def __init__(self):
    self.session_base_bak = None
    self.session_base = tempfile.mkdtemp()

  def __enter__(self):
    self.session_base_bak = common.DEFAULT_SESSION_BASE
    common.DEFAULT_SESSION_BASE = self.session_base

  def __exit__(self, *exc):
    common.DEFAULT_SESSION_BASE = self.session_base_bak
    shutil.rmtree(self.session_base)
