#!/bin/bash -e
# Copyright 2018 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

path=$1
product_variant=$2
shift 2

cd $path
. build/envsetup.sh

export USE_GOMA=true
export USE_NINJA=true
lunch $product_variant

# set verbose since here because "lunch" is too verbose.
set -x
"$@"
