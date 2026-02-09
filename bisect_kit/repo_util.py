# Copyright 2018 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Repo utility.

This module provides wrapper for "repo" (a Google-built repository management
tool that runs on top of git) and related utility functions.
"""

from __future__ import annotations

import logging
import multiprocessing
import os
import re
import shlex
import subprocess
import time
import urllib.parse
import xml.etree.ElementTree

from bisect_kit import codechange
from bisect_kit import errors
from bisect_kit import git_util
from bisect_kit import util
import numpy


logger = logging.getLogger(__name__)

# Relative to repo root dir.
REPO_META_DIR = '.repo'
LOCAL_MANIFESTS_DIR = os.path.join(REPO_META_DIR, 'local_manifests')


def get_manifest_url(manifest_dir: str):
    """Get manifest URL of repo project.

    Args:
      manifest_dir: path of manifest directory

    Returns:
      manifest URL.
    """
    url = git_util.config(manifest_dir, 'remote.origin.url')
    return url


def find_repo_root(path: str):
    """Find the root path of a repo project

    Args:
      path: path

    Returns:
      project root if path is inside a repo project; otherwise None
    """
    path = os.path.abspath(path)
    while not os.path.exists(os.path.join(path, '.repo')):
        if path == '/':
            return None
        path = os.path.dirname(path)
    return path


def _get_repo_sync_env():
    # b/120757273 Even we have set git cookies, git still occasionally asks for
    # username/password for unknown reasons. Then it hangs forever because we are
    # a script. Here we work around the issue by setting GIT_ASKPASS and fail the
    # auth. The failure is usually harmless because bisect-kit will retry.
    env = os.environ.copy()
    env['GIT_ASKPASS'] = '/bin/true'
    return env


class HTTP429Exception(Exception):
    """HTTP 429 (too many requests) error happens."""


def _repo_cmd_random_sleep(mean: float) -> float:
    """Get a random number in seconds to sleep before next repo command retry.

    This is to prevent from an excessive amount of repo git commands
    sent from concurrent bisections.
    The random number is drawn from an exponential distribution at mean |mean|.

    Args:
      mean: the mean of the exponential distrubition.
    Returns:
      the number of seconds to sleep.
    """
    MAX_DELAY_SECONDS = 60 * 60
    MIN_DELAY_SECONDS = 1

    delay = numpy.random.default_rng().exponential(scale=mean, size=1)[0]
    return max(min(delay, MAX_DELAY_SECONDS), MIN_DELAY_SECONDS)


# If repo sync takes too long, it may hang forever.
_REPO_SYNC_TIMEOUT_SECONDS = 3 * 60 * 60


# The mean of initial random delay.
# The git server has QPS limit. The default refill time is 5 seconds.
# As a result, a minimum cool down time of 5 seconds is needed. Set it to 10 to
# play more safe.
_REPO_CMD_MEAN_INITIAL_DELAY_SECONDS = 10


def _repo_cmd(*cmd, repo_dir, timeout=None, initial_delay=False):
    """Run a repo sync command.

    Args:
      cmd: the command
      repo_dir: the repository path
      timeout: timeout in seconds
      initial_delay: whether to introduce a random initial delay to avoid
        network traffic congestion
    """
    env = _get_repo_sync_env()
    try:
        util.check_call('update_depot_tools', cwd=repo_dir, env=env)
    except subprocess.CalledProcessError:
        logger.warning('update_depot_tools failed')

    def catch_http_errors(line):
        line = line.decode('utf8', errors='ignore')
        if re.search('error: RPC failed; HTTP 429', line):
            raise HTTP429Exception(line)

    if timeout:
        started_time = time.time()

    attempt = 0
    delay_mean = _REPO_CMD_MEAN_INITIAL_DELAY_SECONDS
    while True:
        if attempt > 0 or initial_delay:
            delay = _repo_cmd_random_sleep(delay_mean)
            logger.debug(
                'random delay for repo commands %s (with mean %s)',
                delay,
                delay_mean,
            )
            time.sleep(delay)
            delay_mean *= 2

        if timeout:
            remaining_time = timeout - (time.time() - started_time)
            logger.debug('remaining_time: %s', remaining_time)
            if remaining_time < 0:
                raise errors.ExecutionTimeout(
                    'repo command %s timeout' % list(cmd)
                )

        try:
            logger.debug('attempt %s of %s', attempt + 1, list(cmd))
            attempt += 1
            # repo command spawn repo_launcher subprocesses, wrap it in a subshell so
            # the stdout/stderr can be captured.
            # Set binary=True because the command ever return non-UTF8 chars.
            p = util.Popen(
                ['bash', '-c', shlex.join(cmd)],
                cwd=repo_dir,
                env=env,
                binary=True,
                stderr_callback=catch_http_errors,
            )
            p.wait(timeout=remaining_time if timeout else None)
        except HTTP429Exception as e:
            p.terminate()
            logger.error(
                'failed to execute repo command %s: %s. retry later.',
                list(cmd),
                e,
            )
            continue
        # other exceptions are raised immediately.
        if p.returncode == 0:
            logger.debug('repo command %s completed successfully', list(cmd))
            return
        raise subprocess.CalledProcessError(p.returncode, cmd)


def init(
    repo_dir,
    manifest_url,
    manifest_branch=None,
    manifest_name=None,
    repo_url=None,
    reference=None,
    mirror=False,
    groups=None,
):
    """Repo init.

    Args:
      repo_dir: root directory of repo
      manifest_url: manifest repository location
      manifest_branch: manifest branch or revision
      manifest_name: initial manifest file name
      repo_url: repo repository location
      reference: location of mirror directory
      mirror: indicates repo mirror
      groups: repo sync groups, groups should be separate by comma
    """
    root = find_repo_root(repo_dir)
    if root and root != repo_dir:
        raise errors.ExternalError(
            '%s should not be inside another repo project at %s'
            % (repo_dir, root)
        )

    cmd = ['repo', 'init', '--manifest-url', manifest_url]
    if manifest_name:
        cmd += ['--manifest-name', manifest_name]
    if manifest_branch:
        cmd += ['--manifest-branch', manifest_branch]
    if repo_url:
        cmd += ['--repo-url', repo_url]
    if reference:
        cmd += ['--reference', reference]
    if groups:
        cmd += ['--groups', groups]
    if mirror:
        cmd.append('--mirror')
    _repo_cmd(
        *cmd,
        repo_dir=repo_dir,
        timeout=_REPO_SYNC_TIMEOUT_SECONDS,
        initial_delay=True,
    )


def cleanup_repo_generated_files(repo_dir, manifest_name='default.xml'):
    """Cleanup files generated by <copyfile> <linkfile> tags.

    Args:
      repo_dir: root directory of repo
      manifest_name: filename of manifest
    """
    manifest_dir = os.path.join(repo_dir, '.repo', 'manifests')
    manifest_path = os.path.join(manifest_dir, manifest_name)
    if os.path.islink(manifest_path):
        manifest_name = os.readlink(manifest_path)
    parser = ManifestParser(manifest_dir)
    manifest = parser.parse_xml_recursive('HEAD', manifest_name)

    for copyfile in manifest.findall('.//copyfile'):
        dest = copyfile.get('dest')
        if not dest:
            continue
        # `dest` is relative to the top of the tree
        dest_path = os.path.join(repo_dir, dest)
        if not os.path.isfile(dest_path):
            continue
        logger.debug('delete file %r', dest_path)
        os.unlink(dest_path)

    for linkfile in manifest.findall('.//linkfile'):
        dest = linkfile.get('dest')
        if not dest:
            continue
        # `dest` is relative to the top of the tree
        dest_path = os.path.join(repo_dir, dest)
        if not os.path.islink(dest_path):
            continue
        logger.debug('delete link %r', dest_path)
        os.unlink(dest_path)


def sync(repo_dir, jobs=16, manifest_name=None, current_branch=None):
    """Repo sync.

    Args:
      repo_dir: root directory of repo
      jobs: projects to fetch simultaneously
      manifest_name: filename of manifest
      current_branch: fetch only current branch if True; None means following
          repo's default behavior
    """
    # Workaround to prevent garbage files left between repo syncs
    # (http://crbug.com/881783).
    cleanup_repo_generated_files(repo_dir)

    # Previous failed "repo sync" may leave some "repo_launcher" subprocesses,
    # which could hang new "repo sync" commands.
    try:
        util.check_call('pkill', '-f', 'repo_launcher')
    except subprocess.CalledProcessError:
        logger.debug(
            'failed to pkill previous repo_launcher. '
            'Maybe there is no such processes left. '
            'This is in general harmless.'
        )

    cmd = ['repo', 'sync', '-v', '--force-sync', '--no-use-superproject']
    if jobs:
        cmd += ['-j', str(jobs)]
    if manifest_name:
        cmd += ['--manifest-name', manifest_name]
    if current_branch is not None:
        cmd += ['--current-branch' if current_branch else '--no-current-branch']
    _repo_cmd(
        *cmd,
        repo_dir=repo_dir,
        timeout=_REPO_SYNC_TIMEOUT_SECONDS,
        initial_delay=True,
    )


def cherry_pick(repo_dir: str, cl: str):
    """Cherry pick a gerrit CL.

    Args:
      repo_dir: root directory of repo
      cl: gerrit cl to cherry pick
    """
    cmd = ['repo', 'download', '-c', cl]
    _repo_cmd(*cmd, repo_dir=repo_dir)


def abandon(repo_dir, branch_name):
    """Repo abandon.

    Args:
      repo_dir: root directory of repo
      branch_name: branch name to abandon
    """
    # Ignore errors if failed, which means the branch didn't exist beforehand.
    util.call('repo', 'abandon', branch_name, cwd=repo_dir)


def info(repo_dir, query):
    """Repo info.

    Args:
      repo_dir: root directory of repo
      query: key to query
    """
    try:
        output = util.check_output('repo', 'info', '.', cwd=repo_dir)
    except subprocess.CalledProcessError as e:
        if 'Manifest branch:' not in e.output:
            raise
        # "repo info" may exit with error while the data we want is already
        # printed. Ignore errors for such case.
        output = e.output
    for line in output.splitlines():
        if ':' not in line:
            continue
        # remove ANSI Escape Sequences.
        line = re.sub(r'(\x9B|\x1B\[)[0-?]*[ -\/]*[@-~]', '', line)
        key, value = line.split(':', 1)
        key, value = key.strip(), value.strip()
        if key == query:
            return value

    return None


def get_current_branch(repo_dir):
    """Get manifest branch of existing repo directory."""
    return info(repo_dir, 'Manifest branch')


def get_manifest_groups(repo_dir):
    """Get manifest group of existing repo directory."""
    return info(repo_dir, 'Manifest groups')


def list_projects(repo_dir):
    """Repo list.

    Args:
      repo_dir: root directory of repo

    Returns:
      list of paths, relative to repo_dir
    """
    result = []
    for line in util.check_output(
        'repo', 'list', '--path-only', cwd=repo_dir
    ).splitlines():
        result.append(line)
    return result


def cleanup_unexpected_files(repo_dir):
    """Clean up unexpected files in repo tree.

    Note this is not fully equivalent to 'repo sync' from scratch because:
      - This only handle git repo folders. In other words, directories under
        repo_dir not inside any git repo will not be touched.
      - It ignores files if matching gitignore pattern.
    So we can keep cache files to speed up incremental build next time.

    If you want truly clean tree, delete entire tree and repo sync directly
    instead.

    Args:
      repo_dir: root directory of repo
    """
    projects = list_projects(repo_dir)

    # When we clean up project X, we don't want to touch files under X's
    # subprojects. Collect the nested project relationship here.
    nested: dict[str, list[str]] = {}
    # By sorting, parent directory will loop before subdirectories.
    for project_path in sorted(projects):
        components = project_path.split(os.sep)
        for i in range(len(components) - 1, 0, -1):
            head = os.sep.join(components[:i])
            tail = os.sep.join(components[i:])
            if head in nested:
                nested[head].append(tail)
                break
        nested[project_path] = []

    with multiprocessing.Pool() as pool:
        cleanup_jobs = []
        for project_path in projects:
            git_repo = os.path.join(repo_dir, project_path)
            if not os.path.exists(git_repo):
                # It should be harmless to ignore git repo nonexistence because 'repo
                # sync' will restore them.
                logger.warning('git repo not found: %s', git_repo)
                continue
            cleanup_jobs.append((git_repo, nested[project_path]))
        pool.starmap(git_util.distclean, cleanup_jobs)


def _urljoin(base, url):
    # urlparse.urljoin doesn't recognize "persistent-https://" protocol.
    # Following hack replaces "persistent-https" by obsolete protocol "gopher"
    # before urlparse.urljoin and replaces back after urlparse.urljoin calls.
    dummy_scheme = 'gopher://'
    new_scheme = 'persistent-https://'
    assert not base.startswith(dummy_scheme)
    assert not url.startswith(dummy_scheme)
    base = re.sub('^' + new_scheme, dummy_scheme, base)
    url = re.sub('^' + new_scheme, dummy_scheme, url)
    result = urllib.parse.urljoin(base, url)
    result = re.sub('^' + dummy_scheme, new_scheme, result)
    return result


class ManifestParser:
    """Enumerates historical manifest files and parses them."""

    def __init__(self, manifest_dir, load_remote=True):
        self.manifest_dir = manifest_dir
        if load_remote:
            self.manifest_url = get_manifest_url(self.manifest_dir)
        else:
            self.manifest_url = None

    def parse_single_xml(self, content, allow_include=False):
        root = xml.etree.ElementTree.fromstring(content)
        if not allow_include and root.find('include') is not None:
            raise errors.InternalError(
                'Expects self-contained manifest. <include> is not allowed'
            )
        return root

    def parse_xml_recursive(self, git_rev, path):
        content = git_util.get_file_from_revision(
            self.manifest_dir, git_rev, path
        )
        root = xml.etree.ElementTree.fromstring(content)
        default = None
        notice = None
        remotes: dict[str, xml.etree.ElementTree.Element] = {}
        manifest_server = None
        result = xml.etree.ElementTree.Element('manifest')

        for node in root:
            if node.tag == 'include':
                nodes = self.parse_xml_recursive(git_rev, node.get('name'))
            else:
                nodes = [node]

            for subnode in nodes:
                if subnode.tag == 'default':
                    if default is not None and not self.element_equal(
                        default, subnode
                    ):
                        raise errors.ExternalError(
                            'duplicated <default> %s and %s'
                            % (
                                self.element_to_string(default),
                                self.element_to_string(subnode),
                            )
                        )
                    if default is None:
                        default = subnode
                        result.append(subnode)
                elif subnode.tag == 'remote':
                    name = subnode.get('name')
                    if name in remotes and not self.element_equal(
                        remotes[name], subnode
                    ):
                        raise errors.ExternalError(
                            'duplicated <remote> %s and %s'
                            % (
                                self.element_to_string(default),
                                self.element_to_string(subnode),
                            )
                        )
                    if name not in remotes:
                        remotes[name] = subnode
                        result.append(subnode)
                elif subnode.tag == 'notice':
                    if notice is not None and not self.element_equal(
                        notice, subnode
                    ):
                        raise errors.ExternalError('duplicated <notice>')
                    if notice is None:
                        notice = subnode
                        result.append(subnode)
                elif subnode.tag == 'manifest-server':
                    if manifest_server is not None:
                        raise errors.ExternalError(
                            'duplicated <manifest-server>'
                        )
                    manifest_server = subnode
                    result.append(subnode)
                else:
                    result.append(subnode)
        return result

    @classmethod
    def element_to_string(cls, element):
        return xml.etree.ElementTree.tostring(
            element, encoding='unicode'
        ).strip()

    @classmethod
    def get_project_path(cls, project):
        path = project.get('path')
        # default path is its name
        if not path:
            path = project.get('name')
        # Follow repo's behavior to strip trailing slash (crbug/1086043).
        return path.rstrip('/')

    @classmethod
    def get_project_revision(cls, project, default):
        if default is None:
            default = {}
        return project.get('revision', default.get('revision'))

    def element_equal(self, element1, element2):
        """Return if two xml elements are same

        Args:
          element1: An xml element
          element2: An xml element
        """
        if element1.tag != element2.tag:
            return False
        if element1.text != element2.text:
            return False
        if element1.attrib != element2.attrib:
            return False
        if len(element1) != len(element2):
            return False
        return all(
            self.element_equal(node1, node2)
            for node1, node2 in zip(element1, element2)
        )

    def process_parsed_result(self, root, group_constraint='default'):
        if group_constraint not in ('default', 'all'):
            raise ValueError('only "default" and "all" are supported')
        result = {}
        default = root.find('default')
        if default is None:
            default = {}

        remote_fetch_map = {}
        for remote in root.findall('.//remote'):
            name = remote.get('name')
            fetch_url = _urljoin(self.manifest_url, remote.get('fetch'))
            if urllib.parse.urlparse(fetch_url).path not in ('', '/'):
                # TODO(kcwu): support remote url with sub folders
                raise errors.InternalError(
                    'only support git repo at root path of remote server: %s'
                    % fetch_url
                )
            remote_fetch_map[name] = fetch_url

        assert root.find('include') is None

        for project in root.findall('.//project'):
            if group_constraint == 'default':
                if 'notdefault' in project.get('groups', ''):
                    continue
            for subproject in project.findall('.//project'):
                logger.warning(
                    'nested project %s.%s is not supported and ignored',
                    project.get('name'),
                    subproject.get('name'),
                )

            path = self.get_project_path(project)
            revision = self.get_project_revision(project, default)

            remote_name = project.get('remote', default.get('remote'))
            if remote_name not in remote_fetch_map:
                raise errors.InternalError(
                    'unknown remote name=%s' % remote_name
                )
            fetch_url = remote_fetch_map.get(remote_name)
            # Follow repo's behavior to strip trailing slash (crbug/1086043).
            name = project.get('name').rstrip('/')
            repo_url = _urljoin(fetch_url, name)

            result[path] = codechange.PathSpec(path, repo_url, revision)
        return result

    def enumerate_manifest_commits(
        self, start_time, end_time, path, branch=None
    ):
        def parse_dependencies(path, content):
            try:
                root = self.parse_single_xml(content, allow_include=True)
            except xml.etree.ElementTree.ParseError:
                logger.warning('%s syntax error, skip', path)
                return None

            result = []
            for include in root.findall('.//include'):
                result.append(include.get('name'))
            return result

        return git_util.get_history_recursively(
            self.manifest_dir,
            path,
            start_time,
            end_time,
            parse_dependencies,
            branch=branch,
        )


class RepoMirror(codechange.CodeStorage):
    """Repo git mirror."""

    def __init__(self, mirror_dir: str):
        self.mirror_dir = mirror_dir

    def _url_to_cache_dir(self, url) -> str:
        # Here we assume remote fetch url is always at root of server url, so we can
        # simply treat whole path as repo project name.
        path = urllib.parse.urlparse(url).path
        assert path[0] == '/'
        return '%s.git' % path[1:]

    def cached_git_root(self, repo_url: str) -> str:
        cache_path = self._url_to_cache_dir(repo_url)

        # The location of chromeos manifest-internal repo mirror is irregular
        # (http://crbug.com/895957). This is a workaround.
        if cache_path == 'chromeos/manifest-internal.git':
            cache_path = 'manifest-internal.git'

        return os.path.join(self.mirror_dir, cache_path)

    def _load_project_list(self, project_root: str) -> list[str]:
        repo_project_list = os.path.join(project_root, '.repo', 'project.list')
        with open(repo_project_list) as f:
            return f.readlines()

    def _save_project_list(self, project_root: str, lines: list[str]) -> None:
        repo_project_list = os.path.join(project_root, '.repo', 'project.list')
        with open(repo_project_list, 'w') as f:
            f.write(''.join(sorted(lines)))

    def add_to_project_list(
        self, project_root: str, path: str, repo_url: str
    ) -> None:
        del repo_url
        lines = self._load_project_list(project_root)

        line = path + '\n'
        if line not in lines:
            lines.append(line)

        self._save_project_list(project_root, lines)

    def remove_from_project_list(self, project_root: str, path: str) -> None:
        lines = self._load_project_list(project_root)

        line = path + '\n'
        if line in lines:
            lines.remove(line)

        self._save_project_list(project_root, lines)


class Manifest:
    """This class handles a manifest and is able to patch projects."""

    def __init__(self, manifest_internal_dir):
        self.xml = None
        self.manifest_internal_dir = manifest_internal_dir
        self.modified = set()
        self.parser = ManifestParser(manifest_internal_dir)

    def load_from_string(self, xml_string):
        """Load manifest xml from a string.

        Args:
          xml_string: An xml string.
        """
        self.xml = xml.etree.ElementTree.fromstring(xml_string)

    def load_from_commit(self, commit):
        """Load manifest xml snapshot by a commit hash.

        Args:
          commit: A manifest-internal commit hash.
        """
        self.xml = self.parser.parse_xml_recursive(commit, 'default.xml')

    def load_from_timestamp(self, timestamp):
        """Load manifest xml snapshot by a timestamp.

        The function will load a latest manifest before or equal to the timestamp.

        Args:
          timestamp: A unix timestamp.
        """
        commits = git_util.get_history(
            self.manifest_internal_dir, before=timestamp + 1
        )
        self.load_from_commit(commits[-1].rev)

    def to_string(self):
        """Dump current xml to a string.

        Returns:
          A string of xml.
        """
        return ManifestParser.element_to_string(self.xml)

    def is_static_manifest(self):
        """Return true if there is any project without revision in the xml.

        Returns:
          A boolean, True if every project has a revision.
        """
        count = 0
        for project in self.xml.findall('.//project'):
            # check argument directly instead of getting value from default tag
            if not project.get('revision'):
                count += 1
                path = self.parser.get_project_path(project)
                logger.warning('path: %s has no revision', path)
        return count == 0

    def remove_project_revision(self):
        """Remove revision argument from all projects"""
        for project in self.xml.findall('.//project'):
            if 'revision' in project:
                del project['revision']

    def count_path(self, path):
        """Count projects that path is given path.

        Returns:
          An integer, indicates the number of projects.
        """
        result = 0
        for project in self.xml.findall('.//project'):
            if project.get('path') == path:
                result += 1
        return result

    def apply_commit(self, path, revision, overwrite=True):
        """Set revision to a project by path.

        Args:
          path: A project's path.
          revision: A git commit id.
          overwrite: Overwrite flag, the project won't change if overwrite=False
                     and it was modified before.
        """
        if path in self.modified and not overwrite:
            return
        self.modified.add(path)

        count = 0
        for project in self.xml.findall('.//project'):
            if self.parser.get_project_path(project) == path:
                count += 1
                project.set('revision', revision)

        if count != 1:
            logger.warning('found %d path: %s in manifest', count, path)

    def apply_upstream(self, path, upstream):
        """Set upstream to a project by path.

        Args:
          path: A project's path.
          upstream: A git upstream.
        """
        for project in self.xml.findall('.//project'):
            if self.parser.get_project_path(project) == path:
                project.set('upstream', upstream)

    def apply_action_groups(self, action_groups):
        """Apply multiple action groups to xml.

        If there are multiple actions in one repo, only last one is applied.

        Args:
          action_groups: A list of action groups.
        """
        # Apply in reversed order with overwrite=False,
        # so each repo is on the state of last action.
        for action_group in reversed(action_groups):
            for action in reversed(action_group.actions):
                if isinstance(action, codechange.GitCheckoutCommit):
                    self.apply_commit(action.path, action.rev, overwrite=False)
                if isinstance(action, codechange.GitAddRepo):
                    self.apply_commit(action.path, action.rev, overwrite=False)
                if isinstance(action, codechange.GitRemoveRepo):
                    assert self.count_path(action.path) == 0
                    self.modified.add(action.path)

    def apply_manifest(self, manifest):
        """Apply another manifest to current xml.

        By default, all the projects in manifest will be applied and won't
        overwrite modified projects.

        Args:
          manifest: A Manifest object.
        """
        default = manifest.xml.get('default')
        for project in manifest.xml.findall('.//project'):
            path = self.parser.get_project_path(project)
            revision = self.parser.get_project_revision(project, default)
            if path and revision:
                self.apply_commit(path, revision, overwrite=False)
                upstream = project.get('upstream')
                if upstream:
                    self.apply_upstream(path, upstream)
