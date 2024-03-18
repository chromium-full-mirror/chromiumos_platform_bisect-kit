#!/usr/bin/env python3
# Copyright 2023 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Evaluate if a DUT is bootable"""

import logging
import os
import sys

from bisect_kit import bisector_cli
from bisect_kit import cli
from bisect_kit import common
from bisect_kit import core
from bisect_kit import cros_lab_util
from bisect_kit import cros_util
from bisect_kit import errors


logger = logging.getLogger(__name__)


def create_argument_parser():
    parents = [cli.create_session_optional_parser()]
    parser = cli.ArgumentParser(description=__doc__, parents=parents)
    parser.add_argument(
        '--rich-result',
        action='store_true',
        help='Instead of mere exit code, output detailed information in json',
    )
    parser.add_argument(
        'dut',
        nargs='?',
        type=cli.argtype_notempty,
        metavar='DUT',
        default=os.environ.get('DUT', ''),
    )

    return parser


def is_dut_bootable(dut: str) -> bool:
    """Check if a DUT is bootable

    Args:
        dut: dut host name

    Returns:
        True if the DUT is bootable, False otherwise
    """
    try:
        cros_util.reboot(dut)
        return True
    except (errors.SshConnectionError, errors.ExternalError):
        return False


def step_main(args: tuple[str] | None) -> core.StepResult:
    parser = create_argument_parser()
    opts = parser.parse_args(args)
    common.config_logging(opts)

    if cros_lab_util.is_satlab_dut(opts.dut):
        cros_lab_util.write_satlab_ssh_config(opts.dut)

    if not cros_util.is_dut(opts.dut):
        raise errors.BrokenDutException(
            '%r is not a valid DUT address' % opts.dut
        )

    if not cros_util.is_good_dut(opts.dut):
        logger.fatal('%r is not a good DUT', opts.dut)
        if not cros_lab_util.repair(opts.dut, opts.chromeos_root):
            raise errors.BrokenDutException('%r is not a good DUT' % opts.dut)

    if is_dut_bootable(opts.dut):
        return core.StepResult('old', 'DUT is bootable')
    return core.StepResult('new', 'DUT is not bootable')


def action() -> bisector_cli.EvalAction:
    return bisector_cli.EvalAction.WITH_DUT


def main(args: tuple[str] | None = None) -> int:
    return bisector_cli.step_main_wrapper(step_main, args)


if __name__ == '__main__':
    sys.exit(main())
