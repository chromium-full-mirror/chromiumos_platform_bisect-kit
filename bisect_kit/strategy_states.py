# Copyright 2023 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Intrenal states for binary search strategy."""

import dataclasses


@dataclasses.dataclass
class States:
    """Strategy internal states.

    It records the internal states which should can be kept in order to resume
    a bisection (e.g., in stateless bisection).
    """

    state: str | None = None
    init_rounds: int | None = None
    init_range_verified: bool | None = None
    init_verify_state: str | None = None
    count_revold_old: int | None = None
    count_revold_new: int | None = None
    count_revnew_old: int | None = None
    count_revnew_new: int | None = None
    passed_noise_rate: bool = False
