# Copyright 2018 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Test locking module"""

from __future__ import annotations

import multiprocessing
import os
import tempfile
import time
import unittest

from bisect_kit import locking


def lock_test_helper(
    filename: str,
    # pylint: disable=unsubscriptable-object
    # Suppressing a false warning: https://github.com/pylint-dev/pylint/issues/3488
    queue: multiprocessing.Queue[float],
):
    # Use smaller polling_time for shorter test duration.
    with locking.lock_file(filename, polling_time=0.01):
        queue.put(time.time())
        time.sleep(0.5)


class TestLocking(unittest.TestCase):
    """Test locking functions."""

    def setUp(self):
        self.lock_filename = tempfile.mktemp()

    def tearDown(self):
        if os.path.exists(self.lock_filename):
            os.unlink(self.lock_filename)

    def test_lock_file(self):
        # Because the lock cannot reenter in the same process and cannot be shared
        # between threads, run the test on two separated processes via
        # multiprocessing.
        #
        # pylint: disable=unsubscriptable-object
        # Suppressing a false warning: https://github.com/pylint-dev/pylint/issues/3488
        queue: multiprocessing.Queue[float] = multiprocessing.Queue()
        processes = []
        for _ in range(2):
            p = multiprocessing.Process(
                target=lock_test_helper, args=(self.lock_filename, queue)
            )
            p.start()
            processes.append(p)
            time.sleep(0.1)
        for p in processes:
            p.join()
        t1 = float(queue.get())
        t2 = float(queue.get())
        self.assertAlmostEqual(t2 - t1, 0.5, places=1)


if __name__ == '__main__':
    unittest.main()
