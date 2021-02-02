# -*- coding: utf-8 -*-
# Copyright 2021 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""ChromeOS quick provision helper.

Use to provision a DUT using quick-provision script safely.

TODO(kimjae): Transition to using TLS ProvisionDut for F20.
"""

import logging
import os
import re
import socket

from bisect_kit import cros_lab_util
from bisect_kit import cros_util
from bisect_kit import errors
from bisect_kit import util

logger = logging.getLogger(__name__)


_QUICK_PROVISION_SCRIPT = 'quick-provision'
re_chromeos_build= r'^[a-zA-Z-]+\/R\d+-[\.\d-]+$'


def argtype_cros_build(s):
  if not bool(re.match(re_chromeos_build, s)):
    msg = 'invalid cros build'
    raise cli.ArgTypeError(
        msg, 'eve-release/R90-13766.0.0, eve-cq/R90-13766.0.0-XXX-XXX, etc')
  return s


def quick_provision(chromeos_root, host, build):
  """Runs the quick-provision script on the device and
  returns the device in a good state in case of errors.

  Args:
    chromeos_root: chromeos tree root
    host: DUT name/address
    build: build to provision to

  Raises:
    errors.ExternalError: quick-provision fails
  """
  logger.info("Starting quick-provision")
  autotest_path = os.path.join(cros_util.chromeos_root_inside_chroot,
                               'src/third_party/autotest/files')
  quick_provision_cmd = ['test_that',
                         '--args="value=\'%s\'"' % build,
                         host,
                         'provision_QuickProvision',
                         '--autotest_dir',
                         autotest_path]
  try:
    cros_util.cros_sdk(chromeos_root, *quick_provision_cmd)
  except subprocess.CalledProcessError as e:
    raise errors.ExternalError('quick-provision failed') from e
