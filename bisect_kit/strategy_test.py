# -*- coding: utf-8 -*-
# Copyright 2017 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Test strategy module."""

from __future__ import print_function
import collections
import random
import unittest

from bisect_kit import core
from bisect_kit import errors
from bisect_kit import strategy


# pylint: disable=protected-access
class TestNoisyBinarySearch(unittest.TestCase):
  """Test NoisyBinarySearch class."""

  def setUp(self):
    self.rev_info = []
    for i in range(100):
      self.rev_info.append(core.RevInfo(str(i)))
    self.init_prob = [1.0] * len(self.rev_info)

  def test_parse_observation(self):
    Strategy = strategy.NoisyBinarySearch
    self.assertEqual(
        Strategy._parse_observation('new=9/10'), (strategy.NOT_NOISY, (9, 10)))
    self.assertEqual(
        Strategy._parse_observation('old=1/10,new=9/10'), ((1, 10), (9, 10)))

  def test_calculate_probs_0_1(self):
    prob = strategy.NoisyBinarySearch._calculate_probs(0.0, 1.0, self.rev_info,
                                                       self.init_prob)
    self.assertAlmostEqual(prob[10], 0.01)
    self.assertAlmostEqual(prob[50], 0.01)

    self.rev_info[49]['old'] += 1
    prob = strategy.NoisyBinarySearch._calculate_probs(0.0, 1.0, self.rev_info,
                                                       self.init_prob)
    self.assertAlmostEqual(prob[49], 0.0)
    self.assertAlmostEqual(prob[50], 0.02)

  def test_calculate_probs_half(self):
    self.rev_info[49]['old'] += 1
    prob = strategy.NoisyBinarySearch._calculate_probs(0.0, 0.5, self.rev_info,
                                                       self.init_prob)
    self.assertAlmostEqual(prob[49], 0.00666667)
    self.assertAlmostEqual(prob[50], 0.01333333)

  def test_calculate_probs_1_9(self):
    self.rev_info[49]['old'] += 1
    prob = strategy.NoisyBinarySearch._calculate_probs(0.1, 0.9, self.rev_info,
                                                       self.init_prob)
    self.assertAlmostEqual(prob[49], 0.002)
    self.assertAlmostEqual(prob[50], 0.018)

  def test_calculate_probs_wrong_assumption(self):
    self.rev_info[30]['new'] += 1
    self.rev_info[40]['old'] += 1
    with self.assertRaises(errors.WrongAssumption):
      strategy.NoisyBinarySearch._calculate_probs(0.0, 1.0, self.rev_info,
                                                  self.init_prob)

  def test_calculate_probs_underflow(self):
    """Test underflow situation if eval too many times.

    The algorithm may calculate p**n, where n is number of test runs. If n is
    large enough, the whole calculation may underflow. This test makes sure the
    algorithm works properly.
    """
    for i in range(0, 50):
      self.rev_info[i]['old'] += 100
    for i in range(50, 100):
      self.rev_info[i]['new'] += 100
    prob = strategy.NoisyBinarySearch._calculate_probs(0.0, 0.5, self.rev_info,
                                                       self.init_prob)
    self.assertAlmostEqual(prob[49], 0)
    self.assertAlmostEqual(prob[50], 1)

  def test_rebuild(self):
    bsearch = strategy.NoisyBinarySearch(
        self.rev_info, confidence=0.99, oracle=(0, 0.9))
    for i in [0, 10, 20, 30, 40, 50]:
      self.rev_info[i]['old'] += 1
    for i in [60, 70, 80, 90, 99]:
      self.rev_info[i]['new'] += 1

    bsearch.rebuild()

    self.assertLess(bsearch.prob[5], bsearch.prob[15])
    self.assertLess(bsearch.prob[15], bsearch.prob[25])
    self.assertLess(bsearch.prob[25], bsearch.prob[35])
    self.assertLess(bsearch.prob[35], bsearch.prob[45])
    self.assertLess(bsearch.prob[45], bsearch.prob[55])
    self.assertAlmostEqual(bsearch.prob[55], 0.09, 6)
    self.assertAlmostEqual(bsearch.prob[65], 0)
    self.assertAlmostEqual(bsearch.prob[75], 0)
    self.assertAlmostEqual(bsearch.prob[85], 0)
    self.assertAlmostEqual(bsearch.prob[95], 0)

    self.assertEqual(bsearch.get_range(), (40, 60))
    self.assertEqual(bsearch.next_idx(), 54)
    assert not bsearch.is_done()

  def test_get_noise_observation(self):
    bsearch = strategy.NoisyBinarySearch(
        self.rev_info, observation='old=1/10,new=9/10')
    self.assertEqual(bsearch.get_noise_observation(), 'old=1/10,new=9/10')

    self.rev_info[0]['old'] += 1
    self.rev_info[10]['new'] += 1
    self.rev_info[20]['old'] += 2
    self.rev_info[30]['old'] += 20
    self.rev_info[50]['new'] += 20
    self.rev_info[60]['new'] += 20
    self.rev_info[90]['old'] += 2
    self.rev_info[99]['new'] += 1
    bsearch.rebuild()

    self.assertEqual(bsearch.get_noise_observation(), 'old=2/34,new=50/53')

  def test_next_idx_arithmetic_error(self):
    # Larger `n` may produce larger arithmetic error. For example,
    #   n=1e3 could lead to call math.log(-1e-14).
    #   n=1e6 could lead to call math.log(-1e-11).
    #   n=1e7 could lead to call math.log(-1e-10).
    # Here we only test n=1000 because
    #   - we often run bisect with candidates of the same order of magnitude.
    #   - larger n is too slow as unittest.
    n = 1000
    rev_info = []
    for i in range(n):
      rev_info.append(core.RevInfo(str(i)))

    bsearch = strategy.NoisyBinarySearch(rev_info, oracle=(0.01, 1))
    rev_info[n - 1]['new'] += 1
    rev_info[0]['old'] += 1
    bsearch.rebuild()

    rng = random.Random(0)
    for _ in range(10):
      idx = rng.randint(1, n - 2)
      rev_info[idx]['skip'] += 1
      bsearch.update(idx, 'skip')

      # Should not raise ValueError (math domain error).
      bsearch.next_idx()

  def test_rebuild_skip(self):
    bsearch = strategy.NoisyBinarySearch(self.rev_info, oracle=(0, 0.9))
    self.rev_info[0]['old'] += 1
    self.rev_info[99]['new'] += 1
    bsearch.rebuild()
    self.assertEqual(bsearch.get_range(), (0, 99))
    self.assertEqual(bsearch.next_idx(), 45)

    self.rev_info[45]['skip'] += 1
    bsearch.rebuild()
    self.assertEqual(bsearch.get_range(), (0, 99))
    self.assertNotEqual(bsearch.next_idx(), 45)

  def test_skip_then_success(self):
    bsearch = strategy.NoisyBinarySearch(self.rev_info)
    self.rev_info[0]['old'] += 1
    self.rev_info[99]['skip'] += 10
    bsearch.rebuild()
    # Skip too many times, the probability is set to 0.
    self.assertEqual(bsearch.prob[99], 0)

    # Suddenly the result is not 'skip' any more. The probability jump from
    # zero to non-zero.
    self.rev_info[99]['new'] += 1
    bsearch.update(99, 'new')
    self.assertGreater(bsearch.prob[99], 0)

  def test_get_range_with_skip(self):
    bsearch = strategy.NoisyBinarySearch(self.rev_info, oracle=(0, 0.9))
    self.rev_info[0]['old'] += 1
    self.rev_info[33]['old'] += 10
    self.rev_info[34]['skip'] += 10
    self.rev_info[35]['new'] += 10
    bsearch.rebuild()

    self.assertEqual(bsearch.get_range(), (33, 35))

  def test_update_noisy(self):
    bsearch = strategy.NoisyBinarySearch(
        self.rev_info, confidence=0.99, oracle=(0, 0.9))
    bsearch.rebuild()
    for i in [0, 10, 20, 30, 40, 50]:
      self.rev_info[i]['old'] += 1
      bsearch.update(i, 'old')
    for i in [60, 70, 80, 90, 99]:
      self.rev_info[i]['new'] += 1
      bsearch.update(i, 'new')

    self.assertLess(bsearch.prob[5], bsearch.prob[15])
    self.assertLess(bsearch.prob[15], bsearch.prob[25])
    self.assertLess(bsearch.prob[25], bsearch.prob[35])
    self.assertLess(bsearch.prob[35], bsearch.prob[45])
    self.assertLess(bsearch.prob[45], bsearch.prob[55])
    self.assertAlmostEqual(bsearch.prob[55], 0.09, 6)
    self.assertAlmostEqual(bsearch.prob[65], 0)
    self.assertAlmostEqual(bsearch.prob[75], 0)
    self.assertAlmostEqual(bsearch.prob[85], 0)
    self.assertAlmostEqual(bsearch.prob[95], 0)

    self.assertEqual(bsearch.get_range(), (40, 60))
    self.assertEqual(bsearch.next_idx(), 54)
    assert not bsearch.is_done()

  def test_update_classic(self):
    self.rev_info = []
    for i in range(465):
      self.rev_info.append(core.RevInfo(str(i)))
    bsearch = strategy.NoisyBinarySearch(self.rev_info, 462, 463)
    bsearch.rebuild()
    assert not bsearch.is_done()

    self.rev_info[462]['old'] += 1
    bsearch.update(462, 'old')
    self.assertAlmostEqual(bsearch.prob[462], 0)
    assert not bsearch.is_done()

    self.rev_info[463]['new'] += 1
    bsearch.update(463, 'new')
    assert bsearch.is_done()
    self.assertAlmostEqual(bsearch.prob[463], 1)
    self.assertEqual(bsearch.get_range(), (462, 463))

  def perform_search(self, size, ans, old_p, new_p, confidence, random_seed=0):
    """Performs full noisy binary search.

    Args:
      size: Number of candidates.
      ans: Position of answer.
      old_p: False-positive probability for old candidates.
      new_p: True-positive probability for new candidates.
      confidence: Required confidence.
      random_seed: Random seed.

    Returns:
      (count, guess):
        count: Number of eval.
        guess: Best guess.
    """
    rev_info = map(core.RevInfo, map(str, range(size)))
    bsearch = strategy.NoisyBinarySearch(
        rev_info,
        confidence=confidence,
        oracle=(old_p, new_p),
        # Verify range until success.
        verify_confidence=1.0)
    bsearch.rebuild()

    rng = random.Random(random_seed)
    count = 0
    while not bsearch.is_done():
      idx = bsearch.next_idx()
      if rng.random() < (old_p if idx < ans else new_p):
        result = 'new'
      else:
        result = 'old'
      rev_info[idx][result] += 1
      count += 1
      bsearch.update(idx, result)

    return count, bsearch.get_best_guess()

  def test_classic_search(self):
    # Settings for non-noisy case.
    old_p = 0
    new_p = 1
    confidence = 0.99  # Doesn't matter.

    # Extreme case.
    count, result = self.perform_search(2, 1, old_p, new_p, confidence)
    self.assertEqual(count, 2)
    self.assertEqual(result, 1)

    # Tests answer positions.
    for i in range(1, 50):
      count, result = self.perform_search(50, i, old_p, new_p, confidence)
      self.assertLessEqual(7, count)
      self.assertLessEqual(count, 8)
      self.assertEqual(result, i)

    # Slightly larger case.
    count, result = self.perform_search(1000, 42, old_p, new_p, confidence)
    self.assertEqual(count, 11)
    self.assertEqual(result, 42)

  def test_half_noisy_search(self):
    """Tests noisy search with single side flaky."""
    # Extreme case.
    count, result = self.perform_search(2, 1, 0.0, 0.5, 0.999)
    self.assertEqual(result, 1)

    count, result = self.perform_search(3, 2, 0.0, 0.5, 0.999)
    # 1 - 0.5**10 > 0.999, at least 10 times to have enough confidence.
    self.assertGreaterEqual(count, 10)
    self.assertEqual(result, 2)

    # Tests answer positions.
    for i in range(1, 10):
      self.perform_search(10, i, 0, 0.3, 0.999)
      self.perform_search(10, i, 0.3, 1, 0.999)

    # Larger case.
    # Only makes sure it works. Don't verify the values due to randomness.
    self.perform_search(100, 42, 0, 0.3, 0.999)

  def test_full_noisy_search(self):
    """Tests noisy binary search with flaky on two sides."""
    # Only makes sure it works. Don't verify the values due to randomness.
    self.perform_search(1000, 42, 0.05, 0.7, 0.999)

    # Tests answer positions.
    for i in range(1, 20):
      self.perform_search(20, i, 0.1, 0.9, 0.999)

  def test_confidence(self):
    counter_guess = collections.Counter()
    sum_count = 0
    n = 1000
    ans = 7
    for i in range(n):
      count, guess = self.perform_search(
          10, ans, 0.1, 0.9, confidence=0.9, random_seed=i)
      counter_guess[guess] += 1
      sum_count += count

    self.assertLess(float(sum_count) / n, 9)
    common_guess = counter_guess.most_common(1)[0]
    self.assertEqual(common_guess[0], ans)
    # It's expected that not all guesses are correct.
    self.assertLess(common_guess[1], n)
    # With 0.9 confidence, about 0.9 of guesses are correct.
    self.assertGreater(common_guess[1], n * 0.9)


if __name__ == '__main__':
  unittest.main()
