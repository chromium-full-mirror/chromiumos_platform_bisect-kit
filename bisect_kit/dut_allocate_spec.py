#!/usr/bin/env python3
# Copyright 2024 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Manages the spec used for dut allocation."""

import argparse
import dataclasses
import json
import logging

from bisect_kit import common


logger = logging.getLogger(__name__)


@dataclasses.dataclass
class DutAllocateSpec:
    """A class to keep parameters used to allocate DUTs."""

    pools: str | None = None
    # List of "key:val".
    dimensions: list[str] = dataclasses.field(default_factory=list)
    boards: list[str] = dataclasses.field(default_factory=list)
    models: list[str] = dataclasses.field(default_factory=list)
    skus: list[str] = dataclasses.field(default_factory=list)
    dut_name: str | None = None
    satlab_ip: str | None = None
    version_hints: list[str] = dataclasses.field(default_factory=list)
    builder_hints: list[str] = dataclasses.field(default_factory=list)
    time_limit: int = 540
    duration: float | None = None
    parallel: int = 3
    chromeos_root: str | None = None
    session: str | None = None
    sync_with_db: bool = False

    def save(self):
        assert self.session
        if not self.sync_with_db:
            session_file = _session_file_name(self.session)
            logger.debug('writing %s to %s', self, session_file)
            with open(session_file, 'w') as f:
                json.dump(dataclasses.asdict(self), f)


def _session_file_name(session: str) -> str:
    return common.get_session_log_path(session, 'DutAllocateSpec')


def _load_from_local_file(session: str) -> DutAllocateSpec:
    session_file = _session_file_name(session)
    with open(session_file) as f:
        spec = DutAllocateSpec(**json.load(f))
    logger.debug('DutAllocateSpec loaded from %s: %s', session_file, spec)
    return spec


def load(session: str, sync_with_db=False) -> DutAllocateSpec | None:
    assert session
    if not sync_with_db:
        return _load_from_local_file(session)
    return None


def _str_to_list(val):
    """Turns a comma separated string into a list."""
    if not val:
        return []
    return val.split(',')


def parse_from_command_line_args(opts: argparse.Namespace) -> DutAllocateSpec:
    """Create a DutAllocateSpec from command line arguments.

    It is used when invoked from cros_helper.py (i.e., stateful bisections).
    """
    return DutAllocateSpec(
        pools=_str_to_list(opts.pool),
        dimensions=[f'{k}:{v}' for k, v in opts.dimensions],
        boards=_str_to_list(opts.board),
        models=_str_to_list(opts.model),
        skus=_str_to_list(opts.sku),
        dut_name=opts.dut_name,
        satlab_ip=opts.satlab_ip,
        version_hints=_str_to_list(opts.version_hint),
        builder_hints=_str_to_list(opts.builder_hint),
        time_limit=opts.time_limit,
        duration=opts.duration,
        parallel=opts.parallel,
        chromeos_root=opts.chromeos_root,
        session=opts.session,
    )


def parse_from_dut_allocate_command_line_args(
    opts: argparse.Namespace,
) -> DutAllocateSpec:
    """Create a DutAllocateSpec from command line arguments.

    It is used when invoked from diagnose_cros_*.py init (i.e., stateless bisections).
    """
    return DutAllocateSpec(
        pools=_str_to_list(opts.allocate_dut_pool),
        dimensions=[f'{k}:{v}' for k, v in opts.allocate_dut_dimensions],
        boards=_str_to_list(opts.allocate_dut_board),
        models=_str_to_list(opts.allocate_dut_model),
        skus=_str_to_list(opts.allocate_dut_sku),
        dut_name=opts.allocate_dut_dut_name,
        satlab_ip=opts.allocate_dut_satlab_ip,
        version_hints=_str_to_list(opts.allocate_dut_version_hint),
        builder_hints=_str_to_list(opts.allocate_dut_builder_hint),
        time_limit=opts.allocate_dut_time_limit,
        duration=opts.allocate_dut_duration,
        parallel=opts.allocate_dut_parallel,
        chromeos_root=opts.chromeos_root,
        session=opts.session,
    )
