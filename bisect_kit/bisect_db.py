#!/usr/bin/env python3
# Copyright 2024 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Communicates to bisect database in Datastore."""

import enum
import logging

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
                raise errors.DutAllocateSpecError('unexpected dimension %s' % d)
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

    user_dut = b.get('UserDUT')
    if user_dut:
        if cros_lab_util.is_lab_dut(user_dut):
            user_dut = cros_lab_util.dut_host_name(user_dut)
        spec.dut_name = user_dut

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
    spec.session = b.get('ID')

    return spec


def update_retry_device_spec(
    b: datastore.Entity,
    spec: dut_allocate_spec_type.DutAllocateSpec,
) -> bool:
    to_update = {}
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
            logger.error('unable to get bisect with key %s', session)
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
            logger.errors('missing bisect key %s', spec.session)
            return False

        with self._client.transaction():
            key = self._client.key(self._KIND_BISECT, spec.session)
            b = self._client.get(key)
            if not b:
                logger.errors('unable to get bisect with key %s', spec.session)
                return False

            if not update_retry_device_spec(b, spec):
                return False
            self._client.put(b)
        return True
