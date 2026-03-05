#!/usr/bin/env bash
# Copyright 2026 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

set -eu

cd "$(dirname "$0")"/..

# Try to load the venv if any.
[ -f ./venv/bin/activate ] && source ./venv/bin/activate

# Check the mypy version. The default mypy on Debian may be old.
REQUIRED_VER="1.19.1"
CURRENT_VER=$(python3 -m mypy --version | awk '{print $2}')
LOWER_VER=$(printf '%s\n' "$REQUIRED_VER" "$CURRENT_VER" | sort -V | head -n1)

if [ "$LOWER_VER" = "$REQUIRED_VER" ]; then
  echo "mypy version is ok (current $CURRENT_VER >= required $REQUIRED_VER)."
else
  echo "Error: mypy version is $CURRENT_VER. You need $REQUIRED_VER or higher."
  echo "Please upgrade it using: pip install -r requirements_dev.txt"
  exit 1
fi

if [ $# -eq 0 ]; then
  # if no arguments supplied, processes all python files

  # Enables shopt -s nullglob in case no files match
  shopt -s nullglob
  PYTHON_FILES=(*.py bisect_kit/*.py bisect_kit/*/*.py)
  shopt -u nullglob
else
  # if arguments supplied, extract python files from them
  PYTHON_FILES=()

  for file in "$@"; do
    if [[ $file == *.py ]]; then
      PYTHON_FILES+=("$file")
    fi
  done
fi

if [ ${#PYTHON_FILES[@]} -eq 0 ]; then
  echo "No Python files found."
  exit 0
else
  exec python3 -m mypy "${PYTHON_FILES[@]}" --check-untyped-defs
fi
