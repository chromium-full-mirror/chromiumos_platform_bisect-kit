#!/usr/bin/env python3
# Copyright 2023 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Helper functions for DUT allocation."""

from __future__ import annotations

import asyncio
import collections.abc
import dataclasses
import functools
import logging
import random
import time

from bisect_kit import core
from bisect_kit import cros_lab_util
from bisect_kit import cros_util
from bisect_kit import errors


logger = logging.getLogger(__name__)


_models_to_avoid: dict[str, str] = {
    # model: reason
}


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
    if model in _models_to_avoid:
        logger.warning(
            'model=%s is bad (reason:%s), ignore',
            model,
            _models_to_avoid[model],
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


def _normalize_board_name(chromeos_root, board):
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


def _do_allocate_dut(spec: core.DutAllocateSpec):
    """Helper of cmd_allocate_dut.

    Returns:
      (todo, host, board_to_build)
        todo: 'ready' or 'wait'
        host: leased host name
        board_to_build: board name for building image
    """
    if spec.dut_name:
        # If dut_name is specified, pool is ignored.
        spec.pool = None

        if cros_lab_util.is_satlab_dut(spec.dut_name):
            cros_lab_util.write_satlab_ssh_config(spec.dut_name)
        if not spec.builder_hint:
            spec.builder_hint = cros_util.query_dut_board(spec.dut_name)
    elif not spec.pool:
        raise errors.ArgumentError(
            '--pool', 'need to be specified if not --dut_name'
        )
    if spec.version_hint:
        for v in spec.version_hint.split(','):
            if cros_util.is_cros_version(
                v
            ) or cros_util.is_cros_snapshot_version(v):
                continue
            raise errors.ArgumentError(
                '--version_hint',
                'should be ChromeOS version numbers, separated by comma',
            )
    if spec.duration is not None and spec.duration < 60:
        raise errors.ArgumentError('--duration', 'must be at least 60 seconds')

    if spec.satlab_ip:
        if not cros_lab_util.is_satlab_dut(spec.dut_name):
            raise errors.ArgumentError(
                '--satlab_ip', 'Should be only provided with a satlab dut'
            )

        cros_lab_util.write_satlab_ssh_config(spec.dut_name, spec.satlab_ip)

    t0 = time.time()
    dimensions = [f'{key}:{value}' for key, value in spec.dimensions]
    dimensions.append('dut_state:ready')
    if spec.pool:
        dimensions.append('label-pool:' + spec.pool)
    dimensions = _filter_dimensions_by_lab(dimensions, raise_unknown=True)

    # Note: board, model, sku, and dut_name are mutual exclusive so at most one
    # of them is present.
    variants: list[str] = []
    if spec.board:
        for board in spec.board.split(','):
            variants.append(
                'label-board:'
                + _normalize_board_name(spec.chromeos_root, board)
            )
        variants = _filter_dimensions_by_lab(variants)
        if not variants:
            raise errors.NoDutAvailable(
                'Invalid constraints: no board=%s in the lab' % spec.board
            )

    if spec.model:
        for model in spec.model.split(','):
            if model in _models_to_avoid:
                logger.warning(
                    'model=%s is bad (reason:%s), ignore',
                    model,
                    _models_to_avoid[model],
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
                'Invalid constraints: no model=%s in the lab' % spec.model
            )

    if spec.sku:
        for sku in spec.sku.split(','):
            variants.append(
                'label-hwid_sku:' + cros_lab_util.normalize_sku_name(sku)
            )
        variants = _filter_dimensions_by_lab(variants)
        if not variants:
            raise errors.NoDutAvailable(
                'Invalid constraints: no sku=%s in the lab' % spec.sku
            )

    if spec.dut_name:
        for dut_name in spec.dut_name.split(','):
            variants.append('dut_name:' + dut_name)
        variants = _filter_dimensions_by_lab(variants)
        if not variants:
            raise errors.NoDutAvailable(
                'Invalid constraints: no dut_name=%s in the lab' % spec.dut_name
            )

    # Filter variants by prebuilt images.
    boards_with_prebuilt: list[str] | None = None
    if spec.version_hint:
        if not spec.builder_hint:
            spec.builder_hint = spec.board
        if not spec.builder_hint:
            raise errors.ArgumentError(
                '--builder_hint', 'must be specified along with --version_hint'
            )
        boards_with_prebuilt = []
        versions = spec.version_hint.split(',')
        for builder in spec.builder_hint.split(','):
            if not all(cros_util.has_test_image(builder, v) for v in versions):
                logger.warning(
                    'builder=%s does not have prebuilt test image for %s, ignore',
                    builder,
                    spec.version_hint,
                )
                continue
            boards_with_prebuilt.append(
                _normalize_board_name(spec.chromeos_root, builder)
            )
        logger.info('boards with prebuilt: %s', boards_with_prebuilt)
        if not boards_with_prebuilt:
            raise errors.ArgumentError(
                '--version_hint',
                'given builders have no prebuilt for %s' % spec.version_hint,
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
            boards_with_prebuilt, variants, spec.pool
        )
        if not filtered:
            raise errors.NoDutAvailable(
                'The lab has %s devices; but there are no such devices in pool=%s'
                % (variants, spec.pool)
            )
        variants = filtered

    filter_func = functools.partial(_is_acceptable_bot, boards_with_prebuilt)
    while True:
        # Query every time because each iteration takes a few minutes
        bots = _sample_available_bots_with_retry(
            dimensions, variants, spec.parallel, filter_func
        )

        remaining_time = spec.time_limit - (time.time() - t0)
        if remaining_time <= 0:
            break
        timeout = min(120, remaining_time)
        reason = cros_lab_util.make_lease_reason(spec.session)
        async_dut_leaser = cros_lab_util.AsyncDutLeaser(
            [bot['dimensions']['dut_name'][0] for bot in bots],
            reason,
            spec.duration,
            timeout,
        )
        host = asyncio.run(
            async_dut_leaser.lease_parallelly(),
        )
        if host:
            if cros_lab_util.is_satlab_dut(host):
                cros_lab_util.write_satlab_ssh_config(host)
            # Resolve what board we should build during bisection.
            board_to_build = None
            bots = cros_lab_util.swarming_bots_list(['dut_name:' + host])
            host_board = bots[0]['dimensions']['label-board'][0]
            if spec.builder_hint:
                for builder in spec.builder_hint.split(','):
                    if (
                        _normalize_board_name(spec.chromeos_root, builder)
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
class AllocateDutResult:
    """Result returned by allocate_dut."""

    result: str | None = None
    leased_dut: str | None = None
    board: str | None = None
    exception: str | None = None
    text: str | None = None


def allocate_dut(spec: core.DutAllocateSpec) -> AllocateDutResult:
    result = AllocateDutResult()
    try:
        todo, host, board = _do_allocate_dut(spec)
        result.result = todo
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
