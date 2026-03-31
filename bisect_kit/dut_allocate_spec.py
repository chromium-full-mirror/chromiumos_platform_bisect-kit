#!/usr/bin/env python3
# Copyright 2024 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Manages the spec used for dut allocation."""

import argparse
import dataclasses
import json
import logging

from bisect_kit import bisect_db
from bisect_kit import common
from bisect_kit import dut_allocate_spec_type
from bisect_kit import errors


logger = logging.getLogger(__name__)


# Type alias for easier use.
DutAllocateSpec = dut_allocate_spec_type.DutAllocateSpec


def save(spec: DutAllocateSpec, sync_with_db=False):
    assert spec.session
    if not sync_with_db:
        session_file = _session_file_name(spec.session)
        logger.info('writing %s to %s', spec, session_file)
        with open(session_file, 'w') as f:
            json.dump(dataclasses.asdict(spec), f, indent=4)
    else:
        logger.info('writing dut allocate spec back to bisect database')
        ds_client = bisect_db.Client()
        if ds_client.update_retry_device_spec(spec):
            logger.info('dut allocate spec written to bisect database')
        else:
            logger.error('failed to write dut allocate spec to bisect database')


def _session_file_name(session: str) -> str:
    return common.get_session_log_path(session, 'DutAllocateSpec')


def _load_from_local_file(session: str) -> DutAllocateSpec:
    session_file = _session_file_name(session)
    with open(session_file) as f:
        spec = DutAllocateSpec(**json.load(f))
    logger.info('DutAllocateSpec loaded from %s: %s', session_file, spec)
    return _tweak_spec(spec)


def load(session: str, sync_with_db=False) -> DutAllocateSpec | None:
    assert session
    if not sync_with_db:
        return _load_from_local_file(session)

    ds_client = bisect_db.Client()
    spec = None
    try:
        spec = ds_client.load_dut_allocate_spec(session)
    except errors.DutAllocateSpecError as e:
        logger.error(
            'failed to load DutAllocateSpec from Datastore for %s: %s',
            session,
            e,
        )
    logger.info(
        'DutAllocateSpec loaded from Datastore for %s: %s', session, spec
    )
    return _tweak_spec(spec)


def _str_to_list(val: str | list[str]) -> list[str]:
    """Turns a comma separated string into a list."""
    if not val:
        return []

    if isinstance(val, list):
        # Split and flatten to 1D list..
        return sum(map(lambda x: x.split(','), val), [])

    assert isinstance(val, str)
    return val.split(',')


def _tweak_spec(spec: DutAllocateSpec) -> DutAllocateSpec:
    """Tweaks the DutAllocateSpec to handle special SKUs.

    Args:
      spec: The DutAllocateSpec to tweak.

    Returns:
      The tweaked DutAllocateSpec.
    """
    tweaked_skus = _tweak_sku_list(spec.skus)
    if tweaked_skus != spec.skus:
        logger.info(
            'DutAllocateSpec: SKU updated from %s to %s',
            spec.skus,
            tweaked_skus,
        )
        spec.skus = tweaked_skus
    return spec


def _tweak_sku_list(val: list[str]) -> list[str]:
    """Adds a tweak to the SKU list to treat comma-included SKUs

    This is a HACK to Treat some special SKUs (containing a comma).
    Currently we have the following 3 SKUs including a comma:
    - kanix_MTL-U 15W Ultra 5 T2 1.5/4.2 GT 1.8G 1VDBox, IPU_16GB
    - karis_MTL-U 15W Ultra 5 T2 1.5/4.2 GT 1.8G 1VDBox, IPU_8GB
    - screebo_MTL-U 15W Ultra 5 T2 1.5/4.2 GT 1.8G 1VDBox, IPU_16GB
    """

    result: list[str] = []
    for part in val:
        if (
            len(result) > 0
            and result[-1]
            in [
                'kanix_MTL-U 15W Ultra 5 T2 1.5/4.2 GT 1.8G 1VDBox',
                'karis_MTL-U 15W Ultra 5 T2 1.5/4.2 GT 1.8G 1VDBox',
                'screebo_MTL-U 15W Ultra 5 T2 1.5/4.2 GT 1.8G 1VDBox',
            ]
            and part.strip() in ['IPU_8GB', 'IPU_16GB']
        ):
            result[-1] += ', '
            result[-1] += part.strip()
        else:
            result.append(part.strip())

    return result


def parse_from_command_line_args(opts: argparse.Namespace) -> DutAllocateSpec:
    """Create a DutAllocateSpec from command line arguments.

    It is used when invoked from cros_helper.py (i.e., stateful bisections).
    """
    spec = DutAllocateSpec(
        pools=_str_to_list(opts.pool),
        dimensions=[f'{k}:{v}' for k, v in opts.dimensions],
        boards=_str_to_list(opts.board),
        models=_str_to_list(opts.model),
        skus=_str_to_list(opts.sku),
        dut_name=opts.dut_name,
        satlab_ip=opts.satlab_ip,
        version_hints=_str_to_list(opts.version_hint),
        builder_hints=_str_to_list(opts.builder_hint),
        time_limit_seconds=opts.time_limit,
        lease_duration_seconds=opts.duration,
        parallel=opts.parallel,
        session=opts.session,
    )
    return _tweak_spec(spec)


def parse_from_dut_allocate_command_line_args(
    opts: argparse.Namespace,
) -> DutAllocateSpec:
    """Create a DutAllocateSpec from command line arguments.

    It is used when invoked from diagnose_cros_*.py init (i.e., stateless bisections).
    """
    spec = DutAllocateSpec(
        pools=_str_to_list(opts.allocate_dut_pool),
        dimensions=[f'{k}:{v}' for k, v in opts.allocate_dut_dimensions],
        boards=_str_to_list(opts.allocate_dut_board),
        models=_str_to_list(opts.allocate_dut_model),
        skus=_str_to_list(opts.allocate_dut_sku),
        dut_name=opts.allocate_dut_dut_name,
        satlab_ip=opts.allocate_dut_satlab_ip,
        version_hints=_str_to_list(opts.allocate_dut_version_hint),
        builder_hints=_str_to_list(opts.allocate_dut_builder_hint),
        time_limit_seconds=opts.allocate_dut_time_limit,
        lease_duration_seconds=opts.allocate_dut_duration,
        parallel=opts.allocate_dut_parallel,
        session=opts.session,
        public=opts.public_build,
    )
    return _tweak_spec(spec)
