# Copyright 2026 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Tests for setup_cros_bisect."""

import os
import pathlib
import tempfile
import unittest
from unittest import mock

import setup_cros_bisect


class TestSetupCrosBisectLocalMounts(unittest.TestCase):
    """Tests for _add_local_mount cleanup and idempotency."""

    @mock.patch('bisect_kit.supssh_relay.is_enabled', return_value=True)
    def test_add_local_mount_cleans_legacy_and_keeps_custom(
        self, _mock_is_enabled
    ) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            temp_root = pathlib.Path(tempdir)
            fake_home = temp_root / 'home'
            config_dir = fake_home / '.config'
            config_dir.mkdir(parents=True)

            chromeos_tree = temp_root / 'chromeos'
            scripts_dir = chromeos_tree / 'src' / 'scripts'
            scripts_dir.mkdir(parents=True)
            mounts_file = scripts_dir / '.local_mounts'
            mounts_file.write_text(
                '/home/cros-bisect-service/supssh-helper-temporary/'
                'supssh-helper-client.py /usr/bin/supssh\n'
                '/home/cros-bisect-service/.ssh/testing_rsa '
                '/home/cros-bisect-service/.ssh/testing_rsa\n'
                '/opt/custom /mnt/custom\n',
                encoding='utf-8',
            )

            with mock.patch.dict(os.environ, {'HOME': str(fake_home)}):
                # Call twice to verify idempotency (no duplicate ~/.config).
                # pylint: disable=protected-access
                setup_cros_bisect._add_local_mount(str(chromeos_tree))
                setup_cros_bisect._add_local_mount(str(chromeos_tree))

            self.assertEqual(
                mounts_file.read_text(encoding='utf-8').splitlines(),
                ['/opt/custom /mnt/custom', str(config_dir)],
            )

    @mock.patch('bisect_kit.supssh_relay.is_enabled', return_value=False)
    def test_add_local_mount_preserves_legacy_when_disabled(
        self, _mock_is_enabled
    ) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            temp_root = pathlib.Path(tempdir)
            fake_home = temp_root / 'home'
            config_dir = fake_home / '.config'
            config_dir.mkdir(parents=True)

            chromeos_tree = temp_root / 'chromeos'
            scripts_dir = chromeos_tree / 'src' / 'scripts'
            scripts_dir.mkdir(parents=True)
            mounts_file = scripts_dir / '.local_mounts'
            legacy_line = (
                '/home/cros-bisect-service/supssh-helper-temporary/'
                'supssh-helper-client.py /usr/bin/supssh'
            )
            ssh_key_line = (
                '/home/cros-bisect-service/.ssh/testing_rsa '
                '/home/cros-bisect-service/.ssh/testing_rsa'
            )
            mounts_file.write_text(
                f'{legacy_line}\n{ssh_key_line}\n', encoding='utf-8'
            )

            with mock.patch.dict(os.environ, {'HOME': str(fake_home)}):
                # pylint: disable=protected-access
                setup_cros_bisect._add_local_mount(str(chromeos_tree))

            self.assertEqual(
                mounts_file.read_text(encoding='utf-8').splitlines(),
                [legacy_line, ssh_key_line, str(config_dir)],
            )

    @mock.patch('bisect_kit.supssh_relay.is_enabled', return_value=True)
    def test_add_local_mount_cleans_legacy_without_config_dir(
        self, _mock_is_enabled
    ) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            temp_root = pathlib.Path(tempdir)
            fake_home = temp_root / 'home'
            fake_home.mkdir(parents=True)

            chromeos_tree = temp_root / 'chromeos'
            scripts_dir = chromeos_tree / 'src' / 'scripts'
            scripts_dir.mkdir(parents=True)
            mounts_file = scripts_dir / '.local_mounts'
            mounts_file.write_text(
                '/home/cros-bisect-service/supssh-helper-temporary/'
                'supssh-helper-client.py /usr/bin/supssh\n'
                '/opt/custom /mnt/custom\n',
                encoding='utf-8',
            )

            with mock.patch.dict(os.environ, {'HOME': str(fake_home)}):
                # pylint: disable=protected-access
                setup_cros_bisect._add_local_mount(str(chromeos_tree))

            self.assertEqual(
                mounts_file.read_text(encoding='utf-8').splitlines(),
                ['/opt/custom /mnt/custom'],
            )


if __name__ == '__main__':
    unittest.main()
