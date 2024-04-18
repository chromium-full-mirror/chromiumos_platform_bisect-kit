#!/usr/bin/env python3
# Copyright 2024 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Manages the shared DUT pool (go/bisector-shared-dut-pool)"""

from __future__ import annotations

import datetime
from enum import StrEnum
import logging

from bisect_kit import gcloud


logger = logging.getLogger(__name__)


class DutState(StrEnum):
    """Dut state."""

    BUSY = 'busy'
    IDLE = 'idle'


class SharedDutPoolManager(gcloud.DataStoreClient):
    """Class manages the shared DUT pool."""

    _KIND = 'DUT'

    def insert(
        self,
        name: str,
        dimensions: dict,
        state: DutState,
        owner: str,
        user: str,
    ):
        """Insert a DUT.

        If the DUT already exists, the original record is overwritten.

        Args:
          name: The name of the DUT.
          dimensions: A dict for all the DUT dimensions.
          state: a DutState enum.
          owner: the owner bisection ID.
          user: the user bisection ID.
        """
        key = self._client.key(self._KIND, name)
        existing_dut = self._client.get(key)
        if existing_dut is not None:
            logger.warning(
                'The newly released DUT %s already exist in the shared pool: %s. '
                'It probably means the record is not cleaned up correctly',
                name,
                existing_dut,
            )

        dut = self._client.entity(key)
        dut.update(
            {
                'dimensions': dimensions,
                'state': state,
                'owner': owner,
                'user': user,
                'last_update_time': datetime.datetime.now(
                    tz=datetime.timezone.utc
                ),
            }
        )
        self._client.put(dut)
