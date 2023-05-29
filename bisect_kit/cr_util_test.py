# Copyright 2017 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Test cr_util module"""

import pathlib
import unittest

from bisect_kit import cr_util


class TestCrUtil(unittest.TestCase):
    """Test cr_util functions."""

    def test_is_chrome_version(self):
        assert cr_util.is_chrome_version('59.0.3065.0')

        assert not cr_util.is_chrome_version('59.0.3065')
        assert not cr_util.is_chrome_version('59.0.3065.0.0')

    def test_is_version_lesseq(self):
        assert cr_util.is_version_lesseq('75.0.3731.0', '75.0.3731.0')
        assert cr_util.is_version_lesseq('75.0.3731.0', '75.0.3732.0')
        assert not cr_util.is_version_lesseq('75.0.3732.0', '75.0.3731.0')

        assert cr_util.is_version_lesseq('74.0.3726.0', '75.0.3731.0')

    def test_is_direct_relative_version(self):
        assert cr_util.is_direct_relative_version('74.0.3726.0', '75.0.3731.0')
        assert cr_util.is_direct_relative_version('75.0.3731.0', '74.0.3726.0')
        assert cr_util.is_direct_relative_version('73.0.3683.0', '75.0.3731.0')
        assert not cr_util.is_direct_relative_version(
            '73.0.3683.1', '75.0.3731.0'
        )
        assert not cr_util.is_direct_relative_version(
            '75.0.3731.0', '73.0.3683.1'
        )

    def test_extract_branch_from_version(self):
        self.assertEqual(
            cr_util.extract_branch_from_version('59.0.3064.0'), '3064'
        )

    def test_extract_milestone_from_version(self):
        self.assertEqual(
            cr_util.extract_milestone_from_version('59.0.3064.0'), '59'
        )

    def test_get_ancestors(self):
        # Disabling protected member access cros lint error
        # pylint: disable=protected-access
        chrome_version_list = [
            '100.0.1.0',
            '100.0.1.1',
            '100.0.2.0',
            '100.0.2.1',
            '100.0.3.0',
            '100.0.3.1',
            '100.0.3.2',
        ]

        self.assertEqual(
            cr_util._get_ancestors('100.0.3.1', chrome_version_list),
            ['100.0.1.0', '100.0.2.0', '100.0.3.0', '100.0.3.1'],
        )
        self.assertEqual(
            cr_util._get_ancestors('100.0.2.1', chrome_version_list),
            ['100.0.1.0', '100.0.2.0', '100.0.2.1'],
        )

    def test_get_lca(self):
        chrome_version_list = [
            '100.0.1.0',
            '100.0.1.1',
            '100.0.2.0',
            '100.0.2.1',
            '100.0.3.0',
            '100.0.3.1',
            '100.0.3.2',
        ]
        self.assertEqual(
            cr_util.get_lca('100.0.3.1', '100.0.1.1', chrome_version_list),
            '100.0.1.0',
        )
        self.assertEqual(
            cr_util.get_lca('100.0.1.1', '100.0.3.1', chrome_version_list),
            '100.0.1.0',
        )
        self.assertEqual(
            cr_util.get_lca('100.0.1.1', '100.0.1.1', chrome_version_list),
            '100.0.1.1',
        )
        self.assertEqual(
            cr_util.get_lca('100.0.2.0', '100.0.3.1', chrome_version_list),
            '100.0.2.0',
        )

    def test_get_RBE_environment_variables(self):
        # pylint: disable=protected-access
        env = cr_util.get_RBE_environment_variables()

        self.assertTrue('RBE_credential_file' in env)
        self.assertEqual(
            env['RBE_credential_file'],
            pathlib.Path.home() / 'bisect-runner.json',
        )

        self.assertTrue('RBE_use_application_default_credentials' in env)
        self.assertEqual(env['RBE_use_application_default_credentials'], 'true')

        self.assertTrue('RBE_use_gce_credentials' in env)
        self.assertEqual(env['RBE_use_gce_credentials'], 'false')

        self.assertTrue('RBE_automatic_auth' in env)
        self.assertEqual(env['RBE_automatic_auth'], 'false')


if __name__ == '__main__':
    unittest.main()
