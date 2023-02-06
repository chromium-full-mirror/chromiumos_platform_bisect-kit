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

# Check python format with `cros format` which include isort and black tools.
cros format *.py bisect_kit/*.py --check

# Analyze python files with pylint.
# We manually list all python files in the base folder because pylint only
# support iterating python files over a valid package (a folder containing
# __init__.py).
PYTHONPATH="$(pwd):$(pwd)/third_party" \
pylint *.py bisect_kit

# Run test cases with pytest.
pytest -v

executable_list="\
bisect_android_build_id.py
bisect_android_repo.py
bisect_cr_localbuild_internal.py
bisect_cr_localbuild_master.py
bisect_cros_repo.py
bisect_cros_version.py
bisect_git.py
bisect_lacros_prebuilt.py
bisect_list.py
cros_helper.py
diagnose_cros_autotest.py
diagnose_cros_tast.py
eval_cros_autotest.py
eval_cros_os_looping_localbuild.py
eval_cros_os_looping_prebuilt.py
eval_cros_tast.py
runner.py
setup_cros_bisect.py
switch_arc_localbuild.py
switch_arc_prebuilt.py
switch_autotest_prebuilt.py
switch_cros_cr_localbuild_internal.py
switch_cros_cr_localbuild_master.py
switch_cros_localbuild.py
switch_cros_localbuild_buildbucket.py
switch_cros_prebuilt.py
switch_git.py
switch_lacros_prebuilt.py
switch_tast_prebuilt.py
"

# Dry run all executable scripts and make sure they can be loaded correctly.
for executable in $executable_list; do
    ./$executable --help > /dev/null
done
