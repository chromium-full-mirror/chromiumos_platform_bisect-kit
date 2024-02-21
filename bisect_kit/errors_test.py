# Copyright 2024 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Test errors module."""

import unittest

from bisect_kit import errors


class TestErrorType(unittest.TestCase):
    """Test errors.error_type()."""

    def test_error_type(self):
        self.assertEqual('Unknown', errors.error_type(errors.BisectException()))
        self.assertEqual(
            'Fatal', errors.error_type(errors.ArgumentError('name', 'message'))
        )
        self.assertEqual('Fatal', errors.error_type(errors.Uninitialized()))
        self.assertEqual(
            'Retriable', errors.error_type(errors.BisectRetriableError())
        )
        self.assertEqual(
            'Retriable', errors.error_type(errors.BrokenDutException())
        )
        self.assertEqual(
            'Retriable', errors.error_type(errors.TooManyTemporaryErrors())
        )
        self.assertEqual(
            'Retriable', errors.error_type(errors.BisectionTemporaryError())
        )
        self.assertEqual(
            'Retriable', errors.error_type(errors.SshConnectionError())
        )
        self.assertEqual(
            'Contradiction', errors.error_type(errors.WrongAssumption())
        )
        self.assertEqual(
            'Contradiction', errors.error_type(errors.DiagnoseContradiction())
        )
        self.assertEqual(
            'Contradiction', errors.error_type(errors.VerificationFailed())
        )
        self.assertEqual(
            'Contradiction',
            errors.error_type(errors.VerifyInitialRangeFailed(1, 0.1, 0.9)),
        )
        self.assertEqual('Fatal', errors.error_type(errors.InternalError()))
        self.assertEqual('Unknown', errors.error_type(Exception()))
        self.assertEqual('Unknown', errors.error_type(ValueError()))


if __name__ == "__main__":
    unittest.main()
