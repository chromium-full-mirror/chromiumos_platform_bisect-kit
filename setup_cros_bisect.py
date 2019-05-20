#!/usr/bin/env python2
# -*- coding: utf-8 -*-
# Copyright 2018 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Helper script to prepare source trees for ChromeOS bisection.

Typical usage:

  Initial setup:
    $ %(prog)s init --chromeos
    $ %(prog)s init --chrome
    $ %(prog)s init --android=pi-arc-dev

  Sync code if necessary:
    $ %(prog)s sync

  Create source trees for bisection
    $ %(prog)s new --session=12345

  After bisection finished, delete trees
    $ %(prog)s delete --session=12345
"""
from __future__ import print_function
import argparse
import csv
import logging
import os
import subprocess
import time
import urllib2
import urlparse

from bisect_kit import common
from bisect_kit import configure
from bisect_kit import gclient_util
from bisect_kit import git_util
from bisect_kit import locking
from bisect_kit import repo_util
from bisect_kit import util

DEFAULT_MIRROR_BASE = os.path.expanduser('~/git-mirrors')
DEFAULT_WORK_BASE = os.path.expanduser('~/bisect-workdir')
CHECKOUT_TEMPLATE_NAME = 'template'

logger = logging.getLogger(__name__)


class DefaultProjectPathFactory(object):
  """Factory for chromeos/chrome/android source tree paths."""

  def __init__(self, mirror_base, work_base, session):
    self.mirror_base = mirror_base
    self.work_base = work_base
    self.session = session

  def get_chromeos_mirror(self):
    return os.path.join(self.mirror_base, 'chromeos')

  def get_chromeos_tree(self):
    return os.path.join(self.work_base, self.session, 'chromeos')

  def get_android_mirror(self, branch):
    return os.path.join(self.mirror_base, 'android.%s' % branch)

  def get_android_tree(self, branch):
    return os.path.join(self.work_base, self.session, 'android.%s' % branch)

  def get_chrome_cache(self):
    return os.path.join(self.mirror_base, 'chrome')

  def get_chrome_tree(self):
    return os.path.join(self.work_base, self.session, 'chrome')


def subvolume_or_makedirs(opts, path):
  if os.path.exists(path):
    return

  path = os.path.abspath(path)
  if opts.btrfs:
    dirname, basename = os.path.split(path)
    if not os.path.exists(dirname):
      os.makedirs(dirname)
    util.check_call('btrfs', 'subvolume', 'create', basename, cwd=dirname)
  else:
    os.makedirs(path)


def is_btrfs_subvolume(path):
  if util.check_output('stat', '-f', '--format=%T', path).strip() != 'btrfs':
    return False
  return util.check_output('stat', '--format=%i', path).strip() == '256'


def snapshot_or_copytree(src, dst):
  assert os.path.isdir(src), '%s does not exist' % src
  assert os.path.isdir(os.path.dirname(dst))

  # Make sure dst do not exist, otherwise it becomes "dst/name" (one extra
  # depth) instead of "dst".
  assert not os.path.exists(dst)

  if is_btrfs_subvolume(src):
    util.check_call('btrfs', 'subvolume', 'snapshot', src, dst)
  else:
    # -a for recursion and preserve all attributes.
    util.check_call('cp', '-a', src, dst)


def collect_removed_manifest_repos(repo_dir, last_sync_time, only_branch=None):
  manifest_dir = os.path.join(repo_dir, '.repo', 'manifests')
  git_util.fetch(manifest_dir)

  manifest_path = 'default.xml'
  manifest_full_path = os.path.join(manifest_dir, manifest_path)
  # hack for chromeos symlink
  if os.path.islink(manifest_full_path):
    manifest_path = os.readlink(manifest_full_path)

  parser = repo_util.ManifestParser(manifest_dir)
  latest = None
  removed = {}
  for _, git_rev in reversed(
      parser.enumerate_manifest_commits(last_sync_time, None, manifest_path)):
    root = parser.parse_xml_recursive(git_rev, manifest_path)
    if (only_branch and root.find('default') is not None and
        root.find('default').get('revision') != only_branch):
      break
    entries = parser.process_parsed_result(root)
    if latest is None:
      assert entries is not None
      latest = entries
      continue

    for path, path_spec in entries.items():
      if path in latest:
        continue
      if path in removed:
        continue
      removed[path] = path_spec

  return removed


def setup_chromeos_repos(opts, path_factory):
  chromeos_mirror = path_factory.get_chromeos_mirror()
  chromeos_tree = path_factory.get_chromeos_tree()
  subvolume_or_makedirs(opts, chromeos_mirror)
  subvolume_or_makedirs(opts, chromeos_tree)

  manifest_url = (
      'https://chrome-internal.googlesource.com/chromeos/manifest-internal')
  repo_url = 'https://chromium.googlesource.com/external/repo.git'

  if os.path.exists(os.path.join(chromeos_mirror, '.repo', 'manifests')):
    logger.warning(
        '%s has already been initialized, assume it is setup properly',
        chromeos_mirror)
  else:
    logger.info('repo init for chromeos mirror')
    repo_util.init(
        chromeos_mirror,
        manifest_url=manifest_url,
        repo_url=repo_url,
        mirror=True)

    local_manifest_dir = os.path.join(chromeos_mirror, '.repo',
                                      'local_manifests')
    os.mkdir(local_manifest_dir)
    with open(os.path.join(local_manifest_dir, 'manifest-versions.xml'),
              'w') as f:
      f.write('''<?xml version="1.0" encoding="UTF-8"?>
          <manifest>
            <project name="chromeos/manifest-versions" remote="cros-internal" />
          </manifest>
          ''')

  logger.info('repo init for chromeos tree')
  repo_util.init(
      chromeos_tree,
      manifest_url=manifest_url,
      repo_url=repo_url,
      reference=chromeos_mirror)

  with locking.lock_file(
      os.path.join(chromeos_mirror, locking.LOCK_FILE_FOR_MIRROR_SYNC)):
    logger.info('repo sync for chromeos mirror (this takes hours; be patient)')
    repo_util.sync(chromeos_mirror)

  logger.info('repo sync for chromeos tree')
  repo_util.sync(chromeos_tree)


def read_last_sync_time(repo_dir):
  timestamp_path = os.path.join(repo_dir, 'last_sync_time')
  if os.path.exists(timestamp_path):
    with open(timestamp_path) as f:
      return int(f.read())
  else:
    # 4 months should be enough for most bisect cases.
    return int(time.time()) - 86400 * 120


def write_sync_time(repo_dir, sync_time):
  timestamp_path = os.path.join(repo_dir, 'last_sync_time')
  with open(timestamp_path, 'w') as f:
    f.write('%d\n' % sync_time)


def write_extra_manifest_to_mirror(repo_dir, removed):
  local_manifest_dir = os.path.join(repo_dir, '.repo', 'local_manifests')
  if not os.path.exists(local_manifest_dir):
    os.mkdir(local_manifest_dir)
  with open(os.path.join(local_manifest_dir, 'deleted-repos.xml'), 'w') as f:
    f.write('''<?xml version="1.0" encoding="UTF-8"?>\n<manifest>\n''')
    remotes = {}
    for path_spec in removed.values():
      scheme, netloc, remote_path = urlparse.urlsplit(path_spec.repo_url)[:3]
      assert remote_path[0] == '/'
      remote_path = remote_path[1:]
      if (scheme, netloc) not in remotes:
        remote_name = 'remote_for_deleted_repo_%s' % (scheme + netloc)
        remotes[scheme, netloc] = remote_name
        f.write('''  <remote name="%s" fetch="%s" />\n''' %
                (remote_name, '%s://%s' % (scheme, netloc)))
      f.write(
          '''  <project name="%s" path="%s" remote="%s" revision="%s" />\n''' %
          (remote_path, path_spec.path, remotes[scheme, netloc], path_spec.at))
    f.write('''</manifest>\n''')


def generate_extra_manifest_for_deleted_repo(repo_dir, only_branch=None):
  last_sync_time = read_last_sync_time(repo_dir)
  removed = collect_removed_manifest_repos(
      repo_dir, last_sync_time, only_branch=only_branch)
  write_extra_manifest_to_mirror(repo_dir, removed)
  logger.info('since last sync, %d repo got removed', len(removed))


def sync_chromeos_code(opts, path_factory):
  del opts  # unused

  start_sync_time = int(time.time())
  chromeos_mirror = path_factory.get_chromeos_mirror()

  logger.info('repo sync for chromeos mirror')
  generate_extra_manifest_for_deleted_repo(chromeos_mirror)
  repo_util.sync(chromeos_mirror)
  write_sync_time(chromeos_mirror, start_sync_time)

  logger.info('repo sync for chromeos tree')
  chromeos_tree = path_factory.get_chromeos_tree()
  repo_util.sync(chromeos_tree)

  # test_that may use this ssh key and ssh complains its permission is too open
  util.check_call(
      'chmod',
      'o-r,g-r',
      'src/scripts/mod_for_test_scripts/ssh_keys/testing_rsa',
      cwd=chromeos_tree)


def query_chrome_latest_branch():
  result = None
  r = urllib2.urlopen('https://omahaproxy.appspot.com/all')
  for row in csv.DictReader(r):
    if row['true_branch'].isdigit():
      result = max(result, int(row['true_branch']))
  return result


def setup_chrome_repos(opts, path_factory):
  chrome_cache = path_factory.get_chrome_cache()
  subvolume_or_makedirs(opts, chrome_cache)
  chrome_tree = path_factory.get_chrome_tree()
  subvolume_or_makedirs(opts, chrome_tree)

  latest_branch = query_chrome_latest_branch()
  logger.info('latest chrome branch is %d', latest_branch)
  assert latest_branch
  spec = '''
solutions = [
  { "name"        : "buildspec",
    "url"         : "https://chrome-internal.googlesource.com/a/chrome/tools/buildspec.git",
    "deps_file"   : "branches/%d/DEPS",
    "custom_deps" : {
    },
    "custom_vars": {'checkout_src_internal': True},
  },
]
target_os = ['chromeos']
cache_dir = %r
''' % (latest_branch, chrome_cache)

  with locking.lock_file(
      os.path.join(chrome_cache, locking.LOCK_FILE_FOR_MIRROR_SYNC)):
    logger.info('gclient config for chrome')
    gclient_util.config(chrome_tree, spec=spec)

    is_first_sync = not os.listdir(chrome_cache)
    if is_first_sync:
      logger.info('gclient sync for chrome (this takes hours; be patient)')
    else:
      logger.info('gclient sync for chrome')
    gclient_util.sync(
        chrome_tree, with_branch_heads=True, with_tags=True, ignore_locks=True)

    # It's possible that some repos are removed from latest branch and thus
    # their commit history is not fetched in recent gclient sync. So we call
    # 'git fetch' for all existing git mirrors.
    # TODO(kcwu): only sync repos not in DEPS files of latest branch
    logger.info('additional sync for chrome mirror')
    for git_repo_name in os.listdir(chrome_cache):
      # another gclient is running or leftover of previous run; skip
      if git_repo_name.startswith('_cache_tmp'):
        continue
      git_repo = os.path.join(chrome_cache, git_repo_name)
      if not git_util.is_git_bare_dir(git_repo):
        continue
      git_util.fetch(git_repo)

    # Some repos were removed from the DEPS and won't be synced here. They will
    # be synced during DEPS file processing because the necessary information
    # requires full DEPS parsing. (crbug.com/902238)


def sync_chrome_code(opts, path_factory):
  # The sync step is identical to the initial gclient config step.
  setup_chrome_repos(opts, path_factory)


def setup_android_repos(opts, path_factory, branch):
  android_mirror = path_factory.get_android_mirror(branch)
  android_tree = path_factory.get_android_tree(branch)
  subvolume_or_makedirs(opts, android_mirror)
  subvolume_or_makedirs(opts, android_tree)

  manifest_url = ('persistent-https://googleplex-android.git.corp.google.com'
                  '/platform/manifest')
  repo_url = 'https://gerrit.googlesource.com/git-repo'

  if os.path.exists(os.path.join(android_mirror, '.repo', 'manifests')):
    logger.warning(
        '%s has already been initialized, assume it is setup properly',
        android_mirror)
  else:
    logger.info('repo init for android mirror branch=%s', branch)
    repo_util.init(
        android_mirror,
        manifest_url=manifest_url,
        repo_url=repo_url,
        manifest_branch=branch,
        mirror=True)

  logger.info('repo init for android tree branch=%s', branch)
  repo_util.init(
      android_tree,
      manifest_url=manifest_url,
      repo_url=repo_url,
      manifest_branch=branch,
      reference=android_mirror)

  logger.info('repo sync for android mirror (this takes hours; be patient)')
  repo_util.sync(android_mirror, current_branch=True)

  logger.info('repo sync for android tree branch=%s', branch)
  repo_util.sync(android_tree, current_branch=True)


def sync_android_code(opts, path_factory, branch):
  del opts  # unused
  start_sync_time = int(time.time())
  android_mirror = path_factory.get_android_mirror(branch)
  android_tree = path_factory.get_android_tree(branch)

  with locking.lock_file(
      os.path.join(android_mirror, locking.LOCK_FILE_FOR_MIRROR_SYNC)):
    logger.info('repo sync for android mirror branch=%s', branch)
    # Android usually big jump between milestone releases and add/delete lots of
    # repos when switch releases. Because it's infeasible to bisect between such
    # big jump, the deleted repo is useless. In order to save disk, do not sync
    # repos deleted in other branches.
    generate_extra_manifest_for_deleted_repo(android_mirror, only_branch=branch)
    repo_util.sync(android_mirror, current_branch=True)
    write_sync_time(android_mirror, start_sync_time)

  logger.info('repo sync for android tree branch=%s', branch)
  repo_util.sync(android_tree, current_branch=True)


def cmd_init(opts):
  path_factory = DefaultProjectPathFactory(opts.mirror_base, opts.work_base,
                                           CHECKOUT_TEMPLATE_NAME)

  if opts.chromeos:
    setup_chromeos_repos(opts, path_factory)
  if opts.chrome:
    setup_chrome_repos(opts, path_factory)
  for branch in opts.android:
    setup_android_repos(opts, path_factory, branch)


def enumerate_android_branches_available(base):
  branches = []
  for name in os.listdir(base):
    if name.startswith('android.'):
      branches.append(name.partition('.')[2])
  return branches


def do_sync(opts):
  path_factory = DefaultProjectPathFactory(opts.mirror_base, opts.work_base,
                                           CHECKOUT_TEMPLATE_NAME)

  sync_all = False
  if not opts.chromeos and not opts.chrome and not opts.android:
    logger.info('sync trees for all')
    sync_all = True

  if sync_all or opts.chromeos:
    sync_chromeos_code(opts, path_factory)
  if sync_all or opts.chrome:
    sync_chrome_code(opts, path_factory)

  if sync_all:
    android_branches = enumerate_android_branches_available(opts.mirror_base)
  else:
    android_branches = opts.android
  for branch in android_branches:
    sync_android_code(opts, path_factory, branch)


def cmd_sync(opts):
  try:
    do_sync(opts)
  except subprocess.CalledProcessError:
    # Sync may fail due to network or server issues.
    logger.exception('do_sync failed, will retry one minute later')
    time.sleep(60)
    do_sync(opts)


def cmd_new(opts):
  work_dir = os.path.join(opts.work_base, opts.session)
  if not os.path.exists(work_dir):
    os.makedirs(work_dir)

  template_factory = DefaultProjectPathFactory(opts.mirror_base, opts.work_base,
                                               CHECKOUT_TEMPLATE_NAME)
  path_factory = DefaultProjectPathFactory(opts.mirror_base, opts.work_base,
                                           opts.session)

  prepare_all = False
  if not opts.chromeos and not opts.chrome and not opts.android:
    logger.info('prepare trees for all')
    prepare_all = True

  chromeos_template = template_factory.get_chromeos_tree()
  if (prepare_all and os.path.exists(chromeos_template)) or opts.chromeos:
    logger.info('prepare tree for chromeos, %s',
                path_factory.get_chromeos_tree())
    snapshot_or_copytree(chromeos_template, path_factory.get_chromeos_tree())

  chrome_template = template_factory.get_chrome_tree()
  if (prepare_all and os.path.exists(chrome_template)) or opts.chrome:
    logger.info('prepare tree for chrome, %s', path_factory.get_chrome_tree())
    snapshot_or_copytree(chrome_template, path_factory.get_chrome_tree())

  if prepare_all:
    android_branches = enumerate_android_branches_available(opts.mirror_base)
  else:
    android_branches = opts.android
  for branch in android_branches:
    logger.info('prepare tree for android branch=%s, %s', branch,
                path_factory.get_android_tree(branch))
    snapshot_or_copytree(
        template_factory.get_android_tree(branch),
        path_factory.get_android_tree(branch))


def delete_tree(path):
  if is_btrfs_subvolume(path):
    # btrfs should be mounted with 'user_subvol_rm_allowed' option and thus
    # normal user permission is enough.
    util.check_call('btrfs', 'subvolume', 'delete', path)
  else:
    util.check_call('sudo', 'rm', '-rf', path)


def cmd_list(opts):
  print('%-20s  %s' % ('Session', 'Path'))
  for name in os.listdir(opts.work_base):
    if name == CHECKOUT_TEMPLATE_NAME:
      continue
    path = os.path.join(opts.work_base, name)
    print('%-20s  %s' % (name, path))


def cmd_delete(opts):
  assert opts.session
  path_factory = DefaultProjectPathFactory(opts.mirror_base, opts.work_base,
                                           opts.session)

  chromeos_tree = path_factory.get_chromeos_tree()
  if os.path.exists(chromeos_tree):
    if os.path.exists(os.path.join(chromeos_tree, 'chromite')):
      # ignore error
      util.call('cros_sdk', '--unmount', cwd=chromeos_tree)
    delete_tree(chromeos_tree)

  chrome_tree = path_factory.get_chrome_tree()
  if os.path.exists(chrome_tree):
    delete_tree(chrome_tree)

  android_branches = enumerate_android_branches_available(opts.mirror_base)
  for branch in android_branches:
    android_tree = path_factory.get_android_tree(branch)
    if os.path.exists(android_tree):
      delete_tree(android_tree)

  os.rmdir(os.path.join(opts.work_base, opts.session))


def create_parser():
  parser = argparse.ArgumentParser(
      formatter_class=argparse.RawDescriptionHelpFormatter, description=__doc__)
  parser.add_argument(
      '--mirror_base',
      metavar='MIRROR_BASE',
      default=configure.get('MIRROR_BASE', DEFAULT_MIRROR_BASE),
      help='Directory for mirrors (default: %(default)s)')
  parser.add_argument(
      '--work_base',
      metavar='WORK_BASE',
      default=configure.get('WORK_BASE', DEFAULT_WORK_BASE),
      help='Directory for bisection working directories (default: %(default)s)')
  common.add_common_arguments(parser)
  subparsers = parser.add_subparsers(
      dest='command', title='commands', metavar='<command>')

  parser_init = subparsers.add_parser(
      'init', help='Mirror source trees and create template checkout')
  parser_init.add_argument(
      '--chrome', action='store_true', help='init chrome mirror and tree')
  parser_init.add_argument(
      '--chromeos', action='store_true', help='init chromeos mirror and tree')
  parser_init.add_argument(
      '--android',
      metavar='BRANCH',
      action='append',
      default=[],
      help='init android mirror and tree of BRANCH')
  parser_init.add_argument(
      '--btrfs',
      action='store_true',
      help='create btrfs subvolume for source tree')
  parser_init.set_defaults(func=cmd_init)

  parser_sync = subparsers.add_parser(
      'sync',
      help='Sync source trees',
      description='Sync all if no projects are specified '
      '(--chrome, --chromeos, or --android)')
  parser_sync.add_argument(
      '--chrome', action='store_true', help='sync chrome mirror and tree')
  parser_sync.add_argument(
      '--chromeos', action='store_true', help='sync chromeos mirror and tree')
  parser_sync.add_argument(
      '--android',
      metavar='BRANCH',
      action='append',
      default=[],
      help='sync android mirror and tree of BRANCH')
  parser_sync.set_defaults(func=cmd_sync)

  parser_new = subparsers.add_parser(
      'new',
      help='Create new source checkout for bisect',
      description='Create for all if no projects are specified '
      '(--chrome, --chromeos, or --android)')
  parser_new.add_argument('--session', required=True)
  parser_new.add_argument(
      '--chrome', action='store_true', help='create chrome checkout')
  parser_new.add_argument(
      '--chromeos', action='store_true', help='create chromeos checkout')
  parser_new.add_argument(
      '--android',
      metavar='BRANCH',
      action='append',
      default=[],
      help='create android checkout of BRANCH')
  parser_new.set_defaults(func=cmd_new)

  parser_list = subparsers.add_parser(
      'list', help='List existing sessions with source checkout')
  parser_list.set_defaults(func=cmd_list)

  parser_delete = subparsers.add_parser('delete', help='Delete source checkout')
  parser_delete.add_argument('--session', required=True)
  parser_delete.set_defaults(func=cmd_delete)

  return parser


def main():
  common.init()
  parser = create_parser()
  opts = parser.parse_args()
  common.config_logging(opts)

  opts.func(opts)


if __name__ == '__main__':
  main()
