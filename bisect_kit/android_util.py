# -*- coding: utf-8 -*-
# Copyright 2018 The Chromium OS Authors. All rights reserved.
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

from __future__ import print_function
import json
import logging
import os

from bisect_kit import cli
from bisect_kit import repo_util
from bisect_kit import util

logger = logging.getLogger(__name__)

ANDROID_URL_BASE = ('https://android-build.googleplex.com/'
                    'builds/branch/{branch}')
BUILD_IDS_BETWEEN_URL_TEMPLATE = (
    ANDROID_URL_BASE + '/build-ids/between/{end}/{start}')
BUILD_INFO_URL_TEMPLATE = ANDROID_URL_BASE + '/builds?id={build_id}'


def is_android_build_id(s):
  """Is an Android build id."""
  # Build ID is always a number
  return s.isdigit()


def argtype_android_build_id(s):
  if not is_android_build_id(s):
    msg = 'invalid android build id (%s)' % s
    raise cli.ArgTypeError(msg, '9876543')
  return s


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
  logger.warn('fetch_android_build_data need to be hooked')
  import urllib2
  try:
    return urllib2.urlopen(url).read()
  except urllib2.URLError:
    logger.exception('failed to fetch "%s"', url)
    raise


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
  build = json.loads(fetch_android_build_data(url).decode('utf-8'))
  for target in build[0]['targets']:
    if target['target']['name'] == flavor and target.get('successful'):
      return True
  return False


def get_build_ids_between(branch, start, end):
  """Returns a list of build IDs.

  Args:
    branch: The Android branch from which to retrieve the builds.
    start: The starting point build ID. (inclusive)
    end: The ending point build ID. (inclusive)

  Returns:
    A list of build IDs. (str)
  """
  # TODO(kcwu): remove pagination hack after b/68239878 fixed
  build_id_set = set()
  tmp_end = end
  while True:
    url = BUILD_IDS_BETWEEN_URL_TEMPLATE.format(
        branch=branch, start=start, end=tmp_end)
    query_result = json.loads(fetch_android_build_data(url).decode('utf-8'))
    found_new = set(map(int, query_result['ids'])) - build_id_set
    if not found_new:
      break
    build_id_set.update(found_new)
    tmp_end = min(build_id_set)

  logger.debug('Found %d builds in the range.', len(build_id_set))

  return map(str, sorted(build_id_set))


def lunch(android_root, flavor, *args, **kwargs):
  """Helper to run commands with Android lunch env.

  Args:
    android_root: root path of Android tree
    flavor: lunch flavor
    args: command to run
    kwargs: extra arguments passed to util.Popen
  """
  util.check_call('./android_lunch_helper.sh', android_root, flavor, *args,
                  **kwargs)


def fetch_artifact(flavor, build_id, filename, dest):
  """Fetches Android build artifact.

  Args:
    flavor: Android build flavor
    build_id: Android build id
    filename: artifact name
    dest: local path to store the fetched artifact
  """
  util.check_call('/google/data/ro/projects/android/fetch_artifact', '--target',
                  flavor, '--bid', build_id, filename, dest)


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
  manifest_path = os.path.join(android_root, '.repo', 'manifests',
                               manifest_name)
  if not os.path.exists(manifest_path):
    fetch_artifact(flavor, build_id, manifest_name, manifest_path)
  return manifest_name


class AndroidManifestManager(repo_util.ManifestManager):
  """Manifest operations for Android repo"""

  def __init__(self, config):
    self.config = config
    self.flavor = config['flavor']

  def sync_disk_state(self, rev):
    manifest_name = self.fetch_manifest(rev)
    repo_util.sync(
        self.config['android_root'],
        manifest_name=manifest_name,
        current_branch=True)

  def fetch_git_repos(self, rev):
    # TODO(kcwu): fetch git history but don't switch current disk state.
    self.sync_disk_state(rev)

  def enumerate_manifest(self, old, new):
    revlist = get_build_ids_between(self.config['branch'], int(old), int(new))

    assert revlist[0] == old
    assert revlist[-1] == new
    return revlist

  def fetch_manifest(self, rev):
    return fetch_manifest(self.config['android_root'], self.flavor, rev)
