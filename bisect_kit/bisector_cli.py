# Copyright 2019 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Bisector command line interface."""

from __future__ import annotations

import argparse
import collections
import copy
import datetime
import enum
import importlib
import importlib.util
import json
import logging
import os
import re
import subprocess
import sys
import textwrap
import time
import typing

from bisect_kit import cli
from bisect_kit import common
from bisect_kit import core
from bisect_kit import cros_util
from bisect_kit import dut_manager
from bisect_kit import errors
from bisect_kit import math_util
from bisect_kit import strategy
from bisect_kit import util
import experiment


class BaseOptions(argparse.Namespace):
    """Base options common to all subcommands."""

    session: str
    func: typing.Callable[..., typing.Any]


class InitOptions(BaseOptions):
    """Options for the 'init' command."""

    command: typing.Literal['init']
    old: str
    new: str
    term_old: str
    term_new: str
    noisy: str
    old_value: float
    new_value: float
    recompute_init_values: bool
    confidence: float
    endpoint_verification: bool
    test_name: str
    experiments: list[experiment.ID]
    # Common dynamic args from domains
    dut: str
    board: str


class RunOptions(BaseOptions):
    """Options for the 'run' command."""

    command: typing.Literal['run']
    once: bool
    force: bool
    current_rev: str | None
    revs: list[str]


class ConfigOptions(BaseOptions):
    """Options for the 'config' command."""

    command: typing.Literal['config']
    key: typing.Literal[
        'clear',
        'switch',
        'eval',
        'confidence',
        'noisy',
        'term_old',
        'term_new',
        'future_build',
    ]
    value: list[str]


class OldNewSkipOptions(BaseOptions):
    """Options for 'old', 'new', 'skip' commands."""

    command: typing.Literal['old', 'new', 'skip']
    revs: list[str]


class ViewOptions(BaseOptions):
    """Options for the 'view' command."""

    command: typing.Literal['view']
    verbose: bool
    json: bool
    timestamp: float | None


class LogOptions(BaseOptions):
    """Options for the 'log' command."""

    command: typing.Literal['log']
    before: float | None
    after: float | None
    json: bool


class NextOptions(BaseOptions):
    """Options for the 'next' command."""

    command: typing.Literal['next']
    current_rev: str | None


class ResetOptions(BaseOptions):
    """Options for the 'reset' command."""

    command: typing.Literal['reset']


logger = logging.getLogger(__name__)

DEFAULT_CONFIDENCE = 0.999
END_OF_LOG_FILE_MARKER = '== end of log file ==\n'

OLD = 'old'
NEW = 'new'
SKIP = 'skip'
FATAL = 'fatal'
VALUE = 'value'

EXIT_CODE_MAP = {
    OLD: cli.EXIT_CODE_OLD,
    NEW: cli.EXIT_CODE_NEW,
    SKIP: cli.EXIT_CODE_SKIP,
    FATAL: cli.EXIT_CODE_FATAL,
    VALUE: cli.EXIT_CODE_VALUE,
}

step_command_parser = cli.ArgumentParser(prog='config', raise_bad_status=False)
step_command_parser.add_argument(
    '--mode', choices=['git', 'bisectkit'], default='git'
)
step_command_parser.add_argument('command', nargs=argparse.REMAINDER)


def _collect_bisect_result_values(values, line):
    """Collect bisect result values from output line.

    Args:
      values: Collected values are appending to this list.
      line: One line of output string.
    """
    m = re.match(r'^BISECT_RESULT_VALUES=(.+)', line)
    if m:
        try:
            values.extend([float(v) for v in m.group(1).split()])
        except ValueError as e:
            raise errors.InternalError(
                'BISECT_RESULT_VALUES should be list of floats: %r' % m.group(1)
            ) from e


def _is_fatal_returncode(returncode):
    return returncode < 0 or returncode >= 128


def output_rich_result(result: core.StepResult):
    print('rich-result:', json.dumps(result.data))


def _execute_git_step_command(
    args, env=None, capture_values=False
) -> core.StepResult:
    """Helper of do_evaluate() and do_switch().

    The result is determined according to the exit code of evaluator:
      0: 'old'
      1..124, 126, 127: 'new'
      125: 'skip'
      128..255: fatal error
      terminated by signal: fatal error

    p.s. the definition of result is compatible with git-bisect(1).

    It also extracts additional values from evaluate_cmd's stdout lines which
    match the following format:
         BISECT_RESULT_VALUES=<float>[, <float>]*

    Args:
      args: command line arguments
      env: environment variables
      capture_values: capture additional value output

    Returns:
      core.StepResult
    """
    values: list[float] = []
    stderr_lines: list[str] = []
    p = util.Popen(
        args,
        env=env,
        stdout_callback=lambda line: _collect_bisect_result_values(
            values, line
        ),
        stderr_callback=stderr_lines.append,
    )
    returncode = p.wait()
    if _is_fatal_returncode(returncode):
        # Only output error messages of child process if it is fatal error.
        print(cli.format_returncode(returncode))
        print(
            'Last stderr lines of "%s"' % subprocess.list2cmdline(args),
            file=sys.stderr,
        )
        print('=' * 40, file=sys.stderr)
        for line in stderr_lines[-50:]:
            print(line, end='', file=sys.stderr)
        print('=' * 40, file=sys.stderr)

    if _is_fatal_returncode(returncode):
        return core.StepResult('fatal')
    if returncode == 125:
        return core.StepResult('skip')
    if capture_values:
        return core.StepResult('value', values=values)
    if returncode == 0:
        return core.StepResult('old')
    return core.StepResult('new')


def execute_bisectkit_step_command(args, env=None) -> core.StepResult:
    """Helper of do_evaluate() and do_switch().

    For commands executed in this mode, the last line of stdout should be dump of
    json format of core.StepResult (a.k.a. rich result).

    Args:
      args: command line arguments
      env: environment variables

    Returns:
      core.StepResult
    """
    stdout_lines: list[str] = []
    stderr_lines: list[str] = []
    p = util.Popen(
        args,
        env=env,
        stdout_callback=stdout_lines.append,
        stderr_callback=stderr_lines.append,
    )
    returncode = p.wait()
    if _is_fatal_returncode(returncode):
        # Only output error messages of child process if it is fatal error.
        print(cli.format_returncode(returncode))
        print(
            'Last stderr lines of "%s"' % subprocess.list2cmdline(args),
            file=sys.stderr,
        )
        print('=' * 40, file=sys.stderr)
        for line in stderr_lines[-50:]:
            print(line, end='', file=sys.stderr)
        print('=' * 40, file=sys.stderr)

    if stdout_lines and stdout_lines[-1].startswith('rich-result: '):
        data = json.loads(stdout_lines[-1][13:])
        return core.StepResult(**data)
    return core.StepResult('fatal', 'completely failed to run %s' % args[0])


def do_evaluate(
    evaluate_cmd, domain, rev, rev_details, log_file, capture_values=False
) -> core.StepResult:
    """Invokes evaluator command.

    The `evaluate_cmd` can get the target revision from the environment variable
    named 'BISECT_REV'.

    Args:
      evaluate_cmd: evaluator command.
      domain: a bisect_kit.core.Domain instance.
      rev: version to evaluate.
      rev_details:  details of the version
      log_file: hint sub-process where to output detail log
      capture_values: True if we should capture values generated by the command

    Returns:
      (result, values):
        result is one of 'old', 'new', 'skip', 'fatal'.
        values are additional collected values, like performance score.
    """
    env = os.environ.copy()
    env['BISECT_REV'] = rev
    domain.setenv(env, rev, rev_details)

    if log_file:
        logger.info('eval log file = %s', log_file)
        env['LOG_FILE'] = log_file

    extra_args = domain.get_extra_args(core.Phase.EVAL, rev, rev_details)
    exec_opts = step_command_parser.parse_args(evaluate_cmd + extra_args)
    if exec_opts.mode == 'git':
        result = _execute_git_step_command(
            exec_opts.command, env=env, capture_values=capture_values
        )
    else:
        result = execute_bisectkit_step_command(exec_opts.command, env=env)
    if log_file and os.path.exists(log_file):
        with open(log_file, 'a') as f:
            f.write(END_OF_LOG_FILE_MARKER)

    return result


class SwitchAction(enum.StrEnum):
    """The action to perform for a "switch" step.

    There are three sets of switch targets.
    1. DUT is needed, but the step can be broken into BUILD phase and DEPLOY phase where BUILD doesn't require DUT and DEPLOY requires.
    2. DUT is needed, and the step can not be further broken into sub phases.
    3. DUT is not needed.
    """

    # The following 3 enums are for switches which can be broekn down into
    # DEPLOY and BUILD phases.
    # Build the binary under testing.
    BUILD = 'build'

    # Deploy the binary under testing.
    DEPLOY = 'deploy'

    # Build and Deploy the binary under testing.
    BUILD_AND_DEPLOY = 'build_and_deploy'

    # Switch that requires a DUT.
    WITH_DUT = 'with_dut'

    # Switch that doesn't require a DUT.
    WITHOUT_DUT = 'without_dut'

    # Switch script for VM.
    SWITCH_VM = 'switch_vm'


class EvalAction(enum.StrEnum):
    """The action to perform for a "eval" step.

    There are two sets of eval targets.
    1. DUT is needed.
    2. DUT is not needed.
    """

    # Eval that requires a DUT.
    WITH_DUT = 'with_dut'

    # Eval that doesn't require a DUT.
    WITHOUT_DUT = 'without_dut'


def import_module_from_path(full_path: str):
    file_name = os.path.basename(full_path)
    module_name, _ = os.path.splitext(file_name)

    spec = importlib.util.spec_from_file_location(module_name, full_path)
    if not spec:
        return None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)

    return module


def switch_eval_script_action(
    cmd: list[str], default_action: SwitchAction | EvalAction
) -> SwitchAction | EvalAction:
    """Resolves the action by inspecting the script module."""

    exec_opts = step_command_parser.parse_args(cmd)
    executable = exec_opts.command[0]
    module = import_module_from_path(executable)
    if module:
        return module.action()

    logging.warning(
        'Failed to import %s, use default action %s', executable, default_action
    )

    return default_action


def do_switch(
    switch_cmd, domain, rev, rev_details, log_file
) -> core.StepResult | None:
    """Invokes switcher command.

    The `switch_cmd` can get the target revision from the environment variable
    named 'BISECT_REV'.

    The result is determined according to the exit code of switcher:
      0: switch succeeded
      1..127: 'skip'
      128..255: fatal error
      terminated by signal: fatal error

    In other words, any non-fatal errors are considered as 'skip'.

    Args:
      switch_cmd: switcher command.
      domain: a bisect_kit.core.Domain instance.
      rev: version to switch.
      rev_details: details of the version.
      log_file: hint sub-process where to output detail log

    Returns:
      None if switch successfully, otherwise core.StepResult with 'skip' or
      'fatal'.
    """
    env = os.environ.copy()
    env['BISECT_REV'] = rev
    domain.setenv(env, rev, rev_details)

    if log_file:
        logger.info('switch log file = %s', log_file)
        env['LOG_FILE'] = log_file

    extra_args = domain.get_extra_args(core.Phase.SWITCH, rev, rev_details)
    exec_opts = step_command_parser.parse_args(switch_cmd + extra_args)
    if exec_opts.mode == 'git':
        result = _execute_git_step_command(exec_opts.command, env=env)
    else:
        result = execute_bisectkit_step_command(exec_opts.command, env=env)

    if log_file and os.path.exists(log_file):
        with open(log_file, 'a') as f:
            f.write(END_OF_LOG_FILE_MARKER)

    if result.data['status'] == 'new':
        logger.debug('for "switch" step, "new" means this candidate failed')
        result.data['status'] = 'skip'

    if result.data['status'] in ('fatal', 'skip'):
        return result
    assert result.data['status'] == 'old'
    return None


def do_future_build(
    future_build_cmd,
    domain: core.BisectDomain,
    rev: str,
    rev_details,
    log_file: str | None = None,
):
    """Invokes future build command.

    To support future build, switch script needs to pass these requirements:
        1. Has command / argument to start an asynchronous job.
        2. Has ability to seek for previous started asynchronous build jobs,
            and fetch the result of previous jobs.

    For example, switch_cros_localbuild_buildbucket.py can do:
        1. Use --no-deploy to start a build job, the script exits immediately
            after job starts.
        2. When switch a version, the script will check if there's any existing
            job and resue the artifacts.
    """
    env = os.environ.copy()
    env['BISECT_REV'] = rev
    domain.setenv(env, rev, rev_details)

    if log_file:
        logger.debug('future_build log file = %s', log_file)
        env['LOG_FILE'] = log_file

    extra_args = domain.get_extra_args(
        core.Phase.FUTURE_BUILD, rev, rev_details
    )
    exec_opts = step_command_parser.parse_args(future_build_cmd + extra_args)
    if exec_opts.mode != 'bisectkit':
        raise errors.InternalError('Unsupported mode %s' % exec_opts.mode)

    result = execute_bisectkit_step_command(exec_opts.command, env=env)
    if log_file and os.path.exists(log_file):
        with open(log_file, 'a') as f:
            f.write(END_OF_LOG_FILE_MARKER)
    assert result.data['status'] == 'skip'


def step_main_wrapper(
    step_main: typing.Callable[..., core.StepResult],
    args: tuple[str] | None,
) -> int:
    """Wrapper function for switch and eval step.

    We treat most unhandled exceptions are fatal and will stop the whole
    bisection.

    Args:
      step_main: main function
      args: command line arguments
    """
    try:
        result = step_main(args)
    except (
        OSError,
        errors.BisectionTemporaryError,
    ) as e:
        # These exceptions are kind of external dependencies. So we classify them
        # as 'skip' (undecidable now) and let bisection algorithm retry this
        # candidate later.
        exception_name = e.__class__.__name__
        logging.exception(str(e))
        result = core.StepResult(
            'skip', reason=str(e), exception=exception_name
        )
    except errors.BuildError as e:
        # BuildError means the current revision cannot be tested. Skip it
        # forever.
        exception_name = e.__class__.__name__
        logging.exception(str(e))
        result = core.StepResult(
            'skip',
            reason=str(e),
            exception=exception_name,
            times=strategy.SKIP_FOREVER,
        )
    except Exception as e:
        exception_name = e.__class__.__name__
        logging.exception('unhandled exception')
        result = core.StepResult(
            'fatal', reason=str(e), exception=exception_name
        )

    # Because it may fail during argument parsing in the main function, we parse
    # sole '--rich-result' here by ourself.
    parser = cli.ArgumentParser(add_help=False, raise_bad_status=False)
    parser.add_argument('--rich-result', action='store_true')
    opts, _ = parser.parse_known_args(args)

    if opts.rich_result:
        output_rich_result(result)
    elif result.data['status'] == 'value':
        print(
            'BISECT_RESULT_VALUES=',
            ' '.join(str(v) for v in result.data['values']),
        )

    return EXIT_CODE_MAP[result.data['status']]


def switch_main_wrapper(
    switch_main: typing.Callable[..., None], args: tuple[str] | None
) -> int:
    def func(args):
        # "switch" step usually has no return values. No exception is good (old).
        switch_main(args)
        return core.StepResult('old')

    return step_main_wrapper(func, args)


T_co = typing.TypeVar('T_co', bound=core.BisectDomain, covariant=True)


class BisectorCommandLine(typing.Generic[T_co]):
    """Bisector command line interface.

    The typical usage pattern:

      if __name__ == '__main__':
          BisectorCommandLine(CustomDomain).main()

    where CustomDomain is a derived class of core.BisectDomain. See
    bisect_list.py as example.

    If you need to control the bisector using python code, the easier way is
    passing command line arguments to main() function. For example,
      bisector = Bisector(ChromeOSVersionDomain)
      bisector.main(
          'init', '--old', 'R118-15604.0.0', '--new', 'R119-15609.0.0',
          '--term-old', 'PASS', '--term-new', 'FAIL', '--board', 'nissa',
          '--dut', <DUT>
      )
      bisector.main('config', 'clear', 'switch')
      bisector.main(
          'config', 'switch', '--mode=bisectkit',
          './switch_cros_prebuilt.py', '--rich-result'
      )
      bisector.main(
          'config', 'switch', '--mode=bisectkit',
          './switch_tast_prebuilt.py', '--rich-result',
          '--chromeos-root', <chromeos_root>, '--board', 'nissa'
      )
      bisector.main(
          'config', 'eval', '--mode=bisectkit', './eval_cros_tast.py',
          '--rich-result', '--with-private-bundles',
          '--chromeos-root', <chromeos_root>,
          '--test-name', 'tast.audio.CyclicBench.rr10_4thread_10ms',
          '--reboot-before-test', '--prebuilt'
      )
      bisector.main('run')

    Switch and evaluate operations are configured via the 'config' sub command.
    There can be multiple switch command but only one eval command registered.
    If running 'config switch <switch_cmd>' multiple times, all switch command
    would be recorded. As a result, be sure to run 'config clear switch' once to
    clean up existing entry in the session file.

    Some bisections requires a DUT (device under test). This class support DUT
    management in two ways:
    1. By the '--dut' flag in 'init' sub command. The domain script needs to
       define the flag first.
    2. Auto allocate DUT when necessary. Only the python interface supports this
       option by passing in a DutManager to the class initializer:
           dut_manager = DutManager(...)
           bisector = Bisector(CustomDomain, dut_manager)
       When executing the domain script from the command line. The only way
       is to pass in a pre-allocated DUT via the '--dut' flag.

    The DUT is passed to the switch script via the '--dut' flag, and to the eval
    script as the first positional argument. As the above example shows, DUT
    information is missing when configuring the switch and eval script because
    it is handled by the class automatically.

    The class support different types of switch and eval scripts.

    Switch scripts:
    1. The script requires a DUT.
       In the case, the script should define a method called "action"
       which returns SwitchAction.WITH_DUT.
       This class would pass in --dut <DUT> when calling the script.

    2. The script has a build phase and a deploy phase. A DUT is only needed
       in the deploy phase.
       In this case, the script should define a method called "action"
       which returns SwitchAction.BUILD_AND_DEPLOY.
       The class would run the script twice. The first time with an additional
       flag "--no-deploy". The second time with additional flags
       "--dut <DUT> --deploy-only".

    3. The script doesn't need a DUT.
       In this case, the script should define a method called "action"
       which returns SwitchAction.WITHOUT_DUT.
       No additional flag would be passed to the script when executing.
    If the switch script is not a python script, it defaults to the
    "WITHOUT_DUT" case.

    4. VM dummy script.
        In this case, the scripts only passes a SWITCH_VM action,
        _switch_and_eval will uses this action to determine whether it is
        necessary to recreate a VM instance when switching CrOS version.

    Eval scripts:
    1. The script requires a DUT.
       In the case, the script should define a method called "action"
       which returns EvalAction.WITH_DUT.
       This class would pass in <DUT> as the first positional argument when
       calling the eval script.
    2. The script doesn't need a DUT.
       In this case, the eval script should define a method called "action"
       which returns EvalAction.WITHOUT_DUT.
       No additional flag would be passed to the script when executing.
    Similarly, if the eval script is not a python script, it defaults to the
    "WITHOUT_DUT" case.

    All combinations of switch and eval scripts are supported.
    For example, it's fine to set up switch scripts with or without DUT, and
    so to the eval script.
    """

    def __init__(  # pylint: disable=redefined-outer-name
        self,
        domain_cls: type[T_co],
        dut_manager: dut_manager.DutManager | None = None,
    ):
        self.domain_cls = domain_cls
        self.dut_manager = dut_manager

        self.domain: T_co | None = None
        self.session: str | None = None
        self.states: core.BisectStates | None = None
        self.strategy: strategy.NoisyBinarySearch | None = None
        self.future_build_depth: int = 1

        self.previous_strategy_state: str | None = None

    @property
    def config(self) -> core.BisectConfig:
        assert self.states
        return self.states.config

    def _get_term_map(self):
        return {
            'old': self.config.get('term_old'),
            'new': self.config.get('term_new'),
        }

    def _format_result_counter(self, result_counter):
        return core.RevInfo.format_result_counter(
            result_counter, self._get_term_map()
        )

    def future_switch_versions(  # pylint: disable=dangerous-default-value
        self,
        prev_rev: str,
        depth: int,
        possible_status: list[str] = [
            'old',
            'new',
        ],
    ) -> list[str]:
        """Returns a list of versions that may need to switch in the future.

        Assume p = len(possible_status), the longest list length returned would be:
        p^0 + p^1 + ... + p^depth.
        """

        def _enumerate_possible_statuses(
            bisector_cli: BisectorCommandLine,
            states: core.BisectStates,
            bisector_strategy: strategy.NoisyBinarySearch,
            prev_rev: str,
            remain_depth: int,
            possible_status: list[str],
        ) -> typing.Iterator[int]:
            """Recursively enumerates all the possible test result combinations."""
            if remain_depth < 0:
                return
            if bisector_strategy.is_done():
                return

            idx = bisector_strategy.next_idx(
                bisector_cli.estimate_cost(prev_rev)
            )
            rev = states.idx2rev(idx)
            yield idx

            # When Gemini is enabled, sample candidate statuses twice during lookahead
            # to account for LLM non-determinism. Unlike traditional math models which
            # are strictly deterministic, Gemini may suggest different candidate commits
            # across trials even with identical input histories, allowing lookahead to
            # explore multiple potential pathways. Duplicate predictions are naturally
            # deduplicated downstream by the strategy sample collection.
            statuses_to_try = possible_status
            if getattr(bisector_strategy, 'gemini', None):
                statuses_to_try = possible_status * 2

            for status in statuses_to_try:
                # Dummy switch and eval time could be ignored here because the strategy
                # algorithm will ignore empty time sample and calculate the average of
                # other samples automatically.
                sample = {
                    'rev': rev,
                    'index': states.rev2idx(rev),
                    'status': status,
                    'reason': (
                        'Simulated result to predict the next revision for'
                        ' early build kickoff. There are no test logs or'
                        ' failure details available, so no failure analysis'
                        ' can be performed on this sample.'
                    ),
                }
                states_copy = copy.deepcopy(states)
                strategy_copy = copy.deepcopy(bisector_strategy)
                if hasattr(strategy_copy, 'set_logger_prefix'):
                    strategy_copy.set_logger_prefix('[Simulation] ')

                try:
                    states_copy.add_history('sample', **sample)
                    strategy_copy.check_verification_range()
                    strategy_copy.add_sample(idx, **sample)
                except (
                    errors.VerifyNewBehaviorFailed,
                    errors.VerifyOldBehaviorFailed,
                    errors.VerifyInitialRangeFailed,
                    errors.WrongAssumption,
                ):
                    # Ignore test verfication errors.
                    continue
                yield from _enumerate_possible_statuses(
                    bisector_cli,
                    states_copy,
                    strategy_copy,
                    rev,
                    remain_depth - 1,
                    possible_status,
                )

        dedup_indexes = sorted(
            set(
                _enumerate_possible_statuses(
                    self,
                    self.states,
                    self.strategy,
                    prev_rev,
                    depth,
                    possible_status,
                )
            )
        )
        return [self.states.idx2rev(x) for x in dedup_indexes]

    def cmd_reset(self, _opts: ResetOptions):
        """Resets bisect session and clean up saved result."""
        self.states.reset()

    def cmd_init(self, opts: InitOptions):
        """Initializes bisect session.

        See init command's help message for more detail.
        """
        try:
            if (opts.old_value is None) != (opts.new_value is None):
                raise errors.ArgumentError(
                    '--old-value and --new-value', 'both should be specified'
                )
            if opts.old_value is not None and opts.old_value == opts.new_value:
                raise errors.ArgumentError(
                    '--old-value and --new-value',
                    'their values should be different',
                )
            if opts.recompute_init_values and opts.old_value is None:
                raise errors.ArgumentError(
                    '--recompute-init-values',
                    '--old-value and --new-value must be specified '
                    'when --recompute-init-values is present',
                )

            config, candidates = self.domain_cls.init(opts)
            revlist = candidates['revlist']
            logger.info('found %d candidates to bisect', len(revlist))
            logger.debug('revlist %r', revlist)
            if 'new' not in config:
                config['new'] = opts.new
            if 'old' not in config:
                config['old'] = opts.old
            if len(revlist) < 2:
                raise errors.TooFewRevisionsError(
                    f'Too few revisions: {revlist}'
                )
            assert config['new'] in revlist
            assert config['old'] in revlist
            old_idx = revlist.index(config['old'])
            new_idx = revlist.index(config['new'])
            assert old_idx < new_idx

            if opts.test_name:
                config['test_name'] = opts.test_name
            if opts.experiments:
                config['experiments'] = opts.experiments

            config.update(
                confidence=opts.confidence,
                noisy=opts.noisy,
                term_old=opts.term_old,
                term_new=opts.term_new,
                old_value=opts.old_value,
                new_value=opts.new_value,
                recompute_init_values=opts.recompute_init_values,
                endpoint_verification=opts.endpoint_verification,
            )
            rev_details = candidates.get('details', {})
            self.states.init_states(config, revlist, rev_details)
        except Exception as e:
            exception_name = e.__class__.__name__
            self.states.add_history(
                'failed',
                text='%s: %s' % (exception_name, e),
                exception_name=exception_name,
            )
            self.states.save()
            raise

        self.states.save()

    def _step_log_path(self, rev, step):
        # relative to session directory
        log_path = 'log/{bisector}.{timestamp}.{rev}.{step}.txt'.format(
            bisector=self.domain_cls.__name__,
            rev=util.escape_rev(rev),
            timestamp=time.strftime('%Y%m%d-%H%M%S'),
            step=step,
        )
        return log_path

    def _evaluate(
        self,
        eval_cmd: list[str],
        rev: str,
        sample: dict,
        action: EvalAction,
        dut: str | None = None,
    ) -> bool:
        """Evaluate at the rev.

        Args:
          eval_cmd: the eval command.
          rev: the revision.
          sample: the dict to append result to. See _switch_and_eval() for details.
          action: the action to perform.
          dut: the DUT.

        Returns:
          Whether the eval is successful.
        """
        assert self.strategy

        if action == EvalAction.WITH_DUT:
            assert dut, 'dut must be present with action %s' % action
            eval_cmd = eval_cmd + [dut]

        logger.debug('eval (%s) at rev=%s', action, rev)

        eval_log = self._step_log_path(rev, 'eval')
        eval_log_full_path = common.get_session_log_path(self.session, eval_log)

        t0 = time.time()
        result = do_evaluate(
            eval_cmd,
            self.domain,
            rev,
            self.states.details.get(rev, {}),
            eval_log_full_path,
            capture_values=self.strategy.is_value_bisection(),
        )
        t1 = time.time()
        sample['eval_time'] = t1 - t0

        status = result.data['status']
        sample['status'] = status
        if os.path.exists(eval_log_full_path):
            sample['eval_log'] = eval_log
        if result:
            if 'reason' in result.data:
                sample['reason'] = result.data['reason']
            if 'exception' in result.data:
                sample['exception'] = result.data['exception']
            if 'times' in result.data:
                sample['times'] = result.data['times']
        if status in ('skip', 'fatal'):
            logger.debug('eval failed => %s', status)
            return False

        if self.strategy.is_value_bisection():
            if status != 'value':
                raise errors.ExecutionFatalError(
                    'eval command (%s) terminated normally but did not output values'
                    % eval_cmd
                )
            sample['values'] = result.data['values']
            sample['status'] = self.strategy.classify_result_from_values(
                result.data['values']
            )

        return True

    def _switch(
        self,
        switch_cmd: list[str],
        rev: str,
        sample: dict,
        action: SwitchAction,
        log_file: str,
        dut: str | None = None,
    ) -> bool:
        """Switch to a specific rev.

        Args:
          switch_cmd: the switch command.
          rev: the revision.
          sample: the dict to append result to. See _switch_and_eval() for details.
          action: see SwitchAction for details.
          log_file: the path to switch log.
          dut: the DUT.

        Returns
          Whether the switch is successful.
        """
        logger.debug('switch (%s) to rev=%s', action, rev)

        log_full_path = common.get_session_log_path(self.session, log_file)

        extra_args = []
        if action in [
            SwitchAction.DEPLOY,
            SwitchAction.BUILD_AND_DEPLOY,
            SwitchAction.WITH_DUT,
        ]:
            assert dut, 'dut must be present with action %s' % action
            extra_args += ['--dut', dut]
        if action == SwitchAction.BUILD:
            extra_args += ['--no-deploy']
        elif action == SwitchAction.DEPLOY:
            extra_args += ['--deploy-only']

        t0 = time.time()
        result = do_switch(
            switch_cmd + extra_args,
            self.domain,
            rev,
            self.states.details.get(rev, {}),
            log_full_path,
        )
        t1 = time.time()

        duration = t1 - t0
        sample['switch_time'] = sample.get('switch_time', 0) + duration
        sample['switch_%s_time' % action] = duration

        # Note: the following field may already exist in "sample" so they may be
        # overwritten.
        status = result.data['status'] if result else None
        sample['status'] = status
        if os.path.exists(log_full_path):
            sample['switch_log'] = log_file
        if result:
            if 'reason' in result.data:
                sample['reason'] = result.data['reason']
            if 'exception' in result.data:
                sample['exception'] = result.data['exception']
            if 'times' in result.data:
                sample['times'] = result.data['times']
        if status in ('skip', 'fatal'):
            logger.debug('switch (%s) failed => %s', action, status)
            return False
        return True

    def _switch_and_eval(self, rev, prev_rev=None):
        """Switches and evaluates given version.

        If current version equals to target, switch step will be skip.

        Args:
          rev: Target version.
          prev_rev: Previous version.

        Raises:
          errors.InternalError if the control flow reaches unreachable.

        Returns:
          (step, sample):
            step: Last step executed ('switch' or 'eval').
            sample (dict): sampling result of `rev`. The dict contains:
              status: Execution result ('old', 'new', 'fatal', or 'skip').
              values: For eval bisection, collected values from eval step.
              reason: Failure reason, if any.
              exception: The exception class which causes the error, if any.
              switch_time: Total time in switch step
              switch_<type>_time: Time spent in sub step(s) of switch.
              eval_time: How much time in eval step
        """
        # pre-build future revisions.
        if self.config.get('future_build'):
            revisions = self.future_switch_versions(
                prev_rev, self.future_build_depth
            )
            logger.info('Future builds: %s', revisions)
            for r in revisions:
                logger.info('Kicking off future build for %s', r)
                future_build_log = self._step_log_path(r, 'future_build')
                future_build_log_full_path = common.get_session_log_path(
                    self.session, future_build_log
                )
                do_future_build(
                    self.config.get('future_build'),
                    self.domain,
                    r,
                    self.states.details.get(r, {}),
                    future_build_log_full_path,
                )

        # We treat 'rev' as source of truth. 'index' is redundant and just
        # informative.
        sample = {'rev': rev, 'index': self.states.rev2idx(rev)}

        # We use a dummy script to determine if this is a VM bisection.
        vm_cros_version = None
        has_switch_vm_cmd = False
        switch_build_deploy_cmds = []
        switch_with_dut_cmds = []
        switch_without_dut_cmds = []
        for cmd in self.config.get('switch'):
            action = switch_eval_script_action(cmd, SwitchAction.WITHOUT_DUT)
            if action == SwitchAction.BUILD_AND_DEPLOY:
                switch_build_deploy_cmds.append(cmd)
            elif action == SwitchAction.WITH_DUT:
                switch_with_dut_cmds.append(cmd)
            elif action == SwitchAction.WITHOUT_DUT:
                switch_without_dut_cmds.append(cmd)
            elif action == SwitchAction.SWITCH_VM:
                has_switch_vm_cmd = True
        # VM bisection needs to re-create a new instance if version is
        # different.
        if has_switch_vm_cmd:
            vm_cros_version = rev
        logger.debug('switch_build_deploy_cmds: %s', switch_build_deploy_cmds)
        logger.debug('switch_with_dut_cmds: %s', switch_with_dut_cmds)
        logger.debug('switch_without_dut_cmds: %s', switch_without_dut_cmds)
        logger.debug('has_switch_vm_cmd: %s', has_switch_vm_cmd)
        logger.debug('vm_cros_version: %s', vm_cros_version)

        eval_with_dut_cmd = None
        eval_without_dut_cmd = None
        cmd = self.config.get('eval')
        assert cmd, 'no eval script set'
        action = switch_eval_script_action(cmd, EvalAction.WITHOUT_DUT)
        if action == EvalAction.WITH_DUT:
            eval_with_dut_cmd = cmd
        elif action == EvalAction.WITHOUT_DUT:
            eval_without_dut_cmd = cmd
        logger.debug('eval_with_dut_cmd: %s', eval_with_dut_cmd)
        logger.debug('eval_without_dut_cmd: %s', eval_without_dut_cmd)

        # if _switch() is called multiple times, the same log file is appended.
        switch_log = self._step_log_path(rev, 'switch')

        for cmd in switch_without_dut_cmds:
            if not self._switch(
                cmd, rev, sample, SwitchAction.WITHOUT_DUT, switch_log
            ):
                return 'switch', sample

        # Shortcut if no DUT is needed for both switch and eval.
        if not switch_build_deploy_cmds and not switch_with_dut_cmds:
            # Only one of eval_without_dut_cmd and eval_without_dut_cmd should
            # exist.
            if eval_without_dut_cmd:
                self._evaluate(
                    eval_without_dut_cmd, rev, sample, EvalAction.WITHOUT_DUT
                )
                return 'eval', sample

        # A DUT is required for switch and/or eval operation.
        # First, build the image. DUT does not need to present at this point
        # yet.
        if prev_rev != rev:
            for cmd in switch_build_deploy_cmds:
                if not (
                    self._switch(
                        cmd, rev, sample, SwitchAction.BUILD, switch_log
                    )
                ):
                    return 'switch', sample

        def _do_switch_with_dut(dut: str) -> bool:
            """switch with the specific DUT.

            Args:
              dut: the DUT.

            Returns:
              successful or not.
            """
            for cmd in switch_build_deploy_cmds:
                if not (
                    self._switch(
                        cmd,
                        rev,
                        sample,
                        SwitchAction.DEPLOY,
                        switch_log,
                        dut,
                    )
                ):
                    return False
            for cmd in switch_with_dut_cmds:
                if not (
                    self._switch(
                        cmd,
                        rev,
                        sample,
                        SwitchAction.WITH_DUT,
                        switch_log,
                        dut,
                    )
                ):
                    return False
            return True

        # A DUT can be specified via "--dut" flag when running domain_cls
        # scripts with "init" sub command, either via command line execution or
        # through wrapper.py. If the config presents, it takes precedence over
        # dut_manager. In the way the domain_cls scripts can be executed alone
        # from the command line (b/299388823).
        pre_configured_dut = self.config.get('dut')
        logger.debug('pre-configured DUT: %s', pre_configured_dut)
        assert (
            pre_configured_dut or self.dut_manager
        ), 'either pre-configured DUT or dut manager must be present'

        if pre_configured_dut:
            if prev_rev != rev:
                if not _do_switch_with_dut(pre_configured_dut):
                    return 'switch', sample
            if eval_with_dut_cmd:
                self._evaluate(
                    eval_with_dut_cmd,
                    rev,
                    sample,
                    EvalAction.WITH_DUT,
                    pre_configured_dut,
                )
                return 'eval', sample
        else:
            with self.dut_manager.provision(
                vm_cros_version=vm_cros_version
            ) as dut:
                # Deploy is needed if a new version is to be deployed, or a new DUT
                # has been allocated. If it's the same version and the same DUT,
                # we don't need to deploy again.
                if prev_rev != rev or not self.dut_manager.is_previous_dut():
                    if not _do_switch_with_dut(dut):
                        return 'switch', sample
                if eval_with_dut_cmd:
                    self._evaluate(
                        eval_with_dut_cmd, rev, sample, EvalAction.WITH_DUT, dut
                    )
                    return 'eval', sample

        # Even switch requires a DUT, eval might not (e.g., eval manually).
        if eval_without_dut_cmd:
            self._evaluate(
                eval_without_dut_cmd, rev, sample, EvalAction.WITHOUT_DUT
            )
            return 'eval', sample

        # One of 'eval_with_dut_cmd' or 'eval_without_dut_cmd' should exist.
        raise errors.InternalError('Control flow should not reach here.')

    def estimate_cost(self, prev_rev):
        avg_switch_time = math_util.Averager()
        avg_eval_time = math_util.Averager()
        avg_eval_time_by_status: collections.defaultdict[
            str, math_util.Averager
        ] = collections.defaultdict(math_util.Averager)
        for entry in self.states.data['history']:
            if entry['event'] != 'sample':
                continue
            if 'switch_time' in entry:
                avg_switch_time.add(entry['switch_time'])
            if 'eval_time' in entry:
                avg_eval_time.add(entry['eval_time'])
                avg_eval_time_by_status[entry['status']].add(entry['eval_time'])

        if avg_switch_time.count == 0:
            return None
        if avg_eval_time.count == 0:
            return None

        cost_table = []
        for info in self.strategy.rev_info:
            if info.rev == prev_rev:
                switch_time = 0
            else:
                # TODO(kcwu): estimate switch cost sophisticatedly
                switch_time = avg_switch_time.average()
            if (
                avg_eval_time_by_status['old'].count > 0
                and avg_eval_time_by_status['new'].count > 0
            ):
                cost = (
                    switch_time + avg_eval_time_by_status['old'].average(),
                    switch_time + avg_eval_time_by_status['new'].average(),
                )
            else:
                cost = (
                    switch_time + avg_eval_time.average(),
                    switch_time + avg_eval_time.average(),
                )
            cost_table.append(cost)
        return cost_table

    def _next_idx_iter(self, opts: RunOptions, force):
        prev_rev = None
        if opts.revs:
            for rev in opts.revs:
                idx = self.states.rev2idx(rev)
                logger.info(
                    'try idx=%d rev=%s (command line specified)', idx, rev
                )
                prev_rev = yield idx, rev
                if opts.once:
                    break
        else:
            while force or not self.strategy.is_done():
                idx = self.strategy.next_idx(self.estimate_cost(prev_rev))
                rev = self.states.idx2rev(idx)
                logger.info('try idx=%d rev=%s', idx, rev)
                prev_rev = yield idx, rev
                force = False
                if opts.once:
                    break

    def _make_decision(self, text):
        if sys.exc_info() != (None, None, None):
            logger.exception('decision: %s', text)
        else:
            logger.info('decision: %s', text)
        self.states.add_history('decision', text=text)

    def _show_step_summary(self):
        """Show current status after each bisect step."""
        current_state = self.strategy.state
        if self.config.get('recompute_init_values'):
            if (
                self.previous_strategy_state == self.strategy.INITED
                and current_state == self.strategy.STARTED
            ):
                assert self.strategy.threshold is not None
                self._make_decision(
                    'After verifying with the initial revisions, '
                    'use %f as the bisect threshold.' % self.strategy.threshold
                )

        self.previous_strategy_state = current_state

        self.strategy.show_summary()

    def save_progress_with_sample(self, sample: dict):
        """Add sample to history and save internal states.

        Args:
            sample: A sample from _switch_and_eval().

        Raises:
            errors.VerificationFailed
            errors.TooManyTemporaryErrors
        """
        self.states.add_history('sample', **sample)
        try:
            # Call add_sample() to update strategy internal states
            # before saving it to the session file.
            # add_sample() calls self.strategy.check_verification_range()
            # internally, so it bails out if bisection range is unlikely
            # true.
            self.strategy.add_sample(sample['index'], **sample)
        finally:
            # add_sample() may throw exception, but we want to save
            # strategy internal states nevertheless. For example when
            # VerificationFailed is thrown, we want to keep the state so
            # an re-execution throw VerifyNewBehaviorFailed immediately.
            # Also the states are saved after the observation is
            # recorded (e.g., add_history() is called), so we don't loss
            # observations unexpectedly across executions.
            self.states.strategy_states = self.strategy.export_states()
            self.states.save()

    def cmd_run(self, opts: RunOptions):
        """Performs bisection.

        See run command's help message for more detail.

        Raises:
          errors.VerifyInitialRangeFailed: The initial bisection range doesn't
              pass the statistical test.
          errors.VerificationFailed: The bisection range is verified false. We
              expect 'old' at the first rev and 'new' at last rev.
          errors.TooManyTemporaryErrors: Too many errors to narrow down further the
              bisection range.
        """
        # Set dummy values in case exception raised before loop.
        idx, rev = -1, None
        try:
            assert self.config.get('switch')
            assert self.config.get('eval')

            self.states.add_history(
                'start_range',
                old=self.config.get('old'),
                new=self.config.get('new'),
            )
            term_map = self._get_term_map()
            prev_rev: str | None = opts.current_rev
            force = opts.force
            idx_gen = self._next_idx_iter(opts, force)
            while True:
                try:
                    idx, rev = idx_gen.send(prev_rev)
                except StopIteration:
                    break
                if not force:
                    # Bail out if bisection range is unlikely true in order to prevent
                    # wasting time. This is necessary because some configurations (say,
                    # confidence) may be changed before cmd_run() and thus the bisection
                    # range becomes not acceptable.
                    self.strategy.check_verification_range()

                step, sample = self._switch_and_eval(rev, prev_rev=prev_rev)
                status = term_map.get(sample['status'], sample['status'])
                if 'values' in sample:
                    logger.info(
                        'rev=%s status => %s: %s', rev, status, sample['values']
                    )
                else:
                    logger.info('rev=%s status => %s', rev, status)

                self.save_progress_with_sample(sample)

                if sample['status'] == 'fatal':
                    e = errors.reconstruct_from_string(
                        sample.get('exception'), sample.get('reason')
                    )
                    if e:
                        raise e
                    if 'reason' in sample:
                        reason_text = ': %s' % sample['reason']
                    else:
                        reason_text = ''
                    raise errors.ExecutionFatalError(
                        '%s failed' % step + reason_text
                    )
                force = False

                self._show_step_summary()

                if step == 'switch' and sample['status'] == 'skip':
                    # Previous switch failed and thus the current version is unknown. Set
                    # it None, so next switch operation won't be bypassed (due to
                    # optimization).
                    prev_rev = None
                else:
                    prev_rev = rev

            logger.info('done')
            old_idx, new_idx = self.strategy.get_range()
            self.states.add_history('done')
            self.states.save()
        except Exception as e:
            exception_name = e.__class__.__name__
            self.states.add_history(
                'failed',
                text='%s: %s' % (exception_name, e),
                index=idx,
                rev=rev,
                exception_name=exception_name,
            )
            self.states.save()
            raise
        finally:
            if rev and self.strategy.state == self.strategy.STARTED:
                # progress so far
                old_idx, new_idx = self.strategy.get_range()
                self.states.add_history(
                    'range',
                    old=self.states.idx2rev(old_idx),
                    new=self.states.idx2rev(new_idx),
                )
                init_range_verified = self.strategy.init_range_verified
                self.states.add_history(
                    'verified', verified_status=init_range_verified
                )
                self.states.save()

    def cmd_view(self, opts: ViewOptions):
        """Shows remaining candidates."""
        if not self.states.inited:
            if opts.json:
                print('{}')
            else:
                print('not init yet')
            return

        summary: dict[str, typing.Any] = {
            'rev_info': [],
        }
        for sample in self.states.history:
            if sample.get('event') != 'sample':
                continue
            if opts.timestamp is not None:
                t = sample.get('timestamp', None)
                if t is not None and t >= opts.timestamp:
                    continue
            try:
                self.strategy.add_sample(sample['index'], **sample)
            except (
                errors.WrongAssumption,
                errors.VerificationFailed,
                errors.TooManyTemporaryErrors,
            ):
                pass

        for info in self.strategy.rev_info:
            info_dict = info.to_dict()
            detail = self.states.details.get(info.rev, {})
            info_dict.update(detail)
            summary['rev_info'].append(info_dict)

        try:
            old_idx, new_idx = self.strategy.get_range()
            highlight_old_idx, highlight_new_idx = self.strategy.get_range(
                self.strategy.confidence / 10.0
            )
        except errors.WrongAssumption:
            pass
        else:
            old = self.states.idx2rev(old_idx)
            new = self.states.idx2rev(new_idx)
            summary.update(
                {
                    'current_range': [old, new],
                    'highlight_range': [
                        self.states.idx2rev(highlight_old_idx),
                        self.states.idx2rev(highlight_new_idx),
                    ],
                    'prob': self.strategy.get_prob(),
                    'remaining_steps': self.strategy.remaining_steps(),
                }
            )

        if opts.verbose or opts.json:
            interesting_indexes = set(range(len(summary['rev_info'])))
        elif 'current_range' not in summary:
            interesting_indexes = set()
        else:
            interesting_indexes = set([old_idx, new_idx])
            if self.strategy.prob:
                for i, p in enumerate(self.strategy.prob):
                    if p > 0.05:
                        interesting_indexes.add(i)

        self.domain.fill_candidate_summary(summary)
        # This function should be called after get_range(), otherwise reclassify()
        # won't be called and we will get incorrect result_counter for value
        # bisection.
        summary.update(
            {
                'reproduced': self.strategy.check_reproduced(),
            }
        )

        if opts.json:
            print(json.dumps(summary, indent=2, sort_keys=True))
        else:
            self.show_bisect_summary(
                summary, interesting_indexes, verbose=opts.verbose
            )

    def show_bisect_summary(self, summary, interesting_indexes, verbose=False):
        for link in summary.get('links', []):
            if 'name' in link and 'url' in link:
                print('%s: %s' % (link['name'], link['url']))
            if 'note' in link:
                print(link['note'])

        if 'current_range' in summary:
            old, new = summary['current_range']
            old_idx = self.states.data['revlist'].index(old)
            new_idx = self.states.data['revlist'].index(new)
            print(
                'Range: (%s, %s], %s revs left'
                % (old, new, (new_idx - old_idx))
            )
            if summary.get('remaining_steps'):
                print('(roughly %d steps)' % summary['remaining_steps'])
        else:
            old_idx, new_idx = None, None

        for i, rev_info in enumerate(summary['rev_info']):
            if not any(
                [
                    verbose,
                    old_idx is not None
                    and new_idx is not None
                    and old_idx <= i <= new_idx,
                    rev_info['result_counter'],
                ]
            ):
                continue

            detail = []
            if self.strategy.is_noisy() and summary.get('prob'):
                detail.append('%.4f%%' % (summary['prob'][i] * 100))
            if rev_info['result_counter']:
                detail.append(
                    self._format_result_counter(rev_info['result_counter'])
                )
            values = sorted(rev_info['values'])
            if len(values) == 1:
                detail.append('%.3f' % values[0])
            elif len(values) > 1:
                detail.append(
                    'n=%d,avg=%.3f,median=%.3f,min=%.3f,max=%.3f'
                    % (
                        len(values),
                        sum(values) / len(values),
                        values[len(values) // 2],
                        values[0],
                        values[-1],
                    )
                )

            print('[%d] %s\t%s' % (i, rev_info['rev'], ' '.join(detail)))
            if i in interesting_indexes:
                if 'comment' in rev_info:
                    print('\t%s' % rev_info['comment'])
                for action in rev_info.get('actions', []):
                    if 'rev' in action and 'commit_summary' in action:
                        print(
                            '%s %r'
                            % (action['rev'][:10], action['commit_summary'])
                        )
                    if 'link' in action:
                        print('\t%s' % action['link'])

    def _strategy_factory(
        self,
        ignore_skip: bool = False,
        ignore_history=False,
    ) -> strategy.NoisyBinarySearch:
        """Create a strategy.NoisyBinarySearch instance.

        Past bisect history are loaded via BisectStates, if any.

        Args:
          ignore_skip: Whether to ignore 'skip' entries.
            It is useful when retring a bisection since 'skip' are likely to be temporary errors.
            By ignoring the 'skip' entries, we are able to retry a bisection which was failed
            due to errors.TooManyTemporaryErrors previously.
          ignore_history: Whether to ignore all history. Only get the revlist but not previous running result.

        Returns:
          A strategy.NoisyBinarySearch instance.

        """
        if not self.states.inited:
            return None
        term_map = {
            'old': self.config.get('term_old'),
            'new': self.config.get('term_new'),
        }
        rev_info = self.states.get_rev_info(
            term_map,
            ignore_skip=ignore_skip,
            ignore_history=ignore_history,
        )
        assert rev_info
        enable_gemini = bool(
            # Only enable gemini under experiments.
            # If experiment flag is ON,
            experiment.is_in_experiment(
                self.config.get('experiments', []),
                experiment.ID.GEMINI_ASSIT,
            )
            # and is bisecting Chrome or ChromeOS
            and (
                self.config.get('new').startswith('refs/heads/main@')
                or cros_util.is_cros_or_snapshot_version(self.config.get('new'))
            )
            # and is regression from pass to fail
            and self.config.get('term_old') == 'PASS'
            and self.config.get('term_new') == 'FAIL'
        )
        if enable_gemini:
            is_chrome = self.config.get('new').startswith('refs/heads/main@')
            if is_chrome:
                source_root = (
                    os.path.join(self.config.get('chrome_root'), 'src')
                    if self.config.get('chrome_root')
                    else None
                )
            else:
                source_root = self.config.get('chromeos_root')

            return strategy.GeminiFusionStrategy(
                rev_info,
                self.states.rev2idx(self.config.get('old')),
                self.states.rev2idx(self.config.get('new')),
                old_value=self.config.get('old_value'),
                new_value=self.config.get('new_value'),
                term_map=term_map,
                recompute_init_values=self.config.get('recompute_init_values'),
                confidence=self.config.get('confidence'),
                observation=self.config.get('noisy'),
                endpoint_verification=self.config.get('endpoint_verification'),
                saved_states=(
                    None if ignore_history else self.states.strategy_states
                ),
                enable_gemini=enable_gemini,
                test_name=self.config.get('test_name'),
                source_root=source_root,
                chromeos_mirror=self.config.get('chromeos_mirror'),
                board=self.config.get('board'),
                session=self.session,
            )

        return strategy.NoisyBinarySearch(
            rev_info,
            self.states.rev2idx(self.config.get('old')),
            self.states.rev2idx(self.config.get('new')),
            old_value=self.config.get('old_value'),
            new_value=self.config.get('new_value'),
            term_map=term_map,
            recompute_init_values=self.config.get('recompute_init_values'),
            confidence=self.config.get('confidence'),
            observation=self.config.get('noisy'),
            endpoint_verification=self.config.get('endpoint_verification'),
            saved_states=(
                None if ignore_history else self.states.strategy_states
            ),
        )

    def check_done(self, session: typing.Optional[str] = None) -> bool:
        """Checks whether the bisection is done by looking at the history.

        If the bisection is successful (i.e., culprits are found in the bisection), returns True.
        If the bisection can not be reproduced (e.g., verification error), exceptions are raised.

        Args:
          session: The session name.

        Returns:
          True if the bisection already has a verdict.

        Raises:
          errors.VerificationFailed
          errors.WrongAssumption
        """
        logger.debug('checking past state of %s', self.domain_cls.__name__)
        self._create_states(session=session)
        if not self.states.inited and not self.states.load_states():
            logger.debug(
                'failed to load state for %s', self.domain_cls.__name__
            )
            return False

        self.strategy = self._strategy_factory()
        if self.strategy.is_done():
            logger.debug('%s done', self.domain_cls.__name__)
            return True
        # Try to get the next rev idx in order to know whether the bisection is
        # completed or not.
        # Raise VerificationFailed related errors if the bisection has been
        # failed in the past.
        # Since the cost_table is irrelevant to whether the bisection is
        # completed or not (i.e., whether idx = None or not), pass None to it.
        idx = self.strategy.next_idx(cost_table=None)
        logger.debug('idx for %s: %s', self.domain_cls.__name__, idx)
        # None means no more work to do.
        return idx is None

    def current_status(self, session=None) -> dict:
        """Gets current bisect status.

        Returns:
          A dict describing current status. It contains following items:
            inited: True iff the session file is initialized (init command has been
                invoked). If not, below items are omitted.
            old: Start of current estimated range.
            new: End of current estimated range.
            verified: The bisect range is already verified.
            estimated_noise: New estimated noise.
            done: True if bisection is done, otherwise False.
        """
        self._create_states(session=session)
        if not self.states.inited and not self.states.load_states():
            # load_states() is called, but unable to load session file or
            # initialization is unsuccessful.
            return {"inited": False}

        self.strategy = self._strategy_factory()
        left, right = self.strategy.get_range()
        estimated_noise = self.strategy.get_noise_observation()

        result = {
            "inited": True,
            "old": self.states.idx2rev(left),
            "new": self.states.idx2rev(right),
            "verified": self.strategy.check_reproduced(),
            "estimated_noise": estimated_noise,
            "done": self.strategy.is_done(),
        }
        return result

    def cmd_log(self, opts: LogOptions):
        """Prints what has been done so far."""
        history = []
        for entry in self.states.data['history']:
            if opts.before and entry['timestamp'] >= opts.before:
                continue
            if opts.after and entry['timestamp'] <= opts.after:
                continue
            history.append(entry)

        if opts.json:
            print(json.dumps(history, indent=2))
            return

        for entry in history:
            entry_time = datetime.datetime.fromtimestamp(
                int(entry['timestamp'])
            )
            if entry.get('event', 'sample') == 'sample':
                if entry.get('times', 1) > 1:
                    status = '%s*%d' % (entry['status'], entry['times'])
                else:
                    status = entry['status']
                print(
                    '{datetime} {rev} {status} {values} {comment}'.format(
                        datetime=entry_time,
                        rev=entry['rev'],
                        status=status,
                        values=entry.get('values', ''),
                        comment=entry.get('comment', ''),
                    )
                )
            else:
                print('%s %r' % (entry_time, entry))

    def cmd_next(self, opts: NextOptions):
        """Prints next suggested rev to bisect."""
        if self.strategy.is_done():
            print('done')
            return

        idx = self.strategy.next_idx(self.estimate_cost(opts.current_rev))
        rev = self.states.idx2rev(idx)
        print(rev)

    def _add_revs_status_helper(self, revs, status):
        if self.strategy.is_value_bisection():
            assert status not in ('old', 'new')
        for rev, times in revs:
            idx = self.states.rev2idx(rev)
            sample = {'rev': rev, 'index': idx, 'status': status}
            # times=1 is default in the loader. Add 'times' entry only if necessary
            # in order to simplify the dict.
            if times > 1:
                sample['times'] = times
            self.save_progress_with_sample(sample)

    def cmd_new(self, opts: OldNewSkipOptions):
        """Tells bisect engine the said revs have "new" behavior."""
        logger.info('set [%s] as %s', opts.revs, self.config.get('term_new'))
        self._add_revs_status_helper(opts.revs, 'new')

    def cmd_old(self, opts: OldNewSkipOptions):
        """Tells bisect engine the said revs have "old" behavior."""
        logger.info('set [%s] as %s', opts.revs, self.config.get('term_old'))
        self._add_revs_status_helper(opts.revs, 'old')

    def cmd_skip(self, opts: OldNewSkipOptions):
        """Tells bisect engine the said revs have "skip" behavior."""
        logger.info('set [%s] as skip', opts.revs)
        self._add_revs_status_helper(opts.revs, 'skip')

    def _create_states(self, session=None):
        self.session = session
        session_file = common.get_session_log_path(
            session, self.domain_cls.__name__
        )

        if self.states:
            assert self.states.session_file == session_file
        else:
            self.states = core.BisectStates(session_file)

    def cmd_config(self, opts: ConfigOptions):
        """Configures additional setting.

        See config command's help message for more detail.
        """
        assert self.states

        if not self.states.inited and not self.states.load_states():
            raise errors.Uninitialized
        self.domain = self.domain_cls(self.states.config)

        if opts.key != 'clear' and not opts.value:
            print(self.states.config[opts.key])
            return

        if opts.key in [
            'switch',
            'eval',
            'future_build',
        ]:
            exec_opts = step_command_parser.parse_args(opts.value)
            result = cli.check_executable(exec_opts.command[0])
            if result:
                raise errors.ArgumentError('%s command' % opts.key, result)

            if opts.key == 'switch':
                self.states.config.setdefault('switch', [])
                self.states.config['switch'].append(opts.value)
            else:
                self.states.config[opts.key] = opts.value

        elif opts.key == 'clear':
            for value in opts.value:
                self.states.config.pop(value, None)  # type: ignore[misc]

        elif opts.key == 'confidence':
            if len(opts.value) != 1:
                raise errors.ArgumentError(
                    'confidence value',
                    'expected 1 value, %d values given' % len(opts.value),
                )
            try:
                self.states.config[opts.key] = float(opts.value[0])
            except ValueError as e:
                raise errors.ArgumentError(
                    'confidence value',
                    'invalid float value: %r' % opts.value[0],
                ) from e

        elif opts.key == 'noisy':
            if len(opts.value) != 1:
                raise errors.ArgumentError(
                    'noisy value',
                    'expected 1 value, %d values given' % len(opts.value),
                )
            self.states.config[opts.key] = opts.value[0]

        elif opts.key in ('term_old', 'term_new'):
            if len(opts.value) != 1:
                raise errors.ArgumentError(
                    opts.key,
                    'expected 1 value, %d values given' % len(opts.value),
                )
            self.states.config[opts.key] = opts.value[0]

        else:
            # unreachable
            assert 0

        self.states.save()

    def create_argument_parser(self, prog):
        if self.domain_cls.help:
            description = self.domain_cls.help
        else:
            description = 'Bisector for %s' % self.domain_cls.__name__
        description += textwrap.dedent(
            """
        When running switcher and evaluator, it will set BISECT_REV environment
        variable, indicates current rev to switch/evaluate.
    """
        )

        parents = [cli.create_session_optional_parser()]
        parser = cli.ArgumentParser(
            prog=prog,
            formatter_class=argparse.RawDescriptionHelpFormatter,
            description=description,
            raise_bad_status=False,
        )
        subparsers = parser.add_subparsers(
            dest='command', title='commands', metavar='<command>', required=True
        )

        parser_reset = subparsers.add_parser(
            'reset',
            help='Reset bisect session and clean up saved result',
            parents=parents,
        )
        parser_reset.set_defaults(func=self.cmd_reset)

        parser_init = subparsers.add_parser(
            'init',
            help='Initializes bisect session',
            parents=parents + [experiment.common_flags()],
            formatter_class=argparse.RawDescriptionHelpFormatter,
            description=textwrap.dedent(
                """
          Besides arguments for 'init' command, you also need to set at least one
          'switch' command and exactly one 'eval' command line via 'config' command.
            $ bisector config switch <switch command and arguments>
            $ bisector config eval <eval command and arguments>

          The value of --noisy and --confidence could be changed by 'config'
          command after 'init' as well.
        """
            ),
        )
        parser_init.add_argument(
            '--old',
            required=True,
            type=self.domain_cls.revtype,
            help='Start of bisect range, which has old behavior',
        )
        parser_init.add_argument(
            '--new',
            required=True,
            type=self.domain_cls.revtype,
            help='End of bisect range, which has new behavior',
        )
        parser_init.add_argument(
            '--term-old',
            default='OLD',
            help='Alternative term for "old" state (default: %(default)r)',
        )
        parser_init.add_argument(
            '--term-new',
            default='NEW',
            help='Alternative term for "new" state (default: %(default)r)',
        )
        parser_init.add_argument(
            '--noisy',
            help='Enable noisy binary search and specify prior result. '
            'For example, "old=1/10,new=2/3" means old fail rate is 1/10 '
            'and new fail rate increased to 2/3. '
            'Skip if not flaky, say, "new=2/3" means old is always good.',
        )
        parser_init.add_argument(
            '--old-value',
            type=float,
            help='For performance test, value of old behavior',
        )
        parser_init.add_argument(
            '--new-value',
            type=float,
            help='For performance test, value of new behavior',
        )
        parser_init.add_argument(
            '--recompute-init-values',
            action='store_true',
            help='For performance test, recompute initial values',
        )
        parser_init.add_argument(
            '--confidence',
            type=float,
            default=DEFAULT_CONFIDENCE,
            help='Confidence level (default: %(default)r)',
        )
        parser_init.add_argument(
            '--endpoint-verification',
            action='store_true',
            help='Enable statistical method to verify endpoints',
        )
        parser_init.add_argument(
            '--test-name',
            type=str,
            help='name of the test to be bisected.',
        )
        parser_init.set_defaults(func=self.cmd_init)
        self.domain_cls.add_init_arguments(parser_init)

        parser_config = subparsers.add_parser(
            'config', help='Configures additional setting', parents=parents
        )
        parser_config.add_argument(
            'key',
            choices=[
                'clear',
                'switch',
                'eval',
                'confidence',
                'noisy',
                'term_old',
                'term_new',
                'future_build',
            ],
            metavar='key',
            help='What config to change. choices=[%(choices)s]',
        )
        parser_config.add_argument(
            'value', nargs=argparse.REMAINDER, help='New value'
        )
        parser_config.set_defaults(func=self.cmd_config)

        parser_run = subparsers.add_parser(
            'run',
            help='Performs bisection',
            parents=parents,
            formatter_class=argparse.RawDescriptionHelpFormatter,
            description=textwrap.dedent(
                """
        This command does switch and eval to determine candidates having old or
        new behavior.

        By default, it attempts to try versions in binary search manner until
        found the first version having new behavior.

        If version numbers are specified on command line, it just tries those
        versions and record the result.

        Example:
          Bisect automatically.
          $ %(prog)s

          Switch and run version "2.13" and "2.14" and then stop.
          $ %(prog)s 2.13 2.14
        """
            ),
        )
        parser_run.add_argument(
            '-1', '--once', action='store_true', help='Only run one step'
        )
        parser_run.add_argument(
            '--force',
            action='store_true',
            help="Run at least once even it's already done",
        )
        parser_run.add_argument(
            '--current-rev',
            type=self.domain_cls.intra_revtype,
            help='give hint the current rev',
        )
        parser_run.add_argument(
            'revs',
            nargs='*',
            type=self.domain_cls.intra_revtype,
            help='revs to switch+eval; '
            'default is calculating automatically and run until done',
        )
        parser_run.set_defaults(func=self.cmd_run)

        parser_old = subparsers.add_parser(
            'old',
            help='Tells bisect engine the said revs have "old" behavior',
            parents=parents,
        )
        parser_old.add_argument(
            'revs',
            nargs='+',
            type=cli.argtype_multiplier(self.domain_cls.intra_revtype),
        )
        parser_old.set_defaults(func=self.cmd_old)

        parser_new = subparsers.add_parser(
            'new',
            help='Tells bisect engine the said revs have "new" behavior',
            parents=parents,
        )
        parser_new.add_argument(
            'revs',
            nargs='+',
            type=cli.argtype_multiplier(self.domain_cls.intra_revtype),
        )
        parser_new.set_defaults(func=self.cmd_new)

        parser_skip = subparsers.add_parser(
            'skip',
            help='Tells bisect engine the said revs have "skip" behavior',
            parents=parents,
        )
        parser_skip.add_argument(
            'revs',
            nargs='+',
            type=cli.argtype_multiplier(self.domain_cls.intra_revtype),
        )
        parser_skip.set_defaults(func=self.cmd_skip)

        parser_view = subparsers.add_parser(
            'view',
            help='Shows current progress and candidates',
            parents=parents,
        )
        parser_view.add_argument('--verbose', '-v', action='store_true')
        parser_view.add_argument('--json', action='store_true')
        parser_view.add_argument(
            '--timestamp',
            type=float,
            help='Get view at some timestamp. Only history before the timestamp is included.',
        )
        parser_view.set_defaults(func=self.cmd_view)

        parser_log = subparsers.add_parser(
            'log', help='Prints what has been done so far', parents=parents
        )
        parser_log.add_argument('--before', type=float)
        parser_log.add_argument('--after', type=float)
        parser_log.add_argument(
            '--json', action='store_true', help='Machine readable output'
        )
        parser_log.set_defaults(func=self.cmd_log)

        parser_next = subparsers.add_parser(
            'next', help='Prints next suggested rev to bisect', parents=parents
        )
        parser_next.add_argument(
            '--current-rev',
            type=self.domain_cls.intra_revtype,
            help='give hint the current rev',
        )
        parser_next.set_defaults(func=self.cmd_next)

        return parser

    def main(self, *args, **kwargs):
        """Command line main function.

        Args:
          *args: Command line arguments.
          **kwargs: additional non command line arguments passed by script code.
            {
              'prog': Program name; optional.
            }
        """
        parser = self.create_argument_parser(kwargs.get('prog'))
        opts = parser.parse_args(args or None)
        common.config_logging(opts)

        self._create_states(session=opts.session)
        if opts.command not in ('init', 'reset', 'config'):
            if not self.states.inited and not self.states.load_states():
                raise errors.Uninitialized
            self.domain = self.domain_cls(self.states.config)
            # Only ignore "skip" entries when running a bisection, since they are
            # likely to be temporary errors and we want to give it a chance of
            # retry.
            # For other operations like cmd_view, we keep the full history.
            self.strategy = self._strategy_factory(
                ignore_skip=opts.command == 'run',
                ignore_history=opts.command == 'view',
            )

        return opts.func(opts)
