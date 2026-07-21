#!/usr/bin/env bash
# Copyright 2026 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

set -euo pipefail

SCRIPT_DIR="$(dirname "${BASH_SOURCE[0]}")"
cd "${SCRIPT_DIR}"/..

TMP_DIR=$(mktemp -d)
trap 'rm -rf "${TMP_DIR}"' EXIT

VENV_DIR="${TMP_DIR}/venv"
python3 -m venv "${VENV_DIR}"
source "${VENV_DIR}/bin/activate"

pip install twine

PROJECT="google.com:crosperf"
LOCATION="us"
REPOSITORY="cros-bisect-python"
AR_REPO="https://us-python.pkg.dev/${PROJECT}/${REPOSITORY}/"

DOWNLOAD_DIR="${TMP_DIR}/download"
mkdir -p "${DOWNLOAD_DIR}"

echo "Downloading packages from PyPI..."
(
  unset PIP_INDEX_URL
  pip download \
    -r requirements.txt \
    -r requirements_dev.txt \
    -d "${DOWNLOAD_DIR}"

  for py_ver in 38 39 310 311 312 313; do
    pip download \
      -r requirements.txt \
      -r requirements_dev.txt \
      -d "${DOWNLOAD_DIR}" \
      --platform manylinux2014_x86_64 \
      --python-version "${py_ver}" \
      --only-binary=:all: 2>/dev/null || true
  done
)

echo "Fetching remote file list and hashes..."
declare -A REMOTE_SHA256_MAP
declare -A REMOTE_RESOURCE_MAP

PARSE_PY="import json, sys, urllib.parse
data = json.load(sys.stdin)
for item in data:
  name = item['name']
  raw_fn = name.split('/files/')[-1]
  fn = urllib.parse.unquote(raw_fn).split('/')[-1]
  sha = next((h['value'].lower() for h in item.get('hashes', [])
              if h.get('type') == 'SHA256'), '')
  print(f'{fn}\t{sha}\t{name}')"

while IFS=$'\t' read -r fname sha resource_name; do
  [ -n "${fname}" ] || continue
  REMOTE_SHA256_MAP["${fname}"]="${sha}"
  REMOTE_RESOURCE_MAP["${fname}"]="${resource_name}"
done < <(gcloud artifacts files list \
  --repository="${REPOSITORY}" \
  --location="${LOCATION}" \
  --project="${PROJECT}" \
  --format="json" | python3 -c "${PARSE_PY}")

declare -A LOCAL_FILES
TO_UPLOAD=()

for path in "${DOWNLOAD_DIR}"/*; do
  [ -f "${path}" ] || continue
  fname="$(basename "${path}")"
  LOCAL_FILES["${fname}"]=1

  local_sha=$(sha256sum "${path}" | awk '{print $1}')
  remote_sha="${REMOTE_SHA256_MAP[${fname}]:-}"

  if [ "${local_sha}" != "${remote_sha}" ]; then
    TO_UPLOAD+=("${path}")
  fi
done

if [ ${#TO_UPLOAD[@]} -gt 0 ]; then
  echo "Uploading ${#TO_UPLOAD[@]} new/changed package(s)..."
  twine upload \
    --repository-url "${AR_REPO}" \
    "${TO_UPLOAD[@]}"
else
  echo "No new/changed packages to upload."
fi

DELETED_COUNT=0
for fname in "${!REMOTE_RESOURCE_MAP[@]}"; do
  if [ -z "${LOCAL_FILES[${fname}]:-}" ]; then
    resource_name="${REMOTE_RESOURCE_MAP[${fname}]}"
    echo "Deleting unused remote file: ${fname}"
    gcloud artifacts files delete "${resource_name}" \
      --quiet
    DELETED_COUNT=$((DELETED_COUNT + 1))
  fi
done

if [ "${DELETED_COUNT}" -eq 0 ]; then
  echo "No unused remote packages to delete."
else
  echo "Deleted ${DELETED_COUNT} unused remote package(s)."
fi

echo "Successfully synchronized dependencies to Artifact Registry."
