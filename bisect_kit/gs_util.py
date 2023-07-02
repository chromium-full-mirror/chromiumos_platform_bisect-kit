# Copyright 2023 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Wrapper for gsutil command."""

import calendar
import datetime
import errno
import re
import subprocess
import typing

from bisect_kit import errors
from bisect_kit import util


# Assume gsutil is in PATH.
_BIN = 'gsutil'


def _gsutil(*args, **kwargs):
    """gsutil command line wrapper.

    Args:
      *args: command line arguments passed to gsutil
      **kwargs:
        ignore_errors: if true, return '' for failures, for example 'gsutil ls'
          but the path not found.

    Returns:
      stdout of gsutil

    Raises:
      errors.ExternalError: gsutil failed to run
      subprocess.CalledProcessError: command failed
    """
    stderr_lines = []
    try:
        return util.check_output(
            _BIN, *args, stderr_callback=stderr_lines.append
        )
    except subprocess.CalledProcessError as e:
        stderr = ''.join(stderr_lines)
        if re.search(r'ServiceException:.* does not have .*access', stderr):
            raise errors.ExternalError(
                'gsutil failed due to permission. '
                f'Run "{_BIN} config" and follow its instruction. '
                'Fill any string if it asks for project-id'
            ) from e
        if kwargs.get('ignore_errors'):
            return ''
        raise
    except OSError as e:
        if e.errno == errno.ENOENT:
            raise errors.ExternalError(
                f'Unable to run {_BIN}. gsutil is not installed or not in PATH?'
            )
        raise


def cp(*args, **kwargs) -> str:
    """gsutil cp.

    Args:
      *args: arguments passed to 'gsutil cp'
      **kwargs: extra parameters, where
        ignore_errors: if true, return empty string instead of raising exception,
          ex. path not found.

    Returns:
      stdout of 'gsutil cp' result.

    Raises:
      subprocess.CalledProcessError: gsutil failed, usually means path not found
    """
    return _gsutil('cp', *args, **kwargs)


def cat(*args, **kwargs) -> str:
    """gsutil cat.

    Args:
      *args: arguments passed to 'gsutil cat'
      **kwargs: extra parameters, where
        ignore_errors: if true, return empty string instead of raising exception,
          ex. path not found.

    Returns:
      stdout of 'gsutil cat' result.

    Raises:
      subprocess.CalledProcessError: gsutil failed, usually means path not found
    """
    return _gsutil('cat', *args, **kwargs)


def ls(*args, **kwargs) -> list[str]:
    """gsutil ls.

    Args:
      *args: arguments passed to 'gsutil ls'
      **kwargs: extra parameters, where
        ignore_errors: if true, return empty list instead of raising exception,
          ex. path not found.

    Returns:
      list of 'gsutil ls' result. One element for one line of gsutil output.

    Raises:
      subprocess.CalledProcessError: gsutil failed, usually means path not found
    """
    return _gsutil('ls', *args, **kwargs).splitlines()


def stat_max_creation_time(*args, **kwargs) -> int:
    """Returns the max creation time of a set of files.

    For each file, the function will lookup the `create_time_seconds` key is
    set in metadata.

    Args:
      *args: a list of filenames passed to gsutil
      **kwargs: extra parameters for gsutil

    Returns:
      A integer indicates the creation timestamp.

    Raises:
      subprocess.CalledProcessError: gsutil failed, usually means path not found
      errors.ExternalError: creation time is not found
    """
    timestamps = [
        x['create_time_seconds']
        if 'create_time_seconds' in x
        else x.get('Creation time')
        for x in stat(*args, **kwargs).values()
    ]
    if None in timestamps:
        raise errors.ExternalError(
            "didn't find creation time or create_time_seconds"
        )
    return max(timestamps)


def stat(*args, **kwargs) -> dict[str, dict[str, typing.Any]]:
    """Returns a key-value dict for each file in gsutil stat.

    Note that it only supports a small set of field names.

    Args:
      *args: a list of filename passed to gsutil
      **kwargs: extra parameters for gsutil

    Returns:
      A dict, result[filename][key] = value

    Raises:
      subprocess.CalledProcessError: gsutil failed, usually means path not found
    """

    time_format = '%a, %d %b %Y %H:%M:%S GMT'
    supported_fields = [
        'Creation time',
        'Update time',
        'Storage class',
        'Content-Length',
        'Content-Type',
        'create_time_seconds',  # metadata
        'Hash (crc32c)',
        'Hash (md5)',
        'ETag',
        'Generation',
        'Metageneration',
    ]
    datetime_fields = [
        'Creation time',
        'Update time',
    ]
    int_fields = [
        'Content-Length',
        'create_time_seconds',
        'Generation',
    ]

    result = {}
    current_file = None

    for line in _gsutil('stat', *args, **kwargs).splitlines():
        if ':' not in line:
            continue
        key, value = line.split(':', 1)
        key, value = key.strip(), value.strip()

        # fine a new file
        # gs://file_name: will be splitted to key=gs value=//file_name: here
        if key == 'gs':
            file_name = 'gs:%s' % value.rstrip(':')
            if file_name in args:
                current_file = file_name
                result[current_file] = {}
            continue

        # current file not found
        if not current_file:
            continue

        # find a key we want
        if key in supported_fields:
            if key in int_fields:
                value = int(value)
            if key in datetime_fields:
                dt = datetime.datetime.strptime(value, time_format)
                value = int(calendar.timegm(dt.utctimetuple()))
            result[current_file][key] = value
    return result
