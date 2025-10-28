# Copyright 2026 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Test vm_leaser module."""

from unittest import mock
import uuid

from bisect_kit import vm_leaser


@mock.patch('bisect_kit.vm_leaser.time.sleep')
@mock.patch('bisect_kit.vm_leaser.get_image')
def test_insert_image_long_version(mock_get_image, _mock_sleep):
    with mock.patch('bisect_kit.vm_leaser.compute_v1') as mock_compute_v1:
        with mock.patch('bisect_kit.vm_leaser.get_default_client_option'):
            with mock.patch('uuid.uuid4') as mock_uuid:
                mock_uuid.return_value = uuid.UUID(
                    '12345678-1234-5678-1234-567812345678'
                )
                mock_client = mock.Mock()
                mock_compute_v1.ImagesClient.return_value = mock_client

                # Mock get_image to return READY status
                mock_image_obj = mock.Mock()
                mock_image_obj.status = 'READY'
                mock_get_image.return_value = mock_image_obj

                board = 'hatch'
                # A very long version string that needs truncation and sanitization
                vm_cros_version = 'very/long.version/string/that/exceeds/the/sixty/three/character/limit/for/gcp/image/names'
                gs_path = 'gs://bucket/path/to/image'

                image_name = vm_leaser.insert_image(
                    gs_path, board, vm_cros_version
                )

                assert len(image_name) <= 63
                assert image_name.startswith('hatch-')
                assert '12345678' in image_name
                assert '.' not in image_name
                assert '/' not in image_name

                mock_client.insert.assert_called_once()


@mock.patch('bisect_kit.vm_leaser.time.sleep')
@mock.patch('bisect_kit.vm_leaser.get_image')
def test_insert_image_sanitization(mock_get_image, _mock_sleep):
    with mock.patch('bisect_kit.vm_leaser.compute_v1') as mock_compute_v1:
        with mock.patch('bisect_kit.vm_leaser.get_default_client_option'):
            mock_client = mock.Mock()
            mock_compute_v1.ImagesClient.return_value = mock_client

            mock_image_obj = mock.Mock()
            mock_image_obj.status = 'READY'
            mock_get_image.return_value = mock_image_obj

            board = 'hatch'
            vm_cros_version = 'R123-15678.0.0~bisection'
            gs_path = 'gs://bucket/path/to/image'

            image_name = vm_leaser.insert_image(gs_path, board, vm_cros_version)

            assert '.' not in image_name
            assert '~' not in image_name
            assert 'r123-15678-0-0-bisection' in image_name
