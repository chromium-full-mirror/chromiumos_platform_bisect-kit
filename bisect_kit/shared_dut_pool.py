#!/usr/bin/env python3
# Copyright 2024 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Manages the shared DUT pool (go/bisector-shared-dut-pool)"""

from __future__ import annotations

import datetime
from enum import StrEnum
import logging
import time
import typing

from bisect_kit import bisect_db
from bisect_kit import cros_lab_util
from bisect_kit import dut_allocate_spec_type
from bisect_kit import errors
from bisect_kit import gcloud
from google.cloud import datastore
import google.cloud.exceptions


logger = logging.getLogger(__name__)


class DutState(StrEnum):
    """Dut state."""

    BUSY = 'busy'
    IDLE = 'idle'


class SuccessorFinder:
    """Find unfinished bisects which can take over a DUT.

    It queries the bisect database for all recent unfinished bisects.
    If the DutAllocateSpec of some bisect matches a DUT, the DUT can be
    transferred to the bisect.
    """

    def __init__(self, owner):
        """Initializer.

        Args:
          owner: The bisection which requests to find successors for its owned DUT.
        """

        bisect_db_client = bisect_db.Client()
        # A DUT is to be yileded by |owner|, so exclud itself from the list
        # of candidates to yield.
        self._specs = {
            bisect_id: spec
            for bisect_id, spec in (
                bisect_db_client.dut_alloc_spec_for_unfinished_bisects().items()
            )
            if bisect_id != owner
        }

    def match(self, dimensions: dict) -> list[str]:
        """Given a dict of dimensions, returns all matched bisects."""

        matched = []
        for bisect_id, spec in self._specs.items():
            logger.debug(
                'matching dimensions %s against spec %s', dimensions, spec
            )
            if dut_matches_spec(dimensions, spec):
                matched.append(bisect_id)
        logger.debug('all matched bisects: %s', matched)
        return matched


def dut_matches_spec(
    dimensions: dict, spec: dut_allocate_spec_type.DutAllocateSpec
) -> bool:
    """Checks whether a DUT matches a DutAllocateSpec.

    Args:
      dimensions: a dict of DUT dimensions.
      spec: the DutAllocateSpec to check.
    Returns:
      matched or not.
    """

    def field_matched(field_name, values_to_match: typing.Any) -> bool:
        value = dimensions.get(field_name)
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


def filter_dimensions(dimensions: list[str], to_filter: list[str]) -> list[str]:
    """A util function to filter out some dimensions.

    Args:
      dimensions: a list of "key:value" dimensions.
      to_filter: a list of key to filter.

    Returns:
      A list of filtered "key:value" dimensions.
    """
    to_filter_set = set(to_filter)

    filtered = []
    for dimension in dimensions:
        key, _ = dimension.split(':', 1)
        if not key in to_filter_set:
            filtered.append(dimension)
    return filtered


_TRANSACTION_RETRY_LIMIT = 4

_TRANSACTION_RETRY_INTERVAL_SECONDS = 3


def retry_transaction_conflict(func):
    """A decorator to retry Datastore transaction conflict.

    https://cloud.google.com/datastore/docs/concepts/transactions#uses_for_transactions
    """

    def wrapped(*args, **kwargs):
        retry_count = 0
        while True:
            try:
                return func(*args, **kwargs)
            except google.cloud.exceptions.Conflict as e:
                retry_count += 1
                if retry_count > _TRANSACTION_RETRY_LIMIT:
                    raise errors.DatastoreTransactionConflict(
                        'Transaction conflict retry limit %s exceeded'
                        % _TRANSACTION_RETRY_LIMIT
                    ) from e
                logger.warning(
                    'Transaction conflict, retry %s in %s seconds',
                    retry_count,
                    _TRANSACTION_RETRY_INTERVAL_SECONDS,
                )
                time.sleep(_TRANSACTION_RETRY_INTERVAL_SECONDS)

    return wrapped


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

    @retry_transaction_conflict
    def insert_dut(
        self,
        name: str,
        owner: str,
        dimensions: dict,
    ) -> str:
        """Insert a DUT to the shared DUT pool.

        If the owner has already owned some DUTs, try finding a successor
        of the DUT.
        If succeeded, set the owner to the successor. Otherwise, do not
        insert the DUT.

        In theory the DUT should not already exist in the pool.
        In case it happens, the existing records is probably stale.
        So overwrite the existing record.

        Args:
          name: The name of the DUT.
          owner: the owner bisect ID.
          dimensions: a dict for all the DUT dimensions.

        Returns:
          The owner of the DUT or None if failed to insert the DUT.
        """
        with self._client.transaction():
            owned_duts = self.query_by_owner(owner)
            if owned_duts:
                logger.info(
                    'owner %s already owned duts %s, trying to find a successor',
                    owner,
                    list(owned_duts),
                )
                finder = SuccessorFinder(owner)
                matched = finder.match(dimensions)
                if matched:
                    owner = matched[0]
                    logger.info('found successor %s', owner)
                else:
                    logger.info(
                        'unable to find successor, do not insert the DUT'
                    )
                    return None

            # Checking only, write the record no matter it exists or not.
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
                    'owner': owner,
                    'state': DutState.IDLE,
                    'dimensions': dimensions,
                    'lastUpdated': datetime.datetime.now(
                        tz=datetime.timezone.utc
                    ),
                }
            )
            self._client.put(dut)
        return owner

    def _build_query(
        self,
        dimensions: list[str],
        variants: list[str],
        pools: list[str] | None = None,
        state: DutState | None = None,
    ) -> datastore.Query:
        """query DUTs in the shared pool by the given conditions.

        Args:
          dimensions: all DUT dimensions which needs to be satisfied.
          variants: DUT variants (e.g., board/model/sku) where any one of them would do.
          pools: a list of dimension pool where any of one of them would do. Optional.
          state the state to query. Optional.
        Returns:
          A datastore query object which matches AND(AND(dimensions), OR(variants), OR(pools), state).
        """

        def split_dimensions(
            dimensions: list[str],
        ) -> list[list[str]]:
            # dimensions are in the format of <key>:<value> pairs
            # E.g., label-model:quackingstick, label-pools:DUT_POOL_QUOTA.
            return [d.split(':', 1) for d in dimensions]

        and_filters = []
        if dimensions:
            and_filters.append(
                datastore.query.And(
                    [
                        datastore.query.PropertyFilter(
                            'dimensions.' + k, '=', v
                        )
                        for k, v in split_dimensions(dimensions)
                    ]
                ),
            )
        if variants:
            and_filters.append(
                datastore.query.Or(
                    [
                        datastore.query.PropertyFilter(
                            'dimensions.' + k, '=', v
                        )
                        for k, v in split_dimensions(variants)
                    ]
                ),
            )
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
        return q

    def query_by_owner(self, owner: str) -> list[str]:
        """Query DUTs owned by some owner.

        Args:
          owner: the owner in query.

        Returns:
          A list of DUT names owned by the owner.
        """
        q = self._default_query()
        q.add_filter(filter=datastore.query.PropertyFilter('owner', '=', owner))
        return [d.get('name') for d in q.fetch()]

    @retry_transaction_conflict
    def _set_dut_state(self, dut_name: str, state: DutState) -> bool:
        """Set the dut state."""
        with self._client.transaction():
            key = self._default_key(dut_name)
            dut = self._client.get(key)
            if dut is None:
                logger.warning(
                    'DUT %s does not exist in the shared DUT pool.',
                    dut_name,
                )
                return False
            dut.update(
                {
                    'state': state,
                    'lastUpdated': datetime.datetime.now(
                        tz=datetime.timezone.utc
                    ),
                }
            )
            self._client.put(dut)
        return True

    def mark_dut_as_idle(self, dut_name: str) -> bool:
        """mark a DUT as idle.

        If the DUT does not exist in the shared pool, returns False.

        Args:
          dut_name: The name of the DUT.
        """
        return self._set_dut_state(dut_name, DutState.IDLE)

    def mark_dut_as_busy(self, dut_name: str) -> bool:
        """mark a DUT as busy.

        If the DUT does not exist in the shared pool, returns False.

        Args:
          dut_name: The name of the DUT.
        """
        return self._set_dut_state(dut_name, DutState.BUSY)

    @retry_transaction_conflict
    def transfer_owner(self, dut_name: str, receiver: str) -> bool:
        """Transfer the owner of a DUT to another bisect.

        The transfer is not successful if the receiver biscetion has already
        owned some DUTs becuase one bisect is only allowed to own one DUT.

        Args:
          dut_name: the DUT to be transferred.
          receiver: the bisect ID to recieve the DUT.

        Returns:
          Success or not.
        """
        with self._client.transaction():
            query_owned_duts = self._default_query()
            query_owned_duts.add_filter(
                filter=datastore.query.PropertyFilter("owner", "=", receiver)
            )
            owned_duts = list(query_owned_duts.fetch())
            if owned_duts:
                logger.debug(
                    'bisect %s already owns dut(s) %s',
                    receiver,
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

            dut.update(
                {
                    'owner': receiver,
                    'state': DutState.IDLE,
                    'lastUpdated': datetime.datetime.now(
                        tz=datetime.timezone.utc
                    ),
                }
            )
            self._client.put(dut)
            logger.info('DUT %s transferred to %s', dut_name, receiver)
        return True

    def yield_duts(
        self, owner: str, dut_names: list[str]
    ) -> tuple[list[str], list[str]]:
        """Yields a list of DUTs to some other bisects.

        It searches for all unfinished bisects. If some other bisect
        does not own any DUT and its DutAllocateSpec matches a DUT owned by this
        bisect, the DUT is transferred to that bisect.

        Args:
          owner: the owner bisect id.
          dut_names: list of DUT names.
        Returns:
          (list of yielded DUTs, list of not yielded DUTs)
        """
        if not dut_names:
            logger.info('no DUT to yield')
            return [], []

        duts = self._client.get_multi([self._default_key(d) for d in dut_names])

        finder = SuccessorFinder(owner)

        duts_yielded = []
        for dut in duts:
            dut_name = dut.get('name')
            logger.debug('checking dut %s', dut_name)
            bisect_ids = finder.match(dut.get('dimensions', {}))
            for bisect_id in bisect_ids:
                logger.debug('try yielding DUT %s to %s', dut_name, bisect_id)
                if self.transfer_owner(dut_name, bisect_id):
                    duts_yielded.append(dut_name)
                    break

        duts_not_yielded = [
            d.get('name') for d in duts if d.get('name') not in duts_yielded
        ]
        return duts_yielded, duts_not_yielded

    def clean_up_duts_by_owner(self, owner: str):
        """Clean up DUTs owned by a bisect.

        Yields owned DUTs to other bisects if possible.

        For all DUTs yielded, extend the lease time.
        For all DUTs not yielded, return it back to the lab.

        Args:
          owner: the bisect ID.
        """
        logger.info('Clean up DUTs held by %s', owner)

        dut_names = self.query_by_owner(owner)
        logger.info('%s DUT(s) to clean up: %s', len(dut_names), dut_names)

        duts_yielded, duts_not_yielded = self.yield_duts(owner, dut_names)
        logger.info('DUTs transferred: %s', duts_yielded)
        logger.info(
            'DUTs to remove from the shared DUT pool: %s', duts_not_yielded
        )
        self._client.delete_multi(
            [self._default_key(d) for d in duts_not_yielded]
        )
        for d in duts_not_yielded:
            logger.info('returning DUT %s to the lab', d)
            try:
                cros_lab_util.crosfleet_release_dut(d)
            except Exception as e:
                logger.warning('failed to return DUT %s to the lab: %s', d, e)

    def _clean_up_stale_duts(self, dut_names: list[str]) -> list[str]:
        """Remove duts from the shared DUT pool if it's no longer leased by us.

        Args:
          dut_names: a list of DUT names.
        Returns:
          A list of stale DUTs (i.e., those removed from the shared DUT pool).
        """
        stale_duts = []
        for dut_name in dut_names:
            try:
                lease_status = cros_lab_util.query_lease_status(dut_name)
            except Exception as e:
                logger.warning(
                    'failed to check lease status for %s: %s', dut_name, e
                )
            else:
                if not lease_status.is_leased:
                    stale_duts.append(dut_name)
        logger.info(
            'removing stale DUTs from the shared DUT pool: %s', stale_duts
        )
        self._client.delete_multi([self._default_key(d) for d in stale_duts])
        return stale_duts

    # Some dimensions are irrelevant when trying to find a matched DUT.
    # In some cases, it can interfere with the match result.
    # For example, a leased DUT could have "dut_state:needs_repair", but the
    # dimensions passed in could want "dut_state:ready".
    _IRRELEVANT_DIMENSIONS = ['dut_state']

    @retry_transaction_conflict
    def lease_for_bisect(
        self,
        requestor: str,
        dimensions: list[str],
        variants: list[str],
        pools: list[str],
    ) -> tuple[str | None, str | None]:
        """Try leasing a DUT for a bisect.

        It searches for all idle DUTs in the shared DUT pool which match the given spec.
        Always favor a DUT owned by the bisect itself. If the bisect doesn't own a DUT,
        a DUT owned by some other bisects may be returned.
        If no idel DUTs, the lease fails.

        If the lease is successful, the DUT is tranferred to this bisect (if not already).

        Args:
          requestor: the ID of the requesting bisect.
          dimensions: all DUT dimensions which needs to be satisfied.
          variants: DUT variants (e.g., board/model/sku) where any one of them would do.
          pools: a list of dimension pool where any of one of them would do. Optional.

        Returns:
          A pair of (DUT, original owner).
          If DUT is None, it means there is no idle DUTs which satisfies the requirement.
        """
        with self._client.transaction():
            dimensions = filter_dimensions(
                dimensions, self._IRRELEVANT_DIMENSIONS
            )
            q = self._build_query(
                dimensions, variants, pools, state=DutState.IDLE
            )

            matched_duts = list(q.fetch())
            matched_dut_names = [d.get('name') for d in matched_duts]
            logger.debug(
                'leasing DUT for %s: %s matched idle duts in the shared DUT pool: %s',
                requestor,
                len(matched_dut_names),
                matched_dut_names,
            )

            stale_duts = self._clean_up_stale_duts(matched_dut_names)

            # filter out stale_duts from matched_duts.
            matched_duts = [
                d for d in matched_duts if d.get('name') not in stale_duts
            ]
            logger.debug(
                '%s matched idle DUTs after removing stale DUTs: %s',
                len(matched_duts),
                [d.get('name') for d in matched_duts],
            )

            # In theory the DUT owned by a bisect should match the spec and idle
            # (i.e., in |matched_dut_names|). But in some error cases the owned
            # DUT could not be used by the owner. We should clean them up so the
            # bisect can own a new DUT.
            owned_duts = self.query_by_owner(requestor)
            logger.debug('DUTs owned by %s: %s', requestor, owned_duts)

            bad_owned_duts = []
            for d in owned_duts:
                if not d in matched_dut_names:
                    bad_owned_duts.append(d)
                    logger.warning(
                        '%s can not use its own DUT %s, something wrong happened. '
                        'Returning it to the lab',
                        requestor,
                        d,
                    )
                    try:
                        cros_lab_util.crosfleet_release_dut(d)
                    except Exception as e:
                        logger.warning(
                            'failed to return DUT %s to the lab: %s', d, e
                        )
            self._client.delete_multi(
                [self._default_key(d) for d in bad_owned_duts]
            )

            if not matched_duts:
                return None, None

            dut = None
            orig_owner = None
            for d in matched_duts:
                if d.get('owner') == requestor:
                    dut = d
                    break

            if dut is None:
                # matched_duts is not empty and none of them is owned by
                # |requestor|.
                # Returns the first one for now.
                dut = matched_duts[0]

            dut_name = dut.get('name')
            orig_owner = dut.get('owner')

            dut.update(
                {
                    'owner': requestor,
                    'state': DutState.BUSY,
                    'lastUpdated': datetime.datetime.now(
                        tz=datetime.timezone.utc
                    ),
                }
            )
            self._client.put(dut)

            return dut_name, orig_owner
