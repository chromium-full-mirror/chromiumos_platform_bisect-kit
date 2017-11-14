# Copyright 2017 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""The binary search algorithm."""

from __future__ import print_function
import logging
import math
import re
import sys

from bisect_kit import core
from bisect_kit import math_util

logger = logging.getLogger(__name__)

# If a given version got more than SKIP_FOREVER times of 'skip' evaluation
# result, ignores the said version forever.
SKIP_FOREVER = 2

# Constant that denotes version is known not noisy.
NOT_NOISY = (None, None)


class WrongAssumption(Exception):
  """Wrong assumption.

  For non-noisy binary search, the assumption is all versions with old behavior
  occurs before all versions with new behavior. But the eval result contracted
  this ordering assumption.

  p.s. This only happens (could be detected) if users marked versions 'old' and
  'new' manually.

  Suggestion: try noisy search instead (--noisy).
  """


# Old version of pylint may give false-positive warning.
# pylint: disable=unpacking-non-sequence
class NoisyBinarySearch(object):
  """Binary search algorithm supporting noisy signal.

  Example usage:
    bsearch = NoisyBinarySearch(rev_info, observation='old=1/10,new=9/10')
    bsearch.rebuild()
    while not bsearch.is_done():
      bsearch.update(idx, get_result(rev_info[idx]))
    print(bsearch.get_best_guess())
  """

  def __init__(self,
               rev_info,
               old_idx=0,
               new_idx=None,
               oracle=None,
               observation=None,
               confidence=0.999,
               verify_confidence=0.999):
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

    Args:
      rev_info: List of RevInfo.
      old_idx: Index of rev, which has old behavior.
      new_idx: Index of rev, which has new behavior.
      oracle: The oracle model of probability distribution.
      observation: Observed test results.
      confidence: Required confidence for bisection result.
      verify_confidence: Bisect range verification confidence.
    """
    assert oracle is None or observation is None
    if new_idx is None:
      new_idx = len(rev_info) - 1
    self.rev_info = rev_info
    self.old_idx = old_idx
    self.new_idx = new_idx
    self.oracle = oracle
    if observation is None:
      self.prior_observation = None
    else:
      self.prior_observation = self._parse_observation(observation)
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

    self.old_p, self.new_p = self._estimate_model()

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
    old_f = NOT_NOISY
    new_t = NOT_NOISY
    if not observation:
      return old_f, new_t

    for term in observation.split(','):
      m = re.match(r'^(old|new)=(\d+)/(\d+)$', term)
      assert m
      new_behavior, trial = map(int, m.group(2, 3))
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
    (left_new, left_trial), (right_new, right_trial) = self.prior_observation

    if not self.is_range_verified():
      return left_new, left_trial, right_new, right_trial

    old_idx, new_idx = self.get_range()
    if (left_new, left_trial) != NOT_NOISY:
      for info in self.rev_info[:old_idx + 1]:
        left_trial += info['old'] + info['new']
        left_new += info['new']
    if (right_new, right_trial) != NOT_NOISY:
      for info in self.rev_info[new_idx:]:
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

  def _estimate_model_from_observation(self):
    """Estimates model from observation.

    We don't know the real probability distribution. Assume they are Bernoulli
    process and calculates the expected probability from observations.

    Returns:
      old_p: Estimated false-positive probability for old candidates.
      new_p: Estimated true-positive probability for new candidates.
    """
    old_p, new_p = 0, 1

    left_new, left_trial, right_new, right_trial = self._gather_noise_count()
    if (left_new, left_trial) != NOT_NOISY:
      old_p = self._expected_probability_from_observation(left_trial, left_new)
    if (right_new, right_trial) != NOT_NOISY:
      new_p = self._expected_probability_from_observation(
          right_trial, right_new)

    return old_p, new_p

  def _estimate_model(self):
    """Estimates probability of old false-positive and new true-positive.

    In other words, the rates of new behavior is observed at old revisions
    and new revisions respectively.

    Returns:
      old_p: Estimated false-positive probability for old candidates.
      new_p: Estimated true-positive probability for new candidates.
    """
    if self.oracle:
      old_p, new_p = self.oracle
    elif self.prior_observation:
      old_p, new_p = self._estimate_model_from_observation()
      logger.debug('estimated model: old_p=%s, new_p=%s', old_p, new_p)
    else:
      old_p, new_p = 0, 1

    if 0 < old_p < 1.0 - self.confidence:
      logger.warning('Confidence value (%r) is too low to detect '
                     'old candidates with new behavior (p=%r)', self.confidence,
                     old_p)
    if self.confidence < new_p < 1.0:
      logger.warning('Confidence value (%r) is too low to detect '
                     'new candidates with old behavior (p=%r)', self.confidence,
                     1.0 - new_p)
    return old_p, new_p

  def get_noise_observation(self):
    """Gets current noise observation.

    This includes the counts of prior observations from __init__.

    Returns:
      A string indicates eval result noise observation so far. See comments of
      __init__ for detail format.
    """
    left_new, left_trial, right_new, right_trial = self._gather_noise_count()
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
      if info['skip'] > SKIP_FOREVER:
        weighting[i] = 0
      else:
        # Allow retry, but the more 'skip', the lower weighting.
        weighting[i] *= 0.1**info['skip']

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
        impact = math.e**(-math.pi * x**2) * impact_factor
        weighting[j] = min(weighting[j], 1.0 - impact)

      # right to left
      for j in reversed(range(max(0, i - impact_range + 1), i)):
        if rev_info[j]['new'] or rev_info[j]['old']:
          break
        x = float(i - j) / impact_range
        impact = math.e**(-math.pi * x**2) * impact_factor
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
    min_prob = math_util.least([old_p, 1 - old_p, new_p, 1 - new_p])**(
        total_old + total_new) * math_util.least(prob)
    if min_prob < sys.float_info.min:
      prob = map(math_util.ExtendedFloat, prob)
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
          p *= (1 - old_p)**left_old
        if right_new != 0:
          p *= new_p**right_new
        if right_old != 0:
          p *= (1 - new_p)**right_old
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
        raise WrongAssumption(
            'Test results conflict with assumption, try --noisy')
    else:
      assert sum_prob > 0

    # prob[i] /= P(results)
    prob = [x / sum_prob for x in prob]

    # Convert back to float.
    prob = map(float, prob)
    return prob

  def rebuild(self):
    """Rebuilds probability.

    Note, if the probability model is unknown, old_p and new_p will be
    re-estimated after every rebuild() and update(), thus rebuild multiple times
    may result in different probability values.
    """

    assert 0 <= self.old_idx < self.new_idx < len(self.rev_info)
    # First, P(x_i is first new) = Normalize(init_prob[i])
    #                            = Normalize(init_weight[i] * skip_weight[i])
    self.skip_weight = self.get_weight_by_skip(self.rev_info)
    self.init_prob = [p * w for p, w in zip(self.init_weight, self.skip_weight)]
    self.prob = self._calculate_probs(self.old_p, self.new_p, self.rev_info,
                                      self.init_prob)

    self.old_p, self.new_p = self._estimate_model()

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

  def update(self, idx, result):
    """Incremental update by one more test result.

    Note, if the probability model is unknown, old_p and new_p will be
    re-estimated after every rebuild() and update(), thus the calculated
    probabilities by update() and rebuild() may be different.
    TODO(kcwu): rebuild for a while?

    Args:
      idx: index of the new test.
      result: result of the new test. 'old', 'new', or 'skip'.
    """
    assert self.prob
    assert self.rev_info
    self.check_verification_range()

    if self._is_changing_skip_weight(idx, result):
      # Incremental update by skip_weight.
      new_skip_weight = self.get_weight_by_skip(self.rev_info)
      for i, (cur_skip_w,
              new_skip_w) in enumerate(zip(self.skip_weight, new_skip_weight)):
        if new_skip_w != cur_skip_w:
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
    self.old_p, self.new_p = self._estimate_model()

  def get_range(self, confidence=None):
    """Gets narrowed-down bisection range so far.

    Using a heuristic algorithm to find a range (left, right) that,
    sum(prob[left:right]) > confidence. Some optimizations are applied so the
    range may not be the narrowest.

    If `confidence` value is bigger, the returned range is bigger.

    Args:
      confidence: Confidence value. Default is the confidence provided in ctor.

    Returns:
      (left, right):
        left: Index likely before the point of probability change.
        right: Index likely after the point of probability change.
    """
    if not self.prob:
      return self.old_idx, self.new_idx

    if confidence is None:
      confidence = self.confidence

    # Non-zero probabilities sorted by prob (big to small), then index (small
    # to big)
    sorted_prob = sorted(
        ((i, p) for (i, p) in enumerate(self.prob) if p > 0),
        key=lambda (i, p): (-p, i))
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
    assert cut is not None

    left = max(min(candidates) - 1, self.old_idx)
    # Sometimes left has old probability due to 'skip', not 'old'. Seeks for
    # version with 'old'.
    while left > self.old_idx and self.rev_info[left]['old'] == 0:
      left -= 1

    right = max(candidates)
    assert right <= self.new_idx
    return left, right

  def is_range_verified(self):
    """Is bisection range already been verified?"""
    return (self.rev_info[self.old_idx]['old'] > 0 and
            self.rev_info[self.new_idx]['new'] > 0)

  def check_verification_range(self):
    """Checks if bisection range is reasonable.

    If the eval results conflict with the bisection range, abort the
    bisection if the probability is too low.

    Raises:
      core.VerificationFailed: Bisection range is not verified and false events
          occur too many times.
    """
    if self.rev_info[self.old_idx]['old'] == 0:
      # P(try N times and old behavior occurs 0 times)
      p = self.old_p**self.rev_info[self.old_idx]['new']
      if p < 1.0 - self.verify_confidence:
        raise core.VerificationFailed(self.rev_info[self.old_idx].rev, 'old',
                                      'new', self.rev_info[self.old_idx]['new'])

    if self.rev_info[self.new_idx]['new'] == 0:
      # P(try N times and new behavior occurs 0 times)
      p = (1 - self.new_p)**self.rev_info[self.new_idx]['old']
      if p < 1.0 - self.verify_confidence:
        raise core.VerificationFailed(self.rev_info[self.new_idx].rev, 'new',
                                      'old', self.rev_info[self.new_idx]['old'])

  def show_summary(self, more=False):
    """Shows top ranked candidates.

    Args:
      more: If true, shows versions with any test result as well.
    """
    prob_rank = sorted(
        ((p, i) for i, p in enumerate(self.prob) if p > 0), reverse=True)
    interesting = set(i for p, i in prob_rank[:10])

    left, right = self.get_range(1.0 - self.confidence)
    for i in [left, right]:
      interesting.add(i)

    if more:
      for i, info in enumerate(self.rev_info):
        if sum(info.result_counter.values()) > 0:
          interesting.add(i)

    for i in sorted(interesting):
      note = ''
      if (self.prob[i] == prob_rank[0][0] and
          (len(prob_rank) == 1 or prob_rank[0][0] != prob_rank[:5][-1][0])):
        # highlight best candidate
        note = '<------------------'
      values = sorted(self.rev_info[i].values)
      if not values:
        value_display = ''
      elif len(values) == 1:
        value_display = '%.3f' % values[0]
      else:
        value_display = 'n=%d,avg=%.3f,median=%.3f,min=%.3f,max=%.3f' % (
            len(values), sum(values) / len(values), values[len(values) // 2],
            values[0], values[-1])
      logger.info('[%d] %s %.4f%%; %s %s %s', i, self.rev_info[i].rev,
                  self.prob[i] * 100, self.rev_info[i].result_counter,
                  value_display, note)

  def is_done(self):
    """If we have an answer with high confidence."""
    if not self.is_range_verified():
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
    assert self.prob
    return self.prob.index(max(self.prob))

  @staticmethod
  def _next_idx_entropy(old_p, new_p, prob):
    """Determines next index to binary search according to information gain."""
    orig_stats = math_util.EntropyStats(map(float, prob))
    new_stats = orig_stats.multiply(old_p)
    old_stats = orig_stats.multiply(1.0 - old_p)

    # Enumerate |i| as next to query, find best |i| to maximize
    # information gain, i.e. minimize entropy.

    best = float('inf')
    best_i = None
    # |s| = cumulative probability of [p_0,..., p_i]
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
      if entropy < best:
        best = entropy
        best_i = i

    return best_i

  def next_idx(self):
    """Determines next index to binary search.

    Returns:
      Index to try for binary search.
    """
    assert self.prob
    # Try 'new' before 'old' in order to know whether the issue is reproducible
    # earlier
    if self.rev_info[self.new_idx]['new'] == 0:
      return self.new_idx

    if self.rev_info[self.old_idx]['old'] == 0:
      return self.old_idx

    return self._next_idx_entropy(self.old_p, self.new_p, self.prob)
