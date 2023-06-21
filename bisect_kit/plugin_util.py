# Copyright 2023 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Provide decorators that can be used to register and patch functions."""

import typing


_mapping = {}


def register(key: str, *, should_apply: bool = True) -> typing.Callable:
    """Registers the function with the given key.

    Args:
        key: The key to register the function with.
        should_apply: Whether the function should be registered or not.

    Returns:
        A decorator that can be used to register functions.
    """

    def _decorator(func: typing.Callable) -> typing.Callable:
        if should_apply:
            _mapping[key] = func
        return func

    return _decorator


def unregister(key: str):
    """Unregisters the function registered with the given key.

    Args:
        key: The key of the function.
    """
    if key in _mapping:
        del _mapping[key]


def patch(
    key: str | typing.Callable = '', *, should_apply: bool = True
) -> typing.Callable:
    """Use the function registered with the key to patch current function.

    Args:
        key: The key of the registered function. If the key is not provided,
            the default value is 'path.to.module.function_name'.
            e.g. 'bisect_kit.plugin_util.patch'
        should_apply: Whether the function should be patched or not.

    Returns:
        A decorator that can be used to patch functions.
    """

    def _decorator(func: typing.Callable) -> typing.Callable:
        if not should_apply:
            return func
        name = getattr(func, '__name__', None)
        module = getattr(func, '__module__', None)
        if name is None or module is None:
            return func
        new_key = key if key else f'{module}.{name}'
        if new_key not in _mapping:
            return func
        new_func = _mapping[new_key]
        if not new_func.__doc__ and func.__doc__:
            new_func.__doc__ = func.__doc__
        return new_func

    if callable(key):
        # in case user writes @patch instead of @patch()
        func, key = key, ''
        return _decorator(func)
    return _decorator
