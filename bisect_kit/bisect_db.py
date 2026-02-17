#!/usr/bin/env python3
# Copyright 2024 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Communicates to bisect database in Datastore."""

import datetime
import enum
import logging
import typing

from bisect_kit import cros_lab_util
from bisect_kit import dut_allocate_spec_type
from bisect_kit import errors
from bisect_kit import gcloud
from google.cloud import datastore


logger = logging.getLogger(__name__)


# In sync with google3/googleclient/chrome/chromeos_bisector/public/bisect.proto
class DeviceSpecType(enum.IntEnum):
    """Enum for device spec type."""

    NONE = 0
    BOARD = 1
    MODEL = 2
    SKU = 3


def to_dut_allocate_spec(
    b: datastore.Entity,
) -> dut_allocate_spec_type.DutAllocateSpec:
    """Constructs a DutAllocateSpec from a bisection.

    Args:
      b: a bisection loaded from the DataStore.

    Returns:
      a DutAllocateSpec which describes the DUT needed by the bisection.

    Raises:
      errors.DutAllocateSpec if the bisection is malformed.
    """
    spec = dut_allocate_spec_type.DutAllocateSpec(
        builder_hints=b.get('Builders', []),
        pools=b.get('Pools', []),
    )

    dimensions = []
    dimension_filters = b.get('DUTDimensionsFilters', [])
    if dimension_filters:
        for d in dimension_filters:
            parts = d.split(':')
            if len(parts) != 2:
                logger.warning('unexpected dimension "%s"', d)
                continue
            dimensions.append('%s:%s' % (parts[0].strip(), parts[1].strip()))
    spec.dimensions = dimensions

    base_version = b.get('BaseCrosVersion')
    if base_version:
        spec.version_hints = [base_version]
    else:
        old_version = b.get('Old.Version')
        if not old_version:
            raise errors.DutAllocateSpecError('Missing Old.Version')

        new_version = b.get('New.Version')
        if not new_version:
            raise errors.DutAllocateSpecError('Missing New.Version')
        spec.version_hints = [old_version, new_version]

    spec.public = 'public-build' in b.get('Flags', [])

    user_dut = b.get('UserDUT')
    if user_dut:
        # ends with ".cros", or DUTs needs forward.
        if cros_lab_util.is_lab_dut(user_dut):
            # converts it to host name without ".cros"
            spec.dut_name = cros_lab_util.dut_host_name(user_dut)
        # does not ends with ".cros"
        elif cros_lab_util.is_lab_dut(
            cros_lab_util.dut_name_to_address(user_dut)
        ):
            # leave it without ".cros"
            spec.dut_name = user_dut

    if not spec.dut_name:
        retry_device_spec_type = b.get('RetryDeviceSpecType')
        retry_device_spec_value = b.get('RetryDeviceSpecValue')
        if retry_device_spec_type and retry_device_spec_value:
            device_spec_type = retry_device_spec_type
            device_spec_value = [retry_device_spec_value]
        else:
            device_spec_type = b.get('DeviceSpecType')
            device_spec_value = b.get('DeviceSpecValue', [])
        if device_spec_type == DeviceSpecType.BOARD:
            spec.boards = device_spec_value
        elif device_spec_type == DeviceSpecType.MODEL:
            spec.models = device_spec_value
        elif device_spec_type == DeviceSpecType.SKU:
            spec.skus = device_spec_value

    if (
        not spec.boards
        and not spec.models
        and not spec.skus
        and not spec.dut_name
    ):
        raise errors.DutAllocateSpecError(
            'none of board/model/sku/dut_name specified'
        )

    spec.session = b.get('ID')

    return spec


def update_retry_device_spec(
    b: datastore.Entity,
    spec: dut_allocate_spec_type.DutAllocateSpec,
) -> bool:
    to_update: dict[str, typing.Any] = {}
    if b.get('Autotest.Metric'):
        if not spec.skus:
            logger.warning('unable to update retry device spec: no skus')
            return False
        to_update['RetryDeviceSpecType'] = DeviceSpecType.SKU
        to_update['RetryDeviceSpecValue'] = spec.skus[0]
    else:
        if not spec.models:
            logger.warning('unable to update retry device spec: no models')
            return False
        to_update['RetryDeviceSpecType'] = DeviceSpecType.MODEL
        to_update['RetryDeviceSpecValue'] = spec.models[0]

    logger.debug('to_update: %s', to_update)
    b.update(to_update)
    return True


class Client(gcloud.DataStoreClient):
    """A Datastore Client to interact with the Bisect database."""

    _KIND_BISECT = 'Bisect'

    def load_dut_allocate_spec(
        self, session: str
    ) -> dut_allocate_spec_type.DutAllocateSpec | None:
        logger.debug(
            'loading dut allocate spec of %s from bisect database', session
        )
        key = self._client.key(self._KIND_BISECT, session)
        b = self._client.get(key)
        if not b:
            raise errors.DutAllocateSpecError(
                'no biesect with key %s found in database' % session
            )
        spec = to_dut_allocate_spec(b)
        logger.debug('dut allocate spec loaded from bisect database: %s', spec)
        return spec

    def update_retry_device_spec(
        self, spec: dut_allocate_spec_type.DutAllocateSpec
    ) -> bool:
        if not spec.session:
            logger.error('missing bisect key %s', spec.session)
            return False

        with self._client.transaction():
            key = self._client.key(self._KIND_BISECT, spec.session)
            b = self._client.get(key)
            if not b:
                logger.error('unable to get bisect with key %s', spec.session)
                return False

            if not update_retry_device_spec(b, spec):
                return False
            self._client.put(b)
        return True

    # google3/googleclient/chrome/chromeos_bisector/public/bisect.proto
    #   STATUS_UNSPECIFIED = 0;
    #   DONE = 3;
    #   FAILED = 4;
    _BISECT_FINISHED_STATUS = [0, 3, 4]

    _KIND_BISECT = 'Bisect'

    # The number of days to lookback when querying unfinished bisects.
    _UNFINISHED_BISECT_LOOKBACK_DAYS = 7

    def _unfinished_bisects(self) -> list[datastore.Entity]:
        """Queries recent unfinished bisections."""
        q = self._client.query(kind=self._KIND_BISECT)

        start_date = datetime.datetime.now(
            tz=datetime.timezone.utc
        ) - datetime.timedelta(days=self._UNFINISHED_BISECT_LOOKBACK_DAYS)
        filters = [
            datastore.query.PropertyFilter(
                'Status', 'NOT_IN', self._BISECT_FINISHED_STATUS
            ),
            datastore.query.PropertyFilter('ReportedAt', '>=', start_date),
        ]

        logging.debug('querying unfinished bisects: %s', filters)
        q.add_filter(filter=datastore.query.And(filters))

        return list(q.fetch())

    def dut_alloc_spec_for_unfinished_bisects(
        self,
    ) -> dict[str, dut_allocate_spec_type.DutAllocateSpec]:
        """Returns DutAllocateSpec of recent unfinished bisections."""
        bisects = self._unfinished_bisects()
        logging.debug('%s unfinished bisects', len(bisects))

        specs = {}
        for b in bisects:
            bisect_id = b.get('ID')

            # TODO(b/278200317): Remove it when all bisections are stateless.
            # Do not yield DUTs to non-stateless bisections since they'll never
            # use it.
            if not 'stateless' in b.get('Experiments', []):
                continue

            try:
                spec = to_dut_allocate_spec(b)
            except errors.DutAllocateSpecError as e:
                # Ignore errors, since the records in Datastore might be malformed.
                logging.warning(
                    'unable to create DutAllocateSpec for %s: %s',
                    bisect_id,
                    e,
                )
            else:
                specs[bisect_id] = spec

        logging.debug('%s unfinished bisects with valid spec', len(specs))
        return specs

    def query_finished_bisects(
        self,
        after: datetime.datetime,
        before: datetime.datetime,
    ) -> list[datastore.Entity]:
        """Gets all finished bisections in [after, before)
        Args:
          after: the after time.
          before: the before time.
        Returns:
          A list of Bisect entities.
        """
        q = self._client.query(kind=self._KIND_BISECT)
        filters = [
            datastore.query.PropertyFilter(
                'Status', 'IN', self._BISECT_FINISHED_STATUS
            ),
            datastore.query.PropertyFilter('ReportedAt', '<', before),
            datastore.query.PropertyFilter('ReportedAt', '>=', after),
        ]

        logging.debug('querying finished bisects: %s', filters)
        q.add_filter(filter=datastore.query.And(filters))

        return list(q.fetch())

    def get_bisect(self, bisect_id: str) -> datastore.Entity:
        """Get a single bisect.

        Args:
          bisect_id: bisect ID.
        Returns:
          The Bisect entity.
        """
        key = self._client.key('Bisect', bisect_id)
        entity = self._client.get(key)
        return entity
