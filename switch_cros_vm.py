#!/usr/bin/env python3
# Copyright 2024 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Dummy Switcher for ChromeOS VM, the script does nothing but just returns
the switch VM action."""

from __future__ import annotations

from bisect_kit import bisector_cli


def action() -> bisector_cli.SwitchAction:
    return bisector_cli.SwitchAction.SWITCH_VM
