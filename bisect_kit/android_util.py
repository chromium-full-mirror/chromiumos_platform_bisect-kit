# Copyright 2018 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Android utility.

Terminology used in this module:
  "product-variant" is sometimes called "target" and sometimes "flavor".
  I prefer to use "flavor" in the code because
    - "target" is too general
    - sometimes, it is not in the form of "product-variant", for example,
      "cts_arm_64"
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import urllib.request

from bisect_kit import codechange
from bisect_kit import errors
from bisect_kit import git_util
from bisect_kit import plugin_util
from bisect_kit import repo_util
from bisect_kit import util
import google.auth.transport.requests
import google.oauth2.service_account
import googleapiclient
import googleapiclient.discovery
import httplib2


logger = logging.getLogger(__name__)

ANDROID_URL_BASE = 'https://android-build.googleplex.com/builds/branch/{branch}'
BUILD_IDS_BETWEEN_URL_TEMPLATE = (
    ANDROID_URL_BASE + '/build-ids/between/{end}/{start}'
)
BUILD_INFO_URL_TEMPLATE = ANDROID_URL_BASE + '/builds?id={build_id}'


def get_flavor(android_target: str, board_cpu_arch: str) -> str | None:
    """Returns the android flavor.

    Args:
      android_target: The android target fetched from build_report.json
      dut_cpu_arch: CPU architecture of the board

    Returns:
      returns the android flavor or None if the cpu arch of the board is unknown.
    """
    logger.debug(
        'android target: %s, board cpu architecture: %s',
        android_target,
        board_cpu_arch,
    )
    if board_cpu_arch not in ['arm', 'arm64', 'x86_64']:
        return None
    android_flavor = '%s_%s-user' % (android_target, board_cpu_arch)
    return android_flavor


def is_android_build_id(s):
    """Is an Android build id."""
    # Build ID is always a number
    return s.isdigit()


def argtype_android_build_id(s):
    if not is_android_build_id(s):
        msg = 'invalid android build id (%s)' % s
        raise errors.ArgTypeError(msg, '9876543')
    return s


@plugin_util.patch
def fetch_android_build_data(url):
    """Fetches file from android build server.

    Args:
      url: URL to fetch

    Returns:
      file content (str). None if failed.
    """
    # Fetching android build data directly will fail without authentication.
    # Following code is just serving as demo purpose. You should modify or hook
    # it with your own implementation.
    logger.warning('fetch_android_build_data need to be hooked')
    try:
        return urllib.request.urlopen(url).read().decode('utf8')
    except urllib.error.URLError as e:
        logger.exception('failed to fetch "%s"', url)
        raise errors.ExternalError(str(e))


@plugin_util.patch
def is_good_build(branch, flavor, build_id):
    """Determine a build_id was succeeded.

    Args:
      branch: The Android branch from which to retrieve the builds.
      flavor: Target name of the Android image in question.
          E.g. "cheets_x86-userdebug" or "cheets_arm-user".
      build_id: Android build id

    Returns:
      True if the given build was successful.
    """

    url = BUILD_INFO_URL_TEMPLATE.format(branch=branch, build_id=build_id)
    build = json.loads(fetch_android_build_data(url))
    for target in build[0]['targets']:
        if target['target']['name'] == flavor and target.get('successful'):
            return True
    return False


@plugin_util.patch
def get_build_ids_between(branch, flavor, start, end):
    """Returns a list of build IDs.

    Args:
      branch: The Android branch from which to retrieve the builds.
      flavor: Target name of the Android image in question.
          E.g. "cheets_x86-userdebug" or "cheets_arm-user".
      start: The starting point build ID. (inclusive)
      end: The ending point build ID. (inclusive)

    Returns:
      A list of build IDs. (str)
    """
    # Do not remove this argument because our internal implementation need it.
    del flavor  # not used

    # TODO(kcwu): remove pagination hack after b/68239878 fixed
    build_id_set: set[int] = set()
    tmp_end = end
    while True:
        url = BUILD_IDS_BETWEEN_URL_TEMPLATE.format(
            branch=branch, start=start, end=tmp_end
        )
        query_result = json.loads(fetch_android_build_data(url))
        found_new = set(int(x) for x in query_result['ids']) - build_id_set
        if not found_new:
            break
        build_id_set.update(found_new)
        tmp_end = min(build_id_set)

    logger.debug('Found %d builds in the range.', len(build_id_set))

    return [str(bid) for bid in sorted(build_id_set)]


def lunch(android_root, flavor, *args, **kwargs):
    """Helper to run commands with Android lunch env.

    Args:
      android_root: root path of Android tree
      flavor: lunch flavor
      args: command to run
      kwargs: extra arguments passed to util.Popen
    """
    util.check_call(
        './android_lunch_helper.sh', android_root, flavor, *args, **kwargs
    )


def fetch_artifact_from_g3(flavor, build_id, filename, dest_folder):
    """Fetches Android build artifact.

    Args:
      flavor: Android build flavor
      build_id: Android build id
      filename: artifact name
      dest_folder: local path to store the fetched artifact
    """
    util.check_call(
        '/google/data/ro/projects/android/fetch_artifact',
        '--target',
        flavor,
        '--bid',
        build_id,
        filename,
        dest_folder,
    )


def fetch_artifact(flavor, build_id, filename, dest_folder):
    """Fetches Android build artifact.

    Args:
      flavor: Android build flavor
      build_id: Android build id
      filename: artifact name
      dest_folder: local path to store the fetched artifact
    """
    if os.environ.get('SKYLAB_CLOUD_SERVICE_ACCOUNT_JSON'):
        api = AndroidInternalApi()
        api.download_artifact(
            build_id, flavor, filename, os.path.join(dest_folder, filename)
        )
    else:
        fetch_artifact_from_g3(flavor, build_id, filename, dest_folder)


def fetch_manifest(android_root, flavor, build_id):
    """Fetches Android repo manifest of given build.

    Args:
      android_root: root path of Android tree. Fetched manifest file will be
          stored inside.
      flavor: Android build flavor
      build_id: Android build id

    Returns:
      the local filename of manifest (relative to android_root/.repo/manifests/)
    """
    # Assume manifest is flavor independent, thus not encoded into the file name.
    manifest_name = 'manifest_%s.xml' % build_id
    manifest_path = os.path.join(
        android_root, '.repo', 'manifests', manifest_name
    )
    if not os.path.exists(manifest_path):
        fetch_artifact(flavor, build_id, manifest_name, manifest_path)
    return manifest_name


def lookup_build_timestamp(flavor, build_id):
    """Lookup timestamp of Android prebuilt.

    Args:
      flavor: Android build flavor
      build_id: Android build id

    Returns:
      timestamp
    """
    tmp_fn = tempfile.mktemp()
    try:
        fetch_artifact(flavor, build_id, 'BUILD_INFO', tmp_fn)
        with open(tmp_fn) as f:
            data = json.load(f)
        return int(data['sync_start_time'])
    finally:
        if os.path.exists(tmp_fn):
            os.unlink(tmp_fn)


class AndroidSpecManager(codechange.SpecManager):
    """Repo manifest related operations.

    This class fetches and enumerates android manifest files, parses them,
    and sync to disk state according to them.
    """

    def __init__(self, config: dict):
        self.config = config
        self.manifest_dir = os.path.join(
            self.config.get('android_root'), '.repo', 'manifests'
        )

    def collect_float_spec(
        self,
        old: str,
        new: str,
        fixed_specs: list[codechange.Spec] | None = None,
    ) -> list[codechange.Spec]:
        del fixed_specs  # unused
        path = 'default.xml'

        commits: list[git_util.Commit] = []
        old_timestamp = lookup_build_timestamp(self.config.get('flavor'), old)
        new_timestamp = lookup_build_timestamp(self.config.get('flavor'), new)
        for commit in git_util.get_history(self.manifest_dir, path):
            if commit.timestamp < old_timestamp:
                commits = []
            commits.append(commit)
            if commit.timestamp > new_timestamp:
                break

        float_specs = [
            codechange.Spec.new_float(commit.rev, commit.timestamp, path)
            for commit in commits
        ]
        return float_specs

    def collect_fixed_spec(self, old: str, new: str) -> list[codechange.Spec]:
        result = []
        revlist = get_build_ids_between(
            self.config.get('branch'),
            self.config.get('flavor'),
            int(old),
            int(new),
        )
        for rev in revlist:
            manifest_name = fetch_manifest(
                self.config.get('android_root'), self.config.get('flavor'), rev
            )
            path = os.path.join(self.manifest_dir, manifest_name)
            timestamp = lookup_build_timestamp(self.config.get('flavor'), rev)
            result.append(codechange.Spec.new_fixed(rev, timestamp, path))
        return result

    def _load_manifest_content(self, spec: codechange.Spec) -> str:
        if spec.is_fixed():
            manifest_name = fetch_manifest(
                self.config.get('branch'), self.config.get('flavor'), spec.name
            )
            with open(os.path.join(self.manifest_dir, manifest_name)) as f:
                content = f.read()
        else:
            content = git_util.get_file_from_revision(
                self.manifest_dir, spec.name, spec.path
            )
        return content

    def parse_spec(self, spec: codechange.Spec) -> None:
        logger.debug('parse_spec %s', spec.name)
        manifest_content = self._load_manifest_content(spec)
        parser = repo_util.ManifestParser(self.manifest_dir)
        root = parser.parse_single_xml(manifest_content, allow_include=False)
        spec.entries = parser.process_parsed_result(root)
        if spec.is_fixed() and not spec.is_static():
            raise ValueError(
                f'fixed spec {spec.name!r} has unexpected floating entries'
            )

    def sync_disk_state(self, rev: str) -> None:
        manifest_name = fetch_manifest(
            self.config.get('android_root'), self.config.get('flavor'), rev
        )
        repo_util.sync(
            self.config.get('android_root'),
            manifest_name=manifest_name,
            current_branch=True,
        )


class AndroidInternalApi:
    """Wrapper of android internal api. Singleton class"""

    _instance = None
    _default_chunk_size = 20 * 1024 * 1024  # 20M
    _retries = 5

    def __new__(cls):
        if cls._instance is None:
            cls._instance = object.__new__(cls)
        return cls._instance

    def __init__(self):
        if not os.environ.get('SKYLAB_CLOUD_SERVICE_ACCOUNT_JSON'):
            raise errors.InternalError(
                'SKYLAB_CLOUD_SERVICE_ACCOUNT_JSON '
                'must defined when using Android Api'
            )
        self.credentials = self.get_credentials()
        self.session = google.auth.transport.requests.AuthorizedSession(
            self.credentials
        )

    def get_credentials(self):
        # TODO(zjchang): support compute engine type credential
        credentials = (
            google.oauth2.service_account.Credentials.from_service_account_file(
                os.environ.get('SKYLAB_CLOUD_SERVICE_ACCOUNT_JSON'),
                scopes=[
                    'https://www.googleapis.com/auth/androidbuild.internal'
                ],
            )
        )
        return credentials

    def get_artifact_url(self, build_id, target, name, attempt_id=0):
        """Get Android build artifact URL.

        Args:
          build_id: Android build ID.
          target: Android build target.
          name: Artifact name.
          attempt_id: A build attempt. Defaults to 0.

        Returns:
          Artifact download URL.

        Raises:
          errors.ExternalError
        """
        try:
            resp = self.session.get(
                (
                    'https://androidbuild-pa.googleapis.com/v4/builds/'
                    f'{build_id}/{target}/attempts/{attempt_id}/artifacts/{name}/url'
                )
            )
            resp.raise_for_status()
            artifact = resp.json()
        except (googleapiclient.errors.HttpError, httplib2.HttpLib2Error) as e:
            raise errors.ExternalError('Android API error: ' + str(e))

        if artifact and 'signedUrl' in artifact:
            return artifact['signedUrl']

        raise errors.ExternalError(
            'Invalid artifact %s/%s/%s/%s'
            % (build_id, target, attempt_id, name)
        )

    def download_artifact(
        self, build_id, target, name, output_file, attempt_id=0
    ):
        """Download one artifact.

        Args:
          build_id: Android build ID.
          target: Android build target.
          name: Artifact name.
          output_file: Target file path.
          attempt_id: A build attempt. Defaults to 0.

        Raises:
          errors.ExternalError
        """
        url = self.get_artifact_url(
            build_id, target, name, attempt_id=attempt_id
        )

        try:
            with urllib.request.urlopen(url) as resp:
                with open(output_file, mode='wb') as f:
                    shutil.copyfileobj(resp, f, length=self._default_chunk_size)
        except Exception as e:
            raise errors.ExternalError('Download artifact error: ' + str(e))
