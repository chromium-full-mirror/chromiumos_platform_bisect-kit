#!/usr/bin/env python3
# Copyright 2017 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Helper script to manipulate chromeos DUT or query info."""

from __future__ import annotations

import argparse
import asyncio
import collections.abc
import dataclasses
import functools
import json
import logging
import os
import random
import time

from bisect_kit import cli
from bisect_kit import common
from bisect_kit import cros_lab_util
from bisect_kit import cros_util
from bisect_kit import errors


DEFAULT_DUT_POOL = 'DUT_POOL_QUOTA'
logger = logging.getLogger(__name__)

models_to_avoid: dict[str, str] = {
    # model: reason
}


def cmd_version_info(opts):
    info = dataclasses.asdict(
        cros_util.query_version_info(opts.board, opts.version)
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
        raise Exception('unable to lease %s' % host)


def cmd_release_dut(opts):
    host = cros_lab_util.dut_host_name(opts.dut)
    if opts.enable_rootfs_verification:
        cros_lab_util.enable_rootfs_verification(host, opts.chromeos_root)
    cros_lab_util.crosfleet_release_dut(host)
    logger.info('%s released', host)


def _filter_dimensions_by_lab(
    dimensions: list[str],
    raise_unknown: bool = False,
) -> list[str]:
    dimensions = sorted(set(dimensions))
    result = []
    bots_dimensions = cros_lab_util.swarming_bots_dimensions()
    for dimension in dimensions:
        key, value = dimension.split(':', 1)
        if value in bots_dimensions.get(key, []):
            result.append(dimension)
            continue
        msg = f'dimension={dimension} is unknown in the lab, typo? ignored'
        if raise_unknown:
            raise Exception(msg)
        logger.warning(msg)
    return result


def _sample_available_bots(
    dimensions: list[str],
    variants: list[str],
    num: int = 1,
    filter_func: collections.abc.Callable | None = None,
    *,
    is_dead: bool | None = False,
    quarantined: bool | None = False,
    is_busy: bool | None = None,
    limit: int = 10,
) -> list[str]:
    """Query available bots by given constraints and randomly sample the result.

    1. Collect available bots of each variant in `variants` with
       the specified `dimensions`.
    2. Filter collected bots by the `filter_func`.
    3. Randomly sample (at most) `num` bots and return.

    Args:
        dimensions: a list of dimension constraints
        variants: a list of variant constraints
        num: sample (at most) `num` available bots
        filter_func: a boolean filter function will be applied before
            the sampling. Passing `None` means no filtering.

        id_dead: see `cros_lab_util.swarming_bots_list()`
        quarantined: see `cros_lab_util.swarming_bots_list()`
        is_busy: see `cros_lab_util.swarming_bots_list()`
        limit: see `cros_lab_util.swarming_bots_list()`
    Returns: a list of available bots
    """
    bots = []
    for variant in variants:
        # There might be thousand bots available, set 'limit' to reduce swarming
        # API cost. This is not uniform random, but should be good enough.
        bots += cros_lab_util.swarming_bots_list(
            dimensions + [variant],
            is_dead=is_dead,
            quarantined=quarantined,
            is_busy=is_busy,
            limit=limit,
        )

    if filter_func:
        bots = list(filter(filter_func, bots))
    return random.sample(bots, min(num, len(bots)))


def _sample_available_bots_with_retry(
    dimensions: list[str],
    variants: list[str],
    num: int,
    filter_func: collections.abc.Callable | None = None,
    max_attempts: int = 5,
) -> list[str]:
    """Query bots using the _sample_available_bots() with retries.

    In each attemption, the function will try to collect non-busy bots first.
    If there is no available bots, then try to qeury without the non-busy
    constraint.

    Args:
        dimensions: a list of dimension constraints
        variants: a list of variant constraints
        num: sample (at most) `num` available bots
        filter_func: a boolean filter function will be applied before
            the sampling. Passing `None` means no filtering.
        max_attempts: Retry the query at most `max_attempts` times.
    Returns: a list of available bots
    """

    sample_func = functools.partial(
        _sample_available_bots,
        dimensions,
        variants,
        num,
        filter_func,
    )

    if max_attempts <= 0:
        raise ValueError('max_attempts must be at least 1')

    fail_count = 0
    while True:
        # Query bots that satisfy is_busy=False
        if bots := sample_func(is_busy=False):
            return bots
        # If there is no idle bots, remove the is_busy filter and query again
        if bots := sample_func(is_busy=None):
            return bots

        # log all the DUTs for debugging
        sample_func(is_dead=None, quarantined=None, is_busy=None)

        fail_count += 1
        if fail_count >= max_attempts:
            raise errors.NoDutAvailable(
                'no bots satisfy constraints; all are in maintenance state?'
            )
        # sleep 2^fail_count minutes before next try
        time.sleep(60 * (2**fail_count))


def _filter_dimensions_by_board(
    boards_with_prebuilt: list[str] | None,
    dimensions: list[str],
    pool: str | None,
) -> list[str]:
    result = set()
    for dimension in dimensions:
        constraints = [dimension]
        if pool:
            constraints.append('label-pool:' + pool)
        # Ideally, we need only one result. Here we get more (limit=10) in order to
        # cope with incorrect metadata in swarming database (b/196499726).
        bots = cros_lab_util.swarming_bots_list(
            constraints, is_busy=None, limit=10
        )
        for bot in bots:
            board = bot['dimensions']['label-board'][0]
            if board not in boards_with_prebuilt:
                logger.warning(
                    'dimension=%s (board=%s) does not have corresponding '
                    'prebuilt image, ignore',
                    dimension,
                    board,
                )
                continue
            result.add(dimension)
    return list(result)


def _is_acceptable_bot(
    boards_with_prebuilt: list[str] | None,
    bot: dict,
) -> bool:
    model = bot['dimensions']['label-model'][0]
    if model in models_to_avoid:
        logger.warning(
            'model=%s is bad (reason:%s), ignore', model, models_to_avoid[model]
        )
        return False

    if boards_with_prebuilt is None:
        return True

    # Sometimes swarming database has inconsistent records. For example,
    # label-model=kefka + label-board=strago are incorrect (should be
    # label-board=kefka). It is probably human errors (strago is kefka's
    # reference board).
    board = bot['dimensions']['label-board'][0]
    if board not in boards_with_prebuilt:
        dut_name = bot['dimensions']['dut_name'][0]
        logger.warning('%s has unexpected board=%s, ignore', dut_name, board)
        return False

    return True


async def lease_dut_parallelly(duration, bots, reason, timeout=None):
    tasks = []
    hosts = []
    for bot in bots:
        host = bot['dimensions']['dut_name'][0]
        hosts.append(host)
        tasks.append(
            asyncio.create_task(
                cros_lab_util.async_lease_with_retry(
                    host, reason, duration=duration
                )
            )
        )

    try:
        logger.info('trying to lease %d DUTs: %s', len(hosts), hosts)
        for coro in asyncio.as_completed(tasks, timeout=timeout):
            host = await coro
            if host:
                logger.info('leased %s', host)
                # Unfinished lease tasks will be cancelled when asyncio.run is
                # finishing.
                return host
        return None
    except asyncio.TimeoutError:
        return None


def normalize_board_name(chromeos_root, board):
    """Normalize BOARD name.

    Here, we want to find the actual device board. Suffixes like -kernelnext will
    be removed. So we can use that name to query DUTs inside the lab.

    Args:
      chromeos_root: chromeos source root
      board: BOARD name

    Returns:
      normalized BOARD name
    """
    overlays = cros_util.parse_chromeos_overlays(chromeos_root)
    boards_info = cros_util.resolve_basic_boards(overlays)
    return boards_info[board]


def do_allocate_dut(opts: argparse.Namespace | DutAllocateSpec):
    """Helper of cmd_allocate_dut.

    Returns:
      (todo, host, board_to_build)
        todo: 'ready' or 'wait'
        host: leased host name
        board_to_build: board name for building image
    """
    if opts.dut_name:
        # If dut_name is specified, pool is ignored.
        opts.pool = None

        if cros_lab_util.is_satlab_dut(opts.dut_name):
            cros_lab_util.write_satlab_ssh_config(opts.dut_name)
        if not opts.builder_hint:
            opts.builder_hint = cros_util.query_dut_board(opts.dut_name)
    elif not opts.pool:
        raise errors.ArgumentError(
            '--pool', 'need to be specified if not --dut_name'
        )
    if opts.version_hint:
        for v in opts.version_hint.split(','):
            if cros_util.is_cros_version(
                v
            ) or cros_util.is_cros_snapshot_version(v):
                continue
            raise errors.ArgumentError(
                '--version_hint',
                'should be ChromeOS version numbers, separated by comma',
            )
    if opts.duration is not None and opts.duration < 60:
        raise errors.ArgumentError('--duration', 'must be at least 60 seconds')

    if opts.satlab_ip:
        if not cros_lab_util.is_satlab_dut(opts.dut_name):
            raise errors.ArgumentError(
                '--satlab_ip', 'Should be only provided with a satlab dut'
            )

        cros_lab_util.write_satlab_ssh_config(opts.dut_name, opts.satlab_ip)

    t0 = time.time()
    dimensions = [f'{key}:{value}' for key, value in opts.dimensions]
    dimensions.append('dut_state:ready')
    if opts.pool:
        dimensions.append('label-pool:' + opts.pool)
    dimensions = _filter_dimensions_by_lab(dimensions, raise_unknown=True)

    # Note: board, model, sku, and dut_name are mutual exclusive so at most one
    # of them is present.
    variants: list[str] = []
    if opts.board:
        for board in opts.board.split(','):
            variants.append(
                'label-board:' + normalize_board_name(opts.chromeos_root, board)
            )
        variants = _filter_dimensions_by_lab(variants)
        if not variants:
            raise errors.NoDutAvailable(
                'Invalid constraints: no board=%s in the lab' % opts.board
            )

    if opts.model:
        for model in opts.model.split(','):
            if model in models_to_avoid:
                logger.warning(
                    'model=%s is bad (reason:%s), ignore',
                    model,
                    models_to_avoid[model],
                )
                continue
            variants.append('label-model:' + model)
        if not variants:
            raise errors.ArgumentError(
                '--model', 'all specified models are not supported'
            )
        variants = _filter_dimensions_by_lab(variants)
        if not variants:
            raise errors.NoDutAvailable(
                'Invalid constraints: no model=%s in the lab' % opts.model
            )

    if opts.sku:
        for sku in opts.sku.split(','):
            variants.append(
                'label-hwid_sku:' + cros_lab_util.normalize_sku_name(sku)
            )
        variants = _filter_dimensions_by_lab(variants)
        if not variants:
            raise errors.NoDutAvailable(
                'Invalid constraints: no sku=%s in the lab' % opts.sku
            )

    if opts.dut_name:
        for dut_name in opts.dut_name.split(','):
            variants.append('dut_name:' + dut_name)
        variants = _filter_dimensions_by_lab(variants)
        if not variants:
            raise errors.NoDutAvailable(
                'Invalid constraints: no dut_name=%s in the lab' % opts.dut_name
            )

    # Filter variants by prebuilt images.
    boards_with_prebuilt: list[str] | None = None
    if opts.version_hint:
        if not opts.builder_hint:
            opts.builder_hint = opts.board
        if not opts.builder_hint:
            raise errors.ArgumentError(
                '--builder_hint', 'must be specified along with --version_hint'
            )
        boards_with_prebuilt = []
        versions = opts.version_hint.split(',')
        for builder in opts.builder_hint.split(','):
            if not all(cros_util.has_test_image(builder, v) for v in versions):
                logger.warning(
                    'builder=%s does not have prebuilt test image for %s, ignore',
                    builder,
                    opts.version_hint,
                )
                continue
            boards_with_prebuilt.append(
                normalize_board_name(opts.chromeos_root, builder)
            )
        logger.info('boards with prebuilt: %s', boards_with_prebuilt)
        if not boards_with_prebuilt:
            raise errors.ArgumentError(
                '--version_hint',
                'given builders have no prebuilt for %s' % opts.version_hint,
            )
        filtered = _filter_dimensions_by_board(
            boards_with_prebuilt, variants, None
        )
        if not filtered:
            raise errors.NoDutAvailable(
                'Devices with specified constraints (%s) have no prebuilt. '
                'Wrong constraints or wrong version number?' % variants
            )
        variants = filtered
        filtered = _filter_dimensions_by_board(
            boards_with_prebuilt, variants, opts.pool
        )
        if not filtered:
            raise errors.NoDutAvailable(
                'The lab has %s devices; but there are no such devices in pool=%s'
                % (variants, opts.pool)
            )
        variants = filtered

    filter_func = functools.partial(_is_acceptable_bot, boards_with_prebuilt)
    while True:
        # Query every time because each iteration takes a few minutes
        bots = _sample_available_bots_with_retry(
            dimensions, variants, opts.parallel, filter_func
        )

        remaining_time = opts.time_limit - (time.time() - t0)
        if remaining_time <= 0:
            break
        timeout = min(120, remaining_time)
        reason = cros_lab_util.make_lease_reason(opts.session)
        host = asyncio.run(
            lease_dut_parallelly(opts.duration, bots, reason, timeout)
        )
        if host:
            if cros_lab_util.is_satlab_dut(host):
                cros_lab_util.write_satlab_ssh_config(host)
            # Resolve what board we should build during bisection.
            board_to_build = None
            bots = cros_lab_util.swarming_bots_list(['dut_name:' + host])
            host_board = bots[0]['dimensions']['label-board'][0]
            if opts.builder_hint:
                for builder in opts.builder_hint.split(','):
                    if (
                        normalize_board_name(opts.chromeos_root, builder)
                        == host_board
                    ):
                        board_to_build = builder
                        break
                else:
                    raise errors.DutLeaseException(
                        'DUT with unexpected board:%s' % host_board
                    )
            else:
                board_to_build = host_board

            return 'ready', host, board_to_build
        time.sleep(1)

    logger.warning('unable to lease DUT in time limit')
    return 'wait', None, None


@dataclasses.dataclass
class DutAllocateSpec:
    """A class to keep parameters used to allocate DUTs."""

    pool: str | None
    dimensions: list | None
    board: str | None
    model: str | None
    sku: str | None
    dut_name: str | None
    satlab_ip: str | None
    version_hint: str | None
    builder_hint: str | None
    time_limit: int | None
    duration: float | None
    parallel: int | None
    chromeos_root: str | None
    session: str | None


def allocate_dut(opts: argparse.Namespace | DutAllocateSpec):
    leased_dut = None
    try:
        todo, host, board = do_allocate_dut(opts)
        leased_dut = cros_lab_util.dut_name_to_address(host) if host else None
        result = {'result': todo, 'leased_dut': leased_dut, 'board': board}
        print(json.dumps(result))
    except Exception as e:
        logger.exception('cmd_allocate_dut failed')
        exception_name = e.__class__.__name__
        result = {
            'result': 'failed',
            'exception': exception_name,
            'text': str(e),
        }
        print(json.dumps(result))
    if not leased_dut:
        return None
    return cros_lab_util.dut_host_name(leased_dut)


def cmd_allocate_dut(opts):
    allocate_dut(opts)  # Ignore the return value.


def cmd_repair_dut(opts):
    cros_lab_util.repair(opts.dut)


def add_allocate_dut_arguments(
    parser, allow_required: bool, flags_prefix: str = ''
):
    """Add DUT allocate related argument to |parser|.

    Args:
      parser: an argparse object which supports add_argument(), etc.
        It can be an argument parser, an argument group, or others alike.
      allow_required: whether any of the argument are allowed to be set as required.
        When it's False, all arguments should be optional.
      flags_prefix: a string to prefix all flags to avoid possible
        name collision with other flags. If empty, prefix nothing.
    """

    # All flags below are prepended with a prefix to avoid name collision
    # with other flags. Defaults to prepend an empty string.
    def flag_name(name):
        return '--%s%s' % (flags_prefix + '_' if flags_prefix else '', name)

    parser.add_argument(
        flag_name('pool'),
        # TODO(cylee): for possible backward compatibility, remove it after
        # ensuring no caller is using it.
        '--pool',
        help='Pool to search DUT (default: %(default)s)',
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
        flag_name('dut_name'),
        help='allocation criteria; comma separated',
    )

    parser.add_argument(
        flag_name('satlab_ip'),
        # TODO(cylee): for possible backward compatibility, remove it after
        # ensuring no caller is using it.
        '--satlab_ip',
        help='Optional, required only if the dut is a satlab dut',
        type=cli.argtype_re(r'\d+\.\d+\.\d+\.\d+', '172.30.251.188'),
    )
    parser.add_argument(
        flag_name('version_hint'),
        help='chromeos version; comma separated',
    )
    parser.add_argument(
        flag_name('builder_hint'),
        help='chromeos builder; comma separated',
    )
    # Pubsub ack deadline is 10 minutes (b/143663659). Default 9 minutes with 1
    # minute buffer.
    parser.add_argument(
        flag_name('time_limit'),
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
    parser = argparse.ArgumentParser()
    cli.patching_argparser_exit(parser)
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
        '--chromeos_root',
        type=cli.argtype_dir_path,
        default=os.environ.get(
            'DEFAULT_CHROMEOS_ROOT', common.DEFAULT_CHROMEOS_ROOT
        ),
        help='ChromeOS source tree, for overlay data (default: %(default)s)',
    )
    parser_release_dut.add_argument(
        '--enable_rootfs_verification',
        dest='enable_rootfs_verification',
        action='store_true',
        help="Enable rootfs verification when releasing the DUT",
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
    # --chromeos_root is already an argument of the latter in another
    # argument group, so it is left out here.
    parser_allocate_dut.add_argument(
        '--chromeos_root',
        type=cli.argtype_dir_path,
        default=os.environ.get(
            'DEFAULT_CHROMEOS_ROOT', common.DEFAULT_CHROMEOS_ROOT
        ),
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
