# Copyright 2024 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Test dut_allocate_spec module."""

import os
import tempfile
import unittest
from unittest import mock

from bisect_kit import dut_allocate_spec as dut_allocate_spec_module


class TestDutAllocateSpec(unittest.TestCase):
    """Test DutAllocateSpecStates class."""

    def setUp(self):
        self._session_file = tempfile.mktemp()
        self.mock_session_file = self.enterContext(
            mock.patch.object(
                dut_allocate_spec_module,
                '_session_file_name',
                autospec=True,
            )
        )
        self.mock_session_file.return_value = self._session_file
        self.maxDiff = None

    def tearDown(self):
        if os.path.exists(self._session_file):
            os.unlink(self._session_file)

    def test_save_load(self):
        session_name = 'test_session'
        spec = dut_allocate_spec_module.DutAllocateSpec(
            pools=['pool1', 'pool2'],
            boards=['board1'],
            dimensions=['dim1', 'dim2', 'dim3'],
            session=session_name,
        )
        dut_allocate_spec_module.save(spec, sync_with_db=False)

        loaded_spec = dut_allocate_spec_module.load(session_name)
        self.assertEqual(spec, loaded_spec)
