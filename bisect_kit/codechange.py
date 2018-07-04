# -*- coding: utf-8 -*-
# Copyright 2018 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Model of source code organization and changes.

This module modeled complex source code organization, i.e. nested git repos,
and their version relationship, i.e. pinned or floating git repo. In other
words, it's abstraction of chrome's gclient DEPS, and chromeos and Android's
repo manifest.
"""

from __future__ import print_function
import copy
import json
import logging
import os
import re
import shutil

from bisect_kit import cli
from bisect_kit import git_util

logger = logging.getLogger(__name__)

_re_intra_rev = r'^([^,]+)~([^,]+)/(\d+)$'

SPEC_FIXED = 'fixed'
SPEC_FLOAT = 'float'
_DIFF_CACHE_DIR = 'bisectkit-cache'


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
  if m:
    return m.group(1), m.group(2), int(m.group(3))
  else:
    return rev, rev, 0


def argtype_intra_rev(argtype):
  """Validates argument is intra-rev.

  Args:
    argtype: argtype function which validates major version number

  Returns:
    A new argtype function which matches intra-rev
  """

  def argtype_function(s):
    m = re.match(_re_intra_rev, s)
    if m:
      try:
        argtype(m.group(1))
        argtype(m.group(2))
        return s
      except cli.ArgTypeError as e:
        examples = []
        for example in e.example:
          examples.append(make_intra_rev(example, example, 10))
        raise cli.ArgTypeError('Invalid intra rev', examples)
    raise cli.ArgTypeError('Invalid intra rev',
                           [make_intra_rev('<rev1>', '<rev2>', 10)])

  return argtype_function


def _normalize_repo_url(repo_url):
  repo_url = re.sub(r'https://chrome-internal.googlesource.com/a/',
                    r'https://chrome-internal.googlesource.com/', repo_url)
  repo_url = re.sub(r'\.git$', '', repo_url)
  return repo_url


class PathSpec(object):
  """Specified code version of one path.

  Attributes:
    path: local path, relative to project base dir
    repo_url: code repository location
    at: code version, could be git hash or branch name
  """

  def __init__(self, path, repo_url, at):
    self.path = path
    self.repo_url = repo_url
    self.at = at

  def is_static(self):
    return git_util.is_git_rev(self.at)

  def __eq__(self, rhs):
    if self.path != rhs.path:
      return False
    if self.at != rhs.at:
      return False
    if _normalize_repo_url(self.repo_url) != _normalize_repo_url(rhs.repo_url):
      return False
    return True

  def __ne__(self, rhs):
    return not self == rhs


class Spec(object):
  """Collection of PathSpec.

  Spec is analogy to gclient's DEPS and repo's manifest.

  Attributes:
    spec_type: type of spec, SPEC_FIXED or SPEC_FLOAT. SPEC_FIXED means code
        version is pinned and fixed. On the other hand, SPEC_FLOAT is not
        pinned and the actual version (git commit) may change over time.
    name: name of this spec, for debugging purpose. usually version number
        or git hash
    timestamp: timestamp of this spec
    path: path of spec
    entries: paths to PathSpec dict
  """

  def __init__(self, spec_type, name, timestamp, path, entries=None):
    self.spec_type = spec_type
    self.name = name
    self.timestamp = timestamp
    self.path = path
    self.entries = entries

  def copy(self):
    return copy.deepcopy(self)

  def similar_score(self, rhs):
    """Calculates similar score to another Spec.

    Returns:
      score of similarity. Smaller value is more similar.
    """
    score = 0
    for path in set(self.entries) & set(rhs.entries):
      if rhs[path] == self[path]:
        continue
      if rhs[path].at == self[path].at:
        # it's often that remote repo moved around but should be treated as the
        # same one
        score += 0.1
      else:
        score += 1
    score += len(set(self.entries) ^ set(rhs.entries))
    return score

  def is_static(self):
    return all(path_spec.is_static() for path_spec in self.entries.values())

  def is_subset(self, rhs):
    return set(self.entries.keys()) <= set(rhs.entries.keys())

  def __getitem__(self, path):
    return self.entries[path]

  def __contains__(self, path):
    return path in self.entries

  def apply(self, action_group):
    self.timestamp = action_group.timestamp
    self.name = '(%s)' % self.timestamp
    for action in action_group.actions:
      if isinstance(action, GitAddRepo):
        self.entries[action.path] = PathSpec(action.path, action.repo_url,
                                             action.rev)
      elif isinstance(action, GitCheckoutCommit):
        self.entries[action.path].at = action.rev
      elif isinstance(action, GitRemoveRepo):
        del self.entries[action.path]
      else:
        assert 0, 'unknown action: %s' % action.__class__.__name__

  def dump(self):
    # for debugging
    print(self.name, self.path, self.timestamp)
    print('size', len(self.entries))
    for path, path_spec in sorted(self.entries.items()):
      print(path, path_spec.at)

  def diff(self, rhs):
    logger.info('diff between %s and %s', self.name, rhs.name)
    expect = set(self.entries)
    actual = set(rhs.entries)
    common = 0
    for path in sorted(expect - actual):
      logger.info('-%s', path)
    for path in sorted(actual - expect):
      logger.info('+%s', path)
    for path in sorted(expect & actual):
      if self[path] == rhs[path]:
        common += 1
        continue
      if self[path].at != rhs[path].at:
        logger.info(' %s: at %s vs %s', path, self[path].at, rhs[path].at)
      if self[path].repo_url != rhs[path].repo_url:
        logger.info(' %s: repo_url %s vs %s', path, self[path].repo_url,
                    rhs[path].repo_url)
    logger.info('and common=%s', common)


class Action(object):
  """Actions describe changes from one Spec to another.

  Attributes:
    timestamp: action time
    path: action path, which is relative to project root
  """

  def __init__(self, timestamp, path):
    self.timestamp = timestamp
    self.path = path

  def apply(self, _root_dir):
    raise NotImplementedError

  def summary(self, _code_storage):
    raise NotImplementedError

  def __eq__(self, rhs):
    return self.__dict__ == rhs.__dict__

  def serialize(self):
    return self.__class__.__name__, self.__dict__


def unserialize_action(data):
  classes = [GitCheckoutCommit, GitAddRepo, GitRemoveRepo]
  class_name, values = data
  assert class_name in [cls.__name__ for cls in classes
                       ], 'unknown action class: %s' % class_name
  for cls in classes:
    if class_name == cls.__name__:
      return cls(**values)


class ActionGroup(object):
  """Atomic group of Action objects

  This models atomic commits (for example, gerrit topic, or circular
  CQ-DEPEND). Otherwise, one ActionGroup usually consists only one Action
  object.
  """

  def __init__(self, timestamp, comment=None):
    self.timestamp = timestamp
    self.name = None
    self.actions = []
    self.comment = comment

  def add(self, action):
    self.actions.append(action)

  def serialize(self):
    return (self.timestamp, self.name, [a.serialize() for a in self.actions])

  def summary(self, code_storage):
    if self.comment:
      return self.comment
    # TODO(kcwu): support multiple Actions
    assert len(self.actions) == 1
    return self.actions[0].summary(code_storage)

  @staticmethod
  def unserialize(data):
    ag = ActionGroup(data[0])
    ag.name = data[1]
    for x in data[2]:
      ag.add(unserialize_action(x))
    return ag

  def apply(self, root_dir):
    for action in self.actions:
      action.apply(root_dir)


class GitCheckoutCommit(Action):
  """Describes a git commit action.

  Attributes:
    repo_url: the corresponding url of git repo
    rev: git commit to checkout
  """

  def __init__(self, timestamp, path, repo_url, rev):
    super(GitCheckoutCommit, self).__init__(timestamp, path)
    self.repo_url = repo_url
    self.rev = rev

  def apply(self, root_dir):
    git_repo = os.path.join(root_dir, self.path)
    assert git_util.is_git_root(git_repo)
    git_util.checkout_version(git_repo, self.rev)

  def summary(self, code_storage):
    git_root = code_storage.cached_git_root(self.repo_url)
    summary = git_util.get_commit_log(git_root, self.rev).splitlines()[0]
    return 'commit %s %s %r' % (self.rev[:10], self.path, summary)


class GitAddRepo(Action):
  """Describes a git repo add action.

  Attributes:
    repo_url: the corresponding url of git repo to add
    rev: git commit to checkout
  """

  def __init__(self, timestamp, path, repo_url, rev):
    super(GitAddRepo, self).__init__(timestamp, path)
    self.repo_url = repo_url
    self.rev = rev

  def apply(self, root_dir):
    git_repo = os.path.join(root_dir, self.path)
    assert os.path.exists(git_repo)
    assert git_util.is_git_root(git_repo)

  def summary(self, _code_storage):
    return 'add repo %s from %s@%s' % (self.path, self.repo_url, self.rev[:10])


class GitRemoveRepo(Action):
  """Describes a git repo remove action."""

  def __init__(self, timestamp, path):
    super(GitRemoveRepo, self).__init__(timestamp, path)

  def apply(self, root_dir):
    assert self.path
    git_repo = os.path.join(root_dir, self.path)
    assert git_util.is_git_root(git_repo)
    assert 0
    shutil.rmtree(git_repo)

  def summary(self, _code_storage):
    return 'remove repo %s' % self.path


def apply_actions(code_storage, action_groups, root_dir):
  # Speed optimization: only apply the last one of consecutive commits per
  # repo. It is possible to optimize further, but need to take care git repo
  # add/remove within another repo.
  commits = {}

  def batch_apply(commits):
    for i, commit_action in sorted(commits.values()):
      logger.debug('[%d] applying "%r"', i, commit_action.summary(code_storage))
      commit_action.apply(root_dir)

  for i, action_group in enumerate(action_groups, 1):
    for action in action_group.actions:
      if not isinstance(action, GitCheckoutCommit):
        break
    else:
      # If all actions are commits, defer them for batch processing.
      for action in action_group.actions:
        commits[action.path] = (i, action)
      continue

    batch_apply(commits)
    commits = {}
    action.apply(root_dir)

  batch_apply(commits)


class SpecManager(object):
  """Spec related abstract operations.

  This class enumerates Spec instances and switch disk state to Spec.

  In other words, this class abstracts:
    - discovery of gclient's DEPS and repo's manifest
    - gclient sync and repo sync
  """

  def collect_float_spec(self, old, new):
    """Collects float Spec between two versions.

    This method may fetch spec from network. However, it should not switch tree
    version state.
    """
    raise NotImplementedError

  def collect_fixed_spec(self, old, new):
    """Collects fixed Spec between two versions.

    This method may fetch spec from network. However, it should not switch tree
    version state.
    """
    raise NotImplementedError

  def parse_spec(self, spec):
    """Parses information for Spec object.

    Args:
      spec: Spec object. It specifies what to parse and the parsed information
          is stored inside.
    """
    raise NotImplementedError

  def sync_disk_state(self, rev):
    """Switch source tree state to given version."""
    raise NotImplementedError


class CodeStorage(object):
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

  def cached_git_root(self, repo_url):
    """The cached path of given remote git repo.

    Args:
      repo_url: URL of git remote repo

    Returns:
      path of cache folder
    """
    raise NotImplementedError

  def is_ancestor_commit(self, spec, path, old, new):
    """Determine one commit is ancestor of another.

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

  def get_rev_by_time(self, spec, path, timestamp):
    """Get commit hash of given spec by time.

    Args:
      spec: Spec object
      path: local path relative to project root
      timestamp:

    Returns:
      The commit hash of given time. If there are commits with the given
      timestamp, returns the last commit.
    """
    git_root = self.cached_git_root(spec[path].repo_url)
    # spec[path].at is remote reference name. Since git_root is a mirror, it's
    # no need to convert the name.
    return git_util.get_rev_by_time(git_root, timestamp, spec[path].at)

  def get_actions_between_two_commit(self, spec, path, old, new):
    git_root = self.cached_git_root(spec[path].repo_url)
    result = []
    for timestamp, git_rev in git_util.list_commits_between_commits(
        git_root, old, new):
      result.append(
          GitCheckoutCommit(timestamp, path, spec[path].repo_url, git_rev))
    return result

  def is_containing_commit(self, spec, path, rev):
    git_root = self.cached_git_root(spec[path].repo_url)
    return git_util.is_containing_commit(git_root, rev)

  def are_spec_commits_available(self, spec):
    for path, path_spec in spec.entries.items():
      if not path_spec.is_static():
        continue
      if not self.is_containing_commit(spec, path, path_spec.at):
        return False
    return True


class CodeManager(object):
  """Class to reconstruct historical source tree state.

  This class can reconstruct all moments of source tree state and diffs between
  them.

  Attributes:
    root_dir: root path of project source tree
    spec_manager: SpecManager object
    code_storage: CodeStorage object
  """

  def __init__(self, root_dir, spec_manager, code_storage):
    self.root_dir = root_dir
    self.spec_manager = spec_manager
    self.code_storage = code_storage

  def generate_actions_between_specs(self, prev_float, next_float):
    """Generates actions between two float specs.

    Args:
      prev_float: start of spec object (exclusive)
      next_float: end of spec object (inclusive)

    Returns:
      list of Action object (unordered)
    """
    actions = []
    for path in set(prev_float.entries) | set(next_float.entries):

      # Add repo
      if path not in prev_float:
        if next_float[path].is_static():
          next_at = next_float[path].at
        else:
          next_at = self.code_storage.get_rev_by_time(next_float, path,
                                                      next_float.timestamp)
        actions.append(
            GitAddRepo(next_float.timestamp, path, next_float[path].repo_url,
                       next_at))
        continue

      # Existing path is floating, enumerates commits until next spec.
      #
      #                prev_at                 till_at
      # prev branch ---> o --------> o --------> o --------> o --------> ...
      #                       ^                        ^
      #                 prev_float.timestamp        next_float.timestamp
      if not prev_float[path].is_static():
        prev_at = self.code_storage.get_rev_by_time(prev_float, path,
                                                    prev_float.timestamp)
        till_at = self.code_storage.get_rev_by_time(prev_float, path,
                                                    next_float.timestamp)

        actions.extend(
            self.code_storage.get_actions_between_two_commit(
                prev_float, path, prev_at, till_at))
      else:
        prev_at = till_at = prev_float[path].at

      # At next_float.timestamp.
      if path not in next_float:
        # remove repo
        actions.append(GitRemoveRepo(next_float.timestamp, path))
        next_at = None

      elif next_float[path].is_static():
        # pinned to certain commit on different branch
        next_at = next_float[path].at

      elif next_float[path].at == prev_float[path].at:
        # keep floating on the same branch
        next_at = till_at

      else:
        # switch to another branch
        #                prev_at                 till_at
        # prev branch ---> o --------> o --------> o --------> o --------> ...
        #
        #                                            next_at
        # next branch                 ...... o ------> o --------> o -----> ...
        #                       ^                         ^
        #                 prev_float.timestamp        next_float.timestamp
        next_at = self.code_storage.get_rev_by_time(next_float, path,
                                                    next_float.timestamp)

      if next_at and next_at != till_at:
        actions.append(
            GitCheckoutCommit(next_float.timestamp, path,
                              next_float[path].repo_url, next_at))

    return actions

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
        at = self.code_storage.get_rev_by_time(float_spec, path, timestamp)
        path_spec = PathSpec(path_spec.path, path_spec.repo_url, at)

      result[path] = copy.deepcopy(path_spec)

    name = '%s@%s' % (float_spec.path, timestamp)
    return Spec(SPEC_FIXED, name, timestamp, float_spec.path, result)

  def reorder_actions(self, actions):
    """Reorder and cluster actions.

    Args:
      actions: list of Action objects

    Returns:
      list of ActionGroup objects
    """
    # TODO(kcwu): support atomic commits across repos
    actions.sort(key=lambda x: x.timestamp)
    result = []
    for action in actions:
      group = ActionGroup(action.timestamp)
      group.add(action)
      result.append(group)
    return result

  def match_spec(self, target, specs, start_index=0):
    threshold = 3600
    # ideal_index is the index of last spec before target
    # begin and end are the range of indexes within threshold (inclusive)
    ideal_index = None
    begin, end = None, None
    for i, spec in enumerate(specs[start_index:], start_index):
      if spec.timestamp <= target.timestamp:
        ideal_index = i
      if abs(spec.timestamp - target.timestamp) < threshold:
        if begin is None:
          begin = i
        end = i

    candidates = []
    if ideal_index is not None:
      candidates.append(ideal_index)
    if begin is not None:
      candidates.extend(range(begin, end + 1))
    if not candidates:
      logger.error('unable to match %s: all specs are after it', target.name)
      return None

    compatible_candidates = [
        i for i in candidates if specs[i].is_subset(target)
    ]
    if not compatible_candidates:
      logger.error('unable to match %s: no compatible specs', target.name)
      spec = specs[candidates[0]]
      target.diff(spec)
      return None

    scores = []
    for i in compatible_candidates:
      scores.append((specs[i].similar_score(target), i))
    scores.sort()

    score, index = scores[0]
    if score != 0:
      logger.warning('not exactly match (score=%s): %s', score, target.name)
      target.diff(specs[index])

    if index < ideal_index:
      logger.warning(
          '%s (%s) matched earlier spec at %s instead of %s, racing? offset %d',
          target.name, target.timestamp, specs[index].timestamp,
          specs[ideal_index].timestamp,
          specs[ideal_index].timestamp - target.timestamp)
    if index > ideal_index:
      logger.warning(
          'spec committed at %d matched later commit at %d. bad server clock?',
          target.timestamp, specs[index].timestamp)

    return index

  def associate_fixed_and_synthesized_specs(self, fixed_specs,
                                            synthesized_specs):
    # All fixed specs are snapshot of float specs. Theoretically, they
    # should be identical to one of the synthesized specs.
    # However, it's not always true for some reasons --- maybe due to race
    # condition, maybe due to bugs of this bisect-kit.
    # To overcome this glitch, we try to match them by similarity instead of
    # exact match.
    result = []
    last_index = 0
    for i, fixed_spec in enumerate(fixed_specs):
      matched_index = self.match_spec(fixed_spec, synthesized_specs, last_index)
      if matched_index is None:
        if i in (0, len(fixed_specs) - 1):
          logger.error('essential spec mismatch, unable to continue')
          assert 0
        else:
          logger.warning('%s do not match, skip', fixed_spec.name)
        continue
      result.append((i, matched_index))
      last_index = matched_index

    return result

  def _create_make_up_actions(self, fixed_spec, synthesized):
    timestamp = synthesized.timestamp
    make_up = ActionGroup(
        timestamp, comment='make up glitch for %s' % fixed_spec.name)
    for path in set(fixed_spec.entries) & set(synthesized.entries):
      if fixed_spec[path].at == synthesized[path].at:
        continue
      action = GitCheckoutCommit(timestamp, path, synthesized[path].repo_url,
                                 synthesized[path].at)
      make_up.add(action)

    if not make_up.actions:
      return None
    return make_up

  def build_revlist(self, old, new):
    """Build revlist.

    Returns:
      list of rev string
    """
    logger.info('build_revlist')
    revlist = []

    # step 1, find all float and fixed specs in the given range.
    fixed_specs = self.spec_manager.collect_fixed_spec(old, new)
    float_specs = self.spec_manager.collect_float_spec(old, new)
    while float_specs[-1].timestamp > fixed_specs[-1].timestamp:
      float_specs.pop()
    assert float_specs
    for spec in float_specs + fixed_specs:
      self.spec_manager.parse_spec(spec)

    # step 2, synthesize all fixed specs in the range from float specs.
    specs = float_specs + [fixed_specs[-1]]
    actions = []
    logger.debug('len(specs)=%d', len(specs))
    for i in range(len(specs) - 1):
      prev_float = specs[i]
      next_float = specs[i + 1]
      logger.debug('[%d], between %s (%s) and %s (%s)', i, prev_float.name,
                   prev_float.timestamp, next_float.name, next_float.timestamp)
      for action in self.generate_actions_between_specs(prev_float, next_float):
        if action.timestamp < fixed_specs[0].timestamp:
          continue
        actions.append(action)
    action_groups = self.reorder_actions(actions)

    spec = self.synthesize_fixed_spec(float_specs[0], fixed_specs[0].timestamp)
    synthesized = [spec.copy()]
    for action_group in action_groups:
      spec.apply(action_group)
      synthesized.append(spec.copy())

    # step 3, associate fixed specs with synthesized specs.
    associated_pairs = self.associate_fixed_and_synthesized_specs(
        fixed_specs, synthesized)

    # step 4, group actions and cache them
    for i, (fixed_index, synthesized_index) in enumerate(associated_pairs[:-1]):
      next_fixed_index, next_synthesized_index = associated_pairs[i + 1]
      revlist.append(fixed_specs[fixed_index].name)
      this_action_groups = []

      # handle glitch
      if fixed_specs[fixed_index].similar_score(
          synthesized[synthesized_index]) != 0:
        assert synthesized[synthesized_index].is_subset(
            fixed_specs[fixed_index])
        skipped = set(fixed_specs[fixed_index].entries) - set(
            synthesized[synthesized_index].entries)
        if skipped:
          logger.warning(
              'between %s and %s, '
              'bisect-kit cannot analyze commit history of following paths:',
              fixed_specs[fixed_index].name, fixed_specs[next_fixed_index].name)
          for path in sorted(skipped):
            logger.warning('    %s', path)

        make_up = self._create_make_up_actions(fixed_specs[fixed_index],
                                               synthesized[synthesized_index])
        if make_up:
          this_action_groups.append(make_up)

      this_action_groups.extend(
          action_groups[synthesized_index:next_synthesized_index])
      for idx, ag in enumerate(this_action_groups, 1):
        rev = make_intra_rev(fixed_specs[fixed_index].name,
                             fixed_specs[next_fixed_index].name, idx)
        ag.name = rev
        revlist.append(rev)

      self.save_action_groups_between_releases(
          fixed_specs[fixed_index].name, fixed_specs[next_fixed_index].name,
          this_action_groups)
    revlist.append(fixed_specs[associated_pairs[-1][0]].name)

    return revlist

  def save_action_groups_between_releases(self, old, new, action_groups):
    data = [ag.serialize() for ag in action_groups]

    cache_dir = os.path.join(self.root_dir, _DIFF_CACHE_DIR)
    if not os.path.exists(cache_dir):
      os.makedirs(cache_dir)
    cache_filename = os.path.join(cache_dir, '%s,%s.json' % (old, new))
    with file(cache_filename, 'w') as fp:
      json.dump(data, fp, indent=4, sort_keys=True)

  def load_action_groups_between_releases(self, old, new):
    cache_dir = os.path.join(self.root_dir, _DIFF_CACHE_DIR)
    cache_filename = os.path.join(cache_dir, '%s,%s.json' % (old, new))
    if not os.path.exists(cache_filename):
      raise Exception('cached revlist not found: %s' % cache_filename)

    result = []
    for data in json.load(file(cache_filename)):
      result.append(ActionGroup.unserialize(data))

    return result

  def view_rev_diff(self, old, new):
    old_base, _, _ = parse_intra_rev(old)
    _, new_next, _ = parse_intra_rev(new)
    assert old_base != new_next

    revlist = []
    rev_summary = {}
    fixed_specs = self.spec_manager.collect_fixed_spec(old_base, new_next)
    for i, spec in enumerate(fixed_specs[:-1]):
      action_groups = self.load_action_groups_between_releases(
          fixed_specs[i].name, fixed_specs[i + 1].name)
      revlist.append(spec.name)
      rev_summary[spec.name] = ''
      for action_group in action_groups:
        revlist.append(action_group.name)
        rev_summary[action_group.name] = action_group.summary(self.code_storage)

    revlist.append(fixed_specs[-1].name)
    rev_summary[fixed_specs[-1].name] = ''

    old_index = revlist.index(old)
    new_index = revlist.index(new)
    for rev in revlist[old_index:new_index + 1]:
      logger.info('%s %s', rev, rev_summary[rev])

  def switch(self, rev):
    # easy case
    if not re.match(_re_intra_rev, rev):
      self.spec_manager.sync_disk_state(rev)
      return

    rev_old, rev_new, idx = parse_intra_rev(rev)
    action_groups = self.load_action_groups_between_releases(rev_old, rev_new)
    assert 0 <= idx <= len(action_groups)
    action_groups = action_groups[:idx]

    self.spec_manager.sync_disk_state(rev_old)

    apply_actions(self.code_storage, action_groups, self.root_dir)
