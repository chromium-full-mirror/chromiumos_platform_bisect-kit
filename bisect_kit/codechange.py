# Copyright 2018 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Model of source code organization and changes.

This module modeled complex source code organization, i.e. nested git repos,
and their version relationship, i.e. pinned or floating git repo. In other
words, it's abstraction of chrome's gclient DEPS, and chromeos and Android's
repo manifest.
"""

from __future__ import annotations

import abc
import collections
import copy
import dataclasses
import json
import logging
import os
import pathlib
import re
import shutil
import typing

from bisect_kit import errors
from bisect_kit import git_util
from bisect_kit import util


logger = logging.getLogger(__name__)

_re_intra_rev = r'^([^,]+)~([^,]+)/(\d+)$'

_DIFF_CACHE_DIR = 'codemanager-diff'


def make_intra_rev(a, b, index):
    """Makes intra-rev version string.

    Between two major "named" versions a and b, there are many small changes
    (commits) in-between. bisect-kit will identify all those instances and bisect
    them. We give names to those instances and call these names as "intra-rev"
    which stands for minor version numbers within two major version.

    Note, a+index (without b) is not enough to identify an unique change due to
    branches. Take chromeos as example, both 9900.1.0 and 9901.0.0 are derived
    from 9900.0.0, so "9900.0.0 plus 100 changes" may ambiguously refer to states
    in 9900.1.0 and 9901.0.0.

    Args:
      a: the start version
      b: the end version
      index: the index number of changes between a and b

    Returns:
      the intra-rev version string
    """
    return '%s~%s/%d' % (a, b, index)


def parse_intra_rev(rev):
    """Decomposes intra-rev string.

    See comments of make_intra_rev for what is intra-rev.

    Args:
      rev: intra-rev string or normal version number

    Returns:
      (start, end, index). If rev is not intra-rev, it must be normal version
      number and returns (rev, rev, 0).
    """
    m = re.match(_re_intra_rev, rev)
    if not m:
        return rev, rev, 0

    return m.group(1), m.group(2), int(m.group(3))


def argtype_intra_rev(argtype):
    """Validates argument is intra-rev.

    Args:
      argtype: argtype function which validates major version number

    Returns:
      A new argtype function which matches intra-rev
    """

    def argtype_function(s):
        examples = []
        try:
            return argtype(s)
        except errors.ArgTypeError as e:
            examples += e.example

        m = re.match(_re_intra_rev, s)
        if m:
            try:
                argtype(m.group(1))
                argtype(m.group(2))
                return s
            except errors.ArgTypeError as e:
                for example in e.example:
                    examples.append(make_intra_rev(example, example, 10))
                raise errors.ArgTypeError('Invalid intra rev', examples)

        examples.append(make_intra_rev('<rev1>', '<rev2>', 10))
        raise errors.ArgTypeError('Invalid rev', examples)

    return argtype_function


@dataclasses.dataclass
class PathSpec:
    """Specified code version of one path.

    Attributes:
      path: local path, relative to project base dir
      repo_url: code repository location
      rev: code version, could be git hash or branch name
    """

    path: str
    repo_url: str
    rev: str

    def is_static(self) -> bool:
        return git_util.is_git_rev(self.rev)

    @property
    def _normalized_repo_url(self):
        return self.repo_url.removesuffix(r'.git').replace(
            r'https://chrome-internal.googlesource.com/a/',
            r'https://chrome-internal.googlesource.com/',
        )

    def __eq__(self, rhs: object) -> bool:
        if not isinstance(rhs, PathSpec):
            return NotImplemented
        if self.path != rhs.path:
            return False
        if self.rev != rhs.rev:
            return False
        if self._normalized_repo_url != rhs._normalized_repo_url:
            return False
        return True


@dataclasses.dataclass
class Spec:
    """Collection of PathSpec.

    Spec is analogy to gclient's DEPS and repo's manifest.

    Attributes:
      spec_type: type of spec, Spec.FIXED or Spec.FLOAT. Spec.FIXED means code
          version is pinned and fixed. On the other hand, Spec.FLOAT is not
          pinned and the actual version (git commit) may change over time.
      name: name of this spec, for debugging purpose. usually version number
          or git hash
      timestamp: timestamp of this spec
      path: path of spec
      entries: paths to PathSpec dict
      revision: a commit id of manifest-internal indicates the manifest revision,
          this argument is not used in DEPS.
    """

    FIXED: typing.ClassVar[str] = 'fixed'
    FLOAT: typing.ClassVar[str] = 'float'

    spec_type: str
    name: str
    timestamp: int
    path: str
    entries: dict[str, PathSpec] = dataclasses.field(default_factory=dict)
    revision: str | None = None

    @staticmethod
    def new_fixed(*args) -> Spec:
        return Spec(Spec.FIXED, *args)

    @staticmethod
    def new_float(*args) -> Spec:
        return Spec(Spec.FLOAT, *args)

    def copy(self) -> Spec:
        return copy.deepcopy(self)

    def similarity_distance(self, rhs) -> float:
        """Calculates similarity distance to another Spec.

        Args:
          rhs: the other Spec to compare with

        Returns:
          The distance of similarity. Smaller value is more similar.
        """
        distance = 0.0
        for path in set(self.entries) & set(rhs.entries):
            if rhs[path] == self[path]:
                continue
            if rhs[path].rev == self[path].rev:
                # it's often that remote repo moved around but should be treated as the
                # same one
                distance += 0.1
            else:
                distance += 1
        distance += len(set(self.entries) ^ set(rhs.entries))
        return distance

    def is_fixed(self) -> bool:
        return self.spec_type == Spec.FIXED

    def is_float(self) -> bool:
        return self.spec_type == Spec.FLOAT

    def is_static(self) -> bool:
        return all(path_spec.is_static() for path_spec in self.entries.values())

    def is_subset(self, rhs: Spec) -> bool:
        return set(self.entries.keys()) <= set(rhs.entries.keys())

    def __getitem__(self, path: str) -> PathSpec:
        return self.entries[path]

    def __contains__(self, path: str) -> bool:
        return path in self.entries

    def apply(self, action_group) -> None:
        self.timestamp = action_group.timestamp
        self.name = '(%s)' % self.timestamp
        for action in action_group.actions:
            if isinstance(action, GitAddRepo):
                self.entries[action.path] = PathSpec(
                    action.path, action.repo_url, action.rev
                )
            elif isinstance(action, GitCheckoutCommit):
                self.entries[action.path].rev = action.rev
            elif isinstance(action, GitRemoveRepo):
                del self.entries[action.path]
            else:
                assert 0, 'unknown action: %s' % action.__class__.__name__

    def __repr__(self) -> str:
        entries_repr = ',\n'.join(repr(entry) for entry in self.entries)
        return (
            f'Spec(name={self.name}, path={self.path}, '
            f'timestamp={self.timestamp}, #entries={len(self.entries)}, '
            f'entries:[\n{entries_repr}])'
        )

    def show_diff(self, rhs: Spec) -> None:
        logger.info('diff between %s and %s', self.name, rhs.name)
        expect = set(self.entries)
        actual = set(rhs.entries)
        common_count = 0
        for path in sorted(expect - actual):
            logger.info('-%s', path)
        for path in sorted(actual - expect):
            logger.info('+%s', path)
        for path in sorted(expect & actual):
            if self[path] == rhs[path]:
                common_count += 1
                continue
            if self[path].rev != rhs[path].rev:
                logger.info(
                    ' %s: rev %s vs %s', path, self[path].rev, rhs[path].rev
                )
            if self[path].repo_url != rhs[path].repo_url:
                logger.info(
                    ' %s: repo_url %s vs %s',
                    path,
                    self[path].repo_url,
                    rhs[path].repo_url,
                )
        logger.info('and common=%s', common_count)


@dataclasses.dataclass
class Action:
    """Describes changes from one Spec to another.

    Attributes:
      timestamp: action time
      path: action path, which is relative to project root
    """

    timestamp: int
    path: str

    @abc.abstractmethod
    def apply(self, code_storage: CodeStorage, root_dir: str) -> None:
        del code_storage, root_dir
        raise NotImplementedError

    @abc.abstractmethod
    def summary(self) -> dict:
        raise NotImplementedError

    def serialize(self) -> tuple[str, dict]:
        return self.__class__.__name__, dataclasses.asdict(self)

    @staticmethod
    def deserialize(action_data: tuple) -> Action:
        class_name, values = action_data
        for subclass in Action.__subclasses__():
            if class_name == subclass.__name__:
                return subclass(**values)  # type: ignore[abstract]
        raise ValueError(f'Unknown action class: {class_name}')


@dataclasses.dataclass
class ActionGroup:
    """Atomic group of Action objects

    This models atomic actions, ex:
      - repo added/removed in the same manifest commit
      - commits appears at the same time due to repo add
      - gerrit topic
      - circular CQ-DEPEND (Cq-Depend)
    Otherwise, one ActionGroup usually consists only one Action object.
    """

    timestamp: int
    name: str | None = None
    comment: str | None = None
    actions: list[Action] = dataclasses.field(default_factory=list)

    def append(self, action: Action) -> None:
        self.actions.append(action)

    def serialize(self):
        return {
            "timestamp": self.timestamp,
            "name": self.name,
            "comment": self.comment,
            "actions": [a.serialize() for a in self.actions],
        }

    def summary(self) -> dict:
        result: dict[str, typing.Any] = {}
        if self.comment:
            result['comment'] = self.comment
        result['actions'] = [action.summary() for action in self.actions]
        return result

    @staticmethod
    def deserialize(group_data: dict) -> ActionGroup:
        group = ActionGroup(
            timestamp=group_data['timestamp'],
            name=group_data['name'],
            comment=group_data['comment'],
        )
        for action_data in group_data['actions']:
            group.append(Action.deserialize(action_data))
        return group

    def apply(self, code_storage: CodeStorage, root_dir: str) -> None:
        for action in self.actions:
            action.apply(code_storage, root_dir)


@dataclasses.dataclass
class GitCheckoutCommit(Action):
    """Describes a git commit action.

    Attributes:
      repo_url: the corresponding url of git repo
      rev: git commit to checkout
    """

    repo_url: str = ''
    rev: str = ''

    def _ensure_remote(self, code_storage: CodeStorage, root_dir: str) -> None:
        git_repo = os.path.join(root_dir, self.path)
        remotes = git_util.get_remotes(git_repo)

        # Because is_containing_commit is slow, do fast check first.
        assert remotes, 'every git directory should have a remote'
        expected_remote_url = code_storage.cached_git_root(self.repo_url)
        for remote in remotes:
            remote_url = git_util.get_remote_url(git_repo, remote)
            if remote_url == expected_remote_url:
                return

        if len(remotes) > 1:
            # There are more than one tracking remotes, we don't know which one
            # changed.
            logger.warning(
                '%s has more than one tracking remotes: %s', self.path, remotes
            )
            logger.warning(
                'This is unexpected for clean repo/gclient maintained tree'
            )
            if git_util.is_containing_commit(git_repo, self.rev):
                # Anyway, the target commit exists. Safe.
                return
            raise errors.InternalError(
                f'{self.path!r} has more than one tracking remote '
                'and we do not know how to handle remote change'
            )

        remote = remotes[0]
        git_util.set_remote_url(git_repo, remote, expected_remote_url)
        git_util.fetch(git_repo, remote)

    def apply(self, code_storage: CodeStorage, root_dir: str) -> None:
        git_repo = os.path.join(root_dir, self.path)
        assert git_util.is_git_root(git_repo)
        self._ensure_remote(code_storage, root_dir)
        git_util.checkout_version(git_repo, self.rev)

    def summary(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "action_type": 'commit',
            "path": self.path,
            "repo_url": self.repo_url,
            "rev": self.rev,
            "text": f'commit {self.rev[:10]} {self.path}',
        }


@dataclasses.dataclass
class GitAddRepo(Action):
    """Describes a git repo add action.

    Attributes:
      repo_url: the corresponding url of git repo to add
      rev: git commit to checkout
    """

    repo_url: str = ''
    rev: str = ''

    def apply(self, code_storage: CodeStorage, root_dir: str) -> None:
        git_repo = os.path.join(root_dir, self.path)
        if os.path.exists(git_repo):
            if os.path.isdir(git_repo) and not os.listdir(git_repo):
                # mimic gclient's behavior; don't panic
                logger.warning(
                    'adding repo %s; there is already an empty directory; '
                    'assume it is okay',
                    git_repo,
                )
            else:
                assert not os.path.exists(git_repo), (
                    '%s already exists' % git_repo
                )

        reference = code_storage.cached_git_root(self.repo_url)
        git_util.clone(git_repo, self.repo_url, reference=reference)
        git_util.checkout_version(git_repo, self.rev)

        code_storage.add_to_project_list(root_dir, self.path, self.repo_url)

    def summary(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "action_type": 'add_repo',
            "path": self.path,
            "repo_url": self.repo_url,
            "rev": self.rev,
            "text": f'add repo {self.path} from {self.repo_url}@{self.rev[:10]}',
        }


@dataclasses.dataclass
class GitRemoveRepo(Action):
    """Describes a git repo remove action."""

    def apply(self, code_storage: CodeStorage, root_dir: str) -> None:
        assert self.path
        root_dir = os.path.normpath(root_dir)
        git_repo = os.path.join(root_dir, self.path)
        assert git_util.is_git_root(git_repo), (
            '%r should be a git repo' % git_repo
        )
        # TODO(kcwu): other projects may be sub-tree of `git_repo`.
        # They should not be deleted. (crbug/930047)
        shutil.rmtree(git_repo)

        # Remove empty parents. (But don't delete `root_dir` and its upper parents.)
        parent = os.path.dirname(git_repo)
        while (
            parent != root_dir
            and os.path.commonpath([parent, root_dir]) == root_dir
        ):
            if os.listdir(parent):
                break
            os.rmdir(parent)
            parent = os.path.dirname(parent)

        code_storage.remove_from_project_list(root_dir, self.path)

    def summary(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "action_type": 'remove_repo',
            "path": self.path,
            "text": 'remove repo %s' % self.path,
        }


def apply_actions(
    code_storage: CodeStorage, action_groups: list[ActionGroup], root_dir: str
) -> None:
    # Speed optimization: only apply the last one of consecutive commits per
    # repo. It is possible to optimize further, but need to take care git repo
    # add/remove within another repo.
    commits = {}

    def batch_apply(commits):
        for i, _, commit_action in sorted(
            commits.values(), key=lambda x: x[:2]
        ):
            logger.debug('[%d] applying "%r"', i, commit_action.summary())
            commit_action.apply(code_storage, root_dir)

    for i, action_group in enumerate(action_groups, 1):
        for action in action_group.actions:
            if not isinstance(action, GitCheckoutCommit):
                break
        else:
            # If all actions are commits, defer them for batch processing.
            for j, action in enumerate(action_group.actions):
                commits[action.path] = (i, j, action)
            continue

        batch_apply(commits)
        commits = {}
        logger.debug('[%d] applying "%r"', i, action_group.summary())
        action_group.apply(code_storage, root_dir)

    batch_apply(commits)


class SpecManager:
    """Spec related abstract operations.

    This class enumerates Spec instances and switch disk state to Spec.

    In other words, this class abstracts:
      - discovery of gclient's DEPS and repo's manifest
      - gclient sync and repo sync
    """

    @abc.abstractmethod
    def collect_float_spec(
        self, old: str, new: str, fixed_specs: list[Spec] | None = None
    ) -> list[Spec]:
        """Collects float Spec between two versions.

        This method may fetch spec from network. However, it should not switch tree
        version state.

        Args:
          old: old version
          new: new version
          fixed_specs: fixed specs from collect_fixed_spec(old, new) for ChromeOS
              or None for others
        """
        del old, new, fixed_specs
        raise NotImplementedError

    @abc.abstractmethod
    def collect_fixed_spec(self, old: str, new: str) -> list[Spec]:
        """Collects fixed Spec between two versions.

        This method may fetch spec from network. However, it should not switch tree
        version state.
        """
        del old, new
        raise NotImplementedError

    @abc.abstractmethod
    def parse_spec(self, spec: Spec) -> None:
        """Parses information for Spec object.

        Args:
          spec: Spec object. It specifies what to parse and the parsed information
              is stored inside.
        """
        del spec
        raise NotImplementedError

    @abc.abstractmethod
    def sync_disk_state(self, rev: str) -> None:
        """Switch source tree state to given version."""
        del rev
        raise NotImplementedError


class CodeStorage:
    """Query code history and commit relationship without checkout.

    Because paths inside source tree may be deleted or map to different remote
    repo in different versions, we cannot query git information of one version
    but the tree state is at another version. In order to query information
    without changing tree state and fast, we need out of tree source code
    storage.

    This class assumes all git repos are mirrored somewhere on local disk.
    Subclasses just need to implement cached_git_root() which returns the
    location.

    In other words, this class abstracts operations upon gclient's cache-dir
    repo's mirror.
    """

    @abc.abstractmethod
    def cached_git_root(self, repo_url: str) -> str:
        """The cached path of given remote git repo.

        Args:
          repo_url: URL of git remote repo

        Returns:
          path of cache folder
        """
        del repo_url
        raise NotImplementedError

    @abc.abstractmethod
    def add_to_project_list(
        self, project_root: str, path: str, repo_url: str
    ) -> None:
        del project_root, path, repo_url
        raise NotImplementedError

    @abc.abstractmethod
    def remove_from_project_list(self, project_root: str, path: str) -> None:
        del project_root, path
        raise NotImplementedError

    def is_ancestor_commit(
        self, spec: Spec, path: str, old: str, new: str
    ) -> bool:
        """Checks if one commit is ancestor of another.

        Args:
          spec: Spec object
          path: local path relative to project root
          old: commit id
          new: commit id

        Returns:
          True if `old` is ancestor of `new`
        """
        git_root = self.cached_git_root(spec[path].repo_url)
        return git_util.is_ancestor_commit(git_root, old, new)

    def get_rev_by_time(
        self, spec: Spec, path: str, timestamp: int
    ) -> str | None:
        """Get commit hash of given spec by time.

        Args:
          spec: Spec object
          path: local path relative to project root
          timestamp: timestamp

        Returns:
          The commit hash of given time. If there are commits with the given
          timestamp, returns the last commit.
        """
        git_root = self.cached_git_root(spec[path].repo_url)
        # spec[path].rev is remote reference name. Since git_root is a mirror
        # (not a local checkout), there is no need to convert the name.
        return git_util.get_rev_by_time(git_root, timestamp, spec[path].rev)

    def get_actions_between_two_commit(
        self, spec, path, old, new, ignore_not_ancestor=False
    ):
        git_root = self.cached_git_root(spec[path].repo_url)
        result = []
        # not in the same branch, regard as an atomic operation
        # this situation happens when
        # 1. new is branched from old and
        # 2. commit timestamp is not reliable(i.e. commit time != merged time)
        # old and new might not have ancestor relation
        if (
            ignore_not_ancestor
            and old != new
            and not git_util.is_ancestor_commit(git_root, old, new)
        ):
            timestamp = git_util.get_commit_time(git_root, new)
            result.append(
                GitCheckoutCommit(timestamp, path, spec[path].repo_url, new)
            )
            return result

        for commit in git_util.list_commits_between_commits(git_root, old, new):
            result.append(
                GitCheckoutCommit(
                    commit.timestamp, path, spec[path].repo_url, commit.rev
                )
            )
        return result

    def is_containing_commit(self, spec, path, rev):
        git_root = self.cached_git_root(spec[path].repo_url)
        return git_util.is_containing_commit(git_root, rev)

    def are_spec_commits_available(self, spec):
        for path, path_spec in spec.entries.items():
            if not path_spec.is_static():
                continue
            if not self.is_containing_commit(spec, path, path_spec.rev):
                return False
        return True


class CodeManager:
    """Class to reconstruct historical source tree state.

    This class can reconstruct all moments of source tree state and diffs between
    them.

    Attributes:
      root_dir: root path of project source tree
      spec_manager: SpecManager object
      code_storage: CodeStorage object
      session_cache_dir: the root session cache dir.
        Put diff cache files under the log dir so the bisection internal state is kept.
    """

    def __init__(self, root_dir, spec_manager, code_storage, session_cache_dir):
        self.root_dir = root_dir
        self.spec_manager = spec_manager
        self.code_storage = code_storage
        self.diff_cache_dir = pathlib.Path(session_cache_dir) / _DIFF_CACHE_DIR
        logger.debug('CodeManager: diff_cache_dir: %s', self.diff_cache_dir)

    def generate_action_groups_between_specs(self, prev_float, next_float):
        """Generates actions between two float specs.

        Args:
          prev_float: start of spec object (exclusive)
          next_float: end of spec object (inclusive)

        Returns:
          list of ActionGroup object (ordered)
        """
        groups = []
        last_group = ActionGroup(next_float.timestamp)
        removed_path_set = set()

        # `branch_between_float_specs` is currently a chromeos-only logic,
        # and branch behavior is not verified for android and chrome now.
        is_chromeos_branched = False
        if hasattr(
            self.spec_manager, 'branch_between_float_specs'
        ) and self.spec_manager.branch_between_float_specs(
            prev_float, next_float
        ):
            is_chromeos_branched = True

        # Sort alphabetically, so parent directories are handled before children
        # directories.
        for path in sorted(set(prev_float.entries) | set(next_float.entries)):
            # Add repo
            if path not in prev_float:
                if next_float[path].is_static():
                    next_rev = next_float[path].rev
                else:
                    next_rev = self.code_storage.get_rev_by_time(
                        next_float, path, next_float.timestamp
                    )
                last_group.append(
                    GitAddRepo(
                        next_float.timestamp,
                        path,
                        next_float[path].repo_url,
                        next_rev,
                    )
                )
                continue

            # Existing path is floating.
            if not prev_float[path].is_static():
                # Enumerates commits until next spec. Get `prev_rev` and
                # `till_rev` by prev_float and next_float's timestamp.
                #
                # 1. Non-branched case:
                #
                #                prev_rev                till_rev
                # prev branch ---> o --------> o --------> o --------> o --------> ...
                #                       ^                        ^
                #                 prev_float.timestamp        next_float.timestamp
                #
                # building an image between prev_rev and till_rev should follow
                # prev_float's spec.
                #
                # 2. Branched case:
                #
                #                     till_rev
                #              /------->o---------->
                #             /            ^ next_float.timestamp
                #            / prev_rev
                # ---------->o---------------------->
                #                ^prev_float.timestamp
                #
                # building an image between prev_rev and till_rev should follow
                # next_float's spec.
                #
                prev_rev = self.code_storage.get_rev_by_time(
                    prev_float, path, prev_float.timestamp
                )
                if is_chromeos_branched:
                    till_rev = self.code_storage.get_rev_by_time(
                        next_float, path, next_float.timestamp
                    )
                else:
                    till_rev = self.code_storage.get_rev_by_time(
                        prev_float, path, next_float.timestamp
                    )
                actions = self.code_storage.get_actions_between_two_commit(
                    prev_float,
                    path,
                    prev_rev,
                    till_rev,
                    ignore_not_ancestor=True,
                )

                # Assume commits with the same timestamp as manifest/DEPS change are
                # atomic.
                if actions and actions[-1].timestamp == next_float.timestamp:
                    last_group.append(actions.pop())

                for action in actions:
                    group = ActionGroup(action.timestamp)
                    group.append(action)
                    groups.append(group)
            else:
                prev_rev = till_rev = prev_float[path].rev

            # At next_float.timestamp.
            if path not in next_float:
                if path in removed_path_set:
                    continue
                # remove repo
                next_rev = None
                sub_repos = [
                    p for p in prev_float.entries if p.startswith(path + '/')
                ]
                # Remove deeper repo first
                for path2 in sorted(sub_repos, reverse=True):
                    last_group.append(
                        GitRemoveRepo(next_float.timestamp, path2)
                    )
                    removed_path_set.add(path2)
                last_group.append(GitRemoveRepo(next_float.timestamp, path))
                removed_path_set.add(path)
                for path2 in sorted(set(sub_repos) & set(next_float.entries)):
                    last_group.append(
                        GitAddRepo(
                            next_float.timestamp,
                            path2,
                            next_float[path2].repo_url,
                            prev_float[path2].rev,
                        )
                    )

            elif next_float[path].is_static():
                # pinned to certain commit on different branch
                next_rev = next_float[path].rev

            elif next_float[path].rev == prev_float[path].rev:
                # keep floating on the same branch
                next_rev = till_rev

            else:
                # switch to another branch
                #                prev_rev                till_rev
                # prev branch ---> o --------> o --------> o --------> o --------> ...
                #
                #                                            next_rev
                # next branch                 ...... o ------> o --------> o -----> ...
                #                       ^                         ^
                #                 prev_float.timestamp        next_float.timestamp
                next_rev = self.code_storage.get_rev_by_time(
                    next_float, path, next_float.timestamp
                )

            if next_rev and next_rev != till_rev:
                last_group.append(
                    GitCheckoutCommit(
                        next_float.timestamp,
                        path,
                        next_float[path].repo_url,
                        next_rev,
                    )
                )

        groups.sort(key=lambda x: x.timestamp)
        if last_group.actions:
            groups.append(last_group)
        return groups

    def synthesize_fixed_spec(self, float_spec, timestamp):
        """Synthesizes fixed spec from float spec of given time.

        Args:
          float_spec: the float spec
          timestamp: snapshot time

        Returns:
          Spec object
        """
        result = {}
        for path, path_spec in float_spec.entries.items():
            if not path_spec.is_static():
                rev = self.code_storage.get_rev_by_time(
                    float_spec, path, timestamp
                )
                path_spec = PathSpec(path_spec.path, path_spec.repo_url, rev)

            result[path] = copy.deepcopy(path_spec)

        name = '%s@%s' % (float_spec.path, timestamp)
        return Spec(Spec.FIXED, name, timestamp, float_spec.path, result)

    def match_spec(self, target, specs, start_index=0):
        # ideal_index is the index of last spec before target
        ideal_index = None
        for i, spec in enumerate(specs[start_index:], start_index):
            if spec.timestamp <= target.timestamp:
                ideal_index = i
            else:
                break
        if ideal_index is None:
            logger.error(
                'unable to match %s: all specs are after it', target.name
            )
            return None

        distance_list = []
        for i, spec in enumerate(specs[start_index:], start_index):
            if not spec.is_subset(target):
                continue  # incompatible, ignore

            # Tie-break: prefer earlier timestamp and smaller difference.
            if spec.timestamp <= target.timestamp:
                timediff = 0, target.timestamp - spec.timestamp
            else:
                timediff = 1, spec.timestamp - target.timestamp
            distance = spec.similarity_distance(target)
            distance_list.append((distance, timediff, i))

        if not distance_list:
            logger.error('unable to match %s: no compatible specs', target.name)
            spec = specs[start_index]
            target.show_diff(spec)
            return None

        distance_list.sort()
        distance, _, index = distance_list[0]
        if distance != 0:
            logger.warning(
                'not exactly match (distance=%s): %s', distance, target.name
            )
            target.show_diff(specs[index])

        if index < ideal_index:
            logger.warning(
                '%s (%s) matched earlier spec at %s instead of %s, racing? offset %d',
                target.name,
                target.timestamp,
                specs[index].timestamp,
                specs[ideal_index].timestamp,
                specs[index].timestamp - target.timestamp,
            )
        if index > ideal_index:
            logger.warning(
                'spec committed at %d matched later commit at %d. bad server clock?',
                target.timestamp,
                specs[index].timestamp,
            )

        return index

    def associate_fixed_and_synthesized_specs(
        self, fixed_specs, synthesized_specs
    ):
        # All fixed specs are snapshot of float specs. Theoretically, they
        # should be identical to one of the synthesized specs.
        # However, it's not always true for some reasons --- maybe due to race
        # condition, maybe due to bugs of this bisect-kit.
        # To overcome this glitch, we try to match them by similarity instead of
        # exact match.
        result = []
        last_index = 0
        for i, fixed_spec in enumerate(fixed_specs):
            matched_index = self.match_spec(
                fixed_spec, synthesized_specs, last_index
            )
            if matched_index is None:
                if i in (0, len(fixed_specs) - 1):
                    logger.error('essential spec mismatch, unable to continue')
                    raise ValueError(
                        'Commit history analyze failed. '
                        'Bisector cannot deal with this version range.'
                    )
                logger.warning('%s do not match, skip', fixed_spec.name)
                continue
            result.append((i, matched_index))
            last_index = matched_index

        return result

    def _create_make_up_actions(self, fixed_spec, synthesized):
        timestamp = synthesized.timestamp
        make_up = ActionGroup(
            timestamp, comment=f'make up glitch for {fixed_spec.name}'
        )
        for path in set(fixed_spec.entries) & set(synthesized.entries):
            if fixed_spec[path].rev == synthesized[path].rev:
                continue
            make_up.append(
                GitCheckoutCommit(
                    timestamp,
                    path,
                    synthesized[path].repo_url,
                    synthesized[path].rev,
                )
            )

        if not make_up.actions:
            return None
        return make_up

    def _batch_fill_action_commit_log(self, details):
        group_by_repo = collections.defaultdict(list)
        for detail in details.values():
            for action in detail.get('actions', []):
                if action['action_type'] == 'commit':
                    group_by_repo[action['repo_url']].append(action)

        for repo_url, actions in group_by_repo.items():
            git_root = self.code_storage.cached_git_root(repo_url)
            revs = set(a['rev'] for a in actions)
            metas = git_util.get_batch_commit_metadata(git_root, revs)
            for action in actions:
                meta = metas.get(action['rev'])
                action['commit_summary'] = git_util.CommitMeta.get_summary(meta)

    def build_revlist(self, old, new):
        """Build revlist.

        Returns:
          (revlist, details):
            revlist: list of rev string
            details: dict of rev to rev detail
        """
        logger.info('build_revlist: old=%s, new=%s', old, new)
        revlist = []
        details = {}

        # Enable cache for repetitive git operations. The space complexity is
        # O(number of candidates).
        git_util.get_commit_metadata.enable_cache()
        git_util.get_file_from_revision.enable_cache()
        git_util.is_containing_commit.enable_cache()
        git_util.is_ancestor_commit.enable_cache()

        # step 1, find all float and fixed specs in the given range.
        fixed_specs = self.spec_manager.collect_fixed_spec(old, new)
        logger.debug('Found %d fixed specs', len(fixed_specs))
        assert fixed_specs
        for idx, spec in enumerate(fixed_specs):
            logger.debug(
                'Fixed spec %d name:%s, timestamp: %d',
                idx,
                spec.name,
                spec.timestamp,
            )
            self.spec_manager.parse_spec(spec)

        float_specs = self.spec_manager.collect_float_spec(
            old, new, fixed_specs
        )
        assert float_specs
        while float_specs[-1].timestamp > fixed_specs[-1].timestamp:
            float_specs.pop()
        assert float_specs
        logger.debug('Found %d float specs', len(float_specs))
        for idx, spec in enumerate(float_specs):
            logger.debug(
                'Float spec %d name:%s, timestamp: %d',
                idx,
                spec.name,
                spec.timestamp,
            )
            self.spec_manager.parse_spec(spec)

        git_util.fast_lookup.optimize(
            git_util.Period(float_specs[0].timestamp, float_specs[-1].timestamp)
        )
        # step 2, synthesize all fixed specs in the range from float specs.
        specs = float_specs + [fixed_specs[-1]]
        action_groups = []
        logger.debug('len(specs)=%d', len(specs))
        for i in range(len(specs) - 1):
            prev_float = specs[i]
            next_float = specs[i + 1]
            logger.debug(
                '[%d], between %s (%s) and %s (%s)',
                i,
                prev_float.name,
                prev_float.timestamp,
                next_float.name,
                next_float.timestamp,
            )
            action_groups += self.generate_action_groups_between_specs(
                prev_float, next_float
            )

        spec = self.synthesize_fixed_spec(
            float_specs[0], fixed_specs[0].timestamp
        )
        synthesized = [spec.copy()]
        for action_group in action_groups:
            spec.apply(action_group)
            synthesized.append(spec.copy())

        # step 3, associate fixed specs with synthesized specs.
        associated_pairs = self.associate_fixed_and_synthesized_specs(
            fixed_specs, synthesized
        )

        # step 4, group actions and cache them
        for i, (fixed_index, synthesized_index) in enumerate(
            associated_pairs[:-1]
        ):
            next_fixed_index, next_synthesized_index = associated_pairs[i + 1]
            revlist.append(fixed_specs[fixed_index].name)
            this_action_groups = []

            # handle glitch
            if (
                fixed_specs[fixed_index].similarity_distance(
                    synthesized[synthesized_index]
                )
                != 0
            ):
                assert synthesized[synthesized_index].is_subset(
                    fixed_specs[fixed_index]
                )
                skipped = set(fixed_specs[fixed_index].entries) - set(
                    synthesized[synthesized_index].entries
                )
                if skipped:
                    logger.warning(
                        'between %s and %s, '
                        'bisect-kit cannot analyze commit history of following paths:',
                        fixed_specs[fixed_index].name,
                        fixed_specs[next_fixed_index].name,
                    )
                    for path in sorted(skipped):
                        logger.warning('    %s', path)

                make_up = self._create_make_up_actions(
                    fixed_specs[fixed_index], synthesized[synthesized_index]
                )
                if make_up:
                    this_action_groups.append(make_up)

            this_action_groups.extend(
                action_groups[synthesized_index:next_synthesized_index]
            )
            for idx, ag in enumerate(this_action_groups, 1):
                rev = make_intra_rev(
                    fixed_specs[fixed_index].name,
                    fixed_specs[next_fixed_index].name,
                    idx,
                )
                ag.name = rev
                revlist.append(rev)
                details[rev] = ag.summary()

            self.save_action_groups_between_releases(
                fixed_specs[fixed_index].name,
                fixed_specs[next_fixed_index].name,
                this_action_groups,
            )
        revlist.append(fixed_specs[associated_pairs[-1][0]].name)

        self._batch_fill_action_commit_log(details)

        # Verify all repos in between are cached.
        for spec in reversed(float_specs):
            if self.code_storage.are_spec_commits_available(spec):
                continue
            raise errors.InternalError(
                'Some commits in %s (%s) are unavailable'
                % (spec.name, spec.path)
            )

        # Disable cache because there might be write or even destructive git
        # operations when switch git versions. Be conservative now. We can cache
        # more if we observed more slow git operations later.
        git_util.fast_lookup.disable()
        git_util.get_commit_metadata.disable_cache()
        git_util.get_file_from_revision.disable_cache()
        git_util.is_containing_commit.disable_cache()
        git_util.is_ancestor_commit.disable_cache()

        return revlist, details

    def save_action_groups_between_releases(self, old, new, action_groups):
        data = [ag.serialize() for ag in action_groups]

        if not self.diff_cache_dir.exists():
            self.diff_cache_dir.mkdir(parents=True, exist_ok=True)
        cache_filename = self.diff_cache_dir / (
            '%s,%s.json' % (util.escape_rev(old), util.escape_rev(new))
        )
        with cache_filename.open('w') as fp:
            json.dump(data, fp, indent=4, sort_keys=True)

    def load_action_groups_between_releases(self, old, new):
        cache_filename = self.diff_cache_dir / (
            '%s,%s.json' % (util.escape_rev(old), util.escape_rev(new))
        )
        if not cache_filename.exists():
            raise errors.InternalError(
                'cached revlist not found: %s' % cache_filename
            )

        result = []
        with cache_filename.open() as f:
            for data in json.load(f):
                result.append(ActionGroup.deserialize(data))

        return result

    def switch(self, rev):
        rev_old, action_groups = self.get_intra_and_diff(rev)
        self.spec_manager.sync_disk_state(rev_old)
        apply_actions(self.code_storage, action_groups, self.root_dir)

    def get_intra_and_diff(self, rev):
        # easy case
        if not re.match(_re_intra_rev, rev):
            return rev, []

        rev_old, rev_new, idx = parse_intra_rev(rev)
        action_groups = self.load_action_groups_between_releases(
            rev_old, rev_new
        )
        assert 0 <= idx <= len(action_groups)
        action_groups = action_groups[:idx]
        return rev_old, action_groups
