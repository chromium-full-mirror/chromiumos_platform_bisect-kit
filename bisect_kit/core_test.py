# -*- coding: utf-8 -*-
# Copyright 2017 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Test core module."""

from __future__ import print_function
import tempfile
import unittest
import os

from bisect_kit import core


class TestRevInfo(unittest.TestCase):
  """Test core.RevInfo class."""

  def test_simple(self):
    rev_info = core.RevInfo('foo')
    self.assertEqual(rev_info.rev, 'foo')
    self.assertEqual(rev_info['old'], 0)
    self.assertEqual(rev_info['new'], 0)
    self.assertEqual(rev_info['skip'], 0)

  def test_add_sample(self):
    rev_info = core.RevInfo('foo')
    rev_info.add_sample(status='old')
    self.assertEqual(rev_info['old'], 1)
    rev_info.add_sample(status='old', times=2)
    self.assertEqual(rev_info['old'], 3)

  def test_averages(self):
    rev_info = core.RevInfo('foo')
    rev_info.add_sample(status='old', values=[1, 3])
    self.assertEqual(rev_info.averages(), [2])
    rev_info.add_sample(status='old', values=[6, 6])
    self.assertEqual(rev_info.averages(), [2, 6])

  def test_reclassify(self):
    rev_info = core.RevInfo('foo')
    rev_info.add_sample(status='value', values=[1, 3])
    rev_info.add_sample(status='value', values=[10, 10])
    rev_info.add_sample(status='value', values=[1, 100])
    rev_info.add_sample(status='skip')
    rev_info.reclassify(1, 3, 5)
    self.assertEqual(rev_info['old'], 1)
    self.assertEqual(rev_info['new'], 2)
    self.assertEqual(rev_info['skip'], 1)

  def test_reclassify_reverse(self):
    rev_info = core.RevInfo('foo')
    rev_info.add_sample(status='value', values=[1, 3])
    rev_info.add_sample(status='value', values=[10, 10])
    rev_info.add_sample(status='value', values=[1, 100])
    rev_info.reclassify(5, 3, 1)
    self.assertEqual(rev_info['old'], 2)
    self.assertEqual(rev_info['new'], 1)


class TestBisectStates(unittest.TestCase):
  """Test core.BisectStates class."""

  def setUp(self):
    self.session_file = tempfile.mktemp()

  def tearDown(self):
    if os.path.exists(self.session_file):
      os.unlink(self.session_file)

  def test_simple(self):
    states = core.BisectStates(self.session_file)
    states.init_states({}, ['a', 'b', 'c'])
    self.assertEqual(states.idx2rev(1), 'b')
    self.assertEqual(states.rev2idx('b'), 1)

  def test_save_and_load(self):
    states = core.BisectStates(self.session_file)
    revlist = [str(i) for i in range(10)]
    states.init_states({}, revlist)
    states.config['foo'] = 'bar'
    states.save()

    states = core.BisectStates(self.session_file)
    states.load_states()
    self.assertEqual(states.config.get('foo'), 'bar')
    self.assertEqual(states.rev2idx('5'), 5)

  def test_reset(self):
    states = core.BisectStates(self.session_file)
    revlist = [str(i) for i in range(10)]
    states.init_states({}, revlist)
    states.config['foo'] = 'bar'
    states.save()

    states = core.BisectStates(self.session_file)
    states.reset()

    states = core.BisectStates(self.session_file)
    self.assertFalse(states.load_states())


if __name__ == '__main__':
  unittest.main()
