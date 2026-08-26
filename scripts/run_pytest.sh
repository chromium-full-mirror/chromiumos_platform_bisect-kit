#!/usr/bin/env bash
# Copyright 2026 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

set -euo pipefail

cd "$(dirname "$(realpath -e "${BASH_SOURCE[0]}")")"/..

SCRIPT_DIR="$(dirname "${BASH_SOURCE[0]}")"
source "${SCRIPT_DIR}/setup_venv.sh" -r requirements_dev.txt

export PYTHONPATH="$(pwd):$(pwd)/third_party"

# b/236854886: workaround before buildbucket re-generates its python proto.
export PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION=python

#mkdir /tmp/bisect-kit-bin
#echo "#!/bin/bash" > /tmp/bisect-kit-bin/update_depot_tools
#echo "exit 0" >> /tmp/bisect-kit-bin/update_depot_tools
#chmod +x /tmp/bisect-kit-bin/update_depot_tools
#PATH=/tmp/bisect-kit-bin:$PATH

exec pytest -v
