#!/usr/bin/env python3
# Copyright 2021 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Eval for ChromeOS prebuilt to handle bad images"""

import logging
import sys

from bisect_kit import cli
from bisect_kit import common
from bisect_kit import cros_lab_util
from bisect_kit import cros_util
import switch_cros_prebuilt


logger = logging.getLogger(__name__)

OLD = 'old'
NEW = 'new'
FATAL = 'fatal'

EXIT_CODE_MAP = {
    OLD: cli.EXIT_CODE_OLD,
    NEW: cli.EXIT_CODE_NEW,
    FATAL: cli.EXIT_CODE_FATAL,
}


def evaluate(opts):
    switch_cros_prebuilt.inner_main(opts)

    if not cros_util.is_good_dut(opts.dut):
        return NEW

    # Need to check if the DUT actually flashed with requested version.
    short_version = opts.version
    if not cros_util.is_cros_short_version(opts.version):
        _, short_version = cros_util.version_split(opts.version)
    if cros_util.query_dut_short_version(opts.dut) != short_version:
        return NEW

    return OLD


@cli.fatal_error_handler
def main(args=None):
    opts = switch_cros_prebuilt.parse_args(args)
    common.config_logging(opts)

    if cros_lab_util.is_satlab_dut(opts.dut):
        cros_lab_util.write_satlab_ssh_config(opts.dut)

    try:
        logger.info('Starting evaluate')
        returncode = evaluate(opts)
        # TODO(kimjae/kcwu): Add fail to pass, not urgent.
    except Exception:
        logger.exception('evaluate failed')
        returncode = FATAL

    # Whether success or not, DUT must be returned in a good state.
    if not cros_util.is_good_dut(opts.dut):
        logger.fatal('%r is not a good DUT, going to repair', opts.dut)
        if not cros_util.repair_dut(opts.default_chromeos_root, opts.dut):
            returncode = FATAL

    logger.info('done with evaluate')
    sys.exit(EXIT_CODE_MAP[returncode])


if __name__ == '__main__':
    main()
