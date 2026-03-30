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

    # pylint: disable=protected-access
    def test_str_to_list(self):
        self.assertEqual(dut_allocate_spec_module._str_to_list(""), [])
        self.assertEqual(dut_allocate_spec_module._str_to_list([]), [])

        self.assertEqual(
            dut_allocate_spec_module._str_to_list("a,b,c"), ["a", "b", "c"]
        )
        self.assertEqual(
            dut_allocate_spec_module._str_to_list(["a", "b,c"]), ["a", "b", "c"]
        )

        self.assertEqual(
            dut_allocate_spec_module._str_to_list(
                "screebo_MTL-U 15W Ultra 5 T2 1.5/4.2 GT 1.8G 1VDBox,IPU_8GB"
            ),
            ["screebo_MTL-U 15W Ultra 5 T2 1.5/4.2 GT 1.8G 1VDBox", "IPU_8GB"],
        )
        self.assertEqual(
            dut_allocate_spec_module._str_to_list(
                "kanix_MTL-U 15W Ultra 5 T2 1.5/4.2 GT 1.8G 1VDBox, IPU_16GB"
            ),
            ["kanix_MTL-U 15W Ultra 5 T2 1.5/4.2 GT 1.8G 1VDBox", " IPU_16GB"],
        )
        self.assertEqual(
            dut_allocate_spec_module._str_to_list(
                "foo,kanix_MTL-U 15W Ultra 5 T2 1.5/4.2 GT 1.8G 1VDBox,IPU_16GB,bar"
            ),
            [
                "foo",
                "kanix_MTL-U 15W Ultra 5 T2 1.5/4.2 GT 1.8G 1VDBox",
                "IPU_16GB",
                "bar",
            ],
        )
        self.assertEqual(
            dut_allocate_spec_module._str_to_list(
                "karis_MTL-U 15W Ultra 5 T2 1.5/4.2 GT 1.8G 1VDBox,IPU_8GB,"
                + "kanix_MTL-U 15W Ultra 5 T2 1.5/4.2 GT 1.8G 1VDBox,IPU_16GB,"
                + "screebo_MTL-U 15W Ultra 5 T2 1.5/4.2 GT 1.8G 1VDBox,IPU_8GB"
            ),
            [
                "karis_MTL-U 15W Ultra 5 T2 1.5/4.2 GT 1.8G 1VDBox",
                "IPU_8GB",
                "kanix_MTL-U 15W Ultra 5 T2 1.5/4.2 GT 1.8G 1VDBox",
                "IPU_16GB",
                "screebo_MTL-U 15W Ultra 5 T2 1.5/4.2 GT 1.8G 1VDBox",
                "IPU_8GB",
            ],
        )

    # pylint: disable=protected-access
    def test_tweak_sku_list(self):
        self.assertEqual(
            dut_allocate_spec_module._tweak_sku_list(
                [
                    "screebo_MTL-U 15W Ultra 5 T2 1.5/4.2 GT 1.8G 1VDBox",
                    "IPU_8GB",
                ]
            ),
            ["screebo_MTL-U 15W Ultra 5 T2 1.5/4.2 GT 1.8G 1VDBox, IPU_8GB"],
        )
        self.assertEqual(
            dut_allocate_spec_module._tweak_sku_list(
                [
                    "kanix_MTL-U 15W Ultra 5 T2 1.5/4.2 GT 1.8G 1VDBox ",
                    " IPU_16GB",
                ]
            ),
            ["kanix_MTL-U 15W Ultra 5 T2 1.5/4.2 GT 1.8G 1VDBox, IPU_16GB"],
        )
        self.assertEqual(
            dut_allocate_spec_module._tweak_sku_list(
                [
                    "foo",
                    "kanix_MTL-U 15W Ultra 5 T2 1.5/4.2 GT 1.8G 1VDBox",
                    "IPU_16GB",
                    "bar",
                ]
            ),
            [
                "foo",
                "kanix_MTL-U 15W Ultra 5 T2 1.5/4.2 GT 1.8G 1VDBox, IPU_16GB",
                "bar",
            ],
        )
        self.assertEqual(
            dut_allocate_spec_module._tweak_sku_list(
                [
                    "karis_MTL-U 15W Ultra 5 T2 1.5/4.2 GT 1.8G 1VDBox",
                    "IPU_8GB",
                    "kanix_MTL-U 15W Ultra 5 T2 1.5/4.2 GT 1.8G 1VDBox",
                    "IPU_16GB",
                    "screebo_MTL-U 15W Ultra 5 T2 1.5/4.2 GT 1.8G 1VDBox",
                    "IPU_8GB",
                ]
            ),
            [
                "karis_MTL-U 15W Ultra 5 T2 1.5/4.2 GT 1.8G 1VDBox, IPU_8GB",
                "kanix_MTL-U 15W Ultra 5 T2 1.5/4.2 GT 1.8G 1VDBox, IPU_16GB",
                "screebo_MTL-U 15W Ultra 5 T2 1.5/4.2 GT 1.8G 1VDBox, IPU_8GB",
            ],
        )


if __name__ == '__main__':
    unittest.main()
