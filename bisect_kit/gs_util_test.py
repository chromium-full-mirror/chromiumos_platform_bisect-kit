# Copyright 2023 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Test the gs_util module."""

import subprocess
import unittest
from unittest import mock

from bisect_kit import gs_util


def _fake_gsutil_for_stat(*args, **_kwargs):
    del _kwargs
    stat_14469_8 = [
        'gs://chromeos-manifest-versions/buildspecs/99/14469.8.0.xml:',
        '    Creation time:          Fri, 08 Apr 2022 17:31:36 GMT',
        '    Update time:            Fri, 08 Apr 2022 17:32:14 GMT',
        '    Storage class:          STANDARD',
        '    Content-Length:         244194',
        '    Content-Type:           text/xml; charset=utf-8',
        '    Metadata:',
        '        create_time_seconds:1643599521',
        '    Hash (crc32c):          crc32_meow',
        '    Hash (md5):             md5_woof==',
        '    ETag:                   tagtag=',
        '    Generation:             1649439096753030',
        '    Metageneration:         2',
    ]
    # timestamp = 1649438944
    stat_14469_7 = [
        'gs://chromeos-manifest-versions/buildspecs/99/14469.7.0.xml:',
        '    Creation time:          Fri, 08 Apr 2022 17:29:04 GMT',
        '    Update time:            Fri, 08 Apr 2022 17:29:41 GMT',
        '    Storage class:          STANDARD',
        '    Content-Length:         244194',
        '    Content-Type:           text/xml; charset=utf-8',
        '    Hash (crc32c):          crc32_meow',
        '    Hash (md5):             md5_woof==',
        '    ETag:                   tagtag=',
        '    Generation:             1649438944196998',
        '    Metageneration:         2',
    ]
    files = {
        'gs://chromeos-manifest-versions/buildspecs/99/14469.8.0.xml': stat_14469_8,
        'gs://chromeos-manifest-versions/buildspecs/99/14469.7.0.xml': stat_14469_7,
    }

    result = ''
    for arg in args:
        if arg in files:
            result += '\n'.join(files[arg]) + '\n'
    return result


class TestGsUtil(unittest.TestCase):
    """Test the gs_util module."""

    def test_gsutil(self):
        with mock.patch('bisect_kit.util.check_output', return_value='abc'):
            self.assertEqual(gs_util.cat('foo'), 'abc')

        with mock.patch(
            'bisect_kit.util.check_output',
            side_effect=subprocess.CalledProcessError(1, 'cmd'),
        ):
            with self.assertRaises(subprocess.CalledProcessError):
                gs_util.cat('bar')

    @mock.patch('bisect_kit.gs_util._gsutil', _fake_gsutil_for_stat)
    def test_stat(self):
        self.assertDictEqual(gs_util.stat('nothing'), {})
        self.assertDictEqual(
            gs_util.stat(
                'gs://chromeos-manifest-versions/buildspecs/99/14469.8.0.xml'
            ),
            {
                'gs://chromeos-manifest-versions/buildspecs/99/14469.8.0.xml': {
                    'Creation time': 1649439096,
                    'Update time': 1649439134,
                    'Storage class': 'STANDARD',
                    'Content-Length': 244194,
                    'Content-Type': 'text/xml; charset=utf-8',
                    'create_time_seconds': 1643599521,
                    'Hash (crc32c)': 'crc32_meow',
                    'Hash (md5)': 'md5_woof==',
                    'ETag': 'tagtag=',
                    'Generation': 1649439096753030,
                    'Metageneration': '2',
                },
            },
        )

    @mock.patch('bisect_kit.gs_util._gsutil', _fake_gsutil_for_stat)
    def test_stat_max_creation_time(self):
        time7 = 1649438944
        time8 = 1643599521
        file7 = 'gs://chromeos-manifest-versions/buildspecs/99/14469.7.0.xml'
        file8 = 'gs://chromeos-manifest-versions/buildspecs/99/14469.8.0.xml'
        self.assertEqual(gs_util.stat_max_creation_time(file7), time7)
        self.assertEqual(gs_util.stat_max_creation_time(file8), time8)
        self.assertEqual(
            gs_util.stat_max_creation_time(file7, file8),
            max(time7, time8),
        )
