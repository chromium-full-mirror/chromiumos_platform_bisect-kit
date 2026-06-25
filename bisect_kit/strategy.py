# Copyright 2017 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""The binary search algorithm."""

import logging
import math
import re
import sys

from bisect_kit import errors
from bisect_kit import math_util
from bisect_kit import strategy_states
from bisect_kit.gemini_bisect import gemini_util
from scipy.stats import boschloo_exact


class PrefixLoggerAdapter(logging.LoggerAdapter):
    """LoggerAdapter that prepends prefix to log messages."""

    def process(self, msg, kwargs):
        prefix = self.extra.get('prefix', '')
        return f'{prefix}{msg}', kwargs


# If a given version got equal to or more than SKIP_FOREVER times of 'skip'
# evaluation result, ignores the said version forever.
SKIP_FOREVER = 3

# Constant that denotes version is known not noisy.
NOT_NOISY = (None, None)

# Constant that denotes whether the statistical method is used in initial range
ENABLE_AUTO_ENDPOINT_VERIFICATION = False


def trend_score(prev_from, prev_to, now_from, now_to):
    """Calculates "trend score".

    Assume we have observed benchmark number changed from `prev_from` to
    `prev_to` from a dashboard, say, from 100 to 110. Now, we reproduced the same
    test locally and obtained our own benchmark values, from `now_from` to
    `now_to`. Because our local test may not be able to reproduce the exact
    identical changes (100 to 102) for some reasons, but it may have similar
    trend, say, 95 to 97, which is considered as a good reproduction.

    Here, we define the "similar trend" for two cases.
      1. we can reproduce the delta, but the value shifted, ex. previous values
         are 100->102 and our new values are 95->97 (delta=+2).
      2. we can reproduce the proportion change, but the value scaled, ex.
         previous values are 10->15 and our local values are 8->12 (+50%).

    The property of trend score:
      - score=0 if our local values stay the same
      - score=1 if we have similar change (delta or proportion); it's possible
        that score>1.
      - between 0 and 1 if we have the same trend but no so significant
      - negative if opposite direction, ex. previous values are increasing but our
        values are decreasing.

    Some cases proportion is inapplicable (ex. zero to non-zero, negative to
    positive, etc.), then only delta is used in calculation.

    Args:
      prev_from: previous value changed from
      prev_to: previous value changed to
      now_from: our new value changed from
      now_to: our new value changed to

    Returns:
      the score value
    """
    assert prev_to != prev_from
    abs_score = (now_to - now_from) / (prev_to - prev_from)
    if now_from == 0 or prev_from == 0 or prev_to * prev_from < 0:
        return abs_score

    ratio_score = ((now_to - now_from) / now_from) / (
        (prev_to - prev_from) / prev_from
    )
    return max(abs_score, ratio_score)


class NoisyBinarySearch:
    """Binary search algorithm supporting noisy signal.

    Examples:
      bsearch = NoisyBinarySearch(rev_info, observation='old=1/10,new=9/10')
      while not bsearch.is_done():
        bsearch.update(idx, get_result(rev_info[idx]))
      print(bsearch.get_best_guess())
    """

    # States
    #   INITED:
    #     Before both ends are verified.
    #     Recomputing initial values is also done in this phase.
    #     Not yet to calculate probabilities for each candidate.
    #   STARTED:
    #     Started to bisect.
    # There are no error states because exceptions are raised for fatal errors.
    # There are no "done" states because the confidence may become lower after
    # feeding more data.
    INITED = 'inited'
    STARTED = 'started'

    # During the initial range verification phase, there are four init-states:
    #   INIT_DO_ONE:
    #     Run several times at both ends, and the init-state will
    #     change to INIT_CHECK_ONE.
    #     The time is determined by rounds, up to three rounds.
    #   INIT_CHECK_ONE:
    #     Use statistical method to check for regression.
    #     According to the statistical test result, the init-state may
    #     change to INIT_DO_ONE or INIT_DO_TWO.
    #   INIT_DO_TWO:
    #     Do the final round of test. Run 10 times at both ends.
    #   INIT_CHECK_TWO:
    #     Do the final round of statistical test to check.
    #     According to the statistical test result, determine whether
    #     there is a regression.
    # INIT_STATUS_MAP describes transition of the init-states and the alpha value
    # for statistical tests.
    INIT_DO_ONE = 'init_do_one'
    INIT_CHECK_ONE = 'init_check_one'
    INIT_DO_TWO = 'init_do_two'
    INIT_CHECK_TWO = 'init_check_two'

    INIT_STATUS_MAP = {
        INIT_DO_ONE: {
            'next_state': INIT_CHECK_ONE,
            'action': 'counting',
        },
        INIT_CHECK_ONE: {
            'next_state': INIT_DO_TWO,
            'last_state': INIT_DO_ONE,
            'alpha': 0.01,
            'action': 'checking',
        },
        INIT_DO_TWO: {
            'next_state': INIT_CHECK_TWO,
            'action': 'counting',
        },
        INIT_CHECK_TWO: {
            'next_state': None,
            'alpha': 0.05,
            'action': 'checking',
        },
    }

    # INIT_ROUNDS:
    #   INIT_ROUNDS[i] represents the cumulative number of times it has run on
    #   both ends in the i-th round.
    # INIT_P_BOUND:
    #   For the first three rounds(e.g. round i), if the p-value calculated by
    #   the statistical method is greater than or equal to INIT_P_BOUND[i], the
    #   regression doesn't exist.
    #   For the last round, if the p-value calculated by the statistical method
    #   is greater than or equal to alpha, the regression doesn't exist.
    # INIT_P_BOUND is used to eliminate tests which apparently don't have
    #   regression.
    INIT_ROUND = [5, 10, 15, 25]
    INIT_P_BOUND = [0.65, 0.55, 0.5]

    def __init__(
        self,
        rev_info,
        old_idx=0,
        new_idx=None,
        old_value=None,
        new_value=None,
        term_map=None,
        recompute_init_values=False,
        oracle=None,
        observation=None,
        confidence=0.999,
        verify_confidence=0.999,
        endpoint_verification=None,
        saved_states=None,
    ):
        """Initializes NoisyBinarySearch.

        The probability model could be specified in three ways:
          - If oracle=(old_p, new_p) is specified. This means probability
            distribution is known.
              P(new behavior | old version) = old_p
              P(new behavior | new version) = new_p

          - If observation is specified, which means although the probability
            distribution is unknown, some previous test results are provided. The
            algorithm will re-estimate probability overtime once more test results
            feed.

            For example, prior_observation="old=2/3,new=9/10" means
                old version failed 2 times out of 3 times, and
                new version failed 9 times out of 10 times,
            where "new behavior" is "failed" in this case.

            If one of them is known non-noisy, it could be omitted. For example,
            "new=9/10" (old omitted) means old versions always have old behavior.

          - If both oracle and prior_observation are not specified, it means the
            signal is not noisy and this class will fallback to classic binary
            search.

        If recompute_init_values is True, the state transition is:
          1. `old_value` and `new_value` have values
             but `threshold` is None
             => `prob` is None
          2. _next_idx_for_initial() return None, so
             `old_avg`, `new_avg`, and `threshold` could be calculated
             => reclassify 'old' and 'new'

        Args:
          rev_info: List of RevInfo.
          old_idx: Index of rev, which has old behavior.
          new_idx: Index of rev, which has new behavior.
          old_value: For value bisection, the value of old version.
          new_value: For value bisection, the value of new version.
          term_map: Alternative term for states
          recompute_init_values: For value bisection, recompute values of old and
            new versions.
          oracle: The oracle model of probability distribution.
          observation: Observed test results.
          confidence: Required confidence for bisection result.
          verify_confidence: Bisect range verification confidence.
          endpoint_verification: Flag indicating whether the statistical
            verification method is enabled.
          saved_states: internal states from previous execution, if any.
        """
        assert oracle is None or observation is None
        self._state = self.INITED
        # guards self._state. See self.state()
        self.dirty = True
        if new_idx is None:
            new_idx = len(rev_info) - 1
        assert rev_info is not None
        self.rev_info = rev_info
        self.old_idx = old_idx
        self.new_idx = new_idx
        self.old_value = old_value
        self.new_value = new_value
        self.term_map = term_map or {}
        self.recompute_init_values = recompute_init_values
        self.oracle = oracle
        if oracle:
            self.old_p, self.new_p = self.oracle
            self.prior_observation = None
        elif observation:
            self.prior_observation = self._parse_observation(observation)
            self.observation = None
            (left_new, left_trial), (
                right_new,
                right_trial,
            ) = self.prior_observation
            self.old_p, self.new_p = self._observation_to_prob(
                left_new, left_trial, right_new, right_trial
            )
        else:
            self.prior_observation = None
            self.old_p, self.new_p = 0, 1
        self.confidence = confidence
        self.verify_confidence = verify_confidence

        self.prob = None

        # The probability of a revision to be the one introduce new behavior.
        # Initialize weight to 1.0 for revs in between old_idx and new_idx; and
        # to 0.0 otherwise.
        # TODO(kcwu): use heuristic to guess initial weight.
        self.init_weight = [
            1.0 if self.old_idx < i <= self.new_idx else 0.0
            for i in range(len(self.rev_info))
        ]
        self.skip_weight = None
        self.init_prob = None

        self.threshold = None
        if old_value is not None:
            if self.recompute_init_values:
                self.old_avg = None
                self.new_avg = None
            else:
                self.old_avg = old_value
                self.new_avg = new_value
                self.threshold = (old_value + new_value) / 2

        if endpoint_verification is None:
            self.endpoint_verification = ENABLE_AUTO_ENDPOINT_VERIFICATION
        else:
            self.endpoint_verification = endpoint_verification

        # init_verify_state includes: INIT_DO_ONE, INIT_CHECK_ONE,
        #                             INIT_DO_TWO, INIT_CHECK_TWO
        # init_range_verified denotes whether the initial range has been verified
        # with statistical method.
        # count records the results in both ends.
        self.init_rounds = 0
        self._init_verify_state = self.INIT_DO_ONE
        self.init_range_verified = False
        self.count_revold_old = 0
        self.count_revold_new = 0
        self.count_revnew_old = 0
        self.count_revnew_new = 0

        # passed_noise_rate denotes whether the noise rate calculated during the
        # initial range verification stage has been passed to noisy binary search.
        self.passed_noise_rate = False

        # Reads in saved states from previuos execution, if any.
        self.logger = PrefixLoggerAdapter(
            logging.getLogger(__name__), {'prefix': ''}
        )
        self._import_states(saved_states)

    def export_states(self) -> strategy_states.States:
        return strategy_states.States(
            state=self.state,
            init_verify_state=self.init_verify_state,
            init_range_verified=self.init_range_verified,
            init_rounds=self.init_rounds,
            count_revold_old=self.count_revold_old,
            count_revold_new=self.count_revold_new,
            count_revnew_old=self.count_revnew_old,
            count_revnew_new=self.count_revnew_new,
            passed_noise_rate=self.passed_noise_rate,
        )

    def _import_states(self, states: strategy_states.States):
        self.logger.debug('_import_states: %s', states)
        if not states:
            return

        if states.state is not None:
            self._state = states.state
            if self._state == self.STARTED:
                self._init_for_state_started()
            self.dirty = False
        if states.init_verify_state is not None:
            self._init_verify_state = states.init_verify_state
        if states.init_range_verified is not None:
            self.init_range_verified = states.init_range_verified
        if states.init_rounds is not None:
            self.init_rounds = states.init_rounds
        if states.count_revold_old is not None:
            self.count_revold_old = states.count_revold_old
        if states.count_revold_new is not None:
            self.count_revold_new = states.count_revold_new
        if states.count_revnew_old is not None:
            self.count_revnew_old = states.count_revnew_old
        if states.count_revnew_new is not None:
            self.count_revnew_new = states.count_revnew_new
        if states.passed_noise_rate is not None:
            self.passed_noise_rate = states.passed_noise_rate

    def is_noisy(self):
        """Is noisy case?"""
        if self.oracle and self.oracle != (0, 1):
            return True
        return self.prior_observation is not None

    @property
    def init_verify_state(self):
        current_state = self._init_verify_state
        if self.INIT_STATUS_MAP[current_state]['action'] == 'checking':
            return self._init_verify_state

        current_round = self.INIT_ROUND[self.init_rounds]
        if (
            self.count_revnew_old + self.count_revnew_new >= current_round
            and self.count_revold_old + self.count_revold_new >= current_round
        ):
            self._init_verify_state = self.INIT_STATUS_MAP[current_state][
                'next_state'
            ]
        return self._init_verify_state

    @property
    def state(self):
        if self._state == self.INITED:
            if not self.dirty:
                return self._state
            # It may raise exception. So if we don't want to get exception in
            # read-only operations like get_range() across execution, save
            # internal states to make self.dirty False after initialization.
            try:
                idx = self._next_idx_for_initial()
            finally:
                self.dirty = False
            if idx is not None:
                return self._state
            self._state = self.STARTED
            self._init_for_state_started()

        assert self._state == self.STARTED, 'unexpected state: %s' % self._state
        return self._state

    def _init_for_state_started(self):
        """build internal states when self._state reached STARTED.

        Raises:
          errors.WrongAssumption: failed to calculate self.prob.
        """
        assert self._state == self.STARTED
        if self.is_value_bisection() and self.recompute_init_values:
            if self.old_avg is None:
                self.old_avg = math_util.average(
                    self.rev_info[self.old_idx].averages()
                )
            if self.new_avg is None:
                self.new_avg = math_util.average(
                    self.rev_info[self.new_idx].averages()
                )
            if self.threshold is None:
                self.threshold = (self.old_avg + self.new_avg) / 2
            self.rev_info[self.old_idx].reclassify(
                self.old_avg, self.threshold, self.new_avg
            )
            self.rev_info[self.new_idx].reclassify(
                self.old_avg, self.threshold, self.new_avg
            )

        if self.prob is None:
            self._rebuild()
            self._estimate_model()
            assert self.prob

    def add_sample(self, idx, status=None, **kwargs):
        sample = {"status": status, **kwargs}
        self.rev_info[idx].add_sample(**sample)
        self.dirty = True

        status = sample['status']

        if self.endpoint_verification and not self.is_value_bisection():
            self.renew_initial_count(status, idx)

        self.check_verification_range()
        if status == 'skip':
            self.check_proceed_with_skip(
                idx, sample.get('reason', 'something wrong')
            )
        if self.state == self.INITED:
            pass
        elif self.state == self.STARTED:
            if status == 'value':
                status = self.classify_result_from_values(sample['values'])
            assert self.prob
            for _ in range(sample.get('times', 1)):
                self._update(idx, status)
        else:
            assert 0, 'unexpected state: ' + self.state

    @staticmethod
    def _parse_observation(observation):
        """Parses prior from argument.

        Args:
          observation: A string denoting previous evaluation results. See comments
              of __init__ for detail format.

        Returns:
          ((old false-positive, old trials), (new true-positive, new trials))
        """
        # default not noisy
        old_f: tuple[int | None, int | None] = NOT_NOISY
        new_t: tuple[int | None, int | None] = NOT_NOISY
        if not observation:
            return old_f, new_t

        for term in observation.split(','):
            m = re.match(r'^(old|new)=(\d+)/(\d+)$', term)
            assert m
            new_behavior, trial = int(m.group(2)), int(m.group(3))
            assert 0 <= new_behavior <= trial

            if m.group(1) == 'new':
                new_t = new_behavior, trial
            elif m.group(1) == 'old':
                old_f = new_behavior, trial

        return old_f, new_t

    def _gather_noise_count(self):
        """Gathers noise count from current run results.

        After few rounds of binary search, the candidates could be split into three
        groups.
          - left: high confidence to have OLD behavior.
          - middle: not enough confidence, need further inspection.
          - right: high confidence to have NEW behavior.

        Returns:
          left_new: how many NEW in left region.
          left_trial: how many trials in left region.
          right_new: how many NEW in right region.
          right_trial: how many trials in right region.
        """
        if self.observation:
            (left_new, left_trial), (right_new, right_trial) = self.observation
            return left_new, left_trial, right_new, right_trial

        assert self.prior_observation is not None
        (left_new, left_trial), (
            right_new,
            right_trial,
        ) = self.prior_observation

        if (left_new, left_trial) != NOT_NOISY:
            info = self.rev_info[self.old_idx]
            left_trial += info['old'] + info['new']
            left_new += info['new']
        if (right_new, right_trial) != NOT_NOISY:
            info = self.rev_info[self.new_idx]
            right_trial += info['old'] + info['new']
            right_new += info['new']

        return left_new, left_trial, right_new, right_trial

    @staticmethod
    def _expected_probability_from_observation(trial, num):
        """Expected probability from given observation.

        Given `trial` times of Bernoulli experiment, the results are 1 for `num`
        times. Calculates the expected probability.

        Returns:
          prob: the expected probability.
        """
        # P(observation)
        #   = \int_0^1 C(trial, num) * p^num * (1-p)^(trial-num) dp
        #   = 1 / (trial+1)
        # P(p | observation)
        #   = P(observation | p) * P(p) / P(observation)
        #   = C(trial, num) * p^num * (1-p)^(trial-num) * P(p) * (trial+1)
        # E[P(p | observation)]
        #   = \int_0^1 P(p | observation) * p dp
        #   = C(trial, num) * \int_0^1 p^num * (1-p)^(trial-num)*p dp
        #   = (num+1)/(trial+2)
        return float(num + 1) / (trial + 2)

    @staticmethod
    def _observation_to_prob(left_new, left_trial, right_new, right_trial):
        old_p, new_p = 0, 1
        if (left_new, left_trial) != NOT_NOISY:
            old_p = NoisyBinarySearch._expected_probability_from_observation(
                left_trial, left_new
            )
        if (right_new, right_trial) != NOT_NOISY:
            new_p = NoisyBinarySearch._expected_probability_from_observation(
                right_trial, right_new
            )
        return old_p, new_p

    def _estimate_model_from_observation(self):
        """Estimates model from observation.

        We don't know the real probability distribution. Assume they are Bernoulli
        process and calculates the expected probability from observations.

        Because the model will be slightly different after _rebuild(), we will
        repeat until it is stable.
        """
        assert self.prior_observation
        # TODO: endpoint verification for perf metrics has not been implemented yet.
        # When endpoint_verification is enabled, 'observation' will be calculated
        # based on the results of statistical methods, not user input.
        if self.endpoint_verification and not self.is_value_bisection():
            (left_new, left_trial), (right_new, right_trial) = ((0, 0), (0, 0))
        else:
            (left_new, left_trial), (
                right_new,
                right_trial,
            ) = self.prior_observation

        seen = set()
        while True:
            old_idx, new_idx = self.get_range()
            if (old_idx, new_idx) in seen:
                break
            seen.add((old_idx, new_idx))
            if (left_new, left_trial) != NOT_NOISY:
                for info in self.rev_info[: old_idx + 1]:
                    left_trial += info['old'] + info['new']
                    left_new += info['new']
            if (right_new, right_trial) != NOT_NOISY:
                for info in self.rev_info[new_idx:]:
                    right_trial += info['old'] + info['new']
                    right_new += info['new']

            self.observation = (left_new, left_trial), (right_new, right_trial)
            self.old_p, self.new_p = self._observation_to_prob(
                left_new, left_trial, right_new, right_trial
            )

            if (
                not self.passed_noise_rate
                and self.endpoint_verification
                and not self.is_value_bisection()
            ):
                self.logger.info(
                    'Failure rate is recalculated: old=%d/%d (%f%%), new=%d/%d (%f%%)',
                    self.observation[0][0],
                    self.observation[0][1],
                    self.old_p * 100,
                    self.observation[1][0],
                    self.observation[1][1],
                    self.new_p * 100,
                )
                self.passed_noise_rate = True

            if (self.old_p, self.new_p) in seen:
                break
            seen.add((self.old_p, self.new_p))
            self._rebuild()

    def _estimate_model(self):
        """Estimates probability of old false-positive and new true-positive.

        In other words, the rates of new behavior is observed at old revisions
        and new revisions respectively.
        """
        if self.oracle:
            self.old_p, self.new_p = self.oracle
        elif self.prior_observation:
            self._estimate_model_from_observation()
            self.logger.debug(
                'estimated model: old_p=%s, new_p=%s', self.old_p, self.new_p
            )
        else:
            self.old_p, self.new_p = 0, 1

        if 0 < self.old_p < 1.0 - self.confidence:
            self.logger.warning(
                'Confidence value (%r) is too low to detect '
                'old candidates with new behavior (p=%r)',
                self.confidence,
                self.old_p,
            )
        if self.confidence < self.new_p < 1.0:
            self.logger.warning(
                'Confidence value (%r) is too low to detect '
                'new candidates with old behavior (p=%r)',
                self.confidence,
                1.0 - self.new_p,
            )

    def get_noise_observation(self) -> str | None:
        """Gets current noise observation.

        This includes the counts of prior observations from __init__.

        Returns:
          A string indicates eval result noise observation so far. See comments of
          __init__ for detail format.
        """
        assert not self.oracle
        if not self.prior_observation:
            return None
        (
            left_new,
            left_trial,
            right_new,
            right_trial,
        ) = self._gather_noise_count()
        observation = []
        if (left_new, left_trial) != NOT_NOISY:
            observation.append('old=%d/%d' % (left_new, left_trial))
        if (right_new, right_trial) != NOT_NOISY:
            observation.append('new=%d/%d' % (right_new, right_trial))
        return ','.join(observation)

    @staticmethod
    def get_weight_by_skip(rev_info):
        """Gets weighting adjustment according to 'skip' test result.

        There are two cases of 'skip':
          1. The tested version is bad (say, compile error). In such case, its
             neighbors are likely 'skip' as well.
          2. Some thing wrong temporarily (say, network error). Needs retry.

        Ideal solution to handle 'skip' properly during binary search:
          - Avoid (less likely) to waste time to eval the 'skip' versions and
            their neighbors.
          - If other candidates are ruled out, we still need to retry them.
          - If a version keeps 'skip' few times for every retry, it is not
            temporarily case 2, we can ignore them completely.

        It's easy to implement above idea. The secret is that we can assign prior,
        P(x_i is first), of the Bayesian formula.
          - We can assign smaller probabilities if we prefer not to try them.
          - We can assign zero probabilities if we don't want to try them.

        Args:
          rev_info: List of RevInfo.

        Returns:
          list of weighting; 1.0 for not adjusted; otherwise, lower values.
        """
        # heuristic, chosen arbitrary for now
        impact_range = int(math.sqrt(len(rev_info)))
        impact_factor = 0.9

        weighting = [1.0] * len(rev_info)
        for i, info in enumerate(rev_info):
            # If ever non-skip, we don't care their 'skip' record.
            if info['new'] or info['old']:
                continue
            if not info['skip']:
                continue

            # If retried enough times but still 'skip', don't try them forever.
            if info['skip'] >= SKIP_FOREVER:
                weighting[i] = 0
            else:
                # Allow retry, but the more 'skip', the lower weighting.
                weighting[i] *= 0.1 ** info['skip']

        # Skip-forever revisions are broken. Assume that revisions between them
        # are also broken and avoid evaluating them.
        i_last_skip_forever = None
        for i, info in enumerate(rev_info):
            if info['new'] or info['old']:
                i_last_skip_forever = None
                continue

            if not info['skip']:
                continue

            if info['skip'] >= SKIP_FOREVER:
                if i_last_skip_forever is not None:
                    for j in range(i_last_skip_forever, i + 1):
                        weighting[j] = 0
                i_last_skip_forever = i

        # Adjust weighting of neighbors.
        # Following formula is chosen arbitrarily. The main idea is "impact" is
        # correlated to the distance until reaching non-skip version.
        # p.s. e**(-pi*x**2) is borrowed from normal distribution.
        for i, info in enumerate(rev_info):
            # If ever non-skip, we don't care their 'skip' record.
            if info['new'] or info['old']:
                continue
            if not info['skip']:
                continue

            # left to right
            for j in range(i + 1, min(i + impact_range, len(rev_info))):
                if rev_info[j]['new'] or rev_info[j]['old']:
                    break
                x = float(i - j) / impact_range
                impact = math.e ** (-math.pi * x**2) * impact_factor
                weighting[j] = min(weighting[j], 1.0 - impact)

            # right to left
            for j in reversed(list(range(max(0, i - impact_range + 1), i))):
                if rev_info[j]['new'] or rev_info[j]['old']:
                    break
                x = float(i - j) / impact_range
                impact = math.e ** (-math.pi * x**2) * impact_factor
                weighting[j] = min(weighting[j], 1.0 - impact)

        return weighting

    @staticmethod
    def _calculate_probs(old_p, new_p, rev_info, init_prob):
        """Calculates P(x_i is first new | results) for all candidates.

        What we want:
          prob[i] = P(x_i is first new | results)
                  = P(x_i is first new) * P(results | x_i is first new) / P(results)

        Args:
          old_p: P(observe=new | x=old)
          new_p: P(observe=new | x=new)
          rev_info: List of RevInfo.
          init_prob: unnormalized P(x_i is first new)

        Returns:
          prob, where prob[i] = P(x_i is first new | results)

        Raises:
          errors.WrongAssumption if failed to calculate prob.
        """
        assert 0 <= old_p < new_p

        # First, let prob[i] = P(x_i is first new). Because prob[i] will be
        # normalized later, it's okay to use unnormalized init_prob here directly.
        prob = list(init_prob)

        total_old = sum(info['old'] for info in rev_info)
        total_new = sum(info['new'] for info in rev_info)

        # Estimate the lower bound of prob[i].
        # If the value is less than minimum float value, use ExtendedFloat for
        # calculation to prevent underflow. See comments of ExtendedFloat for more
        # detail.
        min_prob = math_util.least([old_p, 1 - old_p, new_p, 1 - new_p]) ** (
            total_old + total_new
        ) * math_util.least(prob)
        if min_prob < sys.float_info.min:
            prob = [math_util.ExtendedFloat(p) for p in prob]
            old_p = math_util.ExtendedFloat(old_p)
            new_p = math_util.ExtendedFloat(new_p)

        # These four variables, {left,right}_{old,new}, are corresponding to x_i.
        # That is, initialized to corresponding to x_0 and incrementally updated at
        # end of each loop iteration.
        # left_old means how many observed old behavior left to x_i. So on and so
        # forth.
        left_old = left_new = 0
        right_old = total_old
        right_new = total_new
        for i, p in enumerate(prob):
            # prob[i] *= P(results | x_i is first new)
            if p != 0:
                if left_new != 0:
                    p *= old_p**left_new
                if left_old != 0:
                    p *= (1 - old_p) ** left_old
                if right_new != 0:
                    p *= new_p**right_new
                if right_old != 0:
                    p *= (1 - new_p) ** right_old
                prob[i] = p

            # Update count for next iteration.
            left_old += rev_info[i]['old']
            left_new += rev_info[i]['new']
            right_old -= rev_info[i]['old']
            right_new -= rev_info[i]['new']

        # P(results) = \sum_i P(results | x_i is first new) * P(x_i is first new)
        #            = \sum_i prob[i]
        sum_prob = sum(prob)

        if old_p == 0 and new_p == 1:
            if sum_prob == 0:
                raise errors.WrongAssumption(
                    'Test results conflict with assumption, try --noisy'
                )
        else:
            assert sum_prob > 0

        # prob[i] /= P(results)
        prob = [x / sum_prob for x in prob]

        # Convert back to float.
        prob = [float(p) for p in prob]
        return prob

    def _rebuild(self):
        """Rebuilds probability.

        Raises:
          errors.WrongAssumption: failed to calculate self.prob.
        """
        assert 0 <= self.old_idx < self.new_idx < len(self.rev_info)
        # First, P(x_i is first new) = Normalize(init_prob[i])
        #                            = Normalize(init_weight[i] * skip_weight[i])
        self.skip_weight = self.get_weight_by_skip(self.rev_info)
        self.init_prob = [
            p * w for p, w in zip(self.init_weight, self.skip_weight)
        ]
        self.prob = self._calculate_probs(
            self.old_p, self.new_p, self.rev_info, self.init_prob
        )

    def _is_changing_skip_weight(self, idx, result):
        if result == 'skip':
            # It is skipped, so skip_weight must be changed.
            return True

        # Not the first non-skip result, skip_weight is unaffected.
        if self.rev_info[idx][result] > 1:
            return False

        assert self.rev_info[idx][result] == 1
        # For the first non-skip result, skip_weight is affected if itself or its
        # neighbor's skip_weight != 1.
        for i in [idx - 1, idx, idx + 1]:
            if 0 <= i < len(self.rev_info) and self.skip_weight[i] != 1:
                return True

        return False

    def is_value_bisection(self):
        return self.old_value is not None and self.new_value is not None

    def classify_result_from_values(self, values):
        assert (
            values
        ), 'eval script terminated normally but does not output values?'
        if self.state == self.INITED and self.recompute_init_values:
            return 'value'
        assert self.threshold is not None
        assert self.old_avg is not None
        assert self.new_avg is not None
        assert values
        avg = math_util.average(values)
        if self.old_avg < self.new_avg:
            return 'old' if avg < self.threshold else 'new'
        return 'new' if avg < self.threshold else 'old'

    def _update(self, idx, result):
        """Incremental update by one more test result.

        Args:
          idx: index of the new test.
          result: result of the new test. 'old', 'new', or 'skip'.

        Raises:
          errors.WrongAssumption: failed to calculate self.prob.
        """
        assert self.prob
        assert self.rev_info

        if self._is_changing_skip_weight(idx, result):
            # Incremental update by skip_weight.
            new_skip_weight = self.get_weight_by_skip(self.rev_info)
            for i, (cur_skip_w, new_skip_w) in enumerate(
                zip(self.skip_weight, new_skip_weight)
            ):
                if new_skip_w != cur_skip_w:
                    # A non-skip result after several skips, the weight will jump from
                    # zero to non-zero and unable to update incrementally.
                    if cur_skip_w == 0:
                        self._rebuild()
                        return
                    self.prob[i] *= new_skip_w / cur_skip_w
            self.skip_weight = new_skip_weight

        # If not skip, prob[i] needs to multiply probability of new observation.
        #   prob[i] *= P(last result | x_i is first new).
        # Let's consider the value of multiplier,
        #   if result=new && i <= idx: P(true-positive)
        #   if result=new && i > idx: P(false-positive)
        #   if result=old && i <= idx: P(true-negative) = 1 - P(true-positive)
        #   if result=old && i > idx: P(false-negative) = 1 - P(false-positive)
        if result == 'new':
            for i in range(idx + 1):
                self.prob[i] *= self.new_p
            for i in range(idx + 1, len(self.prob)):
                self.prob[i] *= self.old_p
        if result == 'old':
            for i in range(idx + 1):
                self.prob[i] *= 1 - self.new_p
            for i in range(idx + 1, len(self.prob)):
                self.prob[i] *= 1 - self.old_p

        sum_prob = sum(self.prob)
        assert sum_prob > 0
        self.prob = [x / sum_prob for x in self.prob]

        if self.prior_observation:
            self._estimate_model()

    def get_range(self, confidence=None):
        """Gets narrowed-down bisection range so far.

        Using a heuristic algorithm to find a semi-open range (left, right] that,
        sum(prob[left+1:right+1]) >= confidence. Some optimizations are applied so
        the range may not be the narrowest.

        If `confidence` value is bigger, the returned range is bigger.

        Args:
          confidence: Confidence value. Default is the confidence provided in ctor.

        Returns:
          (left, right):
            left: Index likely before the point of probability change.
            right: Index likely after the point of probability change.

        Raises:
          errors.WrongAssumption: the bisection range is invalid
        """
        if self.state == self.INITED:
            return self.old_idx, self.new_idx
        assert self.prob

        if confidence is None:
            confidence = self.confidence

        def order_by_prob(x):
            index, prob = x
            return -prob, index

        # Non-zero probabilities sorted by prob (big to small), then index (small
        # to big)
        sorted_prob = sorted(
            ((i, p) for (i, p) in enumerate(self.prob) if p > 0),
            key=order_by_prob,
        )
        cumulative = 0
        cut = None
        candidates = set()
        # This loop finds a range whose sum of probabilities are bigger than the
        # value of confidence. But on the edge of threshold, it will include more
        # candidates until the ratio of probability delta from the cut point is
        # larger than a predefined gap, which is empirical.
        GAP = 0.5
        # p is monotonically decreasing.
        for i, p in sorted_prob:
            cumulative += p
            if cut is None:
                if cumulative >= confidence:
                    cut = p
            else:
                if p < cut * GAP:
                    break
            candidates.add(i)
        assert cut is not None or confidence == 1

        left = max(min(candidates) - 1, self.old_idx)
        # Sometimes left has old probability due to 'skip', not 'old'. Seeks for
        # version with 'old'.
        while left > self.old_idx and self.rev_info[left]['old'] == 0:
            left -= 1

        right = max(candidates)
        assert right <= self.new_idx
        return left, right

    def check_verification_range(self):
        """Checks if bisection range is reasonable.

        If the eval results conflict with the bisection range, abort the
        bisection if the probability is too low.

        Raises:
          errors.VerificationFailed: Bisection range is not verified and false
              events occur too many times.
        """
        if self.state == self.INITED and self.recompute_init_values:
            return

        if (
            self.state == self.INITED
            and self.endpoint_verification
            and not self.is_value_bisection()
        ):
            return

        if self.rev_info[self.old_idx]['old'] == 0:
            # P(try N times and old behavior occurs 0 times)
            p = self.old_p ** self.rev_info[self.old_idx]['new']
            if p < 1.0 - self.verify_confidence:
                raise errors.VerifyOldBehaviorFailed(
                    self.rev_info[self.old_idx].rev,
                    self.rev_info[self.old_idx]['new'],
                    term_old=self.term_map.get('old', 'old'),
                    term_new=self.term_map.get('new', 'new'),
                )

        if self.rev_info[self.new_idx]['new'] == 0:
            # P(try N times and new behavior occurs 0 times)
            p = (1 - self.new_p) ** self.rev_info[self.new_idx]['old']
            if p < 1.0 - self.verify_confidence:
                raise errors.VerifyNewBehaviorFailed(
                    self.rev_info[self.new_idx].rev,
                    self.rev_info[self.new_idx]['old'],
                    term_old=self.term_map.get('old', 'old'),
                    term_new=self.term_map.get('new', 'new'),
                )

    def check_reproduced(self):
        """Checks if the bisection is reproduced.

        The bsearch contains two phases "INITED" and "STARTED".
        In "INITED" state, the "old" and "new" revisions are checked to see if
        the regression is reproducible. After verified, it enters "STARTED"
        phase.

        Returns:
          True if the regression is reproduced.
        """
        return self.state == self.STARTED

    def check_proceed_with_skip(self, idx, reason):
        """Checks if we should proceed after skips.

        We just got one more 'skip' result. Should we tolerate it or stop bisecting?

        Args:
          idx: index of the new skip.
          reason: the reason why a candidate reported 'skip' status

        Raises:
          errors.TooManyTemporaryErrors: skip too many times, probably serious issue and
              unable to proceed
        """
        # we just got skip
        assert self.rev_info[idx]['skip'] > 0

        non_skip_count = self.rev_info[idx]['old'] + self.rev_info[idx]['new']
        if (
            idx in (self.old_idx, self.new_idx)
            and non_skip_count == 0
            and self.rev_info[idx]['skip'] >= SKIP_FOREVER
        ) or (
            self.rev_info[idx]['skip']
            >= (non_skip_count + 1) * (SKIP_FOREVER + 2)
        ):
            raise errors.TooManyTemporaryErrors(
                'unable to evaluate rev=%r (skip) %d times: %s'
                % (self.rev_info[idx].rev, self.rev_info[idx]['skip'], reason)
            )

    def set_logger_prefix(self, prefix: str):
        """Sets the prefix for logging."""
        if hasattr(self.logger, 'extra'):
            self.logger.extra['prefix'] = prefix

    def show_summary(self, more=False):
        """Shows top ranked candidates.

        Args:
          more: If true, shows versions with any test result as well.
        """

        def show_candidate(idx):
            if self.state == self.STARTED and self.is_noisy():
                self.logger.info(
                    '[%d] %s %.4f%%; %s',
                    idx,
                    self.rev_info[idx].rev,
                    self.prob[idx] * 100,
                    self.rev_info[idx].summary(),
                )
            else:
                self.logger.info(
                    '[%d] %s; %s',
                    idx,
                    self.rev_info[idx].rev,
                    self.rev_info[idx].summary(),
                )

        if self.state == self.INITED:
            self.logger.info('Verifying initial range:')
            for i in (self.old_idx, self.new_idx):
                show_candidate(i)
            return

        prob_rank = sorted(
            ((p, i) for i, p in enumerate(self.prob) if p > 0), reverse=True
        )
        left, right = self.get_range(1.0 - self.confidence)
        interesting = set([left, right])

        if self.is_noisy():
            # show top candidates
            interesting.update(i for p, i in prob_rank[:10])
        else:
            # show all skip within range
            for i in range(left + 1, right):
                if self.rev_info[i]['skip'] and self.prob[i] > 0:
                    interesting.add(i)

        if more:
            # show all revs with test results
            for i, info in enumerate(self.rev_info):
                if sum(info.result_counter.values()) > 0:
                    interesting.add(i)

        size = right - left
        if size > 1 and not self.is_noisy():
            steps = math.ceil(math.log(size) / math.log(2))
            self.logger.info(
                'Range: (%s, %s], %d revs left (roughly %.0f steps)',
                self.rev_info[left].rev,
                self.rev_info[right].rev,
                size,
                steps,
            )
        else:
            self.logger.info(
                'Range: (%s, %s], %d revs left',
                self.rev_info[left].rev,
                self.rev_info[right].rev,
                size,
            )

        for i in sorted(interesting):
            show_candidate(i)

    def remaining_steps(self):
        # TODO(kcwu): estimate remaining steps for noisy case
        if self.is_noisy():
            return None

        left, right = self.get_range()
        return math.ceil(math.log(right - left) / math.log(2))

    def is_done(self):
        """If we have an answer with high confidence."""
        if self.state == self.INITED:
            return False
        assert self.prob
        # TODO(kcwu): revise to consider the real probability of true-positive is
        #             far away to estimation.
        return max(self.prob) > self.confidence

    def get_best_guess(self):
        """Gets current best guess.

        If called before binary search done, it just returns one of candidates with
        highest probability.

        Returns:
          Index of the element with max probability.
        """
        if self.state == self.INITED:
            return None
        assert self.prob
        return self.prob.index(max(self.prob))

    @staticmethod
    def _calculate_utilities(old_p, new_p, prob, cost_table=None):
        """Calculates information gain and utility (info gain per cost) for each index."""
        assert prob
        if cost_table is None:
            cost_table = [(1, 1)] * len(prob)
        orig_stats = math_util.EntropyStats([float(p) for p in prob])
        orig_entropy = orig_stats.entropy()
        new_stats = orig_stats.multiply(old_p)
        old_stats = orig_stats.multiply(1.0 - old_p)

        results = []
        s = 0.0
        for i, p in enumerate(prob):
            # If rev[i] is observed with old behavior, the result entropy is
            #   entropy( [p_0, ... p_i] * (1-new_p) + (p_i, ... p_n) * (1-old_p) )
            old_stats.replace(p * (1.0 - old_p), p * (1.0 - new_p))

            # If rev[i] is observed with new behavior, the result entropy is
            #   entropy( [p_0, ... p_i] * new_p + (p_i, ... p_n) * old_p )
            new_stats.replace(p * old_p, p * new_p)

            # Let s = sum(p[:i+1]).
            s += p
            # Expected probability to observe new behavior.
            r = s * new_p + (1.0 - s) * old_p

            # The expected entropy after getting the next result at rev[i].
            entropy = r * new_stats.entropy() + (1.0 - r) * old_stats.entropy()
            information_gain = orig_entropy - entropy
            cost = r * cost_table[i][1] + (1.0 - r) * cost_table[i][0]
            utility = information_gain / cost
            results.append((information_gain, utility))

        return results

    @staticmethod
    def _next_idx_by_utility(old_p, new_p, prob, cost_table=None):
        """Determines next index to binary search according to utility."""
        utilities = NoisyBinarySearch._calculate_utilities(
            old_p, new_p, prob, cost_table
        )
        best = float('-inf')
        best_i = None
        for i, (_, utility) in enumerate(utilities):
            if utility > best:
                best = utility
                best_i = i
        return best_i

    def statistics_test(self, round_idx):
        """Helper function for verify_for_init_state

        Returns:
          p value calculated with Boschloo's Exact Test.
          See https://en.wikipedia.org/wiki/Boschloo%27s_test for reference.
        """
        assert (
            self.count_revnew_old + self.count_revnew_new
            >= self.INIT_ROUND[round_idx]
        )
        assert (
            self.count_revold_old + self.count_revold_new
            >= self.INIT_ROUND[round_idx]
        )

        test_table = [
            [self.count_revold_old, self.count_revnew_old],
            [self.count_revold_new, self.count_revnew_new],
        ]
        res = boschloo_exact(test_table, alternative='greater')
        return res.pvalue

    def _verify_for_init_state(self):
        """Returns index to probe for initial samples with statistical method"""
        current_state = self.init_verify_state

        # DO_ONE & DO_TWO
        if self.INIT_STATUS_MAP[current_state]['action'] == 'counting':
            if (
                self.count_revnew_old + self.count_revnew_new
                < self.INIT_ROUND[self.init_rounds]
            ):
                return self.new_idx

            if (
                self.count_revold_old + self.count_revold_new
                < self.INIT_ROUND[self.init_rounds]
            ):
                return self.old_idx
            raise AssertionError(
                'Should not get the initial state: %s' % current_state
            )

        # CHECK_ONE & CHECK_TWO
        if self.INIT_STATUS_MAP[current_state]['action'] == 'checking':
            pvalue = self.statistics_test(self.init_rounds)
            alpha = self.INIT_STATUS_MAP[current_state]['alpha']

            if pvalue < alpha:
                self.init_range_verified = True
                return None

            if (
                self.INIT_STATUS_MAP[current_state]['next_state'] is not None
                and pvalue < self.INIT_P_BOUND[self.init_rounds]
            ):
                self.init_rounds += 1
                if self.init_rounds == len(self.INIT_P_BOUND):
                    self._init_verify_state = self.INIT_STATUS_MAP[
                        current_state
                    ]['next_state']
                else:
                    self._init_verify_state = self.INIT_STATUS_MAP[
                        current_state
                    ]['last_state']
                return self.new_idx

            left_new = self.count_revold_new
            left_trial = self.count_revold_old + self.count_revold_new
            right_new = self.count_revnew_new
            right_trial = self.count_revnew_old + self.count_revnew_new
            old_p, new_p = self._observation_to_prob(
                left_new, left_trial, right_new, right_trial
            )
            raise errors.VerifyInitialRangeFailed(
                self.init_rounds, old_p, new_p
            )

        # get the wrong init_verify_state
        raise AssertionError(
            "current state should be one of init_verify_state's states, but got %s"
            % current_state
        )

    def _next_idx_for_initial(self):
        """Returns index to probe for initial samples

        1. Enough sample points for old and new version. By "enough", it means
           (weak heuristic),
          - At leat two samples.
          - If the two samples are different, we need more.
          - If eval is fast, we should get more samples.

        2. Compare the value distribution between old and new version. Acquire
        more samples until they are roughly distinguishable.
          - Within reasonable time.
        """
        assert self._state == self.INITED
        if not self.recompute_init_values:
            # If the initial range has already been verified with statistical method,
            # the state will change to STARTED after resuming the test.
            if self.init_range_verified:
                return None

            # Test the initial range of functional tests with statistical method,
            # and get the next index.
            if self.endpoint_verification and not self.is_value_bisection():
                idx = self._verify_for_init_state()
                return idx

            # Test the initial range without statistical method, and get the next
            # index.
            if self.rev_info[self.new_idx]['new'] == 0:
                return self.new_idx
            if self.rev_info[self.old_idx]['old'] == 0:
                return self.old_idx
            return None

        def is_one_side_enough(info):
            count = info['value']
            if count > 30:
                return True
            if count < 2:
                return False
            averages = info.averages()
            if count == 2 and averages[0] != averages[1]:
                return False

            # Spend 5 minutes for one side sounds reasonable.
            return info.eval_time > 300

        def check_trend_score(old_avg, new_avg):
            """Checks whether the recomputed values exhibit the expected trend.

            Args:
              old_avg: average of the recomputed values at the "old" revision.
              new_avg: average of the recomputed values at the "new" revision.

            Raises:
              errors.WrongAssumption if the recomputed values are unlikely to
              match the trend given by old_value and new_value.
            """
            score = trend_score(
                self.old_value, self.new_value, old_avg, new_avg
            )
            if score <= 0:
                raise errors.WrongAssumption(
                    'unable to reproduce the same trend (%f->%f): %f->%f'
                    % (
                        self.old_value,
                        self.new_value,
                        old_avg,
                        new_avg,
                    )
                )
            # Reject if we cannot reproduce the trend even half way.
            # TODO(kcwu): make this threshold configurable.
            if score < 0.5:
                expect_delta = abs(self.new_value - self.old_value)
                actual_delta = abs(new_avg - old_avg)
                if self.old_value == 0 or old_avg == 0:
                    raise errors.WrongAssumption(
                        'unable to reproduce the value gap: '
                        'expect delta=%f; actual delta=%f'
                        % (expect_delta, actual_delta)
                    )
                raise errors.WrongAssumption(
                    'unable to reproduce the value gap: '
                    'expect delta=%f (%f%%); actual delta=%f (%f%%)'
                    % (
                        expect_delta,
                        abs(expect_delta / self.old_value) * 100,
                        actual_delta,
                        abs(actual_delta / old_avg) * 100,
                    )
                )

        if not is_one_side_enough(self.rev_info[self.new_idx]):
            self.logger.debug('%s not enough', self.rev_info[self.new_idx].rev)
            return self.new_idx

        if not is_one_side_enough(self.rev_info[self.old_idx]):
            self.logger.debug('%s not enough', self.rev_info[self.old_idx].rev)
            return self.old_idx

        old_avg = math_util.average(self.rev_info[self.old_idx].averages())
        new_avg = math_util.average(self.rev_info[self.new_idx].averages())
        user_diff = self.new_value - self.old_value
        assert user_diff != 0

        # If the samples have the same (at least 1/2) trend as expected, we can
        # stop collect more samples.
        if trend_score(self.old_value, self.new_value, old_avg, new_avg) > 0.5:
            return None
        self.logger.debug('not distinguishable')

        # If the best case values do not help, it probably has no hope.
        if user_diff > 0:
            new_max = max(self.rev_info[self.new_idx].averages())
            old_min = min(self.rev_info[self.old_idx].averages())
            if (
                trend_score(self.old_value, self.new_value, old_min, new_max)
                < 0.5
            ):
                check_trend_score(old_avg, new_avg)
                return None
        else:
            new_min = min(self.rev_info[self.new_idx].averages())
            old_max = max(self.rev_info[self.old_idx].averages())
            if (
                trend_score(self.old_value, self.new_value, old_max, new_min)
                < 0.5
            ):
                check_trend_score(old_avg, new_avg)
                return None

        # If not obviously distinguishable, spend half hour to collect initial
        # samples.
        if self.rev_info[self.old_idx].eval_time < 1800:
            return self.old_idx
        if self.rev_info[self.new_idx].eval_time < 1800:
            return self.new_idx
        check_trend_score(old_avg, new_avg)
        return None

    def get_prob(self):
        if self.state == self.INITED:
            return None
        return self.prob

    def next_idx(self, cost_table=None):
        """Determines next index to binary search.

        Args:
          cost_table: list of cost estimation of candidates, optional. Each
                      estimation is a tuple (estimated_old, estimated_new) where
            estimated_old: estimated cost if this candidate has old behavior
            estimated_new: estimated cost if this candidate has new behavior

        Returns:
          Index to try for binary search. The returned index is optimized for
          minimal total cost.
        """
        if self.state == self.INITED:
            idx = self._next_idx_for_initial()
            assert idx is not None
            return idx

        assert self.state == self.STARTED
        assert self.prob

        return self._next_idx_by_utility(
            self.old_p, self.new_p, self.prob, cost_table
        )

    def renew_initial_count(self, status, idx):
        if idx == self.old_idx:
            if status == 'old':
                self.count_revold_old += 1
            if status == 'new':
                self.count_revold_new += 1

        if idx == self.new_idx:
            if status == 'old':
                self.count_revnew_old += 1
            if status == 'new':
                self.count_revnew_new += 1


class GeminiFusionStrategy(NoisyBinarySearch):
    """GeminiFusionStrategy that fusions NoisyBinarySearch with Gemini."""

    def __init__(
        self,
        *args,
        enable_gemini=False,
        test_name=None,
        source_root=None,
        chromeos_mirror=None,
        board=None,
        session=None,
        **kwargs,
    ):
        """
        Initializes a GeminiFusionStrategy that fusions NoisyBinarySearch with Gemini.

        Args:
          enable_gemini: should use gemini to analyze commit message and diffs.
          test_name: test name of the bisected test.
          source_root: root of the source tree bisected.
          chromeos_mirror: optional ChromeOS mirror path.
          board: optional board name.
        """
        super().__init__(*args, **kwargs)

        # Initialize gemini agent
        self.gemini = None
        if enable_gemini:
            self._gemini_waiting_result = {}
            self.gemini = gemini_util.BisectAgent(
                test_name,
                source_root,
                [x.rev for x in self.rev_info[self.old_idx : self.new_idx + 1]],
                chromeos_mirror=chromeos_mirror,
                board=board,
                old_p=self.old_p,
                new_p=self.new_p,
                session=session,
            )

    def _rev_to_idx(self, rev: str) -> int:
        for idx, r in enumerate(self.rev_info):
            if r.rev == rev:
                return idx
        raise ValueError(f"Revision {rev} not found in rev_info")

    def _update_gemini_stats(self, cost_table=None):
        if not self.gemini:
            return

        prob = self.prob
        if not prob:
            # Prob can be None if the initial range verification is not
            # complete yet.
            # In theory, Gemini inference will not be involved at this
            # moment, and the values will not be used by Gemini even passed
            # to the agent class.
            # Populate a [0, 1/(N-1), ....] as an in-theory value to the
            # agent class.
            prob = [0.0] + [1.0 / (len(self.rev_info) - 1)] * (
                len(self.rev_info) - 1
            )

        if self.prob:
            utilities = self._calculate_utilities(
                self.old_p, self.new_p, prob, cost_table
            )
        else:
            utilities = [(None, None)] * len(prob)

        self.gemini.update_stats(
            {
                self.rev_info[i].rev: {
                    'status': {
                        self.term_map.get(k, k): v
                        for k, v in self.rev_info[i].result_counter.items()
                    },
                    'prob': prob[i],
                    'info_gain': utilities[i][0],
                    'info_gain_per_cost': utilities[i][1],
                }
                for i in range(len(self.rev_info))
            },
            old_p=self.old_p,
            new_p=self.new_p,
        )

    def add_sample(self, idx, status=None, **kwargs):
        sample = {"status": status, **kwargs}
        super().add_sample(idx, status, **kwargs)

        if self.gemini:
            reason = kwargs.get('reason')
            eval_log = kwargs.get('eval_log')
            self.gemini.add_sample(
                self.rev_info[idx].rev,
                self.term_map.get(sample['status'], sample['status']),
                reason=reason,
                eval_log=eval_log,
            )

            if self.rev_info[idx].rev in self._gemini_waiting_result:
                status_str = self.term_map.get(
                    sample['status'], sample['status']
                )
                prob = self.prob
                if not prob:
                    prob = [0.0] + [1.0 / (len(self.rev_info) - 1)] * (
                        len(self.rev_info) - 1
                    )
                likelihood_new = float(sum(prob[: idx + 1]))
                self._gemini_waiting_result[self.rev_info[idx].rev] = {
                    'status': status_str,
                    'reason': reason,
                    'likelihood_new': likelihood_new,
                    'eval_log': eval_log,
                }

    def next_idx(self, cost_table=None):
        """Determines next index to binary search.

        Args:
          cost_table: list of cost estimation of candidates, optional. Each
                      estimation is a tuple (estimated_old, estimated_new) where
            estimated_old: estimated cost if this candidate has old behavior
            estimated_new: estimated cost if this candidate has new behavior

        Returns:
          Index to try for binary search. The returned index is optimized for
          minimal total cost.
        """
        if self.state == self.INITED:
            idx = self._next_idx_for_initial()
            assert idx is not None
            return idx

        assert self.state == self.STARTED
        assert self.prob

        if (
            self.gemini
            and self._gemini_waiting_result.values()
            and list(self._gemini_waiting_result.values())[0] is None
        ):
            # We should always return previous reasoning until run result is
            # provided. next_idx may be called multiple times by different
            # callers in one iteration.
            return self._rev_to_idx(list(self._gemini_waiting_result.keys())[0])

        if self.gemini and not self.is_done():
            self._update_gemini_stats(cost_table=cost_table)
            try:
                gemini_reasoning = self.gemini.next(
                    list(self._gemini_waiting_result.values() or [None])[0]
                )
            except Exception as e:  # pylint: disable=broad-except
                self.logger.warning(
                    'Gemini agent failed in next(), fallback to math model: %s',
                    e,
                    exc_info=True,
                )
                # TODO(b/527740259): support agent recovery
                self.gemini = None
                gemini_reasoning = None

            self._gemini_waiting_result = {}
            if gemini_reasoning:
                self._gemini_waiting_result = {gemini_reasoning: None}
                self.logger.info('Gemini suggested %s', gemini_reasoning)
                return self._rev_to_idx(gemini_reasoning)

        return self._next_idx_by_utility(
            self.old_p, self.new_p, self.prob, cost_table
        )

    def set_logger_prefix(self, prefix: str):
        super().set_logger_prefix(prefix)
        if self.gemini and hasattr(self.gemini.logger, 'extra'):
            self.gemini.logger.extra['prefix'] = prefix
