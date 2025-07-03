#!/usr/bin/env bash
# Copyright 2025 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

# b/236854886: workaround before buildbucket re-generates its python proto.
export PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION=python

# Dry run all executable scripts and make sure they can be loaded correctly.
find . -maxdepth 1 -perm -110 -type f -name '*.py' -print0 \
  | xargs -0 -P $(nproc) -I {} \
  -- bash -c '{} --help > /dev/null || (echo "{}" failed && exit 255)'
