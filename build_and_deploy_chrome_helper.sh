#!/bin/sh -ex
# Copyright 2017 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
[ -n "$DUT" ]
[ -n "$SDK_BOARD" ]  # should be inside simplechrome shell

if [ -n "$1" ]; then
    TARGETS="$*"
else
    TARGETS="chrome chrome_sandbox nacl_helper"
fi

out_dir=out_${SDK_BOARD}/Release

# This step is redundant and harmless for recent chrome. Keep it for older
# versions.
./buildtools/linux64/gn gen $out_dir --args="$GN_ARGS"

./third_party/depot_tools/autoninja -C $out_dir $TARGETS

for TARGET in $TARGETS; do
    case "$TARGET" in
        chrome_sandbox|nacl_helper)
            # do nothing; they will be deployed along with 'chrome'
            ;;
        chrome)
            # --force to skip prompt and remove rootfs verification
            # automatically
            deploy_chrome --force --build-dir=$out_dir --to=$DUT
            ;;
        *_unittest|*_unittests)
            chmod o+x $out_dir/$TARGET
            rsync $out_dir/$TARGET \
            $DUT:/usr/local/autotest/deps/chrome_test/test_src/out/Release/
            ;;
        *)
            echo "Unknown target $TARGET. Please extend $0 script " \
                "to handle them"
            exit -1
            ;;
    esac
done

