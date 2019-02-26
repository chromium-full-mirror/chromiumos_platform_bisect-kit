#!/bin/sh -ex
# Copyright 2018 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
[ -n "$CROS_WORKON_SRCROOT" ]  # should be inside cros chroot

if [ -n "$1" ]; then
    BOARD="$1"
else
    echo "Usage: $0 board [image_name]"
    exit 1
fi

IMAGE_DIR=../build/images/$BOARD
if [ -n "$2" ]; then
    IMAGE_NAME="$2"
else
    IMAGE_NAME="latest"
fi

# delete dangling symlink
if [ -L "$IMAGE_DIR/$IMAGE_NAME" -a ! -e "$IMAGE_DIR/$IMAGE_NAME" ]; then
    rm "$IMAGE_DIR/$IMAGE_NAME"
fi

# See chromite's code for release build options and flags, especially following
# files:
# - chromite/config/chromeos_config.py
# - chromite/cbuildbot/stages/build_stages.py
# - chromite/cbuildbot/commands.py

# env variables from 'release' template. May be override by individial commands
# below.
# TODO(kcwu): afdo_use
export USE='-cros-debug chrome_internal'
export FEATURES='separatedebug'

FEATURES="$FEATURES -separatedebug splitdebug" \
./update_chroot \
    --toolchain_boards=$BOARD

./setup_board \
    --board=$BOARD \
    --accept_licenses=@CHROMEOS \
    --skip_chroot_upgrade

./build_packages \
    --board=$BOARD \
    --withdev \
    --noworkon \
    --skip_chroot_upgrade \
    --accept_licenses=@CHROMEOS

./build_image \
    --board=$BOARD \
    --noenable_rootfs_verification \
    test

if [ "$IMAGE_NAME" != "latest" ]; then
    ln -sf $(readlink $IMAGE_DIR/latest) $IMAGE_DIR/$IMAGE_NAME
fi
