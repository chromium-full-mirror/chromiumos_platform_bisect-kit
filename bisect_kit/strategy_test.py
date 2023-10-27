# Copyright 2017 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Test strategy module."""

import collections
import random
import unittest

from bisect_kit import core
from bisect_kit import errors
from bisect_kit import math_util
from bisect_kit import strategy
from bisect_kit import testing


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

        with self.assertRaises(errors.UnableToProceed):
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


if __name__ == '__main__':
    unittest.main()
