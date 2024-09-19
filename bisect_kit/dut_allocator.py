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
    is_busy: bool | None = False,
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
        logger.debug(
            'fetch_bots: %s, is_dead: %s, quarantined, %s, is_busy: %s, limit: %s',
            dimensions,
            is_dead,
            quarantined,
            is_busy,
            limit,
        )
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
        if bots := sample_func():
            return bots

        fail_count += 1
        if fail_count >= max_attempts:
            raise errors.NoDutAvailable(
                'no bots satisfy constraints; all are in maintenance state?'
            )
        # sleep one minutes before next try
        time.sleep(60)


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
        bots = cros_lab_util.swarming_bots_list(constraints, limit=10)
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
) -> typing.Tuple[str, str]:
    """A convenient function to DutAllocator.allocate_dut().

    See comment in DutAllocator and DutAllocator.allocate_dut().

    Args:
      spec: the DutAllocateSpec.
      chromeos_root: path to chromeos root.

    Returns:
      (host, builder)
    """
    allocator = DutAllocator(spec, chromeos_root)
    return allocator.allocate_dut(enable_shared_dut_pool)


class DutAllocator:
    """A class to allocate DUT."""

    def __init__(
        self, spec: dut_allocate_spec.DutAllocateSpec, chromeos_root: str
    ):
        """Initializer.

        Args:
          spec: the DutAllocateSpec.
          chromeos_root: path to chromeos root.
        """
        self._spec = spec
        self._chromeos_root = chromeos_root

        # derived fields.
        (
            self._dimensions,
            self._variants,
            self._pools,
            self._boards_with_prebuilt,
        ) = self._parse_spec()

    def _parse_spec(
        self,
    ) -> typing.Tuple[list[str], list[str], list[str], list[str]]:
        """Parses the DutAllocateSpec and save the intermediate artifacts."""
        # Alias for easier access.
        spec = self._spec

        if spec.dut_name:
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
            raise errors.ArgumentError(
                '--duration', 'must be at least 60 seconds'
            )

        if spec.satlab_ip:
            if not cros_lab_util.is_satlab_dut(spec.dut_name):
                raise errors.ArgumentError(
                    '--satlab-ip', 'Should be only provided with a satlab dut'
                )

            cros_lab_util.write_satlab_ssh_config(spec.dut_name, spec.satlab_ip)

        dimensions = spec.dimensions[:]
        dimensions.append('dut_state:ready')
        dimensions = _filter_dimensions_by_lab(dimensions, raise_unknown=True)

        pools = None
        # Pool is ignored if dut_name is specified.
        if spec.pools and not spec.dut_name:
            pools = ['label-pool:%s' % p for p in spec.pools]
            pools = _filter_dimensions_by_lab(pools, raise_unknown=False)
            if not pools:
                raise errors.NoDutAvailable(
                    'none of the specified pools "%s" are available in lab'
                    % spec.pools
                )

        # Note: board, model, sku, and dut_name are mutual exclusive so at most one
        # of them is present.
        variants: list[str] = []
        if spec.boards:
            for board in spec.boards:
                variants.append(
                    'label-board:'
                    + _normalize_board_name(self._chromeos_root, board)
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
                    'Invalid constraints: no dut_name=%s in the lab'
                    % spec.dut_name
                )

        # Filter variants by prebuilt images.
        boards_with_prebuilt: list[str] | None = None
        if spec.version_hints:
            if not spec.builder_hints:
                spec.builder_hints = spec.boards
            if not spec.builder_hints:
                raise errors.ArgumentError(
                    '--builder-hint',
                    'must be specified along with --version-hint',
                )
            boards_with_prebuilt = []
            versions = spec.version_hints
            for builder in spec.builder_hints:
                if not all(
                    cros_util.has_test_image(builder, v) for v in versions
                ):
                    logger.warning(
                        'builder=%s does not have prebuilt test image for %s, ignore',
                        builder,
                        spec.version_hints,
                    )
                    continue
                boards_with_prebuilt.append(
                    _normalize_board_name(self._chromeos_root, builder)
                )
            logger.info('boards with prebuilt: %s', boards_with_prebuilt)
            if not boards_with_prebuilt:
                raise errors.ArgumentError(
                    '--version-hint',
                    'given builders have no prebuilt for %s'
                    % spec.version_hints,
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

        return dimensions, variants, pools, boards_with_prebuilt

    def _resolve_builder_by_host(self, host: str) -> str:
        """Resolve the builder by the leased DUT.

        Since the dut allocate spec may contain multiple builders and boards,
        we need to know the corresponding builder by the leased board.

        Args:
          host: DUT

        Returns:
          The builder name.
        """
        # Alias for easier access.
        spec = self._spec

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
                if (
                    _normalize_board_name(self._chromeos_root, builder)
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
        return board_to_build

    def allocate_dut(
        self, enable_shared_dut_pool=False
    ) -> typing.Tuple[str, str]:
        """Allocate a DUT.

        If enable_shared_dut_pool, it tried to lease one from the shared DUT
        pool first. If failed, try leasing one from the lab and put the leased
        DUT into the shared DUT pool.
        Otherwise, lease one from the lab directly.

        Args:
          enable_shared_dut_pool: whether to enable shared DUT pool.

        Returns:
          (host, builder)
          host: leased host name
          builder: the corresponding builder
        """
        # Alias for easier access.
        spec = self._spec

        logger.debug(
            'allocate_dut(): enable_shared_dut_pool: %s', enable_shared_dut_pool
        )
        if not enable_shared_dut_pool:
            return self.allocate_dut_from_lab()

        pool_manager = shared_dut_pool.SharedDutPoolManager()

        while True:
            host, orig_owner = pool_manager.lease_for_bisect(
                spec.session, self._dimensions, self._variants, self._pools
            )
            logger.info(
                'leased %s with original owner %s from the shared DUT pool',
                host,
                orig_owner,
            )
            if host is not None:
                if orig_owner != spec.session:
                    # The bisect robs the DUT owned by |orig_owner|, so lease a
                    # DUT for |orig_owner| as a compensation in the background.
                    lease_dut_in_background_for_session(
                        orig_owner, self._chromeos_root
                    )
                return host, self._resolve_builder_by_host(host)

            # If failed to lease a DUT from the shared DUT pool, lease one from
            # the lab and add the leasead DUT to the shared pool.
            host, _ = self.allocate_dut_from_lab()
            add_dut_to_shared_pool(host, spec.session)

            # At this point, there should already be a DUT ready to be used.
            # Case 1: When add_dut_to_shared_pool() is called, there is no DUT
            # owned by this bisect yet. The newly leased DUT would be owned by
            # this bisect.
            # Case 2: When add_dut_to_shared_pool() is called, there is already
            # a DUT owned by this bisect (e.g., some other bisect tried to
            # lease one for this bisect in the background). In that case, there
            # is also a DUT owned by the bisect, just not the one leased above.
            #
            # In the next loop iteration, the DUT owned by this bisect should be
            # returned. However, in rare cases a race condition may happen so
            # the DUT is taken by another bisection. In that case, it would try
            # to lease another DUT from the lab, and the loop continues again.
            logger.info(
                'a DUT should already been leased for %s, try again.',
                spec.session,
            )

    def allocate_dut_from_lab(self) -> typing.Tuple[str, str]:
        """Allocate a DUT from the lab.

        Returns:
          (host, builder)
          host: leased host name
          builder: the corresponding builder

        Raises:
            errors.ArgumentError if the input DutAllocateSpec is invalid.
            errors.NoDutAvailable if the DutAllocateSpec could not match any device in lab.
            errors.DutLeaseTimeout if the DUT lease take too long.
            errors.DutLeaseException if other exception happens.
        """
        # Alias for easier access.
        spec = self._spec

        start_time = time.time()
        filter_func = functools.partial(
            _is_acceptable_bot, self._boards_with_prebuilt
        )
        while True:
            # Query every time because each iteration takes a few minutes
            bots = _sample_available_bots_with_retry(
                self._dimensions,
                self._variants,
                self._pools,
                spec.parallel,
                filter_func,
            )

            remaining_time = spec.time_limit_seconds - (
                time.time() - start_time
            )
            if remaining_time <= 0:
                break
            timeout = min(360, remaining_time)
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
                return host, self._resolve_builder_by_host(host)
            time.sleep(1)

        logger.warning('unable to lease DUT in time limit')
        raise errors.DutLeaseTimeout(
            'Unable to lease DUT in %s seconds' % spec.time_limit_seconds
        )


def add_dut_to_shared_pool(host: str, session: str):
    """Add a leased DUT to the shared DUT pool.

    If |session| doesn't own a host already, insert the DUT |host| with owner |session|.
    Otherwise, try finding a successor for |host| and insert |host| with the successor
    as the new owner.
    If failed to find a successor, release |host| to the lab.

    Args:
      host: the leased DUT.
      session: the leaser bisect id.
    """
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
            'failed to insert dut %s with owner %s to the shared DUT pool, '
            'try returning it back to the lab.',
            host,
            session,
        )
        try:
            cros_lab_util.crosfleet_release_dut(host)
        except Exception as e:
            logger.warning('failed to return DUT %s to the lab: %s', host, e)


_BACKGROUND_LEASING_INTERVAL_SECONDS = 60 * 10


def lease_dut_in_background_for_session(session: str, chromeos_root: str):
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
                allocator = DutAllocator(spec, chromeos_root)
                host, _ = allocator.allocate_dut_from_lab()
            except (
                errors.DutLeaseTimeout,
                errors.NoDutAvailable,
                errors.DutLeaseException,
            ) as e:
                logger.info(
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
                        'leased dut %s successfully in the background.'
                        'adding it to the shared DUT pool.',
                        host,
                    )
                    add_dut_to_shared_pool(host, session)
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
