#!/bin/sh -ex
# Copyright 2018 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
[ -n "$DUT" ]
[ -n "$SDK_BOARD" ]  # should be inside simplechrome shell

if [ -n "$1" ]; then
    TARGETS="$*"
else
    TARGETS="chrome nacl_helper"
fi

if [ -n "${OUT_DIR}" ]; then
    out_dir=${OUT_DIR}
else
    out_dir=out_${SDK_BOARD}/Release
fi

AUTOTEST_TEST_BINARY_PATH=\
/usr/local/autotest/deps/chrome_test/test_src/out/Release/
TAST_TEST_BINARY_PATH=/usr/local/libexec/chrome-binary-tests/

for TARGET in $TARGETS; do
    case "$TARGET" in
        nacl_helper)
            # do nothing; they will be deployed along with 'chrome'
            ;;
        chrome)
            # --force to skip prompt and remove rootfs verification
            # automatically
            # Use --mount so the binary is installed under the stateful
            # partition to avoid running out of disk space (b/343934210).
            deploy_chrome --force --mount --build-dir=$out_dir --device=$DUT
            ;;
        *test\
          |*tests\
          |*decoder\
          |fake_dmserver\
          |icudtl.dat\
          |libtest_trace_processor.so\
          |wayland_hdr_client)
            chmod o+x $out_dir/$TARGET
            if ssh $DUT [ ! -d $AUTOTEST_TEST_BINARY_PATH ]; then
                echo "Warning: $AUTOTEST_TEST_BINARY_PATH does not exist" >&2
                ssh $DUT mkdir -p $AUTOTEST_TEST_BINARY_PATH
            fi
            rsync $out_dir/$TARGET $DUT:$AUTOTEST_TEST_BINARY_PATH
            rsync $out_dir/$TARGET $DUT:$TAST_TEST_BINARY_PATH
            ;;
        *)
            echo "Unknown target $TARGET. Please extend $0 script " \
                "to handle them"
            exit -1
            ;;
    esac
done
