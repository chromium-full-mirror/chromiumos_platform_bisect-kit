#!/usr/bin/env python3
# Copyright 2017 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Helper script to manipulate chromeos DUT or query info."""

from __future__ import annotations

import dataclasses
import json
import logging

from bisect_kit import cli
from bisect_kit import common
from bisect_kit import cros_lab_util
from bisect_kit import cros_util
from bisect_kit import dut_allocate_spec
from bisect_kit import dut_allocator
from bisect_kit import errors


DEFAULT_DUT_POOL = 'DUT_POOL_QUOTA'
logger = logging.getLogger(__name__)


def cmd_version_info(opts):
    info = dataclasses.asdict(
        cros_util.query_version_info(
            opts.board, opts.version, is_public_build=opts.public_build
        )
    )
    if opts.name:
        if opts.name not in info:
            logger.error('unknown name=%s', opts.name)
        print(info[opts.name])
    else:
        print(json.dumps(info, sort_keys=True, indent=4))


def cmd_query_dut_board(opts):
    assert cros_util.is_dut(opts.dut)
    print(cros_util.query_dut_board(opts.dut))


def cmd_reboot(opts):
    if not cros_util.is_dut(opts.dut):
        if opts.force:
            logger.warning('%s is not a ChromeOS device?', opts.dut)
        else:
            raise errors.ArgumentError(
                'dut', 'not a ChromeOS device (--force to continue)'
            )

    cros_util.reboot(
        opts.dut, force_reboot_callback=cros_lab_util.reboot_via_servo
    )


def cmd_lease_dut(opts):
    if opts.duration is not None and opts.duration < 60:
        raise errors.ArgumentError('--duration', 'must be at least 60 seconds')
    reason = opts.reason or cros_lab_util.make_lease_reason(opts.session)
    host = cros_lab_util.dut_host_name(opts.dut)
    logger.info('trying to lease %s', host)
    if cros_lab_util.crosfleet_lease_dut(host, reason, opts.duration):
        logger.info('leased %s', host)
    else:
        raise errors.DutLeaseException('unable to lease %s' % host)


def cmd_release_dut(opts):
    host = cros_lab_util.dut_host_name(opts.dut)
    if opts.enable_rootfs_verification:
        cros_lab_util.enable_rootfs_verification(host, opts.chromeos_root)
    cros_lab_util.crosfleet_release_dut(host)
    logger.info('%s released', host)


@dataclasses.dataclass
class AllocateDutResult:
    """Result of allocate_dut."""

    result: str | None = None
    leased_dut: str | None = None
    board: str | None = None
    exception: str | None = None
    text: str | None = None


def allocate_dut(spec: dut_allocate_spec.DutAllocateSpec, chromeos_root: str):
    result = AllocateDutResult()
    try:
        host, board = dut_allocator.allocate_dut(spec, chromeos_root)
        result.result = 'ready'
        if host:
            result.leased_dut = cros_lab_util.dut_name_to_address(host)
        result.board = board
        logger.debug('dut allocated: %s', result)
    except Exception as e:
        logger.exception('cmd_allocate_dut failed')
        result.result = 'failed'
        result.exception = e.__class__.__name__
        result.text = str(e)
        logger.warning('failed to allocate dut: %s', result)
    return result


def cmd_allocate_dut(opts):
    spec = dut_allocate_spec.parse_from_command_line_args(opts)
    result = allocate_dut(spec, opts.chromeos_root)
    print(json.dumps(dataclasses.asdict(result)))


def cmd_repair_dut(opts):
    cros_lab_util.repair(opts.dut, opts.chromeos_root)


def add_allocate_dut_arguments(
    parser, allow_required: bool, flags_prefix: str = ''
):
    """Add DUT allocate related argument to |parser|.

    Args:
      parser: an ArgumentParser object which supports add_argument(), etc.
        It can be an argument parser, an argument group, or others alike.
      allow_required: whether any of the argument are allowed to be set as required.
        When it's False, all arguments should be optional.
      flags_prefix: a string to prefix all flags to avoid possible
        name collision with other flags. If empty, prefix nothing.
    """

    # All flags below are prepended with a prefix to avoid name collision
    # with other flags. Defaults to prepend an empty string.
    def flag_name(name):
        return '--%s%s' % (flags_prefix + '-' if flags_prefix else '', name)

    parser.add_argument(
        flag_name('pool'),
        # TODO(cylee): for possible backward compatibility, remove it after
        # ensuring no caller is using it.
        '--pool',
        help='Pools to search DUT (default: %(default)s); comma separated',
        default=DEFAULT_DUT_POOL,
    )
    parser.add_argument(
        flag_name('dimensions'),
        help='Dimension filters used for searching DUTs, in format '
        'key1=val1 key2=val2 ...',
        type=cli.argtype_key_value,
        nargs='+',
        default=[],
    )

    group = parser.add_mutually_exclusive_group(required=allow_required)
    group.add_argument(
        flag_name('board'),
        help='allocation criteria; comma separated',
    )
    group.add_argument(
        flag_name('model'),
        # TODO(cylee): for possible backward compatibility, remove it after
        # ensuring no caller is using it.
        '--model',
        help='allocation criteria; comma separated',
    )
    group.add_argument(
        flag_name('sku'),
        # TODO(cylee): for possible backward compatibility, remove it after
        # ensuring no caller is using it.
        '--sku',
        help='allocation criteria; comma separated',
    )
    group.add_argument(
        flag_name('dut-name'),
        help='allocation criteria;',
    )

    parser.add_argument(
        flag_name('satlab-ip'),
        # TODO(cylee): for possible backward compatibility, remove it after
        # ensuring no caller is using it.
        '--satlab-ip',
        help='Optional, required only if the dut is a satlab dut',
        type=cli.argtype_re(r'\d+\.\d+\.\d+\.\d+', '172.30.251.188'),
    )
    parser.add_argument(
        flag_name('version-hint'),
        help='chromeos version; comma separated',
    )
    parser.add_argument(
        flag_name('builder-hint'),
        help='chromeos builder; comma separated',
    )
    # Pubsub ack deadline is 10 minutes (b/143663659). Default 9 minutes with 1
    # minute buffer.
    parser.add_argument(
        flag_name('time-limit'),
        type=int,
        default=9 * 60,
        help='Time limit to attempt lease in seconds (default: %(default)s)',
    )
    parser.add_argument(
        flag_name('duration'),
        type=float,
        help='lease duration in seconds; will be round to minutes',
    )
    parser.add_argument(
        flag_name('parallel'),
        type=int,
        default=1,
        help=(
            'The number of multiple lease tasks to speed up '
            '(default: %(default)d)'
        ),
    )


@cli.fatal_error_handler
def main():
    parents = [cli.create_session_optional_parser()]
    parser = cli.ArgumentParser()
    subparsers = parser.add_subparsers(
        dest='command', title='commands', metavar='<command>', required=True
    )

    parser_version_info = subparsers.add_parser(
        'version_info',
        help='Query version info of given chromeos build',
        parents=parents,
        description='Given chromeos `board` and `version`, '
        'print version information of components.',
    )
    parser_version_info.add_argument(
        'board', help='ChromeOS board name, like "samus".'
    )
    parser_version_info.add_argument(
        '--public-build',
        action='store_true',
        help='Use public build artifacts instead of internal ones.',
    )
    parser_version_info.add_argument(
        'version',
        type=cros_util.argtype_cros_version,
        help='ChromeOS version, like "9876.0.0" or "R62-9876.0.0"',
    )
    parser_version_info.add_argument(
        'name',
        nargs='?',
        help='Component name. If specified, output its version string. '
        'Otherwise output all version info as dict in json format.',
    )
    parser_version_info.set_defaults(func=cmd_version_info)

    parser_query_dut_board = subparsers.add_parser(
        'query_dut_board', help='Query board name of given DUT', parents=parents
    )
    parser_query_dut_board.add_argument('dut')
    parser_query_dut_board.set_defaults(func=cmd_query_dut_board)

    parser_reboot = subparsers.add_parser(
        'reboot',
        help='Reboot a DUT',
        parents=parents,
        description='Reboot a DUT and verify the reboot is successful.',
    )
    parser_reboot.add_argument('--force', action='store_true')
    parser_reboot.add_argument('dut')
    parser_reboot.set_defaults(func=cmd_reboot)

    parser_lease_dut = subparsers.add_parser(
        'lease_dut',
        help='Lease a DUT in the lab',
        parents=[cli.create_common_argument_parser()],
        description='Lease a DUT in the lab. '
        'This is implemented by `skylab lease-dut` with additional checking.',
    )
    group = parser_lease_dut.add_mutually_exclusive_group(required=True)
    group.add_argument('--session', help='session name')
    group.add_argument('--reason', help='specify lease reason manually')
    parser_lease_dut.add_argument('dut')
    parser_lease_dut.add_argument(
        '--duration',
        type=float,
        help='duration in seconds; will be round to minutes',
    )
    parser_lease_dut.set_defaults(func=cmd_lease_dut)

    parser_release_dut = subparsers.add_parser(
        'release_dut',
        help='Release a DUT in the lab',
        parents=parents,
        description='Release a DUT in the lab. '
        'This is implemented by `skylab release-dut` with additional checking.',
    )
    parser_release_dut.add_argument('dut')
    parser_release_dut.set_defaults(func=cmd_release_dut)
    parser_release_dut.add_argument(
        '--chromeos-root',
        type=cli.argtype_dir_path,
        default=common.get_default_chromeos_root(),
        help='ChromeOS source tree, for overlay data (default: %(default)s)',
    )
    parser_release_dut.add_argument(
        '--enable-rootfs-verification',
        dest='enable_rootfs_verification',
        action='store_true',
        help='Enable rootfs verification when releasing the DUT',
    )

    parser_allocate_dut = subparsers.add_parser(
        'allocate_dut',
        help='Allocate a DUT in the lab',
        parents=[cli.create_session_optional_parser()],
        description='Allocate a DUT in the lab. It will lease a DUT in the lab '
        'for bisecting. The caller (bisect-kit runner) of this command should '
        'retry this command again later if no DUT available now.',
    )
    add_allocate_dut_arguments(
        parser_allocate_dut,
        allow_required=True,
    )
    # add_allocate_dut_arguments() is shared by both
    # "cros_helper.py allocate_dut" and "diagnose_cros_*.py init".
    # --chromeos-root is already an argument of the latter in another
    # argument group, so it is left out here.
    parser_allocate_dut.add_argument(
        '--chromeos-root',
        type=cli.argtype_dir_path,
        default=common.get_default_chromeos_root(),
        help='ChromeOS source tree, for overlay data (default: %(default)s)',
    )
    parser_allocate_dut.set_defaults(func=cmd_allocate_dut)

    parser_repair_dut = subparsers.add_parser(
        'repair_dut',
        help='Repair a DUT in the lab',
        parents=parents,
        description='Repair a DUT in the lab. '
        'This is simply wrapper of "deploy repair" with additional checking.',
    )
    parser_repair_dut.add_argument('dut')
    parser_repair_dut.set_defaults(func=cmd_repair_dut)

    opts = parser.parse_args()
    common.config_logging(opts)
    opts.func(opts)


if __name__ == '__main__':
    main()
