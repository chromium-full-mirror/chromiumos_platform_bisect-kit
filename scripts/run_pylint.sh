#!/usr/bin/env bash
# Copyright 2025 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

export PYTHONPATH="$(pwd):$(pwd)/third_party"

if [ $# -eq 0 ]; then
  # if no arguments supplied, processes all python files
  PYTHON_FILES=(*.py bisect_kit/*.py bisect_kit/*/*.py)
else
  # if arguments supplied, extract python files from them
  PYTHON_FILES=()

  for file in $@; do
    if [[ $file == *.py ]]; then
      PYTHON_FILES+=("$file")
    fi
  done
fi

if [ ${#PYTHON_FILES[@]} -eq 0 ]; then
  echo No Python files found.
  exit 0
else
  exec pylint "${PYTHON_FILES[@]}"
fi
