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

pip install --require-hashes -r "${SCRIPT_DIR}/bootstrap_requirements.txt"

PROJECT="google.com:crosperf"
LOCATION="us"
REPOSITORY="cros-bisect-python"
AR_REPO="https://us-python.pkg.dev/${PROJECT}/${REPOSITORY}/"

DOWNLOAD_DIR="${TMP_DIR}/download"
mkdir -p "${DOWNLOAD_DIR}"

echo "Re-generating universal requirements with hashes using uv..."
(
  unset PIP_INDEX_URL
  uv pip compile requirements.in \
    --generate-hashes \
    --universal \
    --python-version 3.10 \
    -o requirements.txt
  uv pip compile requirements_dev.in \
    --generate-hashes \
    --universal \
    --python-version 3.10 \
    -o requirements_dev.txt
)

echo "Downloading packages from PyPI..."
(
  unset PIP_INDEX_URL
  for py_ver in 3.10 3.11 3.12 3.13; do
    echo "Downloading packages for Python ${py_ver}"
    uvx --python "${py_ver}" --with pip -- \
      pip download \
        --require-hashes \
        --quiet \
        -r requirements.txt \
        -r requirements_dev.txt \
        -d "${DOWNLOAD_DIR}" \
        --platform manylinux2014_x86_64 \
        --platform manylinux_2_17_x86_64 \
        --platform manylinux_2_28_x86_64 \
        --only-binary=:all:
  done
)

echo "Fetching remote file list and hashes..."
declare -A REMOTE_SHA256_MAP=()
declare -A VERSION_FILES_MAP=()

PARSE_PY="import json, sys, urllib.parse
data = json.load(sys.stdin)
for item in data:
  name = item['name']
  raw_fn = name.split('/files/')[-1]
  fn = urllib.parse.unquote(raw_fn).split('/')[-1]
  ver_res = item.get('owner', '')
  sha = next((h['value'].lower() for h in item.get('hashes', [])
              if h.get('type') == 'SHA256'), '')
  print(f'{fn}\t{sha}\t{ver_res}')"

while IFS=$'\t' read -r fname sha version_res; do
  [ -n "${fname}" ] || continue
  [ -n "${version_res}" ] || continue
  REMOTE_SHA256_MAP["${fname}"]="${sha}"
  VERSION_FILES_MAP["${version_res}"]+=" ${fname}"
done < <(gcloud artifacts files list \
  --repository="${REPOSITORY}" \
  --location="${LOCATION}" \
  --project="${PROJECT}" \
  --format="json" | python3 -c "${PARSE_PY}")

declare -A LOCAL_FILES=()
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
    --non-interactive \
    --repository-url "${AR_REPO}" \
    -u oauth2accesstoken \
    -p "$(gcloud auth print-access-token)" \
    "${TO_UPLOAD[@]}"
else
  echo "No new/changed packages to upload."
fi

if [ ${#LOCAL_FILES[@]} -eq 0 ]; then
  echo "WARNING: No local packages downloaded. Skipping remote file deletion" \
    "for safety."
else
  TO_DELETE_RESOURCES=()
  TO_DELETE_DESCS=()

  if [ ${#VERSION_FILES_MAP[@]} -gt 0 ]; then
    for version_res in "${!VERSION_FILES_MAP[@]}"; do
      if [[ "${version_res}" != *"/packages/"* ]]; then
        continue
      fi

      is_used=0
      for fname in ${VERSION_FILES_MAP["${version_res}"]}; do
        if [ -n "${LOCAL_FILES[${fname}]:-}" ]; then
          is_used=1
          break
        fi
      done

      if [ "${is_used}" -eq 0 ]; then
        TO_DELETE_RESOURCES+=("${version_res}")
        ver_name=$(basename "${version_res}")
        pkg_dir=$(dirname "$(dirname "${version_res}")")
        pkg_name=$(basename "${pkg_dir}")
        TO_DELETE_DESCS+=("${pkg_name} ${ver_name}")
      fi
    done
  fi

  if [ ${#TO_DELETE_RESOURCES[@]} -eq 0 ]; then
    echo "No unused remote package versions to delete."
  else
    echo "The following ${#TO_DELETE_RESOURCES[@]} unused remote package" \
      "version(s) will be deleted:"
    for desc in "${TO_DELETE_DESCS[@]}"; do
      echo "  - ${desc}"
    done
    read -r -p \
      "Delete these remote package version(s) from Artifact Registry? [y/N] " \
      confirm_del
    if [[ "${confirm_del}" =~ ^[Yy]$ ]]; then
      DELETED_COUNT=0
      for version_res in "${TO_DELETE_RESOURCES[@]}"; do
        echo "Deleting version: ${version_res}"
        gcloud artifacts versions delete "${version_res}" \
          --quiet
        DELETED_COUNT=$((DELETED_COUNT + 1))
      done
      echo "Deleted ${DELETED_COUNT} unused remote package version(s)."
    else
      echo "Skipped remote file deletion."
    fi
  fi
fi

echo "Successfully synchronized dependencies to Artifact Registry."
