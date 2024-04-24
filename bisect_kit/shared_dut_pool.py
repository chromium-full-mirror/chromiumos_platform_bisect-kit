#!/usr/bin/env python3
# Copyright 2024 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Manages the shared DUT pool (go/bisector-shared-dut-pool)"""

from __future__ import annotations

import dataclasses
import datetime
from enum import StrEnum
import logging
import typing

from bisect_kit import gcloud
from google.cloud import datastore


logger = logging.getLogger(__name__)


class DutState(StrEnum):
    """Dut state."""

    BUSY = 'busy'
    IDLE = 'idle'


@dataclasses.dataclass
class Dut:
    """A class represents a DUT in Datastore."""

    dut_name: str
    dimensions: dict | None
    state: DutState | None
    owner: str | None
    user: str | None
    last_update_time: datetime.datetime | None


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

    def query(
        self,
        dimensions: list[str],
        variants: list[str],
        pools: list[str] | None = None,
        state: DutState | None = None,
    ) -> list[Dut]:
        """query DUTs in the shared pool by the given conditions.

        Args:
          dimensions: all DUT dimensions which needs to be satisfied.
          variants: DUT variants (e.g., board/model/sku) where any one of them would do.
          pools: a list of dimension pool where any of one of them would do. Optional.
          state the state to query. Optional.
        Returns:
          A list of DUT matches AND(AND(dimensions), OR(variants), OR(pools)).
        """

        def split_dimensions(
            dimensions: list[str],
        ) -> list(typing.Tuple[str, str]):
            # dimensions are in the format of <key>:<value> pairs
            # E.g., label-model:quackingstick, label-pools:DUT_POOL_QUOTA.
            return [d.split(':', 1) for d in dimensions]

        and_filters = [
            datastore.query.And(
                [
                    datastore.query.PropertyFilter('dimensions.' + k, '=', v)
                    for k, v in split_dimensions(dimensions)
                ]
            ),
            datastore.query.Or(
                [
                    datastore.query.PropertyFilter('dimensions.' + k, '=', v)
                    for k, v in split_dimensions(variants)
                ]
            ),
        ]
        if pools:
            and_filters.append(
                datastore.query.Or(
                    [
                        datastore.query.PropertyFilter(
                            'dimensions.' + k, '=', v
                        )
                        for k, v in split_dimensions(pools)
                    ]
                )
            )
        if state:
            and_filters.append(
                datastore.query.PropertyFilter('state', '=', state),
            )

        q = self._client.query(kind=self._KIND)
        q.add_filter(filter=datastore.query.And(and_filters))

        logger.debug('filters: %s', and_filters)

        duts = []
        for entity in q.fetch():
            try:
                dut_name = entity['dimensions']['dut_name'][0]
            except (KeyError, IndexError):
                logger.warning('malformed DUT entity: %s', entity)
                continue

            duts.append(
                Dut(
                    dut_name,
                    entity.get('dimensions', None),
                    entity.get('state', None),
                    entity.get('owner', None),
                    entity.get('user', None),
                    entity.get('last_update_time', None),
                )
            )
        return duts
