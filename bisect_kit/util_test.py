# Copyright 2017 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Test util module."""

from __future__ import print_function

import subprocess
import unittest

from bisect_kit import util


class TestUtilFunctions(unittest.TestCase):
    """Test functions in util module."""

    def test_is_version_lesseq(self):
        assert util.is_version_lesseq('1.1', '1.1')
        assert util.is_version_lesseq('1.1.1', '1.1.1')

        assert util.is_version_lesseq('1.1.1', '1.1.2')
        assert not util.is_version_lesseq('1.1.2', '1.1.0')

        assert util.is_version_lesseq('1.1.1', '2.0.0')
        assert not util.is_version_lesseq('2.0.0', '1.1.1')

    def test_is_direct_relative_version(self):
        assert util.is_direct_relative_version('9123.0.0', '9123.0.0')
        assert util.is_direct_relative_version('9123.0.0', '9100.0.0')
        assert util.is_direct_relative_version('9123.3.0', '9100.0.0')
        assert util.is_direct_relative_version('9123.0.5', '9100.0.0')

        assert not util.is_direct_relative_version('9123.0.0', '9100.1.0')
        assert not util.is_direct_relative_version('9123.0.0', '9100.1.2')

        assert not util.is_direct_relative_version('1.0.1', '2.0.9')


class TestPopen(unittest.TestCase):
    """Test util.Popen."""

    def test_check_output(self):
        output = util.check_output('echo', 'foobar')
        self.assertEqual(output, 'foobar\n')
        output = util.check_output('sh', '-c', 'echo 1; sleep 0.05; echo 2')
        self.assertEqual(output, '1\n2\n')
        output = util.check_output('echo 1; echo 2', shell=True)
        self.assertEqual(output, '1\n2\n')

        with self.assertRaises(subprocess.CalledProcessError):
            util.check_output('false')

        output = util.check_output('env', env=dict(foo='bar'))
        self.assertEqual(output, 'foo=bar\n')

    def test_check_output_in_bytes(self):
        output = util.check_output_in_bytes('echo', 'foobar')
        self.assertEqual(output, b'foobar\n')
        output = util.check_output_in_bytes(
            'sh', '-c', 'echo 1; sleep 0.05; echo 2'
        )
        self.assertEqual(output, b'1\n2\n')
        output = util.check_output_in_bytes('echo 1; echo 2', shell=True)
        self.assertEqual(output, b'1\n2\n')

        with self.assertRaises(subprocess.CalledProcessError):
            util.check_output_in_bytes('false')

        output = util.check_output_in_bytes('env', env=dict(foo='bar'))
        self.assertEqual(output, b'foo=bar\n')

    def test_check_call(self):
        util.check_call('true')
        with self.assertRaises(subprocess.CalledProcessError):
            util.check_call('false')

    def test_call(self):
        self.assertEqual(util.call('true'), 0)
        self.assertEqual(util.call('false'), 1)

    def test_dict_get(self):
        test_dict = {'a': {'b': 123}}
        self.assertEqual(util.dict_get(test_dict, 'a', 'b'), 123)
        self.assertEqual(util.dict_get(test_dict, 'a', 'c', 'd'), None)
        self.assertEqual(util.dict_get(None, 'a', 'c', 'd'), None)

    def test_is_valid_ip(self):
        self.assertEqual(util.is_valid_ip('127.0.0.1'), True)
        self.assertEqual(
            util.is_valid_ip('2001:0db8:75a2:0000:0000:8a2e:0340:9999'), True
        )

        self.assertEqual(util.is_valid_ip('abcd'), False)
        self.assertEqual(util.is_valid_ip('127.0.1'), False)
        self.assertEqual(util.is_valid_ip('127.0.0.999'), False)
        self.assertEqual(
            util.is_valid_ip('2001:0db8:75a2:0000:0000:8a2e:0340'), False
        )


if __name__ == '__main__':
    unittest.main()
