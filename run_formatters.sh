#!/usr/bin/env bash
# Copyright 2022 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

# Print all commands being executed
set -x

# Exit immediately if any command exits with a non-zero status
set -e

# Change pwd to the directory containing this script.
cd "$(dirname "$(realpath -e "${BASH_SOURCE[0]}")")"

# Update depot_tools
update_depot_tools

PYTHON_FILES="*.py bisect_kit/*.py bisect_kit/*/*.py"

# Format python files with `cros format` which include isort and black tools.
cros format ${PYTHON_FILES}

# Check if there is a misspelled word.
grep -r -i 'chromium os' ${PYTHON_FILES} && \
    { echo "Please spell ChromiumOS without spaces"; exit 1; }
grep -r -i 'chrome os' ${PYTHON_FILES} && \
    { echo "Please spell ChromeOS without spaces"; exit 1; }

exit 0
