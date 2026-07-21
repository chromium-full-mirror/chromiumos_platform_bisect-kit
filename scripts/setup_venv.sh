#!/usr/bin/env bash
# Copyright 2026 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"/..

REQUIREMENTS=("$@")

# Ensure a venv is set up and activated safely with flock.
(
  flock -x 9
  if [ ! -d "./venv" ]; then
    python3 -m venv "./venv"
  fi
  source "./venv/bin/activate"

  if [ ${#REQUIREMENTS[@]} -gt 0 ]; then
    # Only use authorized index-url with token on infra.
    if [ -n "${LUCI_CONTEXT:-}" ] || \
       [ -n "${SWARMING_BOT_ID:-}" ] || \
       [ -n "${CHROMEOS_BOT_ID:-}" ]; then
      TOKEN=$(luci-auth token -scopes-cloud 2>/dev/null || true)

      if [ -n "${TOKEN}" ]; then
        PIP_URL="https://oauth2accesstoken:${TOKEN}@us-python.pkg.dev/"
      else
        PIP_URL="https://us-python.pkg.dev/"
      fi
      PIP_URL+="google.com:crosperf/cros-bisect-python/simple/"
      export PIP_INDEX_URL="${PIP_URL}"
    fi

    if ! pip install "${REQUIREMENTS[@]}"; then
      echo "ERROR: Failed to install Python requirements." >&2
      echo "If you added a new dependency, please run" >&2
      echo "  ./scripts/upload_to_python_ar.sh" >&2
      echo "to upload missing packages to Artifact Registry." >&2
      exit 1
    fi
  fi
) 9> "./venv.lock"

if [ -z "${VIRTUAL_ENV:-}" ]; then
  source "./venv/bin/activate"
fi
