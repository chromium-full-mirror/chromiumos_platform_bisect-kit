#!/usr/bin/env python3
# Copyright 2024 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Define the DutAllocateSpec class to resolve some circular dependencies"""

import dataclasses


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
    time_limit_seconds: int = 720
    lease_duration_seconds: float | None = None
    parallel: int = 3
    session: str | None = None
    public: bool = dataclasses.field(default=False)
