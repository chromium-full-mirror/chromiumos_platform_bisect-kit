# Copyright 2017 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Test strategy module."""

import collections
import copy
import logging
import os
import random
import tempfile
import unittest
from unittest import mock

from bisect_kit import core
from bisect_kit import errors
from bisect_kit import math_util
from bisect_kit import strategy
from bisect_kit import testing
from bisect_kit.gemini_bisect import gemini_util


class TestStrategy(unittest.TestCase):
    """Test global functions in strategy module."""

    def test_trend_score(self):
        self.assertEqual(strategy.trend_score(1.0, 2.0, 1.0, 2.0), 1.0)
        self.assertEqual(strategy.trend_score(1.0, 3.0, 100, 101), 0.5)
        self.assertLess(strategy.trend_score(1.0, 3.0, 101, 100), 0)
        self.assertEqual(strategy.trend_score(100.0, 300.0, 10.0, 30.0), 1.0)
        self.assertEqual(strategy.trend_score(100.0, 300.0, 10.0, 20.0), 0.5)
        self.assertLess(strategy.trend_score(100.0, 300.0, 20.0, 10.0), 0)

        self.assertEqual(strategy.trend_score(0.0, 300.0, 0.0, 30.0), 0.1)
        self.assertLess(strategy.trend_score(-10.0, 10.0, 10.0, -10.0), 0)


# pylint: disable=protected-access
class TestNoisyBinarySearch(unittest.TestCase):
    """Test NoisyBinarySearch class."""

    def setUp(self):
        self.rev_info = []
        for i in range(100):
            self.rev_info.append(core.RevInfo(str(i)))
        self.init_prob = [1.0] * len(self.rev_info)
        self.random_list = testing.RandomNum()

    def test_parse_observation(self):
        Strategy = strategy.NoisyBinarySearch
        self.assertEqual(
            Strategy._parse_observation('new=9/10'),
            (strategy.NOT_NOISY, (9, 10)),
        )
        self.assertEqual(
            Strategy._parse_observation('old=1/10,new=9/10'), ((1, 10), (9, 10))
        )

    def test_calculate_probs_0_1(self):
        prob = strategy.NoisyBinarySearch._calculate_probs(
            0.0, 1.0, self.rev_info, self.init_prob
        )
        self.assertAlmostEqual(prob[10], 0.01)
        self.assertAlmostEqual(prob[50], 0.01)

        self.rev_info[49]['old'] += 1
        prob = strategy.NoisyBinarySearch._calculate_probs(
            0.0, 1.0, self.rev_info, self.init_prob
        )
        self.assertAlmostEqual(prob[49], 0.0)
        self.assertAlmostEqual(prob[50], 0.02)

    def test_calculate_probs_half(self):
        self.rev_info[49]['old'] += 1
        prob = strategy.NoisyBinarySearch._calculate_probs(
            0.0, 0.5, self.rev_info, self.init_prob
        )
        self.assertAlmostEqual(prob[49], 0.00666667)
        self.assertAlmostEqual(prob[50], 0.01333333)

    def test_calculate_probs_1_9(self):
        self.rev_info[49]['old'] += 1
        prob = strategy.NoisyBinarySearch._calculate_probs(
            0.1, 0.9, self.rev_info, self.init_prob
        )
        self.assertAlmostEqual(prob[49], 0.002)
        self.assertAlmostEqual(prob[50], 0.018)

    def test_calculate_probs_wrong_assumption(self):
        self.rev_info[30]['new'] += 1
        self.rev_info[40]['old'] += 1
        with self.assertRaises(errors.WrongAssumption):
            strategy.NoisyBinarySearch._calculate_probs(
                0.0, 1.0, self.rev_info, self.init_prob
            )

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
        prob = strategy.NoisyBinarySearch._calculate_probs(
            0.0, 0.5, self.rev_info, self.init_prob
        )
        self.assertAlmostEqual(prob[49], 0)
        self.assertAlmostEqual(prob[50], 1)

    def test_prob(self):
        bsearch = strategy.NoisyBinarySearch(
            self.rev_info,
            confidence=0.99,
            oracle=(0, 0.9),
        )
        for i in [0, 10, 20, 30, 40, 50]:
            bsearch.add_sample(i, 'old')
        for i in [60, 70, 80, 90, 99]:
            bsearch.add_sample(i, 'new')

        prob = bsearch.get_prob()
        self.assertLess(prob[5], prob[15])
        self.assertLess(prob[15], prob[25])
        self.assertLess(prob[25], prob[35])
        self.assertLess(prob[35], prob[45])
        self.assertLess(prob[45], prob[55])
        self.assertAlmostEqual(prob[55], 0.09, 6)
        self.assertAlmostEqual(prob[65], 0)
        self.assertAlmostEqual(prob[75], 0)
        self.assertAlmostEqual(prob[85], 0)
        self.assertAlmostEqual(prob[95], 0)

        self.assertEqual(bsearch.get_range(), (40, 60))
        self.assertEqual(bsearch.next_idx(), 54)
        assert not bsearch.is_done()

    def test_get_noise_observation(self):
        bsearch = strategy.NoisyBinarySearch(
            self.rev_info,
            observation='old=1/10,new=9/10',
        )
        self.assertEqual(bsearch.get_noise_observation(), 'old=1/10,new=9/10')

        bsearch.add_sample(0, 'old')
        bsearch.add_sample(10, 'new')
        bsearch.add_sample(20, 'old', times=2)
        bsearch.add_sample(30, 'old', times=20)
        bsearch.add_sample(50, 'new', times=20)
        bsearch.add_sample(60, 'new', times=20)
        bsearch.add_sample(90, 'old', times=2)
        bsearch.add_sample(99, 'new')

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

        bsearch = strategy.NoisyBinarySearch(
            rev_info,
            oracle=(0.01, 1),
        )
        bsearch.add_sample(n - 1, 'new')
        bsearch.add_sample(0, 'old')

        rng = random.Random(0)
        for _ in range(10):
            idx = rng.randint(1, n - 2)
            bsearch.add_sample(idx, 'skip')

            # Should not raise ValueError (math domain error).
            bsearch.next_idx()

    def test_many_skip(self):
        bsearch = strategy.NoisyBinarySearch(
            self.rev_info,
            oracle=(0.1, 0.9),
        )
        for _ in range(strategy.SKIP_FOREVER - 1):
            bsearch.add_sample(99, 'skip')

        with self.assertRaises(errors.TooManyTemporaryErrors):
            bsearch.add_sample(99, 'skip')

    def test_skip(self):
        bsearch = strategy.NoisyBinarySearch(
            self.rev_info,
            oracle=(0, 0.9),
        )
        bsearch.add_sample(0, 'old')
        bsearch.add_sample(99, 'new')
        self.assertEqual(bsearch.get_range(), (0, 99))
        self.assertEqual(bsearch.next_idx(), 45)

        bsearch.add_sample(45, 'skip')
        self.assertEqual(bsearch.get_range(), (0, 99))
        self.assertNotEqual(bsearch.next_idx(), 45)

    def test_skip_then_success(self):
        bsearch = strategy.NoisyBinarySearch(
            self.rev_info,
        )
        bsearch.add_sample(0, 'old')
        bsearch.add_sample(99, 'new')
        bsearch.add_sample(98, 'skip', times=strategy.SKIP_FOREVER)
        # Skip too many times, the probability is set to 0.
        self.assertEqual(bsearch.get_prob()[98], 0)

        # Suddenly the result is not 'skip' any more. The probability jump from
        # zero to non-zero.
        bsearch.add_sample(98, 'new')
        self.assertGreater(bsearch.get_prob()[98], 0)

    def test_skip_forever(self):
        bsearch = strategy.NoisyBinarySearch(
            self.rev_info,
        )
        bsearch.add_sample(0, 'old')
        bsearch.add_sample(99, 'new')
        bsearch.add_sample(40, 'skip', times=strategy.SKIP_FOREVER)
        # Skip too many times, the probability is set to 0.
        self.assertEqual(bsearch.get_prob()[40], 0)

        # For each revision between skip-forever revisions, the probablity is
        # also set to 0.
        bsearch.add_sample(50, 'skip', times=strategy.SKIP_FOREVER)
        self.assertEqual(bsearch.get_prob()[40:51], [0] * 11)

    def test_get_range_with_skip(self):
        bsearch = strategy.NoisyBinarySearch(
            self.rev_info,
            oracle=(0, 0.9),
        )
        bsearch.add_sample(0, 'old')
        bsearch.add_sample(33, 'old', times=10)
        bsearch.add_sample(34, 'skip', times=strategy.SKIP_FOREVER)
        bsearch.add_sample(35, 'new', times=10)
        bsearch.add_sample(99, 'new')

        self.assertEqual(bsearch.get_range(), (33, 35))

    def test_noisy(self):
        bsearch = strategy.NoisyBinarySearch(
            self.rev_info,
            confidence=0.99,
            oracle=(0, 0.9),
        )
        self.assertTrue(bsearch.is_noisy())
        for i in [0, 10, 20, 30, 40, 50]:
            bsearch.add_sample(i, 'old')
        for i in [60, 70, 80, 90, 99]:
            bsearch.add_sample(i, 'new')

        prob = bsearch.get_prob()
        self.assertLess(prob[5], prob[15])
        self.assertLess(prob[15], prob[25])
        self.assertLess(prob[25], prob[35])
        self.assertLess(prob[35], prob[45])
        self.assertLess(prob[45], prob[55])
        self.assertAlmostEqual(prob[55], 0.09, 6)
        self.assertAlmostEqual(prob[65], 0)
        self.assertAlmostEqual(prob[75], 0)
        self.assertAlmostEqual(prob[85], 0)
        self.assertAlmostEqual(prob[95], 0)

        self.assertEqual(bsearch.get_range(), (40, 60))
        self.assertEqual(bsearch.next_idx(), 54)
        assert not bsearch.is_done()

        bsearch.show_summary()

    def test_classic(self):
        self.rev_info = []
        for i in range(465):
            self.rev_info.append(core.RevInfo(str(i)))
        bsearch = strategy.NoisyBinarySearch(
            self.rev_info,
            462,
            463,
        )
        assert not bsearch.is_done()

        bsearch.add_sample(462, 'old')
        assert not bsearch.is_done()

        bsearch.add_sample(463, 'new')
        assert bsearch.is_done()
        self.assertAlmostEqual(bsearch.get_prob()[463], 1)
        self.assertEqual(bsearch.get_range(), (462, 463))
        self.assertEqual(bsearch.remaining_steps(), 0)
        bsearch.show_summary()

    @staticmethod
    def perform_search(
        size,
        ans,
        old_p,
        new_p,
        confidence,
        endpoint_verification,
        random_seed=0,
        cost_func=None,
    ):
        """Performs full noisy binary search.

        Args:
          size: Number of candidates.
          ans: Position of answer.
          old_p: False-positive probability for old candidates.
          new_p: True-positive probability for new candidates.
          confidence: Required confidence.
          endpoint_verification: Flag indicating whether the statistical
            verification method is enabled.
          random_seed: Random seed.
          cost_func: cost function.

        Returns:
          (switch_count, eval_count, guess):
            switch_count: Number of switch candidates.
            eval_count: Number of eval.
            guess: Best guess.
        """
        if cost_func is None:
            # pylint: disable=unnecessary-lambda-assignment
            cost_func = lambda *a: None
        rev_info = [core.RevInfo(str(i)) for i in range(size)]
        bsearch = strategy.NoisyBinarySearch(
            rev_info,
            confidence=confidence,
            oracle=(old_p, new_p),
            # Verify range until success.
            verify_confidence=1.0,
            endpoint_verification=endpoint_verification,
        )

        rng = random.Random(random_seed)
        switch_count = 0
        eval_count = 0
        prev_idx = None
        while not bsearch.is_done():
            cost_table = cost_func(prev_idx)
            idx = bsearch.next_idx(cost_table)
            if rng.random() < (old_p if idx < ans else new_p):
                result = 'new'
            else:
                result = 'old'

            if idx != prev_idx:
                switch_count += 1
            eval_count += 1
            bsearch.add_sample(idx, result)
            prev_idx = idx

        return switch_count, eval_count, bsearch.get_best_guess()

    def perform_stress(
        self,
        size,
        old_p,
        new_p,
        confidence,
        endpoint_verification,
        cost_func=None,
        num=None,
        answer=None,
    ):
        if num is None:
            num = size - 1
        switch_counts = []
        eval_counts = []
        results = []
        for i in range(num):
            if answer is None:
                # evenly distributed in range [1, size-1]
                ans = 1 + i * (size - 1) // num
            else:
                ans = answer
            switch_count, eval_count, result = self.perform_search(
                size,
                ans,
                old_p,
                new_p,
                confidence,
                endpoint_verification,
                random_seed=i,
                cost_func=cost_func,
            )
            switch_counts.append(switch_count)
            eval_counts.append(eval_count)
            results.append(result)
        return switch_counts, eval_counts, results

    def perform_search_for_noise_rate(
        self,
        size,
        ans,
        old_p,
        new_p,
        observation,
        confidence,
        endpoint_verification,
        cost_func=None,
    ):
        """Performs full noisy binary search.

        Args:
          size: Number of candidates.
          ans: Position of answer.
          old_p: False-positive probability for old candidates.
          new_p: True-positive probability for new candidates.
          observation: Test results observed by users.
          confidence: Required confidence.
          endpoint_verification: Flag indicating whether the statistical
            verification method is enabled.
          cost_func: cost function.

        Returns:
          prior_observation: prior_observation after the initial verification stage.
        """
        if cost_func is None:
            # pylint: disable=unnecessary-lambda-assignment
            cost_func = lambda *a: None
        rev_info = [core.RevInfo(str(i)) for i in range(size)]
        bsearch = strategy.NoisyBinarySearch(
            rev_info,
            confidence=confidence,
            observation=observation,
            verify_confidence=1.0,
            endpoint_verification=endpoint_verification,
        )
        prev_idx = None
        while bsearch.state == bsearch.INITED:
            cost_table = cost_func(prev_idx)
            idx = bsearch.next_idx(cost_table)
            if self.random_list.get_random_num() < (
                old_p if idx < ans else new_p
            ):
                result = 'new'
            else:
                result = 'old'

            bsearch.add_sample(idx, result)
            prev_idx = idx
        return bsearch.observation

    def test_classic_search(self):
        # Settings for non-noisy case.
        old_p = 0
        new_p = 1
        confidence = 0.99  # Doesn't matter.

        # Test the case with statistical verification method
        # Extreme case.
        _, eval_count, result = self.perform_search(
            2, 1, old_p, new_p, confidence, endpoint_verification=True
        )
        self.assertEqual(result, 1)

        # Tests answer positions.
        _, counts, results = self.perform_stress(
            50, old_p, new_p, confidence, endpoint_verification=True
        )
        self.assertEqual(results, list(range(1, 50)))

        # Slightly larger case.
        _, eval_count, result = self.perform_search(
            1000, 42, old_p, new_p, confidence, endpoint_verification=True
        )
        self.assertEqual(result, 42)

        # Test the case without statistical verification method
        # In addition to the same test as above, test eval_count
        _, eval_count, result = self.perform_search(
            2, 1, old_p, new_p, confidence, endpoint_verification=False
        )
        self.assertEqual(eval_count, 2)
        self.assertEqual(result, 1)

        _, counts, results = self.perform_stress(
            50, old_p, new_p, confidence, endpoint_verification=False
        )
        self.assertLessEqual(7, min(counts))
        self.assertLessEqual(max(counts), 8)
        self.assertEqual(results, list(range(1, 50)))

        _, eval_count, result = self.perform_search(
            1000, 42, old_p, new_p, confidence, endpoint_verification=False
        )
        self.assertLessEqual(eval_count, 12)
        self.assertEqual(result, 42)

    def test_half_noisy_search(self):
        """Tests noisy search with single side flaky."""
        # Extreme case.
        for endpoint_verification in [True, False]:
            _, _, result = self.perform_search(
                2, 1, 0.0, 0.5, 0.999, endpoint_verification
            )
            self.assertEqual(result, 1)

            _, eval_count, result = self.perform_search(
                3, 2, 0.0, 0.5, 0.999, endpoint_verification
            )
            # 1 - 0.5**10 > 0.999, at least 10 times to have enough confidence.
            self.assertGreaterEqual(eval_count, 10)
            self.assertEqual(result, 2)

            # Larger case.
            # Only makes sure it works. Don't verify the values due to randomness.
            self.perform_search(100, 42, 0, 0.3, 0.999, endpoint_verification)

        # Tests answer positions.
        self.perform_stress(10, 0, 0.3, 0.999, endpoint_verification=False)
        self.perform_stress(10, 0.3, 1, 0.999, endpoint_verification=False)

    def test_full_noisy_search(self):
        """Tests noisy binary search with flaky on two sides."""
        # Only makes sure it works. Don't verify the values due to randomness.
        self.perform_search(
            1000, 42, 0.05, 0.7, 0.999, endpoint_verification=True
        )
        self.perform_search(
            1000, 42, 0.05, 0.7, 0.999, endpoint_verification=False
        )

        # Tests answer positions.
        self.perform_stress(20, 0.1, 0.9, 0.999, endpoint_verification=True)
        self.perform_stress(20, 0.1, 0.9, 0.999, endpoint_verification=False)

    def test_non_uniform_costs(self):
        size = 100
        switch_cost = 600
        eval_cost = 60
        old_p = 0.05
        new_p = 0.7
        num_simulation = 10

        def cost_func(prev_idx):
            if prev_idx is None:
                return None
            result = []
            for i in range(size):
                if i == prev_idx:
                    cost = [eval_cost, eval_cost]
                else:
                    cost = [switch_cost + eval_cost, switch_cost + eval_cost]
                result.append(cost)
            return result

        switch_counts, eval_counts, _ = self.perform_stress(
            size,
            old_p,
            new_p,
            0.999,
            endpoint_verification=False,
            num=num_simulation,
        )
        cost1 = (
            math_util.average(switch_counts) * switch_cost
            + math_util.average(eval_counts) * eval_cost
        )

        switch_counts, eval_counts, _ = self.perform_stress(
            size,
            old_p,
            new_p,
            0.999,
            endpoint_verification=False,
            num=num_simulation,
            cost_func=cost_func,
        )
        cost2 = (
            math_util.average(switch_counts) * switch_cost
            + math_util.average(eval_counts) * eval_cost
        )
        # With more runs, the averages are actually near 14860 and 8990,
        # respectively.
        self.assertTrue(cost1 > cost2 + 4000)

    def test_confidence(self):
        n = 1000
        ans = 7
        counts, _, results = self.perform_stress(
            10,
            0.1,
            0.9,
            confidence=0.9,
            endpoint_verification=False,
            answer=ans,
            num=n,
        )
        self.assertLess(float(sum(counts)) / n, 9)
        common_guess = collections.Counter(results).most_common(1)[0]
        self.assertEqual(common_guess[0], ans)
        # It's expected that not all guesses are correct.
        self.assertLess(common_guess[1], n)
        # With 0.9 confidence, about 0.9 of guesses are correct.
        self.assertGreater(common_guess[1], n * 0.9)

    def test_value_bisect(self):
        bsearch = strategy.NoisyBinarySearch(
            self.rev_info,
            0,
            99,
            old_value=10.0,
            new_value=20.0,
            endpoint_verification=False,
        )
        self.assertTrue(bsearch.is_value_bisection())
        self.assertEqual(bsearch.classify_result_from_values([100]), 'new')
        self.assertEqual(bsearch.classify_result_from_values([0]), 'old')

        # verify the range
        self.assertEqual(bsearch.next_idx(), 99)
        bsearch.add_sample(99, 'new', values=[20.0], eval_time=1000)
        self.assertEqual(bsearch.next_idx(), 0)
        bsearch.add_sample(0, 'old', values=[11.0], eval_time=1000)

        # middle point
        self.assertEqual(bsearch.next_idx(), 49)

    def test_value_bisect_unreproducible(self):
        bsearch = strategy.NoisyBinarySearch(
            self.rev_info,
            0,
            99,
            old_value=10.0,
            new_value=20.0,
            endpoint_verification=False,
        )
        self.assertEqual(bsearch.next_idx(), 99)

        with self.assertRaises(errors.VerifyNewBehaviorFailed):
            bsearch.add_sample(99, 'old', values=[1.0], eval_time=1000)

    def test_value_bisect_noisy_unreproducible(self):
        bsearch = strategy.NoisyBinarySearch(
            self.rev_info,
            0,
            99,
            old_value=10.0,
            new_value=20.0,
            observation='old=1/100,new=99/100',
            endpoint_verification=False,
        )
        self.assertEqual(bsearch.next_idx(), 99)

        # It's acceptable to have opposite status few times due to noise.
        bsearch.add_sample(99, 'old', values=[1.0], eval_time=1000)

        # Pretty high confidence that opposite 100 times is almost impossible.
        with self.assertRaises(errors.VerifyNewBehaviorFailed):
            for _ in range(100):
                bsearch.add_sample(99, 'old', values=[1.0], eval_time=1000)

    def test_recompute_init_values(self):
        bsearch = strategy.NoisyBinarySearch(
            self.rev_info,
            0,
            99,
            old_value=10.0,
            new_value=20.0,
            recompute_init_values=True,
            endpoint_verification=False,
        )
        self.assertTrue(bsearch.is_value_bisection())
        self.assertEqual(bsearch.classify_result_from_values([100]), 'value')
        self.assertEqual(bsearch.classify_result_from_values([0]), 'value')
        self.assertEqual(bsearch.next_idx(), 99)
        self.assertEqual(bsearch.next_idx(), 99)

        # same value, twice is enough
        bsearch.add_sample(99, 'value', values=[20.0], eval_time=1000)
        self.assertEqual(bsearch.next_idx(), 99)
        bsearch.add_sample(99, 'value', values=[20.0], eval_time=1000)
        self.assertEqual(bsearch.next_idx(), 0)

        # different value, at least 3 times
        bsearch.add_sample(0, 'value', values=[10.0], eval_time=1000)
        self.assertEqual(bsearch.next_idx(), 0)
        bsearch.add_sample(0, 'value', values=[11.0], eval_time=1000)
        self.assertEqual(bsearch.next_idx(), 0)
        bsearch.add_sample(0, 'value', values=[12.0], eval_time=1000)
        self.assertEqual(bsearch.next_idx(), 49)

    def test_recompute_init_values_unreproducible(self):
        bsearch = strategy.NoisyBinarySearch(
            self.rev_info,
            0,
            99,
            old_value=10.0,
            new_value=20.0,
            recompute_init_values=True,
            endpoint_verification=False,
        )

        # same value, twice is enough
        bsearch.add_sample(99, 'value', values=[20.0], eval_time=1000)
        self.assertEqual(bsearch.next_idx(), 99)
        bsearch.add_sample(99, 'value', values=[20.0], eval_time=1000)
        self.assertEqual(bsearch.next_idx(), 0)

        # different value, at least 3 times
        bsearch.add_sample(0, 'value', values=[19.0], eval_time=1000)
        self.assertEqual(bsearch.next_idx(), 0)
        bsearch.add_sample(0, 'value', values=[18.0], eval_time=1000)
        self.assertEqual(bsearch.next_idx(), 0)
        with self.assertRaises(errors.WrongAssumption):
            bsearch.add_sample(0, 'value', values=[21.0], eval_time=1000)

    def test_recompute_init_values_undecidable(self):
        bsearch = strategy.NoisyBinarySearch(
            self.rev_info,
            0,
            99,
            old_value=10.0,
            new_value=20.0,
            recompute_init_values=True,
            endpoint_verification=False,
        )

        # same value, twice is enough
        bsearch.add_sample(99, 'value', values=[20.0], eval_time=1000)
        bsearch.add_sample(99, 'value', values=[20.0], eval_time=1000)

        # One sample with good trend
        bsearch.add_sample(0, 'value', values=[10.0], eval_time=100)
        self.assertEqual(bsearch.next_idx(), 0)
        # But others are not, so need more samples
        for _ in range(10):
            bsearch.add_sample(0, 'value', values=[17.0], eval_time=100)
            self.assertEqual(bsearch.next_idx(), 0)

    def test_pass_noise_rate(self):
        """Tests whether noise rate was passed to the noisy binary search."""
        # With endpoint_verification, prior_observation will be appointed by the
        # results of statistical tests.
        observation = self.perform_search_for_noise_rate(
            1000,
            42,
            0.3,
            0.7,
            'old=1/10,new=9/10',
            0.999,
            endpoint_verification=True,
        )
        self.assertEqual(observation[0], (7, 25))
        self.assertEqual(observation[1], (20, 25))

        # Without endpoint_verification, prior_observation will be appointed by
        # users.
        observation = self.perform_search_for_noise_rate(
            1000,
            42,
            0.3,
            0.7,
            'old=1/10,new=9/10',
            0.999,
            endpoint_verification=False,
        )
        self.assertEqual(observation[0], (1, 11))
        self.assertEqual(observation[1], (10, 11))


class TestStates(unittest.TestCase):
    """Test internal states are saved and loaded correctly.

    - On sucess
      - the bsearch runs successfully.
      - check_reproduced() returns True.
      - get_range() returns correct result before saved and after loaded.

    - On Failure in initialization phase.
        - the bsearch raises some exception.
        - check_reproduced() returns False.
        - get_range() returns correct resutl before saved and after loaded
          without raising exceptions.
    """

    def test_noise_search_done(self):
        """Tests noisy binary search with flaky on two sides."""
        rev_info = [core.RevInfo(str(i)) for i in range(1000)]
        bsearch = strategy.NoisyBinarySearch(
            rev_info,
            confidence=0.999,
            oracle=(0.05, 0.7),
            verify_confidence=1.0,
            endpoint_verification=True,
        )
        random_seed = 0
        rng = random.Random(random_seed)
        while not bsearch.is_done():
            idx = bsearch.next_idx()
            if rng.random() < (0.05 if idx < 42 else 0.7):
                result = 'new'
            else:
                result = 'old'
            bsearch.add_sample(idx, result)
        self.assertEqual(bsearch.get_range(), (41, 42))

        states = bsearch.export_states()
        saved_bsearch = strategy.NoisyBinarySearch(
            bsearch.rev_info,
            confidence=0.999,
            oracle=(0.05, 0.7),
            verify_confidence=1.0,
            endpoint_verification=True,
            saved_states=states,
        )
        self.assertEqual(saved_bsearch.get_range(), (41, 42))

    def test_noise_search_always_old_behavior(self):
        """Tests noisy binary search where the test always returns old behavior."""
        rev_info = [core.RevInfo(str(i)) for i in range(1000)]
        bsearch = strategy.NoisyBinarySearch(
            rev_info,
            confidence=0.999,
            oracle=(0.05, 0.7),
            verify_confidence=1.0,
            endpoint_verification=True,
        )
        with self.assertRaises(errors.VerifyInitialRangeFailed):
            while not bsearch.is_done():
                idx = bsearch.next_idx()
                bsearch.add_sample(idx, 'old')
        self.assertFalse(bsearch.check_reproduced())
        # Make sure get_range() doesn't raise.
        self.assertEqual(bsearch.get_range(), (0, 999))

        states = bsearch.export_states()
        saved_bsearch = strategy.NoisyBinarySearch(
            bsearch.rev_info,
            confidence=0.999,
            oracle=(0.05, 0.7),
            verify_confidence=1.0,
            endpoint_verification=True,
            saved_states=states,
        )
        self.assertFalse(saved_bsearch.check_reproduced())
        # Make sure get_range() doesn't raise.
        self.assertEqual(saved_bsearch.get_range(), (0, 999))

    def test_noise_search_always_new_behavior(self):
        """Tests noisy binary search where the test always returns new behavior."""
        rev_info = [core.RevInfo(str(i)) for i in range(1000)]
        bsearch = strategy.NoisyBinarySearch(
            rev_info,
            confidence=0.999,
            oracle=(0.05, 0.7),
            verify_confidence=1.0,
            endpoint_verification=True,
        )
        with self.assertRaises(errors.VerifyInitialRangeFailed):
            while not bsearch.is_done():
                idx = bsearch.next_idx()
                bsearch.add_sample(idx, 'new')
        self.assertFalse(bsearch.check_reproduced())
        # Make sure get_range() doesn't raise.
        self.assertEqual(bsearch.get_range(), (0, 999))

        states = bsearch.export_states()
        saved_bsearch = strategy.NoisyBinarySearch(
            bsearch.rev_info,
            confidence=0.999,
            oracle=(0.05, 0.7),
            verify_confidence=1.0,
            endpoint_verification=True,
            saved_states=states,
        )
        self.assertFalse(saved_bsearch.check_reproduced())
        # Make sure get_range() doesn't raise.
        self.assertEqual(saved_bsearch.get_range(), (0, 999))

    def test_value_bisect_done(self):
        rev_info = [core.RevInfo(str(i)) for i in range(100)]
        bsearch = strategy.NoisyBinarySearch(
            rev_info,
            0,
            99,
            old_value=10.0,
            new_value=20.0,
            endpoint_verification=False,
        )
        # verify the range
        bsearch.add_sample(99, 'new', values=[20.0], eval_time=1000)
        bsearch.add_sample(0, 'old', values=[11.0], eval_time=1000)

        self.assertTrue(bsearch.check_reproduced())
        self.assertEqual(bsearch.get_range(), (0, 99))

        states = bsearch.export_states()
        saved_bsearch = strategy.NoisyBinarySearch(
            bsearch.rev_info,
            0,
            99,
            old_value=10.0,
            new_value=20.0,
            endpoint_verification=False,
            saved_states=states,
        )

        self.assertTrue(saved_bsearch.check_reproduced())
        self.assertEqual(saved_bsearch.get_range(), (0, 99))

    def test_value_bisect_always_old_behavior(self):
        rev_info = [core.RevInfo(str(i)) for i in range(100)]
        bsearch = strategy.NoisyBinarySearch(
            rev_info,
            0,
            99,
            old_value=10.0,
            new_value=20.0,
            endpoint_verification=False,
        )
        with self.assertRaises(errors.VerifyNewBehaviorFailed):
            while not bsearch.is_done():
                idx = bsearch.next_idx()
                bsearch.add_sample(idx, 'old', values=[10.0])
        self.assertFalse(bsearch.check_reproduced())
        # Make sure get_range() doesn't raise.
        self.assertEqual(bsearch.get_range(), (0, 99))

        states = bsearch.export_states()
        saved_bsearch = strategy.NoisyBinarySearch(
            bsearch.rev_info,
            0,
            99,
            old_value=10.0,
            new_value=20.0,
            endpoint_verification=False,
            saved_states=states,
        )

        self.assertFalse(saved_bsearch.check_reproduced())
        # Make sure get_range() doesn't raise.
        self.assertEqual(saved_bsearch.get_range(), (0, 99))

    def test_value_bisect_always_new_behavior(self):
        rev_info = [core.RevInfo(str(i)) for i in range(100)]
        bsearch = strategy.NoisyBinarySearch(
            rev_info,
            0,
            99,
            old_value=10.0,
            new_value=20.0,
            endpoint_verification=False,
        )
        with self.assertRaises(errors.VerifyOldBehaviorFailed):
            while not bsearch.is_done():
                idx = bsearch.next_idx()
                bsearch.add_sample(idx, 'new', values=[20.0])
        self.assertFalse(bsearch.check_reproduced())
        # Make sure get_range() doesn't raise.
        self.assertEqual(bsearch.get_range(), (0, 99))

        states = bsearch.export_states()
        saved_bsearch = strategy.NoisyBinarySearch(
            bsearch.rev_info,
            0,
            99,
            old_value=10.0,
            new_value=20.0,
            endpoint_verification=False,
            saved_states=states,
        )

        self.assertFalse(saved_bsearch.check_reproduced())
        # Make sure get_range() doesn't raise.
        self.assertEqual(saved_bsearch.get_range(), (0, 99))

    def test_recompute_init_values_done(self):
        rev_info = [core.RevInfo(str(i)) for i in range(100)]
        bsearch = strategy.NoisyBinarySearch(
            rev_info,
            0,
            99,
            old_value=10.0,
            new_value=20.0,
            recompute_init_values=True,
            endpoint_verification=False,
        )
        # same value, twice is enough
        bsearch.add_sample(99, 'value', values=[20.0], eval_time=1000)
        bsearch.add_sample(99, 'value', values=[20.0], eval_time=1000)

        # different value, at least 3 times
        bsearch.add_sample(0, 'value', values=[10.0], eval_time=1000)
        bsearch.add_sample(0, 'value', values=[11.0], eval_time=1000)
        bsearch.add_sample(0, 'value', values=[12.0], eval_time=1000)

        self.assertTrue(bsearch.check_reproduced())
        self.assertEqual(bsearch.get_range(), (0, 99))

        states = bsearch.export_states()
        saved_bsearch = strategy.NoisyBinarySearch(
            bsearch.rev_info,
            0,
            99,
            old_value=10.0,
            new_value=20.0,
            endpoint_verification=False,
            saved_states=states,
        )

        self.assertTrue(saved_bsearch.check_reproduced())
        self.assertEqual(saved_bsearch.get_range(), (0, 99))

    def test_recompute_init_values_always_old_behavior(self):
        rev_info = [core.RevInfo(str(i)) for i in range(100)]
        bsearch = strategy.NoisyBinarySearch(
            rev_info,
            0,
            99,
            old_value=10.0,
            new_value=20.0,
            recompute_init_values=True,
            endpoint_verification=False,
        )

        # same value, twice is enough
        bsearch.add_sample(0, 'value', values=[10.0], eval_time=1000)
        bsearch.add_sample(0, 'value', values=[10.0], eval_time=1000)

        # different value, at least 3 times
        bsearch.add_sample(99, 'value', values=[11.0], eval_time=1000)
        bsearch.add_sample(99, 'value', values=[12.0], eval_time=1000)
        with self.assertRaises(errors.WrongAssumption):
            bsearch.add_sample(99, 'value', values=[9.0], eval_time=1000)

        self.assertFalse(bsearch.check_reproduced())
        # Make sure get_range() doesn't raise.
        self.assertEqual(bsearch.get_range(), (0, 99))

        states = bsearch.export_states()
        saved_bsearch = strategy.NoisyBinarySearch(
            bsearch.rev_info,
            0,
            99,
            old_value=10.0,
            new_value=20.0,
            endpoint_verification=False,
            saved_states=states,
        )

        self.assertFalse(saved_bsearch.check_reproduced())
        # Make sure get_range() doesn't raise.
        self.assertEqual(saved_bsearch.get_range(), (0, 99))

    def test_recompute_init_values_always_new_behavior(self):
        rev_info = [core.RevInfo(str(i)) for i in range(100)]
        bsearch = strategy.NoisyBinarySearch(
            rev_info,
            0,
            99,
            old_value=10.0,
            new_value=20.0,
            recompute_init_values=True,
            endpoint_verification=False,
        )

        # same value, twice is enough
        bsearch.add_sample(99, 'value', values=[20.0], eval_time=1000)
        bsearch.add_sample(99, 'value', values=[20.0], eval_time=1000)

        # different value, at least 3 times
        bsearch.add_sample(0, 'value', values=[19.0], eval_time=1000)
        bsearch.add_sample(0, 'value', values=[18.0], eval_time=1000)
        with self.assertRaises(errors.WrongAssumption):
            bsearch.add_sample(0, 'value', values=[21.0], eval_time=1000)

        self.assertFalse(bsearch.check_reproduced())
        # Make sure get_range() doesn't raise.
        self.assertEqual(bsearch.get_range(), (0, 99))

        states = bsearch.export_states()
        saved_bsearch = strategy.NoisyBinarySearch(
            bsearch.rev_info,
            0,
            99,
            old_value=10.0,
            new_value=20.0,
            endpoint_verification=False,
            saved_states=states,
        )

        self.assertFalse(saved_bsearch.check_reproduced())
        # Make sure get_range() doesn't raise.
        self.assertEqual(saved_bsearch.get_range(), (0, 99))


class TestGeminiFusionStrategy(unittest.TestCase):
    """Test GeminiFusionStrategy class."""

    def setUp(self):
        self.rev_info = []
        for i in range(10):
            self.rev_info.append(core.RevInfo(str(i)))

    def test_update_stats_provides_info_gain(self):
        mock_agent = mock.MagicMock()
        mock_agent.next.return_value = None
        with mock.patch(
            'bisect_kit.gemini_bisect.gemini_util.BisectAgent',
            return_value=mock_agent,
        ) as mock_agent_class:

            bsearch = strategy.GeminiFusionStrategy(
                self.rev_info,
                confidence=0.99,
                oracle=(0.1, 0.9),
                enable_gemini=True,
                test_name='my_test',
                source_root='my_root',
                endpoint_verification=False,
            )
            mock_agent_class.assert_called_once_with(
                'my_test',
                'my_root',
                [str(i) for i in range(10)],
                chromeos_mirror=None,
                board=None,
                old_p=0.1,
                new_p=0.9,
                session=None,
            )
            # Before bisection starts, state is INITED. Prob is None, utilities are None.
            bsearch.add_sample(0, 'old')
            bsearch.add_sample(9, 'new')

            # We transition to STARTED, and self.prob is populated.
            self.assertEqual(bsearch.state, bsearch.STARTED)

            # Let's call next_idx to trigger _update_gemini_stats with cost_table
            cost_table = [(1, 10)] * len(bsearch.rev_info)
            bsearch.next_idx(cost_table=cost_table)

            # Check that update_stats was called with old_p and new_p
            self.assertTrue(mock_agent.update_stats.called)
            self.assertEqual(
                mock_agent.update_stats.call_args[1].get('old_p'), 0.1
            )
            self.assertEqual(
                mock_agent.update_stats.call_args[1].get('new_p'), 0.9
            )

            # Retrieve the last call arguments
            last_stats = mock_agent.update_stats.call_args[0][0]

            # Check that 'info_gain' and 'info_gain_per_cost' are present in the stats
            for rev in bsearch.rev_info:
                rev_stats = last_stats[rev.rev]
                self.assertIn('info_gain', rev_stats)
                self.assertIn('info_gain_per_cost', rev_stats)
                # Since prob is valid, info_gain and info_gain_per_cost should be floats
                self.assertIsInstance(rev_stats['info_gain'], float)
                self.assertIsInstance(rev_stats['info_gain_per_cost'], float)

    def test_update_stats_with_gemini_suggestion(self):
        mock_agent = mock.MagicMock()
        mock_agent.next.return_value = '5'
        with mock.patch(
            'bisect_kit.gemini_bisect.gemini_util.BisectAgent',
            return_value=mock_agent,
        ):
            bsearch = strategy.GeminiFusionStrategy(
                self.rev_info,
                confidence=0.99,
                oracle=(0.1, 0.9),
                enable_gemini=True,
                test_name='my_test',
                source_root='my_root',
                endpoint_verification=False,
            )
            bsearch.add_sample(0, 'old')
            bsearch.add_sample(9, 'new')

            # We transition to STARTED, and self.prob is populated.
            self.assertEqual(bsearch.state, bsearch.STARTED)

            idx = bsearch.next_idx()
            self.assertEqual(idx, 5)
            idx2 = bsearch.next_idx()
            self.assertEqual(idx2, 5)

    def test_failure_reason_handling(self):
        mock_agent = mock.MagicMock()
        mock_agent.next.return_value = '5'
        with mock.patch(
            'bisect_kit.gemini_bisect.gemini_util.BisectAgent',
            return_value=mock_agent,
        ):
            bsearch = strategy.GeminiFusionStrategy(
                self.rev_info,
                confidence=0.99,
                oracle=(0.1, 0.9),
                enable_gemini=True,
                test_name='my_test',
                source_root='my_root',
                endpoint_verification=False,
            )
            bsearch.add_sample(0, 'old')
            bsearch.add_sample(9, 'new')

            bsearch.next_idx()
            bsearch.add_sample(
                5, 'new', reason='Test crashed with null pointer'
            )

            mock_agent.add_sample.assert_any_call(
                '5',
                'new',
                reason='Test crashed with null pointer',
                eval_log=None,
            )

    def test_likelihood_new_handling(self):
        mock_agent = mock.MagicMock()
        mock_agent.next.return_value = '5'
        with mock.patch(
            'bisect_kit.gemini_bisect.gemini_util.BisectAgent',
            return_value=mock_agent,
        ) as mock_agent_class:
            bsearch = strategy.GeminiFusionStrategy(
                self.rev_info,
                confidence=0.99,
                oracle=(0.1, 0.9),
                enable_gemini=True,
                test_name='my_test',
                source_root='my_root',
                endpoint_verification=False,
            )
            mock_agent_class.assert_called_once_with(
                'my_test',
                'my_root',
                [str(i) for i in range(10)],
                chromeos_mirror=None,
                board=None,
                old_p=0.1,
                new_p=0.9,
                session=None,
            )
            bsearch.add_sample(0, 'old')
            bsearch.add_sample(9, 'new')

            bsearch.next_idx()
            bsearch.add_sample(5, 'new', reason='Flaky failure')

            self.assertIn('5', bsearch._gemini_waiting_result)
            waiting_res = bsearch._gemini_waiting_result['5']
            self.assertIn('likelihood_new', waiting_res)
            self.assertIsInstance(waiting_res['likelihood_new'], float)
            self.assertGreaterEqual(waiting_res['likelihood_new'], 0.0)
            self.assertLessEqual(waiting_res['likelihood_new'], 1.0)

    def test_likelihood_new_handling_deterministic(self):
        mock_agent = mock.MagicMock()
        mock_agent.next.return_value = '5'
        with mock.patch(
            'bisect_kit.gemini_bisect.gemini_util.BisectAgent',
            return_value=mock_agent,
        ) as mock_agent_class:
            bsearch = strategy.GeminiFusionStrategy(
                self.rev_info,
                confidence=0.99,
                enable_gemini=True,
                test_name='my_test',
                source_root='my_root',
                endpoint_verification=False,
            )
            mock_agent_class.assert_called_once_with(
                'my_test',
                'my_root',
                [str(i) for i in range(10)],
                chromeos_mirror=None,
                board=None,
                old_p=0,
                new_p=1,
                session=None,
            )
            bsearch.add_sample(0, 'old')
            bsearch.add_sample(9, 'new')

            bsearch.next_idx()
            bsearch.add_sample(5, 'new', reason='Deterministic failure')

            self.assertIn('5', bsearch._gemini_waiting_result)
            waiting_res = bsearch._gemini_waiting_result['5']
            self.assertIn('likelihood_new', waiting_res)
            self.assertIsInstance(waiting_res['likelihood_new'], float)
            self.assertAlmostEqual(waiting_res['likelihood_new'], 1.0)

    def test_next_idx_exception_fallback(self):
        mock_agent = mock.MagicMock()
        mock_agent.next.side_effect = Exception('Gemini Internal Error')
        with mock.patch(
            'bisect_kit.gemini_bisect.gemini_util.BisectAgent',
            return_value=mock_agent,
        ):
            bsearch = strategy.GeminiFusionStrategy(
                self.rev_info,
                confidence=0.99,
                oracle=(0.1, 0.9),
                enable_gemini=True,
                test_name='my_test',
                source_root='my_root',
                endpoint_verification=False,
            )
            bsearch.add_sample(0, 'old')
            bsearch.add_sample(9, 'new')

            idx = bsearch.next_idx()
            self.assertIsNone(bsearch.gemini)
            self.assertIsNotNone(idx)

    def test_deepcopy(self):
        mock_agent = mock.MagicMock()
        with mock.patch(
            'bisect_kit.gemini_bisect.gemini_util.BisectAgent',
            return_value=mock_agent,
        ):
            bsearch = strategy.GeminiFusionStrategy(
                self.rev_info,
                confidence=0.99,
                oracle=(0.1, 0.9),
                enable_gemini=True,
                test_name='my_test',
                source_root='my_root',
                endpoint_verification=False,
            )
            bsearch_copy = copy.deepcopy(bsearch)
            self.assertIsNotNone(bsearch_copy.gemini)
            self.assertIsNot(bsearch_copy.gemini, bsearch.gemini)

    def test_noisy_binary_search_set_logger_prefix(self):
        bsearch = strategy.NoisyBinarySearch(self.rev_info)
        bsearch.set_logger_prefix('[Simulation] ')
        self.assertEqual(bsearch.logger.extra['prefix'], '[Simulation] ')

    def test_gemini_fusion_strategy_set_logger_prefix(self):
        mock_agent = mock.MagicMock()
        mock_agent.logger = mock.MagicMock()
        mock_agent.logger.extra = {'prefix': ''}
        with mock.patch(
            'bisect_kit.gemini_bisect.gemini_util.BisectAgent',
            return_value=mock_agent,
        ):
            bsearch = strategy.GeminiFusionStrategy(
                self.rev_info,
                confidence=0.99,
                oracle=(0.1, 0.9),
                enable_gemini=True,
                test_name='my_test',
                source_root='my_root',
                endpoint_verification=False,
            )
            bsearch.set_logger_prefix('[Simulation] ')
            self.assertEqual(bsearch.logger.extra['prefix'], '[Simulation] ')
            self.assertEqual(mock_agent.logger.extra['prefix'], '[Simulation] ')

    def test_prefix_logger_adapter_formatting(self):
        test_logger = logging.getLogger('test_prefix_logger')
        adapter = strategy.PrefixLoggerAdapter(test_logger, {'prefix': ''})
        with self.assertLogs(
            logger='test_prefix_logger', level='INFO'
        ) as log_capture:
            adapter.info('Without prefix')
            adapter.extra['prefix'] = '[Simulation] '
            adapter.info('With prefix')

        self.assertEqual(
            log_capture.output,
            [
                'INFO:test_prefix_logger:Without prefix',
                'INFO:test_prefix_logger:[Simulation] With prefix',
            ],
        )

    def test_logger_prefix_in_logs(self):
        with self.assertLogs(
            logger='bisect_kit.strategy', level='INFO'
        ) as log_capture:
            bsearch = strategy.NoisyBinarySearch(self.rev_info)
            bsearch.set_logger_prefix('[Simulation] ')
            bsearch.logger.info('Hello World')

        self.assertEqual(
            log_capture.output,
            ['INFO:bisect_kit.strategy:[Simulation] Hello World'],
        )


class TestBisectAgent(unittest.TestCase):
    """Test BisectAgent class."""

    @mock.patch('google.auth.transport.requests.AuthorizedSession')
    @mock.patch(
        'google.oauth2.service_account.Credentials.from_service_account_file'
    )
    @mock.patch('bisect_kit.gemini_bisect.gemini_util.GeminiAgent')
    @mock.patch('bisect_kit.git_util.get_commit_metadata')
    @mock.patch('bisect_kit.git_util.CommitMeta.get_summary')
    @mock.patch('bisect_kit.cr_util.build_revlist')
    def test_get_revisions_with_failure_reasons(
        self,
        mock_build_revlist,
        mock_get_summary,
        _mock_get_metadata,
        _mock_gemini_agent,
        _mock_creds,
        _mock_session,
    ):
        mock_build_revlist.return_value = (
            None,
            {
                '1': {'actions': [{'rev': 'commit1'}]},
                '2': {'actions': [{'rev': 'commit2'}]},
            },
        )
        mock_get_summary.return_value = 'Subject message'

        with mock.patch.dict(
            'os.environ', {'SKYLAB_CLOUD_SERVICE_ACCOUNT_JSON': 'dummy_path'}
        ):
            agent = gemini_util.BisectAgent(
                test_name='my_test',
                repo='my_repo',
                revlist=['1', '2'],
            )

            agent.add_sample('1', 'PASS')
            agent.add_sample('2', 'FAIL', reason='Crashed with NullPointer')

            # Default get_range: reasons not present
            revs = agent._get_revisions()
            self.assertEqual(revs[0]['rev_id'], '1')
            self.assertEqual(revs[0]['run_results'], ['PASS'])
            self.assertEqual(revs[1]['rev_id'], '2')
            self.assertEqual(revs[1]['run_results'], ['FAIL'])

            # get_range with include_reason=True
            revs_with_reason = agent._get_revisions(include_reason=True)
            self.assertEqual(revs_with_reason[0]['rev_id'], '1')
            self.assertEqual(
                revs_with_reason[0]['run_results'],
                [{'status': 'PASS', 'reason': None}],
            )
            self.assertEqual(revs_with_reason[1]['rev_id'], '2')
            self.assertEqual(
                revs_with_reason[1]['run_results'],
                [
                    {
                        'status': 'FAIL',
                        'reason': 'Crashed with NullPointer',
                    }
                ],
            )

            agent_copy = copy.deepcopy(agent)
            self.assertIsNot(agent_copy, agent)
            self.assertIsNot(agent_copy._agent, agent._agent)
            self.assertEqual(agent_copy._test_name, agent._test_name)
            self.assertEqual(agent_copy._revlist, agent._revlist)
            self.assertEqual(
                dict(agent_copy._rev_samples), dict(agent._rev_samples)
            )

    @mock.patch('google.auth.transport.requests.AuthorizedSession')
    @mock.patch(
        'google.oauth2.service_account.Credentials.from_service_account_file'
    )
    @mock.patch('bisect_kit.gemini_bisect.gemini_util.GeminiAgent')
    @mock.patch('bisect_kit.cr_util.build_revlist')
    def test_read_log_adhoc(
        self,
        mock_build_revlist,
        _mock_gemini_agent,
        _mock_creds,
        _mock_session,
    ):
        mock_build_revlist.return_value = (
            None,
            {
                '1': {'actions': [{'rev': 'commit1'}]},
            },
        )

        with tempfile.TemporaryDirectory() as tmp_repo:
            tast_dir = os.path.join(
                tmp_repo, 'tmp/tast_results_tmp/system_logs'
            )
            os.makedirs(tast_dir)
            syslog_file = os.path.join(tast_dir, 'syslog')
            with open(syslog_file, 'w', encoding='utf-8') as f:
                f.write('system log content\n')

            with tempfile.NamedTemporaryFile('w+', encoding='utf-8') as tmp:
                tmp.write('ssh to DUT\nrunning test\ntest completed PASS\n')
                tmp.flush()

                with mock.patch.dict(
                    'os.environ',
                    {'SKYLAB_CLOUD_SERVICE_ACCOUNT_JSON': 'dummy_path'},
                ):
                    agent = gemini_util.BisectAgent(
                        test_name='my_test',
                        repo=tmp_repo,
                        revlist=['1'],
                    )

                    # Test log reading when no sample added yet
                    resp = agent._read_log_file(agent._last_run_eval_log)
                    self.assertIn('error', resp)

                    # Add sample with eval_log
                    agent.add_sample('1', 'PASS', eval_log=tmp.name)
                    agent._pending_run_at_call = mock.MagicMock(id='dummy_id')
                    with mock.patch.object(agent, '_run', return_value=None):
                        agent.next(
                            {
                                'status': 'PASS',
                                'reason': None,
                                'eval_log': tmp.name,
                            }
                        )

                    # Test read_last_log helper
                    resp = agent._read_log_file(agent._last_run_eval_log)
                    self.assertEqual(
                        resp,
                        {
                            'log': 'ssh to DUT\nrunning test\ntest completed PASS\n'
                        },
                    )

                    # Test read_last_log with file_name parameter
                    resp = agent._read_log_file(
                        agent._last_run_eval_log, file_name='system_logs/syslog'
                    )
                    self.assertEqual(resp, {'log': 'system log content\n'})
                    self.assertEqual(
                        agent._list_collected_files(), ['system_logs/syslog']
                    )
                    resp_missing = agent._read_log_file(
                        agent._last_run_eval_log, file_name='nonexistent.txt'
                    )
                    self.assertIn('error', resp_missing)

                # Test read_log out of bounds run_index
                eval_log = agent._rev_samples['1'][0].get('eval_log')
                resp = agent._read_log_file(eval_log)
                self.assertEqual(
                    resp,
                    {'log': 'ssh to DUT\nrunning test\ntest completed PASS\n'},
                )

    @mock.patch('google.auth.transport.requests.AuthorizedSession')
    @mock.patch(
        'google.oauth2.service_account.Credentials.from_service_account_file'
    )
    @mock.patch('bisect_kit.gemini_bisect.gemini_util.GeminiAgent')
    @mock.patch('bisect_kit.cros_util.build_revlist')
    @mock.patch('bisect_kit.git_util.get_commit_metadata')
    @mock.patch('bisect_kit.git_util.CommitMeta.get_summary')
    @mock.patch('bisect_kit.git_util.git_show')
    @mock.patch('os.path.exists')
    def test_chromeos_streamlined_commits(
        self,
        mock_exists,
        mock_git_show,
        mock_get_summary,
        _mock_get_metadata,
        mock_build_revlist,
        _mock_gemini_agent,
        _mock_creds,
        _mock_session,
    ):
        mock_exists.side_effect = (
            lambda path: path.endswith('.git')
            and 'my_cros_root/.git' not in path
        )
        mock_git_show.side_effect = (
            lambda repo, commit: f"git show {commit} in {repo}"
        )

        details = {
            'R149-16656.0.0-118296~R149-16656.0.0-118297/110': {
                'actions': [
                    {
                        'action_type': 'commit',
                        'commit_summary': 'fix: first bug in platform',
                        'path': 'src/platform/bisect-kit',
                        'repo_url': 'url1',
                        'rev': 'commit_sha1',
                    },
                    {
                        'action_type': 'commit',
                        'commit_summary': 'fix: second bug in chromite',
                        'path': 'src/chromite',
                        'repo_url': 'url2',
                        'rev': 'commit_sha256',
                    },
                ]
            }
        }
        mock_build_revlist.return_value = (None, details)
        mock_get_summary.return_value = 'fix: first bug in platform'

        with mock.patch.dict(
            'os.environ', {'SKYLAB_CLOUD_SERVICE_ACCOUNT_JSON': 'dummy_path'}
        ):
            agent = gemini_util.BisectAgent(
                test_name='my_test',
                repo='my_cros_root',
                revlist=['R149-16656.0.0-118296~R149-16656.0.0-118297/110'],
            )

            # Test _get_revisions
            revs = agent._get_revisions()
            self.assertEqual(
                revs[0]['rev_id'],
                'R149-16656.0.0-118296~R149-16656.0.0-118297/110',
            )
            self.assertEqual(
                revs[0]['repo'], 'my_cros_root/src/platform/bisect-kit'
            )
            self.assertEqual(revs[0]['subject'], 'fix: first bug in platform')

            # Test _get_revision_detail
            detail = agent._get_revision_detail(
                'R149-16656.0.0-118296~R149-16656.0.0-118297/110'
            )
            self.assertEqual(
                detail['repo'], 'my_cros_root/src/platform/bisect-kit'
            )
            self.assertEqual(
                detail['git show'],
                'git show commit_sha1 in my_cros_root/src/platform/bisect-kit',
            )

    @mock.patch('google.auth.transport.requests.AuthorizedSession')
    @mock.patch(
        'google.oauth2.service_account.Credentials.from_service_account_file'
    )
    @mock.patch('bisect_kit.gemini_bisect.gemini_util.GeminiAgent')
    @mock.patch('bisect_kit.cr_util.build_revlist')
    def test_bisect_agent_flaky_rates(
        self,
        mock_build_revlist,
        _mock_gemini_agent,
        _mock_creds,
        _mock_session,
    ):
        mock_build_revlist.return_value = (
            None,
            {
                '1': {'actions': [{'rev': 'commit1'}]},
                '2': {'actions': [{'rev': 'commit2'}]},
            },
        )
        with mock.patch.dict(
            'os.environ', {'SKYLAB_CLOUD_SERVICE_ACCOUNT_JSON': 'dummy_path'}
        ):
            agent = gemini_util.BisectAgent(
                test_name='my_test',
                repo='my_repo',
                revlist=['1', '2'],
                old_p=0.2,
                new_p=0.8,
            )
            self.assertEqual(agent._old_p, 0.2)
            self.assertEqual(agent._new_p, 0.8)

            agent.update_stats({}, old_p=0.15, new_p=0.85)
            self.assertEqual(agent._old_p, 0.15)
            self.assertEqual(agent._new_p, 0.85)


if __name__ == '__main__':
    unittest.main()
