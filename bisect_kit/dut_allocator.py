#!/usr/bin/env python3
# Copyright 2023 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Helper functions for DUT allocation."""

from __future__ import annotations

import asyncio
import collections.abc
import functools
import logging
import random
import threading
import time
import typing

from bisect_kit import bisect_db
from bisect_kit import cros_lab_util
from bisect_kit import cros_util
from bisect_kit import dut_allocate_spec
from bisect_kit import errors
from bisect_kit import shared_dut_pool


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
            raise errors.DutLeaseException(msg)
        logger.warning(msg)
    return result


def _sample_available_bots(
    dimensions: list[str],
    variants: list[str],
    pools: list[str] | None,
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
        pools: a list of pools. None means not to consider pools.
        num: sample (at most) `num` available bots
        filter_func: a boolean filter function will be applied before
            the sampling. Passing `None` means no filtering.

        id_dead: see `cros_lab_util.swarming_bots_list()`
        quarantined: see `cros_lab_util.swarming_bots_list()`
        is_busy: see `cros_lab_util.swarming_bots_list()`
        limit: see `cros_lab_util.swarming_bots_list()`
    Returns: a list of available bots
    """

    def fetch_bots(dimensions):
        logger.debug('fetch_bots: %s', dimensions)
        # There might be thousand bots available, set 'limit' to reduce swarming
        # API cost. This is not uniform random, but should be good enough.
        return cros_lab_util.swarming_bots_list(
            dimensions,
            is_dead=is_dead,
            quarantined=quarantined,
            is_busy=is_busy,
            limit=limit,
        )

    bots = []
    if not pools:
        for variant in variants:
            bots += fetch_bots(dimensions + [variant])
    else:
        for pool in pools:
            for variant in variants:
                bots += fetch_bots(dimensions + [variant, pool])

    if filter_func:
        bots = list(filter(filter_func, bots))
    return random.sample(bots, min(num, len(bots)))


def _sample_available_bots_with_retry(
    dimensions: list[str],
    variants: list[str],
    pools: list[str] | None,
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
        pools: a list of pools. None means not to consider pools.
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
        pools,
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
    pools: list[str] | None,
) -> list[str]:
    result = set()

    def sample_bots(constraints, result):
        # Ideally, we need only one result. Here we get more (limit=10) in order to
        # cope with incorrect metadata in swarming database (b/196499726).
        logger.debug('sample_bots: %s', constraints)
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

    for dimension in dimensions:
        constraints = [dimension]
        if pools:
            for pool in pools:
                sample_bots(constraints + [pool], result)
        else:
            sample_bots(constraints, result)
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


def allocate_dut(
    spec: dut_allocate_spec.DutAllocateSpec,
    chromeos_root: str,
    enable_shared_dut_pool=False,
    lease_from_shared_dut_pool=True,
) -> typing.Tuple[str, str]:
    """Helper of cmd_allocate_dut.

    Args:
      spec: the DutAllocateSpec
      enable_shared_dut_pool: whether to enable shared DUT pool.
      lease_from_shared_dut_pool: whether to lease from the shared DUT pool.
        In some cases (e.g., background leasing), we may want to lease from the
        lab directly.

    Note that enable_shared_dut_pool and lease_from_shared_dut_pool differs in that
    enable_shared_dut_pool is a transitional parameter. It can be removed after the
    shared DUT pool is fully launched. lease_from_shared_dut_pool is mandatory.

    Returns:
      (host, board_to_build)
        host: leased host name
        board_to_build: board name for building image

    Raises:
        errors.ArgumentError if the input DutAllocateSpec is invalid.
        errors.NoDutAvailable if the DutAllocateSpec could not match any device in lab.
        errors.DutLeaseTimeout if the DUT lease take too long.
        errors.DutLeaseException if other exception happens.
    """
    if spec.dut_name:
        # If dut_name is specified, pool is ignored.
        spec.pools = []

        if cros_lab_util.is_satlab_dut(spec.dut_name):
            cros_lab_util.write_satlab_ssh_config(spec.dut_name)
        if not spec.builder_hints:
            spec.builder_hints = [cros_util.query_dut_board(spec.dut_name)]
    elif not spec.pools:
        raise errors.ArgumentError(
            '--pool', 'need to be specified if not --dut-name'
        )
    if spec.version_hints:
        for v in spec.version_hints:
            if cros_util.is_cros_version(
                v
            ) or cros_util.is_cros_snapshot_version(v):
                continue
            raise errors.ArgumentError(
                '--version-hint',
                'should be ChromeOS version numbers, separated by comma',
            )
    if (
        spec.lease_duration_seconds is not None
        and spec.lease_duration_seconds < 60
    ):
        raise errors.ArgumentError('--duration', 'must be at least 60 seconds')

    if spec.satlab_ip:
        if not cros_lab_util.is_satlab_dut(spec.dut_name):
            raise errors.ArgumentError(
                '--satlab-ip', 'Should be only provided with a satlab dut'
            )

        cros_lab_util.write_satlab_ssh_config(spec.dut_name, spec.satlab_ip)

    t0 = time.time()
    dimensions = spec.dimensions[:]
    dimensions.append('dut_state:ready')
    dimensions = _filter_dimensions_by_lab(dimensions, raise_unknown=True)

    pools = None
    if spec.pools:
        pools = ['label-pool:%s' % p for p in spec.pools]
        pools = _filter_dimensions_by_lab(pools, raise_unknown=False)
        if not pools:
            raise errors.NoDutAvailable(
                'all specified pools "%s" are not available in lab' % spec.pools
            )

    # Note: board, model, sku, and dut_name are mutual exclusive so at most one
    # of them is present.
    variants: list[str] = []
    if spec.boards:
        for board in spec.boards:
            variants.append(
                'label-board:' + _normalize_board_name(chromeos_root, board)
            )
        variants = _filter_dimensions_by_lab(variants)
        if not variants:
            raise errors.NoDutAvailable(
                'Invalid constraints: no board=%s in the lab' % spec.boards
            )

    if spec.models:
        for model in spec.models:
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
                'Invalid constraints: no model=%s in the lab' % spec.models
            )

    if spec.skus:
        for sku in spec.skus:
            variants.append(
                'label-hwid_sku:' + cros_lab_util.normalize_sku_name(sku)
            )
        variants = _filter_dimensions_by_lab(variants)
        if not variants:
            raise errors.NoDutAvailable(
                'Invalid constraints: no sku=%s in the lab' % spec.skus
            )

    if spec.dut_name:
        variants.append('dut_name:' + spec.dut_name)
        variants = _filter_dimensions_by_lab(variants)
        if not variants:
            raise errors.NoDutAvailable(
                'Invalid constraints: no dut_name=%s in the lab' % spec.dut_name
            )

    # Filter variants by prebuilt images.
    boards_with_prebuilt: list[str] | None = None
    if spec.version_hints:
        if not spec.builder_hints:
            spec.builder_hints = spec.boards
        if not spec.builder_hints:
            raise errors.ArgumentError(
                '--builder-hint', 'must be specified along with --version-hint'
            )
        boards_with_prebuilt = []
        versions = spec.version_hints
        for builder in spec.builder_hints:
            if not all(cros_util.has_test_image(builder, v) for v in versions):
                logger.warning(
                    'builder=%s does not have prebuilt test image for %s, ignore',
                    builder,
                    spec.version_hints,
                )
                continue
            boards_with_prebuilt.append(
                _normalize_board_name(chromeos_root, builder)
            )
        logger.info('boards with prebuilt: %s', boards_with_prebuilt)
        if not boards_with_prebuilt:
            raise errors.ArgumentError(
                '--version-hint',
                'given builders have no prebuilt for %s' % spec.version_hints,
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
            boards_with_prebuilt, variants, pools
        )
        if not filtered:
            raise errors.NoDutAvailable(
                'The lab has %s devices; but there are no such devices in pool=%s'
                % (variants, spec.pools)
            )
        variants = filtered

    def resolve_builder_by_host(host: str) -> str:
        """Resolve the builder by the leased DUT.

        Since the dut allocate spec may contain multiple builders and boards,
        we need to know the corresponding builder by the leased board.
        """
        if cros_lab_util.is_satlab_dut(host):
            cros_lab_util.write_satlab_ssh_config(host)
        # Resolve what board we should build during bisection.
        board_to_build = None
        bots = cros_lab_util.swarming_bots_list(['dut_name:' + host])
        if not bots:
            raise errors.DutLeaseException(
                'unable to get dut info of %s when resolving builder' % host
            )
        try:
            host_board = bots[0]['dimensions']['label-board'][0]
        except (KeyError, IndexError) as e:
            raise errors.DutLeaseException(
                'malformed bots info %s' % bots
            ) from e

        if spec.builder_hints:
            for builder in spec.builder_hints:
                if _normalize_board_name(chromeos_root, builder) == host_board:
                    board_to_build = builder
                    break
            else:
                raise errors.DutLeaseException(
                    'DUT with unexpected board:%s' % host_board
                )
        else:
            board_to_build = host_board
        return board_to_build

    logger.debug(
        'enable_shared_dut_pool: %s, lease_from_shared_dut_pool: %s',
        enable_shared_dut_pool,
        lease_from_shared_dut_pool,
    )
    if enable_shared_dut_pool and lease_from_shared_dut_pool:
        pool_manager = shared_dut_pool.SharedDutPoolManager()
        host, orig_owner = pool_manager.lease_for_bisect(
            spec.session, dimensions, variants, pools
        )
        logger.info(
            'leased %s with original owner %s from the shared DUT pool',
            host,
            orig_owner,
        )
        if host is not None:
            if orig_owner != spec.session:
                # The bisect robs the DUT owned by |orig_owner|, so lease a DUT
                # for |orig_owner| as a compensation in the background.
                lease_dut_in_background_for_session(
                    orig_owner, chromeos_root, enable_shared_dut_pool
                )
            return host, resolve_builder_by_host(host)

    filter_func = functools.partial(_is_acceptable_bot, boards_with_prebuilt)
    while True:
        # Query every time because each iteration takes a few minutes
        bots = _sample_available_bots_with_retry(
            dimensions, variants, pools, spec.parallel, filter_func
        )

        remaining_time = spec.time_limit_seconds - (time.time() - t0)
        if remaining_time <= 0:
            break
        timeout = min(120, remaining_time)
        reason = cros_lab_util.make_lease_reason(spec.session)
        async_dut_leaser = cros_lab_util.AsyncDutLeaser(
            [bot['dimensions']['dut_name'][0] for bot in bots],
            reason,
            spec.lease_duration_seconds,
            timeout,
        )
        host = asyncio.run(
            async_dut_leaser.lease_parallelly(),
        )
        if host:
            # Add the leased DUT to the shared pool.
            if enable_shared_dut_pool:
                add_dut_to_shared_pool(host, spec.session)
            return host, resolve_builder_by_host(host)
        time.sleep(1)

    logger.warning('unable to lease DUT in time limit')
    raise errors.DutLeaseTimeout(
        'Unable to lease DUT in %s seconds' % spec.time_limit_seconds
    )


def add_dut_to_shared_pool(host: str, session: str):
    logger.info('putting %s into the shared DUT pool', host)

    bots = cros_lab_util.swarming_bots_list(['dut_name:' + host])
    assert len(bots) == 1
    bot = bots[0]

    pool_manager = shared_dut_pool.SharedDutPoolManager()
    # If |session| already owns a DUT, insert_dut() would try to find a new
    # owner. In that case, |session| would be different from |owner|.
    owner = pool_manager.insert_dut(
        host,
        session,
        bot['dimensions'],
    )
    if owner:
        logger.info(
            'dut %s with owner %s inserted to the shared DUT pool '
            'successfully.',
            host,
            owner,
        )
    else:
        logger.info(
            'failed to insert dut %s with owner %s to the shared DUT pool ',
            host,
            session,
        )


_BACKGROUND_LEASING_INTERVAL_SECONDS = 60 * 10


def lease_dut_in_background_for_session(
    session: str, chromeos_root: str, enable_shared_dut_pool: bool
):
    def _lease(spec: dut_allocate_spec.DutAllocateSpec):
        pool_manager = shared_dut_pool.SharedDutPoolManager()
        while True:
            owned_duts = pool_manager.query_by_owner(session)
            if owned_duts:
                # The original owner might have taken it back or leased a new
                # one by itself.
                logger.info(
                    'bisect %s already owned DUT(s) %s, stop trying to lease another one.',
                    session,
                    owned_duts,
                )
                return
            logger.info('try leasing a DUT for %s in the background', session)
            try:
                host, _ = allocate_dut(
                    spec,
                    chromeos_root,
                    enable_shared_dut_pool=enable_shared_dut_pool,
                    # When leasing DUT in the background, it needs a DUT from
                    # the lab instead of the shared DUT pool.
                    lease_from_shared_dut_pool=False,
                )
            except errors.DutLeaseTimeout as e:
                logger.debug(
                    'failed to lease a DUT for %s: %s, '
                    'trying to lease later in %s seconds',
                    session,
                    e,
                    _BACKGROUND_LEASING_INTERVAL_SECONDS,
                )
                time.sleep(_BACKGROUND_LEASING_INTERVAL_SECONDS)
            else:
                if host:
                    logger.info(
                        'leased dut %s successfully in the background', host
                    )
                    return

    ds_client = bisect_db.Client()
    try:
        spec = ds_client.load_dut_allocate_spec(session)
    except errors.DutAllocateSpecError:
        logger.warning('unable to get DutAllocateSpec for %s', session)
        return

    logger.info(
        'start leasing a DUT for %s in the background: %s', session, spec
    )
    thread = threading.Thread(target=_lease, args=(spec,), daemon=True)
    thread.start()
