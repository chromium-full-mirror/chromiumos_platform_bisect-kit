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
    export UV_INDEX_URL="${PIP_URL}"
  fi

  UV_CMD=()
  if command -v uv >/dev/null 2>&1; then
    UV_CMD=("uv")
  else
    TMP_BOOTSTRAP=$(mktemp -d)
    trap 'rm -rf "${TMP_BOOTSTRAP}"' EXIT
    PIP_CMD=(python3 -m pip)
    if ! python3 -m pip --version >/dev/null 2>&1; then
      if command -v pip3 >/dev/null 2>&1; then
        PIP_CMD=(pip3)
      elif command -v pip >/dev/null 2>&1; then
        PIP_CMD=(pip)
      fi
    fi
    "${PIP_CMD[@]}" install \
      --target "${TMP_BOOTSTRAP}" \
      --require-hashes \
      -r bootstrap_requirements.txt >/dev/null
    UV_CMD=(env PYTHONPATH="${TMP_BOOTSTRAP}" python3 -m uv)
  fi

  PYTHON_VERSION="$(
    cat .python-version 2>/dev/null | tr -d '[:space:]' || echo "3.13"
  )"
  if [ ! -d "./venv" ]; then
    "${UV_CMD[@]}" venv ./venv --python "${PYTHON_VERSION}"
  else
    "${UV_CMD[@]}" venv --allow-existing ./venv --python "${PYTHON_VERSION}"
  fi

  if [ ${#REQUIREMENTS[@]} -gt 0 ]; then
    if ! "${UV_CMD[@]}" pip install \
      --python "./venv/bin/python" \
      --require-hashes \
      "${REQUIREMENTS[@]}"; then
      echo "ERROR: Failed to install Python requirements." >&2
      echo "If you added a new dependency, please run" >&2
      echo "  ./scripts/upload_to_python_ar.sh" >&2
      echo "to upload missing packages to Artifact Registry." >&2
      exit 1
    fi
  fi
) 9> "./venv.lock"

source "./venv/bin/activate"
