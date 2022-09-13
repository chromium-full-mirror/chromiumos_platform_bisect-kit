#!/bin/sh
# Copyright 2017 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

# old
export DIALOG_OK=0
# new
export DIALOG_EXTRA=1
# skip
export DIALOG_CANCEL=125
# abort
export DIALOG_ESC=128

TEXT="$BISECT_REV
What is current behavior?

[old]: current behavior is as same as old version

[new]: current behavior is as same as new version

[skip]: this version is neither old nor new, or unable to tell temporarily.

If you want to abort bisection, press [Esc] key.
"

# show dialog on tty, because bisector will capture and hide stdout and stderr.
export DIALOG_TTY=1
dialog --yes-label old --extra-button --extra-label new --no-label skip  \
    --yesno "$TEXT" 20 60
