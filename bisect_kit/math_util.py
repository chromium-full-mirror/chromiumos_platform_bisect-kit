# Copyright 2017 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Math utilities."""

import copy
import functools
import math
import sys


def log(x):
    """Wrapper of log(x) handling zero and negative value."""
    # Due to arithmetic error, x may become tiny negative.
    # -1e-10 is not enough, see strategy_test.test_next_idx_arithmetic_error for
    # detail.
    if 0 >= x > -1e-9:
        return 0.0
    return math.log(x)


def _xlogx(x):
    return x * log(x)


def least(values):
    """Finds minimum non-zero value.

    Args:
      values: Non-negative values.

    Returns:
      The minimum non-zero value.

    Raises:
      ValueError: if all values are zero or negative.
    """
    return min(x for x in values if x > 0)


def average(values):
    """Calculates average of values.

    Args:
      values: numbers, at least one element

    Raises:
      ValueError: if empty
    """
    if not values:
        raise ValueError('calculate average of empty list')
    return float(sum(values)) / len(values)


class Averager:
    """Helper class to calculate average."""

    def __init__(self):
        self.count = 0
        self.total = 0

    def add(self, value):
        self.count += 1
        self.total += value

    def average(self):
        if self.count == 0:
            raise ValueError('calculate average of empty list')
        return self.total / self.count


class EntropyStats:
    r"""A data structure to maintain cross entropy of a set of values.

    This is a math helper for NoisyBinarySearch. Given a set of probability,
    this class can incrementally calculate their cross entropy.

    Algorithm:
      Given unnormalized p_i, their cross entropy could be expressed as
        Let S = \sum_i p_i
        Normalized(p): p_i' = p_i/S
        CrossEntropy(p) = \sum_i { -(p_i/S) * log (p_i/S) }
                        = \sum_i { -(p_i/S) (log p_i - log S) }
                        = \sum_i { -p_i/S * log p_i } +  \sum_i { p_i/S * log S }
                        = -1/S * \sum_i { p_i*log p_i } + log S
                        = log S - 1/S * \sum_i { p_i*log p_i }

      So we can maintain sum=|S| and sum_log=|\sum_i { p_i*log p_i }| for
      incremental update.
    """

    def __init__(self, p):
        """Initializes EntropyStats.

        Args:
          p: A list of probability value. Could be unnormalized.
        """
        self.sum = sum(p)
        self.sum_log = sum(_xlogx(x) for x in p)

    def entropy(self):
        """Returns cross entropy."""
        if self.sum == 0:
            return 0
        return log(self.sum) - self.sum_log / self.sum

    def replace(self, old, new):
        """Replaces one random variable in the collection with another.

        Args:
          old: original value to be replaced.
          new: new value
        """
        self.sum += new - old
        self.sum_log += _xlogx(new) - _xlogx(old)

    def multiply(self, value):
        """Multiplies all values in the collection by |value|.

        Returns:
          A new instance of EntropyStats with calculated value.
        """
        other = copy.copy(self)
        # \sum_i { (p_i*value) * log (p_i*value) }
        # = \sum_i { (p_i*value) * (log p_i + log value) }
        # = value * \sum_i { p_i log p_i } + \sum_i { p_i * value * log value }
        # = value * \sum_i { p_i log p_i } + \sum_i { p_i } * value * log value
        other.sum_log = value * self.sum_log + self.sum * _xlogx(value)

        other.sum *= value
        return other


@functools.total_ordering
class ExtendedFloat:
    """Custom floating point number with larger range of exponent.

    Problem:
      In the formula of noisy binary search algorithm, we need to calculate p**n
      (where n is test count) and normalize probabilities. When n is hundreds or
      more, p**n goes too small and becomes zero (underflow), the normalization
      step will fail.

      Since n > 100 is not unreasonable large for noisy bisection, we need a
      numeric type less likely or never underflow.

    Solution:
      Supports a floating number is represented as
          x = mantissa * 2.**exponent
      We use python's float to represent `mantissa` and python's int to represent
      `exponent`.

      For mantissa, we can accept calculated probability is not super accurate
      because the worst case is just running test one or two more times. So we
      keep mantissa in python's built-in float.

      For exponent, we represent it using python's int, which is virtually
      unlimited digits.

    Alternative solutions:
      1. Maybe we can rewrite the formula to avoid underflow. But I want to keep
         the arithmetic code simple.

      2. Use python stdlib's decimal or fractions. But they are very slow --
         decimal is 6-50x slower, fractions is 9-760x slower while ExtendedFloat
         is about 1.2-3.3x slower [1].

         [1] test setup: easy N=1000, oracle=(0.01, 0.2)
                         hard N=1000, oracle=(0.45, 0.55)

    Attributes:
      mantissa: The mantissa part of floating number. Its range is 0.5 <=
          abs(mantissa) < 1.0.
      exponent: The exponent part of floating number.
    """

    def __init__(self, value, exp=0):
        self.mantissa, self.exponent = math.frexp(value)
        self.exponent = self.exponent + exp if value else 0

    def __float__(self):
        return math.ldexp(self.mantissa, self.exponent)

    def __neg__(self):
        return ExtendedFloat(-self.mantissa, self.exponent)

    def __abs__(self):
        return ExtendedFloat(abs(self.mantissa), self.exponent)

    def __add__(self, other):
        if not isinstance(other, ExtendedFloat):
            other = ExtendedFloat(other)
        if self.mantissa == 0:
            return other
        if self.exponent < other.exponent:
            return other.__add__(self)
        value = math.ldexp(other.mantissa, other.exponent - self.exponent)
        return ExtendedFloat(value + self.mantissa, self.exponent)

    __radd__ = __add__

    def __pow__(self, p):
        # Because 0.5 <= abs(mantissa) < 1.0, using float is enough without
        # underflow for small p.
        if p < -sys.float_info.min_exp:
            return ExtendedFloat(self.mantissa**p, self.exponent * p)

        half_pow = self.__pow__(p >> 1)
        result = half_pow.__mul__(half_pow)
        if p & 1:
            result = result.__mul__(self)
        return result

    def __sub__(self, other):
        return self.__add__(-other)

    def __rsub__(self, other):
        return ExtendedFloat(other).__add__(-self)

    def __mul__(self, other):
        if not isinstance(other, ExtendedFloat):
            other = ExtendedFloat(other)
        return ExtendedFloat(
            self.mantissa * other.mantissa, self.exponent + other.exponent
        )

    __rmul__ = __mul__

    def __truediv__(self, other):
        if not isinstance(other, ExtendedFloat):
            other = ExtendedFloat(other)
        return ExtendedFloat(
            self.mantissa / other.mantissa, self.exponent - other.exponent
        )

    def __rtruediv__(self, other):
        return ExtendedFloat(other).__truediv__(self)

    def __repr__(self):
        return 'ExtendedFloat(%f, %d)' % (self.mantissa, self.exponent)

    def __eq__(self, other):
        if not isinstance(other, ExtendedFloat):
            other = ExtendedFloat(other)
        return (
            self.mantissa == other.mantissa and self.exponent == other.exponent
        )

    def __lt__(self, other):
        if not isinstance(other, ExtendedFloat):
            other = ExtendedFloat(other)
        if self.mantissa * other.mantissa <= 0:
            return self.mantissa < other.mantissa

        if self.exponent == other.exponent:
            return self.mantissa < other.mantissa

        if self.mantissa > 0:
            return self.exponent < other.exponent
        return self.exponent > other.exponent

    def __round__(self, digits):
        return round(float(self), digits)
