# Copyright 2023 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""A helper class to encapsulate DUT allocation and monitoring logic."""

from __future__ import annotations

import contextlib
import dataclasses
import functools
import inspect
import json
import logging
import os
import shutil
import tempfile
import threading
import time
import typing
from typing import Optional

from bisect_kit import buildbucket_util
from bisect_kit import common
from bisect_kit import core
from bisect_kit import cros_lab_util
from bisect_kit import cros_util
from bisect_kit import dut_allocator
from bisect_kit import errors
from bisect_kit import gs_util
from bisect_kit import util
from bisect_kit import vm_leaser
from google.protobuf import json_format


logger = logging.getLogger(__name__)


@dataclasses.dataclass
class DutLeaseRecord:
    """Statistics for a single DUT lease."""

    name: str = ''
    start_timestamp: float = 0.0
    end_timestamp: float = 0.0
    duration_secs: float = 0.0


class DutLeasesLogWritter:
    """A helper class to write dut leases log to a text file."""

    def __init__(self, filename):
        self._filename = filename

    def add_dut_lease_record(self, record: DutLeaseRecord):
        dirname = os.path.dirname(self._filename)
        if not os.path.exists(dirname):
            os.makedirs(dirname)

        records = []
        if os.path.exists(self._filename):
            with open(self._filename) as f:
                records = json.load(f)
        records.append(dataclasses.asdict(record))

        tmp_fn = tempfile.mktemp()
        with open(tmp_fn, 'w') as f:
            f.write(json.dumps(records, indent=4, sort_keys=True))
        # Move is an atomic operation, so the file won't be corrupted due
        # to program terminated by any reason.
        shutil.move(tmp_fn, self._filename)


class DelayedDutReleaser:
    """A helper class to release dut after a pre-defined period of time.

    During a bisection , sometimes DUTs are leased in a consecutive manner
    (e.g., multiple "eval" operations in a row for a fixed version). In that
    case, leasing a new DUT and initializing it every time is unnecessary.
    This class starts a separated thread to release a DUT after a certain
    period of time. If a new lease request comes during the period, the
    release is canceled and the same DUT is reused.
    """

    _DELAYED_SECONDS = 60

    def __init__(
        self, dut, callback: typing.Callable[[float], None] | None = None
    ):
        """Initializer.

        Args:
            dut: the DUT.
            callback: a callback to call when the DUT is released. It accepts an argument as the DUT release timestamp.
        """
        self._dut = dut
        self._callback = callback

        self._timer = None

        self._lock = threading.Lock()
        self._has_released = False
        self._has_canceled = False

    def start(self):
        self._timer = threading.Timer(self._DELAYED_SECONDS, self.run)
        self._timer.start()

    def cancel(self) -> bool:
        """Returns whether the cancellation is successful.

        If the release process has been started, return False.
        Otherwise, return True.
        """
        with self._lock:
            if self._has_released:
                return False
            self._timer.cancel()
            self._has_canceled = True
        return True

    def run(self):
        with self._lock:
            if self._has_canceled:
                return
            logger.info('releasing DUT %s', self._dut)
            cros_lab_util.crosfleet_release_dut(
                cros_lab_util.dut_host_name(self._dut)
            )
            self._has_released = True

        if self._callback:
            self._callback(time.time())

    def get_dut(self):
        return self._dut


class DutManager:
    """A helper class to encapsulate DUT allocation and monitoring logic.

    An instance of this class can be used as a context manager which is
    responsible for DUT lease and release (if necessary) and DUT lease
    status monitoring.

    Example usage:
        dut_manager = DutManager(states, should_auto_allocate)
        ...
        with dut_manager.provision() as dut:
            ...
    """

    def __init__(
        self,
        name: str,
        states: typing.Type[core.DiagnoseStates] | None,
        pre_allocated_dut: str | None,
        should_auto_allocate: bool,
        dut_init_func: typing.Callable[[DutManager], None] | None = None,
        dut_leave_context_check_func: typing.Callable[[DutManager], None]
        | None = None,
        should_force_monitoring: bool = False,
        vm_cros_version: Optional[str] = None,
        is_cros_localbuild_vm: bool = False,
    ):
        """initializer.

        Args:
            name: a name for the DutManager. Useful for logging.
            states: an instance of core.DiagnoseStates which contains the diagnose
                config and dut allocate spec.
            pre_allocated_dut: a pre-allocated DUT or ':lab:' if no
                pre-allocated DUT.
            should_auto_allocate: whether to auto allocate DUTs if a
                pre-allocated doesn't exist.
            dut_init_func: a function which accepts a DUT to do some
                initialization after a DUT is auto-allocated.
            dut_leave_context_check_func: a function to call when leaving the
                context manager. It typically asserts some conditions on the
                DUT. If the function raises exceptions, DutManager would raise
                the exception when leaving the context manager.
            should_force_monitoring: Whether to spawn a thread to monitor DUT lease
                status.
            vm_cros_version: ChromeOS version used to boot up a VM machine.
            is_cros_localbuild_vm: True if bisecting localbuild images for VM.
        """
        self._name: str = name
        self._states: typing.Type[core.DiagnoseStates] | None = states
        self._should_auto_allocate: bool = should_auto_allocate
        self._pre_allocated_dut: str | None = None
        if pre_allocated_dut != cros_lab_util.LAB_DUT:
            self._pre_allocated_dut = pre_allocated_dut
        self._dut_init_func: typing.Callable[[str], None] | None = dut_init_func
        self._dut_leave_context_check_func: typing.Callable[
            [str], None
        ] | None = dut_leave_context_check_func
        self._should_force_monitoring: bool = should_force_monitoring

        # DUT allocated by this DutManager, if any.
        self._auto_allocated_dut: str | None = None
        self._builder_of_auto_allocated_dut: str | None = None
        self._auto_allocated_time: float | None = None

        # Whether dut_init_func has been called on the auto-allocated DUT.
        self._is_dut_initialized: bool = False

        # Object handling dut releasing
        self._dut_releaser: typing.Type[DelayedDutReleaser] | None = None

        # Whether dut is the previous leased one in consecutive
        # provision() calls of a DutManager instance.
        self._is_restored_dut: bool = False

        # Arguments for VM bisection.
        self._vm_cros_version: Optional[str] = vm_cros_version
        self._allocated_vm: Optional[vm_leaser.VMLeaseResult] = None
        self._is_cros_localbuild_vm = is_cros_localbuild_vm
        if self.is_vm:
            assert not should_force_monitoring
            # Currently only allow one board input for VM bisection.
            assert self.dut_allocate_spec.board is not None
            assert ',' not in self.dut_allocate_spec.board
            assert (
                self._vm_cros_version is None
                or cros_util.is_cros_or_snapshot_version(self._vm_cros_version)
            )
        else:
            assert self._vm_cros_version is None
            assert not self._is_cros_localbuild_vm

    @property
    def config(self):
        return self._states.config

    @property
    def dut_allocate_spec(self):
        return self._states.dut_allocate_spec

    @property
    def is_vm(self):
        if not self._states or not self._states.dut_allocate_spec:
            return False
        return cros_util.is_vm_board(self._states.dut_allocate_spec.board)

    def is_restored_dut(self):
        logger.info('is_restored_dut: %s', self._is_restored_dut)
        return self._is_restored_dut

    @property
    def dut(self) -> str:
        """Returns the DUT managed by this DutManager.

        If a pre-allocated DUT exists, there should be no auto-allocated DUT.

        Returns:
          The DUT string or None if no DUT is available.
        """
        caller = inspect.stack()[1].function

        if self._auto_allocated_dut:
            logger.info(
                '%s: auto-allocated DUT %s', caller, self._auto_allocated_dut
            )
            return self._auto_allocated_dut

        if self._pre_allocated_dut:
            logger.info(
                '%s: pre-allocated DUT %s', caller, self._pre_allocated_dut
            )
            return self._pre_allocated_dut

        logger.info('%s: no allocated DUT', caller)
        return None

    def _wait_ssh_avaliable(self):
        """Wait until DUT to be ssh able.

        Raises:
            errors.SshConnectionError: If unable to ssh after timeout.
        """
        util.ssh_cmd(
            self._auto_allocated_dut,
            'cat',
            '/etc/lsb-release',
            connect_timeout=1,
            max_attempts=30,
            retry_interval=3,
        )

    # TODO(zjchang): refactor according to
    # http://crrev.com/c/5266043/comment/9865e85b_1c0486ac/
    def _search_vm_image(self) -> str:
        """Returns gs path of image used to boot up VM.

        Returns:
            GS image path of local build image.

        Raises:
            subprocess.CalledProcessError: If path not exists.
        """
        if not self._is_cros_localbuild_vm:
            return cros_util.search_image(
                self.dut_allocate_spec.board, self._vm_cros_version
            )[cros_util.ImageType.VM_IMAGE]

        # TODO(zjchang): Currently it's not possible for build script to pass
        # gs path to dut_manager. We should have a way to communicate between
        # them.
        # TODO(zjchang): Handle the error here gracefully as same as normal
        # switch script.
        api = buildbucket_util.BuildbucketApi()
        build = api.search_usable_build(
            board=self.dut_allocate_spec.board,
            filter_digests=False,
            bisector_chromeos_version=self._vm_cros_version,
            return_ongoing_builds=False,
        )
        assert build

        output_properties = json_format.MessageToDict(build.output.properties)
        gs_bucket = output_properties['artifacts']['gs_bucket']
        gs_path = output_properties['artifacts']['gs_path']
        gs_image_path = (
            f'gs://{gs_bucket}/{gs_path}/{cros_util.vm_image_filename}'
        )
        gs_util.ls(gs_image_path)
        return gs_image_path

    def _lease_dut(self):
        """Lease a DUT."""
        if self.is_vm:
            if not self._vm_cros_version:
                logger.error('vm_cros_version not set, ignore allocating VM')
                return
            self._allocated_vm = vm_leaser.allocate_dut(
                self.dut_allocate_spec,
                self._search_vm_image(),
                self._vm_cros_version,
            )
            host = self._allocated_vm.public_host
            builder = self.dut_allocate_spec.board
            leased_dut = host
            logger.debug(
                'Leased VM cros version: %s, builder: %s, host: %s',
                self._vm_cros_version,
                builder,
                host,
            )
        else:
            host, builder = dut_allocator.allocate_dut(self.dut_allocate_spec)
            if not host:
                # This should not happen.
                raise errors.InternalError('Leased DUT invalid: %s' % host)
            leased_dut = cros_lab_util.dut_name_to_address(host)
            logger.debug('Leased DUT %s with builder %s', host, builder)
        # A DUT is leased at this point successfully.
        self._auto_allocated_dut = leased_dut
        self._builder_of_auto_allocated_dut = builder
        self._auto_allocated_time = time.time()
        self._is_dut_initialized = False
        self._is_restored_dut = False
        self._wait_ssh_avaliable()

    def _release_vm(self):
        """Release a VM instance, and deletes its image."""
        assert self.is_vm
        if not self._auto_allocated_dut:
            logger.warning('No allocated DUT, skip')
            return
        vm_leaser.release_vm(self._allocated_vm)
        vm_leaser.delete_image(
            self._allocated_vm.gce_image_name,
            gcp_project=self._allocated_vm.gce_image_project,
        )
        self._allocated_vm = None
        self._auto_allocated_dut = None

    def __enter__(self):
        """Allocates a DUT if necessary.

        1. If a valid pre-allocated DUT or auto-allocation DUT exists, nothing needs to be done.
        2. Otherwise, if a DUT is auto leased before and has not been released yet, restore it.
        3. Otherwise, allocate a DUT if `should_auto_allocate` is True.
        4. Otherwise, the DUT allocation is deferred. No valid DUT is available
          in the context managed by this manager.
        """
        logger.info(
            'entering DutManager context: should_auto_allocate = %s',
            self._should_auto_allocate,
        )
        # Do nothing if a DUT is already available.
        dut = self.dut
        if dut:
            return self

        # Restore previous leased DUT if it hasn't been released.
        if self._dut_releaser:
            if not self._dut_releaser.cancel():
                logger.info(
                    'dut releaser has been started, can not be canceled'
                )
            else:
                self._auto_allocated_dut = self._dut_releaser.get_dut()
                # self._is_dut_initialized remains the same state as in the previous lease.
                # If it has been initialized, self._is_dut_initialized = True so
                # it won't be initialized again.
                self._is_restored_dut = True
                logger.info(
                    'dut releaser has been canceled. DUT %s restored',
                    self._auto_allocated_dut,
                )
                return self

        if not self._should_auto_allocate:
            logger.info('DUT is not allocated but allocation is deferred')
            return self

        # Try allocating a DUT now.
        self._lease_dut()
        return self

    def __exit__(self, ex_type, ex_value, ex_traceback):
        """Releases the DUT allocated, if any."""
        logger.info('leaving DutManager context')

        error = None
        dut = self.dut
        if dut and self._dut_leave_context_check_func:
            logger.info(
                'calling dut leave context check function for dut %s', dut
            )
            try:
                self._dut_leave_context_check_func(dut)
            except Exception as e:
                error = e

        if self._auto_allocated_dut and not self.is_vm:
            self._schedule_release_dut()
            self._auto_allocated_dut = None
        if self.is_vm:
            self._release_vm()

        if error:
            raise error

    def _schedule_release_dut(self):
        """Schedules to release the DUT auto leased by this instance earlier."""

        def add_dut_lease_record(start_timestamp: float, end_timestamp: float):
            # This function is called as a callback in another thread from
            # DelayedDutReleaser. To avoid race condition, the records are written
            # to a separated log file instead of the file backs DiagnoseStates.
            writer = DutLeasesLogWritter(self.dut_leases_log_path())
            writer.add_dut_lease_record(
                DutLeaseRecord(
                    name=self._name,
                    start_timestamp=start_timestamp,
                    end_timestamp=end_timestamp,
                    duration_secs=end_timestamp - start_timestamp,
                )
            )

        assert not self.is_vm
        logger.info('scheduling to release DUT %s', self._auto_allocated_dut)

        assert self._auto_allocated_time is not None

        self._dut_releaser = DelayedDutReleaser(
            self._auto_allocated_dut,
            functools.partial(add_dut_lease_record, self._auto_allocated_time),
        )
        self._dut_releaser.start()

    def run_init_func(self):
        dut = self.dut
        if not dut:
            logger.info('no dut allocated yet, can not run init func')
            return

        if self._is_dut_initialized:
            logger.info(
                'dut %s has been initialized, do not run initialization function',
                dut,
            )
        else:
            if self._dut_init_func:
                logger.info('running initialization function for %s...', dut)
                self._dut_init_func(dut)
            self._is_dut_initialized = True

    @contextlib.contextmanager
    def provision(self, vm_cros_version: Optional[str] = None):
        """Main entry point of an instance.

        1. Call context manager to allocate a DUT if necessary.
        2. Common post processing if necessry.
        3. Call the registered init functions on the DUT if the DUT is
          considered uninitialized.
        4. Spawn a DUT lease status monitor thread if the DUT is auto
          allocated or should_force_monitoring is True.
        5. Return the DUT. Note that None may be returned if allocation
          is deffered.

        Args:
            vm_cros_version: CrOS version wants to provision. If None, VM
              bisection uses CrOS version passed in __init__.
        """
        if self.is_vm:
            if self._vm_cros_version is None and vm_cros_version is None:
                logger.warning('vm version is not defined, skip provision')
                yield None
                return
            if vm_cros_version:
                assert cros_util.is_cros_or_snapshot_version(vm_cros_version)
                self._vm_cros_version = vm_cros_version
        else:
            assert vm_cros_version is None
        with self:
            dut = self.dut
            if not dut:
                yield None
                return

            if dut == self._auto_allocated_dut:
                self._update_states(dut)

            self.run_init_func()

            # Note that the monitoring thread is spawn only for DUT auto allocated by
            # the instance, because a pre-allocated DUT layer might have already been
            # monitored.
            # Use should_force_monitoring = True for cases we want to spwan the monitoring
            # thread anyway.
            # Also, currently there is no logic to monitor VM instances.
            # TODO(zjchang): Implement logic to monitor VM instances.
            if not self.is_vm:
                if (
                    dut == self._auto_allocated_dut
                    or self._should_force_monitoring
                ):
                    logger.info('monitoring DUT lease status for %s', dut)
                    reason = cros_lab_util.make_lease_reason(
                        self.config['session']
                    )
                    with cros_lab_util.dut_lease_status_monitor(dut, reason):
                        yield dut
                        return
            yield dut

    def _update_states_from_swarming(self, dut):
        """Updates states from swarming by the leased dut.

        Args:
          dut: dut address
        """
        logger.info('updating states by %s', dut)

        host = cros_lab_util.dut_host_name(dut)

        # Query the bot id by dut name first.
        bots = cros_lab_util.swarming_bots_list(['dut_name:' + host])
        if len(bots) != 1:
            raise errors.ExternalError(
                'Expect 1 DUT with name %s but %s returned' % (host, len(bots))
            )
        bot_id = bots[0]['botId']

        # Query DUT labels by bot id.
        bot_info = cros_lab_util.swarming_bot_info(bot_id)
        logger.debug('bot_info: %s', bot_info)

        dimensions = bot_info['dimensions']

        def get_first_element_at_key(d, k):
            """Expect d[k] to be a list and returns the first element.

            Args:
              d: A dictionary.
              k: The key.
            Raises:
              errors.ExternalError if d[k] doesn't exist or is empty.
            Returns:
              d[k][0]
            """
            if not k in d or not d[k]:
                raise errors.ExternalError(
                    'Can not get first element at key %s' % k
                )
            return d[k][0]

        if self.config.get('metric'):
            self.dut_allocate_spec.board = None
            self.dut_allocate_spec.model = None
            self.dut_allocate_spec.sku = get_first_element_at_key(
                dimensions, 'label-hwid_sku'
            )
            self.dut_allocate_spec.dut_name = None
            logger.info(
                'perf bisection, set dut allocation spec to sku: %s',
                self.dut_allocate_spec.sku,
            )
        else:
            self.dut_allocate_spec.board = None
            self.dut_allocate_spec.model = get_first_element_at_key(
                dimensions, 'label-model'
            )
            self.dut_allocate_spec.sku = None
            self.dut_allocate_spec.dut_name = None
            logger.info(
                'funtional bisection, set dut allocation spec to model: %s',
                self.dut_allocate_spec.model,
            )

    def _update_states(self, dut):
        """Updates states by the leased dut.

        In stateless bisection, we must guarantee that the DUTs leased in
        different rounds are compatible. For non-vm instances:

        - For functional bisection, we stick to DUTs with the same model in
          subsequence leases.
        - For perf bisection, we stick to DUTs with the same sku in
          subsequence leases.

        Args:
          dut: dut address
        """
        if not self.is_vm:
            self._update_states_from_swarming(dut)

        # If DUT is pre-alllocated, config['board'] would have been populated
        # with the corresponding builder and this code path should not be
        # executed. On the other hand, if DUT is auto-allocated, this code path
        # is executed and we should update config['board'] accordingly.
        self.config['board'] = self._builder_of_auto_allocated_dut
        # TODO(cylee): Not sure if this is needed. Remove if not used.
        self.config['allocated_dut'] = dut
        self._states.save()

    def dut_leases_log_path(self):
        statistics = self._states.statistics
        if statistics.dut_leases_log_path:
            return statistics.dut_leases_log_path

        log_file = 'dut_leases.{timestamp}.json'.format(
            timestamp=time.strftime('%Y%m%d-%H%M%S'),
        )
        log_path = common.get_session_log_path(
            self.config['session'], f'log/{log_file}'
        )

        statistics.dut_leases_log_path = log_path
        self._states.save()

        return log_path
