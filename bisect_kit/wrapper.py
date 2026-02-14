# Copyright 2018 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Wrapper of bisector scripts"""

import contextlib
import importlib
import logging
import sys
import typing

from bisect_kit import bisector_cli
from bisect_kit import core
from bisect_kit import errors
from bisect_kit import util


logger = logging.getLogger(__name__)


class BisectorWrapper:
    """Wrapper of bisector scripts as python module."""

    def __init__(self, name, session, dut_manager=None, is_stateless=False):
        self.name = name
        self.module = importlib.import_module(name)
        self.session = session
        self.dut_manager = dut_manager
        self.is_stateless = is_stateless

        self.domain_cls = None
        for x in vars(self.module).values():
            if isinstance(x, type) and issubclass(x, core.BisectDomain):
                self.domain_cls = x
                break
        assert self.domain_cls

    def call(self, cmd, *args, stdout=sys.stdout):
        bisector = bisector_cli.BisectorCommandLine(
            self.domain_cls, self.dut_manager
        )
        full_args = [cmd, '--session', self.session] + list(args)
        logger.debug('call %s: %s', self.name, full_args)
        with contextlib.redirect_stdout(stdout):
            bisector.main(*full_args, prog=self.name)

    def current_status(self) -> dict:
        bisector = bisector_cli.BisectorCommandLine(self.domain_cls)
        return bisector.current_status(session=self.session)

    def check_done(self) -> bool:
        """Checks whether the bisection is done by looking at the history.

        If not, we can shortcut and not call narrow_down() at all.

        Returns:
          True if the bisection already has a verdict.
        """
        bisector = bisector_cli.BisectorCommandLine(self.domain_cls)
        return bisector.check_done(session=self.session)

    def init_if_necessary(
        self,
        old: str,
        new: str,
        init_args: list[str],
        switch_cmds: list[list[str]] | None = None,
        eval_cmd: list[str] | None = None,
        future_build_cmd: list[str] | None = None,
        old_value: float | None = None,
        new_value: float | None = None,
        term_old: str | None = None,
        term_new: str | None = None,
        recompute_init_values: bool = False,
        noisy: str | None = None,
        endpoint_verification: bool = False,
        test_name: str | None = None,
        experiments: list[str] | None = None,
    ) -> None:
        status = self.current_status()
        if not status['inited']:
            common_init_args: list[str] = ['--old', old, '--new', new]
            if term_old:
                common_init_args += ['--term-old', term_old]
            if term_new:
                common_init_args += ['--term-new', term_new]
            if noisy:
                common_init_args += ['--noisy', noisy]
            if old_value is not None:
                common_init_args += ['--old-value', str(old_value)]
            if new_value is not None:
                common_init_args += ['--new-value', str(new_value)]
            if recompute_init_values:
                common_init_args.append('--recompute-init-values')
            if endpoint_verification:
                common_init_args.append('--endpoint-verification')
            if test_name is not None:
                common_init_args += ['--test-name', str(test_name)]
            if experiments:
                common_init_args += ['--experiments']
                common_init_args += experiments
            # Note that, if 'init' failed, no session file for this bisector is
            # created. In other words, the error event is only recorded in
            # diagnoser's log.
            self.call('init', *(common_init_args + init_args))

        if switch_cmds:
            # config 'switch' is accumulative. So we need to clear existing
            # values first.
            self.call(
                'config',
                'clear',
                'switch',
            )
            for cmd in switch_cmds:
                self.call(
                    'config',
                    'switch',
                    '--mode=bisectkit',
                    *(cmd + ['--session', self.session]),
                )
        self.call(
            'config',
            'eval',
            '--mode=bisectkit',
            *(eval_cmd or []),
        )
        if future_build_cmd:
            self.call(
                'config', 'future_build', '--mode=bisectkit', *future_build_cmd
            )

    def narrow_down(
        self,
        should_allocate_dut: bool,
        init_once: util.InitOnce | None = None,
        dut_precondition: typing.Callable[[str], bool] | None = None,
    ) -> tuple[str, str, str | None]:
        """Run bisection on the given domain.
          It first looks at the history session file to see if the bisection
          already has a verdict.
          If yes, it returns immediately with previous result directly.
          If not, it start/continue running the bisection.

        Args:
            should_allocate_dut: whether a DUT should be allocated by
              self.dut_manager before running the bisection.
            init_once: init function which should be executed before
              actual run the bisection.
            dut_precondition: a predicate which checks some DUT precondition.
              When given, should_allocate_dut must be True.
              It is called with the allocated DUT as the argument and returns
              a bool. If the function returns False, an
              errors.DutPreconditionNotMet is raised.

        Returns:
            (narrowed done old version, narrowed done new version, estimated noise)
        """
        status = self.current_status()
        logger.info(
            '%s old=%s, new=%s, done=%s',
            self.name,
            status['old'],
            status['new'],
            status['done'],
        )

        def get_result() -> tuple[str, str, str | None]:
            """Gets bisection result from current status."""
            status = self.current_status()
            logger.info(
                '%s result old=%s, new=%s, noisy=%s, done=%s',
                self.name,
                status['old'],
                status['new'],
                status['estimated_noise'],
                status['done'],
            )
            self.call('view')
            return status['old'], status['new'], status['estimated_noise']

        # Shortcut if no further action should be done.
        # If there were verification errors, exceptions are thrown.
        if self.check_done():
            return get_result()

        if init_once:
            init_once.run()
        try:
            if should_allocate_dut:
                with self.dut_manager.provision() as dut:
                    if dut_precondition and not dut_precondition(dut):
                        raise errors.DutPreconditionNotMet()
                    self.call('run')
            else:
                self.call('run')
        except Exception as e:
            if self.is_stateless and isinstance(e, errors.BisectRetriableError):
                logger.debug('Retriable error %s, abort immeidately', e)
                # If the error is retriable, raise immediately and a schedule a
                # retry soon.
                raise
            # If bisector failed after bisect range verified, we can continue with
            # knowledge of the partial results. For example,
            #  - ChromeOS prebuilt bisector may cut down half the bisect range and
            #    failed. Although it didn't find the narrowest range, we can still
            #    continue to bisect Android and Chrome using the new range.
            #  - If Android prebuilt bisection failed after verification, although
            #    the range may not shrink, it indicates the culprit is inside Android
            #    and we should continue bisect Android localbuild.
            status = self.current_status()
            if status['verified']:
                logger.exception(
                    'got exception; still can continue with partial results'
                )
            else:
                raise
        return get_result()
