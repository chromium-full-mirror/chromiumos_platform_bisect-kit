# Copyright 2020 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Cache mechanism to speed up heavy operations."""

import functools


class Cache:
    """A simple cache decorator with enable/disable control.

    Sample usage:
      @Cache
      def foo(x):
        return ...

      @Cache.default_disabled
      def bar(x):
        return ...

      foo.disable_cache()
      bar.enable_cache()
    """

    def __init__(self, func, enable=True):
        self.wrapped = func
        self.enabled = enable
        self.data = {}
        functools.update_wrapper(self, func)

    @staticmethod
    def default_disabled(func):
        return Cache(func, enable=False)

    def __call__(self, *args):
        if not self.enabled:
            return self.wrapped(*args)

        result = self.data.get(args)
        if result is None:
            result = self.wrapped(*args)
            self.data[args] = result
        return result

    def enable_cache(self):
        self.enabled = True

    def disable_cache(self):
        self.enabled = False
        self.data = {}
