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

# Check python format with isort.
isort --check .

# Check python format with black.
black --check --diff .

# Analyze python files with pylint.
# We manually list all python files in the base folder because pylint only
# support iterating python files over a valid package (a folder containing
# __init__.py).
PYTHONPATH="$(pwd):$(pwd)/third_party" \
pylint *.py bisect_kit

# Run test cases with pytest.
pytest -v
