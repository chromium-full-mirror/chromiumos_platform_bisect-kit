# Copyright 2018 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""File locks"""

import contextlib
import errno
import fcntl
import logging
import time


logger = logging.getLogger(__name__)

# Lock file to prevent multiple bisect instances sync code at the same time.
# Each bisection's workdir is supposed only used by one bisector and no need to
# lock. However the code mirror is shared and need lock to protect.
# Relative to mirror root.
LOCK_FILE_FOR_MIRROR_SYNC = '.bisect-kit.sync.lock'

LOCK_FILE_FOR_BUILD = '/var/lock/bisect-kit.build'


@contextlib.contextmanager
def lock_file(filename, polling_time=1.0):
    """Process level file lock.

    This is advisory file lock between processes. If the process holding a lock
    is terminated, the lock will be unlocked automatically.

    This is implemented by python's fcntl.lockf (backed by fcntl(2) syscall). Due
    to the mess and weakness of unix's file locking, do not recursively lock the
    same filename (no matter the same process, child process, or other thread).

    Args:
      filename: lock filename
      polling_time: delay between two locking trial
    """
    with open(filename, 'w') as f:
        try_count = 0
        # Loop forever until lock successfully.
        while True:
            try:
                try_count += 1
                fcntl.lockf(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except IOError as exc:
                if exc.errno != errno.EAGAIN:
                    raise
                if try_count % 60 == 1:
                    logger.warning('waiting for lock file %s ...', filename)
                time.sleep(polling_time)

        try:
            yield
        finally:
            fcntl.lockf(f, fcntl.LOCK_UN)
