# Copyright 2023 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""A helper class to encapsulate DUT allocation and monitoring logic."""

import contextlib
import dataclasses
import inspect
import logging
import sys
import time
import typing

from bisect_kit import core
from bisect_kit import cros_lab_util
from bisect_kit import errors
import cros_helper


logger = logging.getLogger(__name__)


class DiagnoseStates(core.States):
    """Diagnose states."""

    def __init__(self, session_file: str):
        super().__init__(session_file)
        # Initialized in init_done().
        self._dut_allocate_spec: cros_helper.DutAllocateSpec = None

    def init(self, config: dict, dut_allocate_spec_dict: dict):
        self.data = dict(
            config=config, history=[], dut_allocate_spec=dut_allocate_spec_dict
        )
        self.init_done()

    def init_done(self):
        """Post process after loading the data json.

        Keep a mutable member dataclass for dut_allocate_spec, so it's easier
        and safer for clients to access the attributes.
        """
        if 'dut_allocate_spec' not in self.data:
            raise errors.InternalError(
                '"dut_allocate_spec" not in session file, have you run '
                '"%s init" correctly?' % sys.argv[0]
            )
        self._dut_allocate_spec = cros_helper.DutAllocateSpec(
            **self.data['dut_allocate_spec']
        )
        super().init_done()

    def save(self):
        """Save the object back to the json file.

        If _dut_allocate_spec is modified, the content is saved.
        """
        self.data['dut_allocate_spec'] = dataclasses.asdict(
            self._dut_allocate_spec
        )
        super().save()

    @property
    def config(self):
        return self.data['config']

    @property
    def dut_allocate_spec(self) -> cros_helper.DutAllocateSpec:
        return self._dut_allocate_spec

    def add_history(self, event, **kwargs):
        entry = dict(timestamp=time.time(), event=event, **kwargs)
        self.data['history'].append(entry)
        self.save()


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
        states: typing.Type[DiagnoseStates],
        pre_allocated_dut: str,
        should_auto_allocate: bool,
        dut_init_func: typing.Callable[[str], None] | None = None,
        force_monitoring: bool = False,
    ):
        """initializer.

        Args:
            states: an instance of DiagnoseStates which contains the diagnose
                config and dut allocate spec.
            pre_allocated_dut: a pre-allocated DUT or ':lab:' if no
                pre-allocated DUT.
            should_auto_allocate: whether to auto allocate DUTs if a
                pre-allocated doesn't exist.
            dut_init_func: a function which accepts a DUT to do some
                initialization after a DUT is auto-allocated.
            force_monitoring: Whether to spawn a thread to monitor DUT lease
                status.
        """
        self._states = states
        self._should_auto_allocate = should_auto_allocate
        if pre_allocated_dut == cros_lab_util.LAB_DUT:
            self._pre_allocated_dut = None
        else:
            self._pre_allocated_dut = pre_allocated_dut
        self._dut_init_func = dut_init_func
        self._force_monitoring = force_monitoring

        # DUT allocated by this DutManager, if any.
        self._auto_allocated_dut = None

        # Whether dut_init_func has been called on the auto-allocated DUT.
        self._dut_initialized = False

    @property
    def config(self):
        return self._states.config

    @property
    def dut_allocate_spec(self):
        return self._states.dut_allocate_spec

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

    def __enter__(self):
        """Allocates a DUT if necessary.

        1. If a valid pre-allocated DUT or auto-allocation DUT exists, nothing needs to be done.
        2. Otherwise, allocate a DUT if `should_auto_allocate` is True.
        3. Otherwise, the DUT allocation is deferred. No valid DUT is available
          in the context managed by this manager.
        """
        logger.info(
            'entering DutManager context: should_auto_allocate = %s',
            self._should_auto_allocate,
        )
        # Do nothing if a DUT is already available.
        dut = self.dut()
        if dut:
            return self

        if not self._should_auto_allocate:
            logger.info('DUT is not allocated but allocation is deferred')
            return self

        # Try allocate a DUT now.
        host = cros_helper.allocate_dut(self.dut_allocate_spec)
        if not host:
            raise errors.NoDutAvailable('unable to allocate DUT')
        # A DUT is leased at this point successfully.
        self._auto_allocated_dut = cros_lab_util.dut_name_to_address(host)
        self._dut_initialized = False
        return self

    def __exit__(self, ex_type, ex_value, ex_traceback):
        """Releases the DUT allocated, if any."""
        logger.info('leaving DutManager context')
        if self._auto_allocated_dut:
            logger.info('releasing DUT %s', self._auto_allocated_dut)
            cros_lab_util.crosfleet_release_dut(
                cros_lab_util.dut_host_name(self._auto_allocated_dut)
            )
            self._auto_allocated_dut = None
            self._dut_initialized = False

    @contextlib.contextmanager
    def provision(self):
        """Main entry point of an instance.

        1. Call context manager to allocate a DUT if necessary.
        2. Common post processing if necessry.
        3. Call the registered init functions on the DUT if the DUT is
          considered uninitialized.
        4. Spawn a DUT lease status monitor thread if the DUT is auto
          allocated or force_monitoring is True.
        5. Return the DUT. Note that None may be returned if allocation
          is deffered.
        """
        with self:
            dut = self.dut()
            if not dut:
                yield None
                return

            if dut == self._auto_allocated_dut:
                self.update_dut_alloc_spec(dut)

                if self._dut_initialized:
                    logger.info(
                        'dut %s considered initialized, do not run initialization function',
                        dut,
                    )
                else:
                    if self._dut_init_func:
                        logger.info(
                            'running initialization function for %s...', dut
                        )
                        self._dut_init_func(dut)
                    self._dut_initialized = True

            # Note that the monitoring thread is spawn only for DUT auto allocated by
            # the instance, because a pre-allocated DUT layer might have already been
            # monitored.
            # Use force_monitoring = True for cases we want to spwan the monitoring
            # thread anyway.
            if dut == self._auto_allocated_dut or self._force_monitoring:
                logger.info('monitoring DUT lease status for %s', dut)
                reason = cros_lab_util.make_lease_reason(self.config['session'])
                with cros_lab_util.dut_lease_status_monitor(dut, reason):
                    yield dut
                    return
            yield dut

    def update_dut_alloc_spec(self, dut):
        """Updates dut allocate spec by the actual leased dut.

        In stateless bisection, we must guarantee that the DUTs leased in
        different rounds are compatible.
        - For functional bisection, we stick to DUTs with the same model in
          subsequence leases.
        - For perf bisection, we stick to DUTs with the same sku in
          subsequence leases.

        Args:
          dut: dut address
        """
        logger.info('updating dut alloc spec by %s', dut)

        host = cros_lab_util.dut_host_name(dut)

        # Query the bot id by dut name first.
        bots = cros_lab_util.swarming_bots_list(['dut_name:' + host])
        if len(bots) != 1:
            raise errors.ExternalError(
                'Expect 1 DUT with name %s but %s returned' % (host, len(bots))
            )
        bot_id = bots[0]['bot_id']

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

        if self.config['metric']:
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

        # TODO(cylee): Not sure if this is needed. Remove if not used.
        self.config['allocated_dut'] = dut
        self._states.save()
