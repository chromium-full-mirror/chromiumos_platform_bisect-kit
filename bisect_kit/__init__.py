# Copyright 2019 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

import pathlib
import sys


BISECT_KIT_ROOT = pathlib.Path(__file__).resolve().parent.parent
PLATFORM_ROOT = BISECT_KIT_ROOT.parent

# grpc, buildbucket protobuf modules
sys.path.append(str(BISECT_KIT_ROOT / 'third_party'))
# plugins
sys.path.append(str(PLATFORM_ROOT / 'bisect-kit-internal'))


try:
    # load plugins
    from internal import internal  # type: ignore
except ImportError:
    pass
