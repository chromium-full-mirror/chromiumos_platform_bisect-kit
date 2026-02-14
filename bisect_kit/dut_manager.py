# Copyright 2023 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""A helper class to encapsulate DUT allocation and monitoring logic."""

from __future__ import annotations

import contextlib
import dataclasses
from enum import Enum
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
from bisect_kit import dut_allocate_spec as dut_allocate_spec_module
from bisect_kit import dut_allocator
from bisect_kit import errors
from bisect_kit import gs_util
from bisect_kit import shared_dut_pool
from bisect_kit import util
from bisect_kit import vm_leaser
import experiment
from google.protobuf import json_format


logger = logging.getLogger(__name__)


class VmImageType(Enum):
    """Image type which VM use"""

    # Release or snapshot image, which is built periodically on CI.
    RELEASE_BUILD = 1
    # Localbuild: image built by the local bisector instance.
    LOCAL_BUILD = 2
    # Localbuild: image built by the remote builder on buildbucket.
    BUILDBUCKET_BUILD = 3


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
        self,
        dut,
        dut_is_broken: bool = False,
        enable_shared_dut_pool: bool = False,
        callback: typing.Callable[[float], None] | None = None,
    ):
        """Initializer.

        Args:
            dut: the DUT.
            enable_shared_dut_pool: whether to enable the shared DUT pool.
            callback: a callback to call when the DUT is released. It accepts an argument as the DUT release timestamp.
        """
        self._dut = dut
        self._dut_is_broken = dut_is_broken
        self._enable_shared_dut_pool = enable_shared_dut_pool
        self._callback = callback

        self._timer: threading.Timer | None = None

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
            if self._enable_shared_dut_pool:
                if self._dut_is_broken:
                    logger.info(
                        'not returning the DUT to the shared DUT pool as it is broken'
                    )
                else:
                    logger.info(
                        'returning the DUT to the shared DUT pool instead of the ChromeOS lab'
                    )
                    pool_manager = shared_dut_pool.SharedDutPoolManager()
                    if not pool_manager.mark_dut_as_idle(
                        cros_lab_util.dut_host_name(self._dut),
                    ):
                        logger.info(
                            'failed to return the DUT to the shared DUT pool, try returning to the ChromeOS lab directly'
                        )
                    else:
                        self._has_released = True

            if not self._has_released:
                try:
                    cros_lab_util.crosfleet_release_dut(
                        cros_lab_util.dut_host_name(self._dut)
                    )
                except Exception as e:
                    logger.error('failed to release %s: %s', self._dut, e)
            self._has_released = True

        if self._callback:
            self._callback(time.time())

    def wait(self):
        """Block waiting the owned DUT to be released."""
        if self._timer:
            logger.info('waiting for %s to be released', self._dut)
            self._timer.join()
        else:
            logger.info('no DUT to be released')

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
        states: core.DiagnoseStates | None,
        dut_allocate_spec: dut_allocate_spec_module.DutAllocateSpec | None,
        pre_allocated_dut: str | None,
        should_auto_allocate: bool,
        dut_init_func: typing.Callable[[DutManager], None] | None = None,
        dut_leave_context_check_func: (
            typing.Callable[[DutManager], None] | None
        ) = None,
        should_force_monitoring: bool = False,
        vm_cros_version: Optional[str] = None,
        vm_image_type: Optional[VmImageType] = None,
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
            vm_image_type: Type of image to run a VM machine with.
        """
        self._name: str = name
        self._states: core.DiagnoseStates | None = states
        self._dut_allocate_spec: (
            dut_allocate_spec_module.DutAllocateSpec | None
        ) = dut_allocate_spec
        self._should_auto_allocate: bool = should_auto_allocate
        self._pre_allocated_dut: str | None = None
        if pre_allocated_dut != cros_lab_util.LAB_DUT:
            self._pre_allocated_dut = pre_allocated_dut
        self._dut_init_func: typing.Callable[[DutManager], None] | None = (
            dut_init_func
        )
        self._dut_leave_context_check_func: (
            typing.Callable[[DutManager], None] | None
        ) = dut_leave_context_check_func
        self._should_force_monitoring: bool = should_force_monitoring

        # DUT allocated by this DutManager, if any.
        self._auto_allocated_dut: str | None = None
        self._builder_of_auto_allocated_dut: str | None = None
        self._auto_allocated_time: float | None = None

        # Whether dut_init_func has been called on the auto-allocated DUT.
        self._is_dut_initialized: bool = False

        # Object handling dut releasing
        self._dut_releaser: DelayedDutReleaser | None = None

        # Whether dut is the previous leased one in consecutive
        # provision() calls of a DutManager instance.
        self._is_restored_dut: bool = False

        # Handle nested call of provision().
        self._nested_counter = 0

        # Arguments for VM bisection.
        self._vm_cros_version: Optional[str] = vm_cros_version
        self._allocated_vm: Optional[vm_leaser.VMLeaseResult] = None
        self._vm_image_type = vm_image_type
        if self.is_vm:
            assert not should_force_monitoring
            assert vm_image_type is not None, "Image type should be set"

            # Currently only allow one board input for VM bisection.
            if len(self.dut_allocate_spec.boards) != 1:
                raise errors.ArgumentError(
                    '--base-cros-version',
                    'Multiple boards (%s) including VM specified. Please specify only one board for VM.'
                    % ", ".join(self.dut_allocate_spec.boards),
                )

            if self._vm_cros_version is not None:
                if self._vm_image_type in [
                    VmImageType.RELEASE_BUILD,
                    VmImageType.BUILDBUCKET_BUILD,
                ] and not cros_util.is_cros_or_snapshot_version(
                    self._vm_cros_version
                ):
                    raise errors.ArgumentError(
                        '--base-cros-version',
                        'No CrOS VM image of "%s" found'
                        % self._vm_cros_version,
                    )

                # We don't check the image in case of VmImageType.LOCAL_BUILD,
                # since it needs to check the existence of the image. The check
                # will be done in self._search_image() later.
        else:
            assert self._vm_cros_version is None
            assert self._vm_image_type is None

    @property
    def config(self) -> core.DiagnoserConfig:
        assert self._states
        return self._states.config

    @property
    def dut_allocate_spec(self):
        return self._dut_allocate_spec

    @property
    def is_vm(self):
        """Check if it should initiate the VM bisection, instead of the real-DUT bisection."""
        if not self.dut_allocate_spec or not self.dut_allocate_spec.boards:
            return False
        return any(
            cros_util.is_vm_board(board)
            for board in self.dut_allocate_spec.boards
        )

    def is_previous_dut(self):
        logger.info(
            'pre_allocated_dut: %s, is_restored_dut: %s',
            self._pre_allocated_dut,
            self._is_restored_dut,
        )
        return self._pre_allocated_dut is not None or self._is_restored_dut

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
            connect_timeout=5,
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
            errors.ExecutionFatalError: If an image for given board and version
                not exists.
        """
        assert self.is_vm, "Only VM DUT should use this method."
        assert (
            self._vm_image_type is not None
        ), "Only VM DUT should use this method."
        assert self._vm_cros_version, "CrOS version must be set."

        if self._vm_image_type == VmImageType.RELEASE_BUILD:
            images = cros_util.search_image(
                self.dut_allocate_spec.boards[0],
                self._vm_cros_version,
                is_public_build=self.config.get('is_public_build', False),
            )

            if cros_util.ImageType.VM_IMAGE not in images:
                raise errors.ExecutionFatalError(
                    'No VM image for %s (%s, is_public_build: %s) found'
                    % (
                        self.dut_allocate_spec.boards[0],
                        self._vm_cros_version,
                        self.config.get('is_public_build', False),
                    )
                )
            return images[cros_util.ImageType.VM_IMAGE]

        if self._vm_image_type == VmImageType.LOCAL_BUILD:
            if self._vm_cros_version:
                gs_path = os.path.join(
                    'gs://crosperf-chromeos-builds/',
                    self.dut_allocate_spec.boards[0],
                    util.escape_rev(self._vm_cros_version),
                    cros_util.disk_vm_image_bundle_filename,
                )
                logger.info('Downloading CrOS image "%s"', gs_path)
                if gs_util.ls(gs_path, ignore_errors=True):
                    return gs_path

                logger.warning(
                    'CrOS build image for the revision "%s" does not '
                    + 'exists: "%s"',
                    self._vm_cros_version,
                    gs_path,
                )
            else:
                raise errors.InternalError(
                    'vm_cros_version is not set',
                )

        if self._vm_image_type == VmImageType.BUILDBUCKET_BUILD:
            # TODO(zjchang): Currently it's not possible for build script to pass
            # gs path to dut_manager. We should have a way to communicate between
            # them.
            # TODO(zjchang): Handle the error here gracefully as same as normal
            # switch script.
            api = buildbucket_util.BuildbucketApi()
            build = api.search_usable_build(
                board=self.dut_allocate_spec.boards[0],
                filter_digests=False,
                bisector_chromeos_version=self._vm_cros_version,
                return_ongoing_builds=False,
            )
            assert build

            output_properties = json_format.MessageToDict(
                build.output.properties
            )
            gs_bucket = output_properties['artifacts']['gs_bucket']
            gs_path = output_properties['artifacts']['gs_path']
            gs_image_path = (
                f'gs://{gs_bucket}/{gs_path}/{cros_util.vm_image_filename}'
            )
            gs_util.ls(gs_image_path)
            return gs_image_path

        # Should not reach this code.
        raise errors.InternalError(
            'Unsupported VM image: %s' % self._vm_image_type
        )

    def _lease_dut(self):
        """Lease a DUT."""
        if self.is_vm:
            if not self._vm_cros_version:
                logger.error('vm_cros_version not set, ignore allocating VM')
                return
            self._allocated_vm = vm_leaser.allocate_dut(
                self.dut_allocate_spec.boards[0],
                self._search_vm_image(),
                self._vm_cros_version,
            )
            host = self._allocated_vm.public_host
            builder = self.dut_allocate_spec.boards[0]
            leased_dut = host
            logger.debug(
                'Leased VM cros version: %s, builder: %s, host: %s',
                self._vm_cros_version,
                builder,
                host,
            )
        else:
            enable_shared_dut_pool = experiment.is_in_experiment(
                self.config.get('experiments'),
                experiment.ID.SHARED_DUT_POOL,
            )
            host, builder = dut_allocator.allocate_dut(
                self.dut_allocate_spec,
                self.config.get('chromeos_root'),
                enable_shared_dut_pool=enable_shared_dut_pool,
            )
            if not host:
                # This should not happen.
                raise errors.InternalError('Leased DUT invalid: %s' % host)

            if enable_shared_dut_pool:
                pool_manager = shared_dut_pool.SharedDutPoolManager()
                if not pool_manager.mark_dut_as_busy(host):
                    logger.info(
                        'failed to lease DUT %s from the shared DUT pool', host
                    )

            leased_dut = cros_lab_util.dut_name_to_address(host)
            logger.debug('Leased DUT %s with builder %s', host, builder)
        # A DUT is leased at this point successfully.
        self._auto_allocated_dut = leased_dut
        self._builder_of_auto_allocated_dut = builder
        self._auto_allocated_time = time.time()
        self._is_dut_initialized = False
        self._is_restored_dut = False
        self._wait_ssh_avaliable()
        self._update_states(leased_dut)

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
        dut_is_broken = isinstance(ex_value, errors.BrokenDutException)
        if dut and self._dut_leave_context_check_func:
            logger.info(
                'calling dut leave context check function for dut %s', dut
            )
            try:
                self._dut_leave_context_check_func(dut)
            except Exception as e:
                error = e

        if self._auto_allocated_dut and not self.is_vm:
            self._schedule_release_dut(dut_is_broken)
            self._auto_allocated_dut = None
        if self.is_vm:
            self._release_vm()

        if error:
            raise error

    def _schedule_release_dut(self, dut_is_broken: bool):
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
            dut_is_broken,
            experiment.is_in_experiment(
                self.config.get('experiments'),
                experiment.ID.SHARED_DUT_POOL,
            ),
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
        self._nested_counter += 1
        try:
            yield from self._do_provision(vm_cros_version)
        finally:
            self._nested_counter -= 1

    def _do_provision(self, vm_cros_version: Optional[str] = None):
        if self.is_vm:
            if self._vm_cros_version is None and vm_cros_version is None:
                logger.warning('vm version is not defined, skip provision')
                yield None
                return

            if vm_cros_version:
                if self._vm_image_type in [
                    VmImageType.RELEASE_BUILD,
                    VmImageType.BUILDBUCKET_BUILD,
                ] and not cros_util.is_cros_or_snapshot_version(
                    vm_cros_version
                ):
                    raise errors.ArgumentError(
                        '--base-cros-version',
                        'No CrOS VM image of "%s" found' % vm_cros_version,
                    )

                # We don't check the image in case of VmImageType.LOCAL_BUILD,
                # since it needs to check the existence of the image. The check
                # will be done in self._search_image() later.

                self._vm_cros_version = vm_cros_version
        else:
            assert vm_cros_version is None

        logger.debug('provision with nested counter: %s', self._nested_counter)
        if self._nested_counter > 1:
            yield self.dut
            return

        with self:
            dut = self.dut
            if not dut:
                yield None
                return

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
                    session_id = self.config.get('session')
                    reason = cros_lab_util.make_lease_reason(session_id)
                    with cros_lab_util.dut_lease_status_monitor(
                        dut, reason, session_id
                    ):
                        yield dut
                        return
            yield dut

    def _update_states_from_swarming(self, dut: str):
        """Updates states from swarming by the leased dut.

        Args:
          dut: dut address
        """
        logger.info('updating states by %s', dut)

        if self.dut_allocate_spec.dut_name:
            logger.info(
                'a specific dut %s is used, no need to update the device spec.',
                self.dut_allocate_spec.dut_name,
            )
            return

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
            self.dut_allocate_spec.boards = []
            self.dut_allocate_spec.models = []
            self.dut_allocate_spec.skus = [
                get_first_element_at_key(dimensions, 'label-hwid_sku')
            ]
            self.dut_allocate_spec.dut_name = None
            logger.info(
                'perf bisection, set dut allocation spec to sku: %s',
                self.dut_allocate_spec.skus,
            )
        else:
            self.dut_allocate_spec.boards = []
            self.dut_allocate_spec.models = [
                get_first_element_at_key(dimensions, 'label-model')
            ]

            self.dut_allocate_spec.skus = []
            self.dut_allocate_spec.dut_name = None
            logger.info(
                'funtional bisection, set dut allocation spec to model: %s',
                self.dut_allocate_spec.models,
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
        dut_allocate_spec_module.save(
            self.dut_allocate_spec,
            self.config.get('sync_dut_allocate_spec_with_db'),
        )

    def dut_leases_log_path(self):
        statistics = self._states.statistics
        if statistics.dut_leases_log_path:
            return statistics.dut_leases_log_path

        log_file = 'dut_leases.{timestamp}.json'.format(
            timestamp=time.strftime('%Y%m%d-%H%M%S'),
        )
        log_path = common.get_session_log_path(
            self.config.get('session'), f'log/{log_file}'
        )

        statistics.dut_leases_log_path = log_path
        self._states.save()

        return log_path

    def wait_for_release(self):
        """Wait until the DUT it leased to be released.

        Because of the design of DelayedDutReleaser, a DUT may be waiting to
        be released later. This method blocks until the DUT is released.
        If there is no DUT waiting to be released, return immediately.
        """
        if self._dut_releaser:
            self._dut_releaser.wait()
        else:
            logger.debug('no DUT releaser')
