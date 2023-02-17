# Copyright 2019 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

import pathlib
import sys

from bisect_kit import configure


BISECT_KIT_ROOT = pathlib.Path(__file__).resolve().parent.parent

# grpc, buildbucket protobuf modules
sys.path.append(str(BISECT_KIT_ROOT / 'third_party'))

configure.load_plugins(configure.load_config().iter_plugins())
