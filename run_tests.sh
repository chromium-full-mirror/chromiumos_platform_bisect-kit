#!/usr/bin/env bash
# Copyright 2022 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

set -x
set -e

# change pwd to the directory containing this script
cd "$(dirname "$(realpath -e "${BASH_SOURCE[0]}")")"

pytest -v
