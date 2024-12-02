#!/usr/bin/env bash
# Copyright 2022 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

# Print all commands being executed
set -x

# Exit immediately if any command exits with a non-zero status
set -e

# b/236854886: workaround before buildbucket re-generates its python proto.
export PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION=python

# Change pwd to the directory containing this script
cd "$(dirname "$(realpath -e "${BASH_SOURCE[0]}")")"

# Update depot_tools
update_depot_tools

# Check python format with `cros format` which include isort and black tools.
cros format *.py bisect_kit/*.py bisect_kit/*/*.py --check

# Analyze python files with pylint.
# We manually list all python files in the base folder because pylint only
# support iterating python files over a valid package (a folder containing
# __init__.py).
#
# --disable too-many-positional-arguments because our code contains a lot of
# existing functions with many arguments.
PYTHONPATH="$(pwd):$(pwd)/third_party" \
pylint --disable too-many-positional-arguments *.py bisect_kit

# Run test cases with pytest.
pytest -v

# Dry run all executable scripts and make sure they can be loaded correctly.
for executable in $(find . -maxdepth 1 -perm -110 -type f -name '*.py'); do
    ./$executable --help > /dev/null
done
