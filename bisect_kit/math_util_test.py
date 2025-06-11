# Copyright 2017 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Test math module."""

import math
import sys
import unittest

from bisect_kit import math_util
from bisect_kit.math_util import EntropyStats
from bisect_kit.math_util import ExtendedFloat


class TestMathUtil(unittest.TestCase):
    """Test math functions"""

    def test_log(self):
        # Normal case.
        self.assertAlmostEqual(math_util.log(math.e**10), math.log(math.e**10))

        # Special case for zero and tiny negative.
        self.assertAlmostEqual(math_util.log(0), 0)
        self.assertAlmostEqual(math_util.log(-1e-10), 0)

        # Still raise exception for unexpected non-tiny negative.
        with self.assertRaises(ValueError):
            math_util.log(-1)

    def test_least(self):
        self.assertEqual(math_util.least([0.1, 0.2, 0.3]), 0.1)
        self.assertEqual(math_util.least([0.0, 0.2, 0.3]), 0.2)
        self.assertEqual(math_util.least([0.0, 0.0, 0.3]), 0.3)
        with self.assertRaises(ValueError):
            math_util.least([0.0, 0.0, 0.0])
        with self.assertRaises(ValueError):
            math_util.least([-1])
        with self.assertRaises(ValueError):
            math_util.least([])

    def test_average(self):
        self.assertAlmostEqual(math_util.average([5]), 5)
        self.assertAlmostEqual(math_util.average([1, 2]), 1.5)

        with self.assertRaises(ValueError):
            math_util.average([])


class TestEntropyStats(unittest.TestCase):
    """Test EntropyStats"""

    def normalize_and_calculate_entropy(self, p):
        s = sum(p)
        normalized_p = [x / s for x in p]
        return sum(-x * math.log(x) if x else 0 for x in normalized_p)

    def test_entropy(self):
        p = [0.2, 0.1, 0.3, 0.5, 0.0]
        es = EntropyStats(p)
        self.assertAlmostEqual(
            es.entropy(), self.normalize_and_calculate_entropy(p)
        )

        p = [0.0, 0.0]
        es = EntropyStats(p)
        self.assertEqual(es.entropy(), 0)

    def test_replace(self):
        p = [0.2, 0.1, 0.3, 0.5]
        es = EntropyStats(p)

        es.replace(p[0], 0.4)
        p[0] = 0.4
        self.assertAlmostEqual(
            es.entropy(), self.normalize_and_calculate_entropy(p)
        )

        es.replace(p[2], 0.1)
        p[2] = 0.1
        self.assertAlmostEqual(
            es.entropy(), self.normalize_and_calculate_entropy(p)
        )

    def test_multiply(self):
        p = [0.2, 0.1, 0.3, 0.5]
        es = EntropyStats(p)
        es2 = es.multiply(0.9)
        p = [x * 0.9 for x in p]
        self.assertAlmostEqual(
            es2.entropy(), self.normalize_and_calculate_entropy(p)
        )

        # Make sure multiply() is effective.
        es2.replace(p[0], 0.2)
        p[0] = 0.2
        self.assertAlmostEqual(
            es2.entropy(), self.normalize_and_calculate_entropy(p)
        )


class TestExtendedFloat(unittest.TestCase):
    """Test ExtendedFloat class."""

    def setUp(self):
        self.a = 1234.0
        self.b = 2345.0
        self.c = -3456.0
        # Small and large number within float's range.
        self.small = 10 ** (sys.float_info.min_10_exp + 10)
        self.large = 10 ** (sys.float_info.max_10_exp - 10)

        self.a_ex = ExtendedFloat(self.a)
        self.b_ex = ExtendedFloat(self.b)
        self.c_ex = ExtendedFloat(self.c)
        self.one = ExtendedFloat(1)
        self.zero = ExtendedFloat(0)

        # In float's range.
        self.small_ex = ExtendedFloat(self.small)
        self.large_ex = ExtendedFloat(self.large)
        # Out of float's range.
        self.tiny_ex = self.small_ex**100
        self.huge_ex = self.large_ex**100

    def test_basic_value(self):
        self.assertEqual(float(ExtendedFloat(0)), 0)
        self.assertEqual(float(ExtendedFloat(0, 10)), 0)
        self.assertEqual(float(ExtendedFloat(1)), 1)
        self.assertEqual(float(ExtendedFloat(-1)), -1)
        self.assertEqual(float(ExtendedFloat(1234)), 1234)
        self.assertEqual(float(ExtendedFloat(1234, 100)), 1234 * 2**100)
        self.assertEqual(float(ExtendedFloat(1234, -100)), 1234 * 2**-100)

        self.assertNotEqual(self.small_ex, self.zero)
        self.assertNotEqual(self.tiny_ex, self.zero)

    def test_eq(self):
        # Use assertTrue() instead of rich assert functions in order to invoke the
        # intended operator.
        self.assertTrue(ExtendedFloat(0) == ExtendedFloat(0))
        self.assertTrue(ExtendedFloat(1) == ExtendedFloat(1))
        self.assertTrue(ExtendedFloat(16) == ExtendedFloat(1, 4))
        self.assertTrue(ExtendedFloat(-20) == ExtendedFloat(-20))
        self.assertTrue(ExtendedFloat(1) == 1)
        self.assertTrue(1 == ExtendedFloat(1))

    def test_ne(self):
        # Use assertTrue() instead of rich assert functions in order to invoke the
        # intended operator.
        self.assertTrue(ExtendedFloat(1) != ExtendedFloat(-1))
        self.assertTrue(ExtendedFloat(1) != ExtendedFloat(0))

    def test_lt(self):
        # Use assertTrue() instead of rich assert functions in order to invoke the
        # intended operator.
        self.assertTrue(ExtendedFloat(0) < ExtendedFloat(2))
        self.assertTrue(ExtendedFloat(0) < ExtendedFloat(10))
        self.assertTrue(ExtendedFloat(-10) < ExtendedFloat(10))
        self.assertTrue(ExtendedFloat(-10) < ExtendedFloat(0))
        self.assertTrue(ExtendedFloat(-8) < ExtendedFloat(-2))
        self.assertTrue(ExtendedFloat(0.125) < ExtendedFloat(0.25))
        self.assertTrue(ExtendedFloat(0) < ExtendedFloat(0.25))
        self.assertTrue(ExtendedFloat(0.5) < ExtendedFloat(0.6))

        self.assertTrue(ExtendedFloat(0.5) < ExtendedFloat(1))
        self.assertTrue(ExtendedFloat(-1) < ExtendedFloat(-0.5))
        self.assertTrue(ExtendedFloat(-0.25) < ExtendedFloat(-0.125))
        self.assertTrue(ExtendedFloat(-0.6) < ExtendedFloat(-0.5))

    def test_neg(self):
        self.assertEqual(-self.zero, 0)
        self.assertEqual(-(-1 * self.zero), 0)
        self.assertEqual(-self.a_ex, -self.a)
        self.assertEqual(-(-1 * self.a_ex), self.a)

    def test_add(self):
        self.assertEqual(self.a_ex + self.zero, self.a)
        self.assertEqual(self.zero + self.a_ex, self.a)

        self.assertEqual(self.a_ex + self.b_ex, self.a + self.b)
        self.assertEqual(self.a_ex + self.b, self.a + self.b)
        self.assertEqual(self.a + self.b_ex, self.a + self.b)

        ## Negative value.
        self.assertEqual(self.a_ex + self.c_ex, self.a + self.c)
        self.assertEqual(self.c_ex + self.c_ex, self.c + self.c)

        # The precision of mantissa is not enough, so this behavior is expected.
        self.assertEqual(self.large_ex + self.small_ex, self.large)
        self.assertEqual(self.huge_ex + self.tiny_ex, self.huge_ex)
        self.assertEqual(self.tiny_ex + self.huge_ex, self.huge_ex)

    def test_sub(self):
        self.assertEqual(self.a_ex - self.zero, self.a)
        self.assertEqual(self.zero - self.a_ex, -self.a_ex)

        self.assertEqual(self.a - self.a_ex, 0)
        self.assertEqual(self.a_ex - self.a, 0)
        self.assertEqual(self.a_ex - self.a_ex, 0)

        self.assertEqual(self.a_ex - self.b_ex, self.a - self.b)
        self.assertEqual(self.a - self.b_ex, self.a - self.b)
        self.assertEqual(self.a_ex - self.b, self.a - self.b)

        # Negative value.
        self.assertEqual(self.a_ex - self.c_ex, self.a - self.c)
        self.assertEqual(self.c_ex - self.c_ex, self.c - self.c)

        # The precision of mantissa is not enough, so this behavior is expected.
        self.assertEqual(self.large_ex - self.small_ex, self.large)
        self.assertEqual(self.huge_ex - self.tiny_ex, self.huge_ex)
        self.assertEqual(self.tiny_ex - self.huge_ex, -self.huge_ex)

    def test_mul(self):
        self.assertEqual(self.zero * self.zero, 0)
        self.assertEqual(self.zero * self.a_ex, 0)

        self.assertEqual(self.a_ex * self.b_ex, self.a * self.b)
        self.assertEqual(self.a * self.b_ex, self.a * self.b)
        self.assertEqual(self.a_ex * self.b, self.a * self.b)

        self.assertEqual(
            self.huge_ex * self.huge_ex, (-self.huge_ex) * (-self.huge_ex)
        )
        self.assertEqual(
            self.huge_ex * self.tiny_ex, self.tiny_ex * self.huge_ex
        )

    def test_div(self):
        self.assertEqual(self.zero / self.a_ex, 0)
        self.assertEqual(self.a_ex / self.a_ex, 1)

        self.assertEqual(self.a_ex / self.b_ex, self.a / self.b)
        self.assertEqual(self.a / self.b_ex, self.a / self.b)
        self.assertEqual(self.a_ex / self.b, self.a / self.b)

    def test_pow(self):
        self.assertEqual(self.zero**0, 1)
        self.assertEqual(self.zero**1, 0)
        self.assertEqual(self.zero**100000, 0)

        self.assertEqual(self.one**0, 1)
        self.assertEqual(self.one**1, 1)
        self.assertEqual(self.one**100000, 1)

        self.assertEqual(self.a_ex**0, self.a**0)
        self.assertEqual(self.a_ex**1, self.a**1)
        self.assertEqual(self.a_ex**10, self.a**10)

        x = self.a_ex
        for _ in range(30):
            x *= x
        self.assertAlmostEqual(self.a_ex ** (2**30) / x, 1)

        y = self.one
        for _ in range(12345):
            y *= self.a_ex
        self.assertAlmostEqual(self.a_ex**12345 / y, 1)


if __name__ == '__main__':
    unittest.main()
