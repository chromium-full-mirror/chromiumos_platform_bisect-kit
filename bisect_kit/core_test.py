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


class TestBisectStates(unittest.TestCase):
  """Test core.BisectStates class."""

  def setUp(self):
    self.session_file = tempfile.mktemp()

  def tearDown(self):
    if os.path.exists(self.session_file):
      os.unlink(self.session_file)

  def test_simple(self):
    states = core.BisectStates(self.session_file)
    states.init({}, ['a', 'b', 'c'])
    self.assertEqual(states.idx2rev(1), 'b')
    self.assertEqual(states.rev2idx('b'), 1)

    states.add_sample(1, 'old', times=5)
    self.assertEqual(states.get(1)['old'], 5)

    states.add_sample(1, 'old', comment='foobar')
    self.assertEqual(states.get(1)['old'], 6)

  def test_save_and_load(self):
    states = core.BisectStates(self.session_file)
    revlist = map(str, range(10))
    states.init({}, revlist)
    states.add_sample(1, 'old')
    states.add_sample(1, 'old')
    states.add_sample(5, 'new')
    states.config['foo'] = 'bar'
    states.save()

    states = core.BisectStates(self.session_file)
    states.load()
    self.assertEqual(states.get(1)['old'], 2)
    self.assertEqual(states.config.get('foo'), 'bar')
    self.assertEqual(states.rev2idx('5'), 5)

  def test_reset(self):
    states = core.BisectStates(self.session_file)
    revlist = map(str, range(10))
    states.init({}, revlist)
    states.config['foo'] = 'bar'
    states.save()

    states = core.BisectStates(self.session_file)
    states.reset()

    states = core.BisectStates(self.session_file)
    self.assertFalse(states.load())


if __name__ == '__main__':
  unittest.main()
