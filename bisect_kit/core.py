# Copyright 2017 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Bisection core module."""

from __future__ import annotations

import argparse
import dataclasses
import enum
import importlib
import json
import logging
import os
import shutil
import tempfile
import time
import typing

from bisect_kit import common
from bisect_kit import math_util
from bisect_kit import strategy_states
import experiment


logger = logging.getLogger(__name__)


class StepResult:
    """Result for each bisection test run."""

    def __init__(
        self,
        status: typing.Literal['old', 'new', 'skip', 'fatal', 'value'],
        reason: str | None = None,
        values: list[float] | None = None,
        exception: str | None = None,
        times: int | None = None,
    ):
        self.data: dict[str, typing.Any] = {'status': status}
        if values:
            self.data['values'] = values
        if reason:
            self.data['reason'] = reason
        if exception:
            self.data['exception'] = exception
        if times:
            self.data['times'] = times


class RevInfo:
    """Aggregated evaluation result of one revision.

    The count of results can be easily accessed by using [] operator.

    Attributes:
      rev (str): revision id
      result_counter (dict): count of results, example: dict(new=3, old=2)
      values (list of list of numbers):
          list of values collected during evaluations. There could be more than
          one value for a single evaluation.
      switch_time: total duration of switch step for such revision
      eval_time: total duration of eval step for such revision
    """

    def __init__(self, rev, term_map=None):
        self.rev = rev

        self.term_map = term_map or {}
        self.result_counter = {}
        self.values = []
        self.switch_time = 0
        self.eval_time = 0

    def to_dict(self):
        result = vars(self).copy()
        del result['term_map']
        result['averages'] = self.averages()
        # backward compatible with old behavior
        # TODO(kcwu): remove this after callers migrated
        result['values'] = self.averages()
        return result

    def __getitem__(self, key):
        return self.result_counter.get(key, 0)

    def __setitem__(self, key, value):
        """Shortcut of add_sample()."""
        self.result_counter[key] = value
        # Prune dead entries, so it looks good if we output 'result_counter'
        # directly.
        if value == 0:
            del self.result_counter[key]

    @classmethod
    def format_result_counter(cls, result_counter, term_map):
        result = []
        for status, count in sorted(result_counter.items()):
            status = term_map.get(status, status)
            result.append('%s:%s' % (status, count))
        return ', '.join(result)

    def counter_string(self):
        return self.format_result_counter(self.result_counter, self.term_map)

    def summary(self):
        """Summary of the result of this revision."""
        averages = sorted(self.averages())
        if not averages:
            return self.counter_string()
        if len(averages) == 1:
            return '%s %.3f' % (self.counter_string(), averages[0])

        return '%s n=%d,avg=%.3f,median=%.3f,min=%.3f,max=%.3f' % (
            self.counter_string(),
            len(averages),
            math_util.average(averages),
            averages[len(averages) // 2],
            averages[0],
            averages[-1],
        )

    def averages(self):
        """Takes the average of sample values.

        In other words, one (average) value for each sample.
        """
        return [math_util.average(v) for v in self.values]

    def add_sample(
        self,
        status=None,
        values=None,
        times=None,
        switch_time=None,
        eval_time=None,
        **kwargs,
    ):
        if 'rev' in kwargs:
            assert kwargs['rev'] == self.rev
        assert status in (None, 'value', 'old', 'new', 'skip', 'fatal')

        if times is None:
            times = 1
        if values:
            assert isinstance(values, list)
            assert times == 1
            self.values.append(values)
        self[status] += times
        if switch_time:
            self.switch_time += switch_time
        if eval_time:
            self.eval_time += eval_time

    def reclassify(self, old_avg, threshold, new_avg):
        """Reclassify status by values."""
        assert self['value'] == len(self.values) and self.values
        assert self['old'] + self['new'] == 0
        for avg in self.averages():
            if old_avg < new_avg:
                status = 'old' if avg < threshold else 'new'
            else:
                status = 'new' if avg < threshold else 'old'
            self['value'] -= 1
            self[status] += 1


class StatesEncoder(json.JSONEncoder):
    """Special handling of dataclasses when serializing to json.

    So dataclasses can be reconstructed from json later.
    See rebuild_from_json().
    """

    def default(self, o):
        if dataclasses.is_dataclass(o):
            d = dataclasses.asdict(o)
            d['__class__'] = {
                '__module__': o.__class__.__module__,
                '__name__': o.__class__.__name__,
            }
            return d

        return json.JSONEncoder.default(self, o)


def rebuild_from_json(config):
    """Reconstruct a dict which may contain dataclasses from serialized json."""
    if isinstance(config, dict):
        if '__class__' in config:
            cls = getattr(
                importlib.import_module(config['__class__']['__module__']),
                config['__class__']['__name__'],
            )
            del config['__class__']
            return cls(**config)
        for key, value in config.items():
            config[key] = rebuild_from_json(value)
    elif isinstance(config, list):
        for i, item in enumerate(config):
            config[i] = rebuild_from_json(item)

    return config


class States:
    """Base class for serializing program state to disk.

    After an instance is created, there are two ways to initialize the instance.
    1. Define custom initialization functions. By the end of the initialization,
      _init_done() should be called to mark the instance as initialized.
    2. Load the data from session_file by calling load_states().
      Subclass should define _init_from_dict() to reconstruct the internal states.
      _init_done() should be called in _init_from_dict() if the reconstructs is
      successful.

    To save internal states to session_file, call save().
    Subclass should define _pack_to_dict() which dumps the internal states as a
    dict to be written to the json file.
    """

    def __init__(self, session_file):
        """Initializes States.

        Args:
          session_file: path of session file.
        """
        self.session_file = session_file
        logger.debug('session file: %s', self.session_file)

        # `inited` means that _init_done() has ever been invoked (including the good
        # session loaded via load_states()). On the other hand, if the session has
        # never successfully initialized, `inited` will be still False after
        # load_states().
        self.inited = False

    def reset(self):
        """Invalidates the instance and deletes saved file.

        After called, the instance should not be used unless calling initialization methods again.
        """
        self.inited = False
        os.unlink(self.session_file)

    def _init_done(self):
        """Mark the state as initialized."""
        self.inited = True

    def _init_from_dict(self, unused_data: dict) -> bool:
        """Reconstruct derived fields from the dict "data".

        Subclass should override this method.

        If the reconstruct is successful, self._init_done() should be called.

        Args:
            data: the dict to reconstruct from.

        Returns:
            Whether the reconstruct is successful.
        """
        self._init_done()
        return True

    def _pack_to_dict(self) -> dict:
        """Construct the dict to be stored to json file.

        Subclass should override this method.

        Returns:
          The dict to be stored.
        """
        return {}

    def load_states(self) -> bool:
        """Loads saved data from file.

        Returns:
          True if loaded successfully.
        """
        if not os.path.exists(self.session_file):
            return False
        with open(self.session_file) as f:
            return self._init_from_dict(json.load(f))

    def save(self):
        dirname = os.path.dirname(self.session_file)
        if not os.path.exists(dirname):
            os.makedirs(dirname)

        data = self._pack_to_dict()

        tmp_fn = tempfile.mktemp()
        with open(tmp_fn, 'w') as f:
            f.write(
                json.dumps(data, indent=4, sort_keys=True, cls=StatesEncoder)
            )
        # Move is an atomic operation, so the session file won't be corrupted due
        # to program terminated by any reason.
        shutil.move(tmp_fn, self.session_file)


# TODO(cylee): deprecated. Use dut_allocator.DutAllocateSpec.
@dataclasses.dataclass
class DutAllocateSpec:
    """A class to keep parameters used to allocate DUTs."""

    pool: str | None = None
    dimensions: list | None = None
    board: str | None = None
    model: str | None = None
    sku: str | None = None
    dut_name: str | None = None
    satlab_ip: str | None = None
    version_hint: str | None = None
    builder_hint: str | None = None
    time_limit: int | None = None
    duration: float | None = None
    parallel: int | None = None
    chromeos_root: str | None = None
    session: str | None = None


@dataclasses.dataclass
class DiagnoseStatistics:
    """General bisection Statistics."""

    dut_leases_log_path: str | None = None
    start_timestamp: float | None = None
    end_timestamp: float | None = None
    duration_secs: float | None = None


class BisectConfig(typing.TypedDict, total=False):
    """Configuration for BisectDomain."""

    board: str | None
    term_old: str | None
    term_new: str | None
    noisy: str | None
    old: str
    new: str
    test_name: str | None
    experiments: list[experiment.ID] | None
    confidence: float
    switch: list[list[str]]
    eval: list[str]
    clear: str
    future_build: list[str]
    chrome_root: str
    chromeos_root: str
    chromeos_mirror: str


class DiagnoserConfig(typing.TypedDict, total=False):
    """Configuration for CrosDiagnoser."""

    session: str
    mirror_base: str
    work_base: str
    chrome_work_base: str
    chromeos_root: str
    chromeos_mirror: str
    chrome_root: str
    chrome_mirror: str
    android_root: str
    android_mirror: str
    dut: str
    board: str | None
    old: str
    new: str
    is_public_build: bool
    bisect_chrome: bool
    bisect_android: bool
    base_cros_version: str
    cros_prebuilt_old: str | None
    cros_prebuilt_new: str | None
    chrome_localbuild_old: str | None
    chrome_localbuild_new: str | None
    chrome_dcheck_build: bool
    chrome_cfi_thinlto_build: bool
    chromium_patch_cls: list[str] | None
    android_prebuilt_old: str | None
    android_prebuilt_new: str | None
    term_old: str | None
    term_new: str | None
    test_name: str | None
    tast_patch_cls: list[str] | None
    tast_revision: str | None
    tast_private_revision: str | None
    tast_runtime_revision: str | None
    fail_to_pass: bool
    metric: str | None
    old_value: float | None
    new_value: float | None
    recompute_init_values: bool
    noisy: str | None
    always_reflash: bool
    reboot_before_test: bool
    bypass_chromeos_prebuilt: bool
    bypass_chromeos_build: bool
    bypass_chrome_build: bool
    bypass_android_prebuilt: bool
    bypass_android_build: bool
    bypass_dlc_prebuilt: bool
    disable_rootfs_verification: bool
    chrome_deploy_image: bool
    disable_snapshot: bool
    disable_buildbucket_chromeos: bool
    enable_buildbucket_chrome: bool
    extra_test_variables: list[str]
    endpoint_verification: bool
    consider_test_crash_as_failure: bool
    experiments: list[experiment.ID] | None
    sync_dut_allocate_spec_with_db: bool
    board_cpu_arch: str | None
    allocated_dut: str | None


class _BisectStatesData(typing.TypedDict, total=False):
    """Data for BisectStates."""

    history: typing.Required[list[typing.Any]]
    config: DiagnoserConfig
    revlist: list[str]
    details: dict[str, typing.Any]
    strategy_states: strategy_states.States | None


class DiagnoseStates(States):
    """Diagnose states."""

    def __init__(self, session_file: str):
        super().__init__(session_file)
        self._data: dict[str, typing.Any] = {}

    def init_states(self, config: DiagnoserConfig):
        self._data = {
            'config': config,
            'history': [],
            'statistics': DiagnoseStatistics(),
        }

        self._init_done()

    def _init_from_dict(self, data: dict) -> bool:
        """Inherit from States."""
        self._data = rebuild_from_json(data)
        if (
            'config' in self._data
            and 'history' in self._data
            and 'statistics' in self._data
        ):
            self._init_done()
            return True
        return False

    def _pack_to_dict(self) -> dict:
        """Inherit from States."""
        return self._data

    @property
    def config(self) -> DiagnoserConfig:
        assert self.inited
        return self._data['config']

    @property
    def history(self) -> list:
        assert self.inited
        return self._data['history']

    @property
    def statistics(self) -> DiagnoseStatistics:
        return self._data['statistics']

    def add_history(self, event, **kwargs):
        entry = {"timestamp": time.time(), "event": event, **kwargs}
        self.history.append(entry)
        self.save()


class BisectStates(States):
    """Bisection states.

    After instantiation, init_states() or load_states() should be invoked before
    access state values.
    """

    def __init__(self, session_file):
        """Initializes BisectStates.

        Args:
          session_file: path of session file.
        """
        super().__init__(session_file)

        self.data: _BisectStatesData = {
            # What have been done so far. Each entry contains at least
            # timestamp, rev, and result.
            "history": []
        }

        # Mapping of rev to idx; constructed from data['revlist'].
        self.rev_index = {}

    @classmethod
    def from_bisector_class(
        cls, bisector_cls: str, session: str
    ) -> BisectStates:
        """Initializes BisectStates from a bisector class name."""
        session_file = common.get_session_log_path(session, bisector_cls)
        return cls(session_file)

    @property
    def config(self) -> BisectConfig:
        assert self.inited
        return self.data.get('config', {})

    @property
    def details(self) -> dict[str, typing.Any]:
        assert self.inited
        return self.data.get('details', {})

    @property
    def history(self) -> dict[str, typing.Any]:
        assert self.inited
        return self.data.get('history', {})

    @property
    def strategy_states(self) -> strategy_states.States | None:
        assert self.inited
        states = self.data.get('strategy_states')
        if states:
            # Returns a copy so the internal states is not modified
            # accidentally.
            # Call setter strategy_states() to set it explicitly.
            return dataclasses.replace(
                typing.cast(strategy_states.States, states)
            )
        return None

    @strategy_states.setter
    def strategy_states(self, states: strategy_states.States):
        # Make a copy so the modification of the argument at the caller site
        # doesn't propogate.
        self.data['strategy_states'] = dataclasses.replace(states)

    def init_states(
        self,
        config: BisectConfig,
        revlist: list[str],
        details: dict[str, typing.Any] | None = None,
    ):
        """Initializes attributes data, rev_info and rev_index.

        Args:
          config: bisection configuration.
          revlist: version list.
          details: dict of rev details.
        """
        self.data.update(
            # Bisection configurations (dict), values are determined by cmd_init
            # and each domain's init functions. There will be 'old' and 'new' at
            # least.
            config=config,
            # List of bisect candidates (version numbers).
            revlist=revlist,
            details=details or {},
        )
        self._init_from_dict(self.data)

    def _init_from_dict(self, data: dict) -> bool:
        """Inherit from States."""
        self.data = rebuild_from_json(data)

        # reset variables
        self.rev_index = {}

        if 'revlist' not in self.data:
            # The session is not fully initialized.
            return False

        for i, rev in enumerate(self.data['revlist']):
            self.rev_index[rev] = i

        self._init_done()
        return True

    def _pack_to_dict(self) -> dict:
        """Inherit from States."""
        return self.data

    def get_rev_info(
        self, term_map=None, ignore_skip=False, ignore_history=False
    ) -> list[RevInfo]:
        """Gets aggregated rev info.

        Args:
          term_map: Alternative term for states
          ignore_skip: Whether to ignore 'skip' entries.
          ignore_history: Whether to ignore previous running result.

        Returns:
          list of RevInfo, which aggregated previous test samples.
        """
        assert self.inited
        rev_info = []
        for rev in self.data['revlist']:
            rev_info.append(RevInfo(rev, term_map=term_map))
        if ignore_history:
            return rev_info

        for entry in self.data['history']:
            if entry.get('event', 'sample') != 'sample':
                continue
            if ignore_skip and entry.get('status') == 'skip':
                continue
            idx = self.rev2idx(entry['rev'])
            rev_info[idx].add_sample(**entry)
        return rev_info

    def get_init_range_verified(self) -> bool:
        assert self.inited
        for entry in self.data['history']:
            if entry.get('event') != 'verified':
                continue
            return entry['verified_status']
        return False

    def idx2rev(self, idx: int) -> str:
        assert self.inited
        return self.data['revlist'][idx]

    def rev2idx(self, rev: str) -> int:
        assert self.inited
        return self.rev_index[rev]

    def add_history(self, event, **kwargs):
        # This function is allowed to be called before fully initialized
        # (self.inited=False) in order to record error events.
        entry = {"event": event, "timestamp": time.time(), **kwargs}
        self.data['history'].append(entry)


class Phase(enum.StrEnum):
    """Phases of bisection steps where extra arguments might be needed."""

    SWITCH = 'switch'
    EVAL = 'eval'
    FUTURE_BUILD = 'future_build'


class BisectDomain:
    """Base class of bisection domain.

    "BisectDomain" is in the sense of "domain of math function". Mapping to
    specific problems, "domain" usually means version numbers, git hashes,
    timestamp, or any ordered strings. In other words, it means "what to bisect".

    The main purposes of this class are:
      - Takes care initial setup of bisection.
      - Enumerate version numbers need to bisect.
      - Provide users the information to difference of two version numbers.
    """

    # Bisector help message shown on command line --help.
    help = ''

    @staticmethod
    def revtype(rev):
        """Validates version string of two ends of bisect range.

        Args:
          rev: a version string from command line argument.

        Returns:
          The original or normalized version string if it is valid.

        Raises:
          TypeError or ValueError:
            Indicates rev is invalid.
          argparse.ArgumentTypeError:
            Indicates rev is invalid (with additional message.)
        """

    @classmethod
    def intra_revtype(cls, intra_rev):
        """Validates intra version string within bisect range.

        'rev' means the version string of two ends of bisect range. 'intra_rev'
        means other versions within the bisect range. intra_revtype equals to
        revtype by default.

        Args:
          intra_rev: a version string from command line argument.

        Returns:
          The original or normalized version string if it is valid.

        Raises:
          TypeError or ValueError:
            Indicates rev is invalid.
          argparse.ArgumentTypeError:
            Indicates rev is invalid (with additional message.)
        """
        return cls.revtype(intra_rev)

    @staticmethod
    def add_init_arguments(parser):
        """Adds additional arguments for init subcommand of bisector.

        Args:
          parser: An argparse.ArgumentParser instance.
        """

    @staticmethod
    def init(opts: argparse.Namespace) -> tuple[BisectConfig, dict]:
        """Initializes BisectDomain.

        This is called by bisector's "init" command.

        Args:
          opts: An argparse.Namespace to hold command line arguments.

        Returns:
          (config, revdata):
            config (dict): values saved to the per session storage. The bisection
                range could be adjusted by setting config['old'] and config['new'].
            revdata (dict):
              revlist: list of version strings need to bisect. The
                  bisect range `old` and `new` must be inside the list (but
                  unnecessary to be the first and the last one).
              details (dict): detail information for each rev
        """
        raise NotImplementedError

    def __init__(self, config: BisectConfig):
        raise NotImplementedError

    def setenv(self, env, rev, rev_details=None):
        """Sets environment variables needed by switchers and evaluators.

        Args:
          env: The dict to hold environment variables.
          rev: Current bisecting version.
          rev_details: Details of the current version.
        """

    def get_extra_args(
        self,
        _phase: Phase,
        _rev: str,
        _rev_details=None,
    ) -> list[str]:
        """Gets extra command line arguments needed by switchers and evaluators.

        Args:
          phase: Phase enum (SWITCH, EVAL, or FUTURE_BUILD).
          rev: Current bisecting version.
          rev_details: Details of the current version.

        Returns:
          A list of command line arguments.
        """
        return []

    def fill_candidate_summary(self, summary):
        """Fill detail of candidates.

        This is for 'view' subcommand to display information of remaining
        candidates.

        Args:
          summary: dict of candidate details. It is prepopulated following fields:
              rev_info:
              current_range:
              highlight_range:
              prob:
              remaining_steps:
            This method can modify or fill more fields into the dict.
              links:
              rev_info:
        """
