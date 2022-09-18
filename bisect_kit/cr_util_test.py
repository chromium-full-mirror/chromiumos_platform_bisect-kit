# -*- coding: utf-8 -*-
# Copyright 2017 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Test cr_util module"""

from __future__ import print_function
import unittest
from unittest import mock

from bisect_kit import cr_util
from bisect_kit import errors
from bisect_kit import git_util
from bisect_kit import testing


class TestCrUtil(unittest.TestCase):
  """Test cr_util functions."""

  def test_is_chrome_version(self):
    assert cr_util.is_chrome_version('59.0.3065.0')

    assert not cr_util.is_chrome_version('59.0.3065')
    assert not cr_util.is_chrome_version('59.0.3065.0.0')

  def test_is_version_lesseq(self):
    assert cr_util.is_version_lesseq('75.0.3731.0', '75.0.3731.0')
    assert cr_util.is_version_lesseq('75.0.3731.0', '75.0.3732.0')
    assert not cr_util.is_version_lesseq('75.0.3732.0', '75.0.3731.0')

    assert cr_util.is_version_lesseq('74.0.3726.0', '75.0.3731.0')

  def test_is_direct_relative_version(self):
    assert cr_util.is_direct_relative_version('74.0.3726.0', '75.0.3731.0')
    assert cr_util.is_direct_relative_version('75.0.3731.0', '74.0.3726.0')
    assert cr_util.is_direct_relative_version('73.0.3683.0', '75.0.3731.0')
    assert not cr_util.is_direct_relative_version('73.0.3683.1', '75.0.3731.0')
    assert not cr_util.is_direct_relative_version('75.0.3731.0', '73.0.3683.1')

  def test_extract_branch_from_version(self):
    self.assertEqual(cr_util.extract_branch_from_version('59.0.3064.0'), '3064')

  def test_extract_milestone_from_version(self):
    self.assertEqual(
        cr_util.extract_milestone_from_version('59.0.3064.0'), '59')

  def test_query_commit_position(self):
    chrome_src = 'dummy'
    git_rev = '0000111122223333'

    # master
    with testing.mock_function_by_file(git_util, 'get_commit_log',
                                       'chrome_commit_log/e4940332'):
      self.assertEqual(
          cr_util.query_commit_position(chrome_src, git_rev),
          ('master', '514085'))

    # branch
    with testing.mock_function_by_file(git_util, 'get_commit_log',
                                       'chrome_commit_log/27101da1'):
      self.assertEqual(
          cr_util.query_commit_position(chrome_src, git_rev), ('3255', '4'))

    # master (old)
    with testing.mock_function_by_file(git_util, 'get_commit_log',
                                       'chrome_commit_log/44f159f6'):
      self.assertEqual(
          cr_util.query_commit_position(chrome_src, git_rev),
          ('master', '284374'))

    # branch (old)
    with testing.mock_function_by_file(git_util, 'get_commit_log',
                                       'chrome_commit_log/3fe30a09'):
      self.assertEqual(
          cr_util.query_commit_position(chrome_src, git_rev),
          ('2100', '284389'))

    # fail
    with mock.patch.object(git_util, 'get_commit_log', return_value='foo'):
      with self.assertRaises(errors.InternalError):
        cr_util.query_commit_position(chrome_src, git_rev)


if __name__ == '__main__':
  unittest.main()
