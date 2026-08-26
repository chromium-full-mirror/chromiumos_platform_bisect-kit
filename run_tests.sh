#!/usr/bin/env bash
# Copyright 2022 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

# Print all commands being executed
set -x

# Exit immediately if any command exits with a non-zero status
set -e

# Change pwd to the directory containing this script
cd "$(dirname "$(realpath -e "${BASH_SOURCE[0]}")")"

# Update depot_tools
update_depot_tools

# Check python format with `cros format` which include isort and black tools.
cros format *.py bisect_kit/*.py bisect_kit/*/*.py --check

# Analyze python files with pylint.
./scripts/run_pylint.sh

# Run test cases with pytest.
./scripts/run_pytest.sh

# Dry run all executable scripts and make sure they can be loaded correctly.
./scripts/dry_run_commands.sh
