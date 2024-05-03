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

from bisect_kit import bisect_db
from bisect_kit import dut_allocate_spec_type
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


def dut_matches_spec(
    dut: datastore.Entity, spec: dut_allocate_spec_type.DutAllocateSpec
) -> bool:
    """Checks whether a DUT matches a DutAllocateSpec.

    Args:
      dut: the DUT Entity loaded the Datastore.
      spec: the DutAllocateSpec to check.
    Returns:
      matched or not.
    """

    def field_matched(field_name, values_to_match: any) -> bool:
        value = dut.get('dimensions', {}).get(field_name)
        if not value:
            return False

        if isinstance(value, list):
            values = set(value)
        else:
            values = set([value])

        if not isinstance(values_to_match, list):
            values_to_match = [values_to_match]

        return bool(values.intersection(values_to_match))

    if spec.dut_name:
        return field_matched('dut_name', spec.dut_name)

    if spec.pools and not field_matched('label-pool', spec.pools):
        return False
    if spec.boards and not field_matched('label-board', spec.boards):
        return False
    if spec.models and not field_matched('label-model', spec.models):
        return False
    if spec.skus and not field_matched('label-hwid_sku', spec.skus):
        return False
    if spec.dimensions:
        for dimension in spec.dimensions:
            key, value = dimension.split(':', 1)
            if not field_matched(key, value):
                return False
    return True


class SharedDutPoolManager(gcloud.DataStoreClient):
    """Class manages the shared DUT pool."""

    _KIND_DUT = 'DUT'

    def _common_parent_key(self):
        """Default common parent key

        Datastore transactions requires all participated entities to be in the same entity group.
        So make all DUTs children of the default entity.
        """
        return self._client.key('DutGroup', 'default')

    def _default_key(self, name):
        """Constructs a key under the default common entity group."""
        return self._client.key(
            self._KIND_DUT, name, parent=self._common_parent_key()
        )

    def _default_query(self):
        """Performs a query under the default common entity group."""
        return self._client.query(
            kind=self._KIND_DUT, ancestor=self._common_parent_key()
        )

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
        key = self._default_key(name)
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
                'name': name,
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
        q = self._default_query()
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

    def update(
        self,
        name: str | None = None,
        dimensions: dict | None = None,
        state: DutState | None = None,
        owner: str | None = None,
        user: str | None = None,
    ) -> bool:
        """Update a DUT.

        If the DUT does not exist in the shared pool, returns False.
        If a field is not given or is None, leave it intact.

        Args:
          name: The name of the DUT.
          dimensions: A dict for all the DUT dimensions.
          state: a DutState enum.
          owner: the owner bisection ID.
          user: the user bisection ID.
        """
        with self._client.transaction():
            key = self._default_key(name)
            dut = self._client.get(key)
            if dut is None:
                logger.warning(
                    'DUT %s does not exist in the shared DUT pool', name
                )
                return False
            to_update = {
                k: v
                for k, v in {
                    'name': name,
                    'dimensions': dimensions,
                    'state': state,
                    'owner': owner,
                    'user': user,
                    'last_update_time': datetime.datetime.now(
                        tz=datetime.timezone.utc
                    ),
                }.items()
                if v is not None
            }
            logger.debug('dut %s to be updated with: %s', name, to_update)
            dut.update(to_update)
            self._client.put(dut)
            return True

    def transfer_owner(self, dut_name: str, bisect_id: str) -> bool:
        """Transfer the owner of a DUT to another bisection.

        The transfer is not successful if the receiver biscetion has already
        owned some DUTs becuase one bisection is only allowed to own one dUT.

        Args:
          dut_name: the DUT to be transferred.
          bisect_id: the bisection ID to recieve the DUT.

        Returns:
          Success or not.
        """
        with self._client.transaction():
            query_owned_duts = self._default_query()
            query_owned_duts.add_filter(
                filter=datastore.query.PropertyFilter("owner", "=", bisect_id)
            )
            owned_duts = list(query_owned_duts.fetch())
            if owned_duts:
                logger.debug(
                    'bisection %s already owns dut(s) %s',
                    bisect_id,
                    [d.get('name') for d in owned_duts],
                )
                return False

            key = self._default_key(dut_name)
            dut = self._client.get(key)
            if not dut:
                logger.error(
                    "to-be-transferred dut %s not in the shared DUT pool",
                    dut_name,
                )
                return False

            dut.update({'owner': bisect_id, 'state': DutState.IDLE})
            self._client.put(dut)
            logger.info('DUT %s transferred to %s', dut_name, bisect_id)
        return True

    def yield_duts(self, session: str) -> (list[str], list[str]):
        """Yields all DUTs owned by this session to some other bisections.

        It searches for all unfinished bisections. If some other bisection
        does not own any DUT and its DutAllocateSpec matches a DUT owned by this
        session, the DUT is transferred to that bisection.

        Args:
          session: the session ID of this bisection.
        Returns:
          (list of yielded DUTs, list of not yielded DUTs)
        """
        query_owned_duts = self._default_query()
        query_owned_duts.add_filter(
            filter=datastore.query.PropertyFilter("owner", "=", session)
        )
        owned_duts = list(query_owned_duts.fetch())
        if not owned_duts:
            logger.info('no duts for session %s to yield.', session)
            return [], []
        logger.info(
            '%s DUT(s) to yield: %s',
            len(owned_duts),
            [d.get('name') for d in owned_duts],
        )

        bisect_db_client = bisect_db.Client()
        specs = bisect_db_client.dut_alloc_spec_for_unfinished_bisects()

        duts_yielded = []
        for dut in owned_duts:
            dut_name = dut.get('name')
            for bisect_id, spec in specs.items():
                logger.debug(
                    'checking dut %s against spec %s\ndut: %s\nspec: %s\n',
                    dut_name,
                    bisect_id,
                    dut,
                    spec,
                )
                if dut_matches_spec(dut, spec):
                    logger.debug('yielding DUT %s to %s', dut_name, bisect_id)
                    if self.transfer_owner(dut_name, bisect_id):
                        duts_yielded.append(dut_name)
                        break

        duts_not_yielded = [
            d.get('name')
            for d in owned_duts
            if d.get('name') not in duts_yielded
        ]
        return duts_yielded, duts_not_yielded
