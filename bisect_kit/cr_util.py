#!/usr/bin/env python
# Copyright 2017 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Chrome utility."""

from __future__ import print_function
import json
import logging
import os
import re
import subprocess
import urllib2

from bisect_kit import cli
from bisect_kit import core
from bisect_kit import git_util
from bisect_kit import util

logger = logging.getLogger(__name__)

RE_CHROME_VERSION = r'^\d+\.\d+\.\d+\.\d+$'


def is_chrome_version(s):
  """Is a chrome version string?"""
  return bool(re.match(RE_CHROME_VERSION, s))


def extract_branch_from_version(rev):
  """Extracts branch number from version string

  Args:
    rev: chrome version string

  Returns:
    branch number (str). For example, return '3064' for '59.0.3064.0'.
  """
  return rev.split('.')[2]


def argtype_chrome_version(s):
  if not is_chrome_version(s):
    raise cli.ArgTypeError('invalid chrome version', '59.0.3064.0')
  return s


def query_commit_position(chrome_src, git_rev):
  """Queries chrome commit position by git hash.

  Chrome (excluding dependencies) maintains "Cr-Commit-Position", an
  incremental serial number of commits per branch in commit log.

  p.s. Earlier than Aug 2014, the serial number was just svn version number and
  mirrored to git as "git-svn-id". Although this function recognizes
  "git-svn-id", the rest of bisect-kit is never tested against such old version
  and doesn't guarantee to work.

  Args:
    chrome_src: git repo path of chrome/src
    git_rev: git hash id

  Returns:
    (branch, position):
      branch: branch name, e.g. "master" or "2133". Note, it will
          return "master" for svn's trunk for convenient.
      position: '123'

  Raises:
    ValueError: failed to recognize commit position.
  """
  msg = git_util.get_commit_log(chrome_src, git_rev)
  m = re.search(r'Cr-Commit-Position: (\S+)', msg)
  if m:
    position = m.group(1)
    m = re.match(r'refs/heads/master@\{#(\d+)\}', position)
    if m:
      return 'master', m.group(1)
    m = re.match(r'refs/branch-heads/(\d+)@\{#(\d+)\}', position)
    if m:
      return m.group(1), m.group(2)
    raise ValueError('Unrecognized commit position: ' + position)

  m = re.search(r'git-svn-id: (\S+)', msg)
  if m:
    position = m.group(1)
    m = re.match(r'svn://svn.chromium.org/chrome/trunk/src@(\d+)', position)
    if m:
      return 'master', m.group(1)
    m = re.match(r'svn://svn.chromium.org/chrome/branches/(\d+)/src@(\d+)',
                 position)
    if m:
      return m.group(1), m.group(2)
    raise ValueError('Unrecognized svn-id: ' + position)

  raise ValueError('Unable to recognize commit position from ' + git_rev)


def query_git_rev_by_commit_position(chrome_src, branch, commit_position):
  """Looks up git commit by chrome's commit position.

  This function cannot find early commits which is still using 'git-svn-id'.

  Args:
    chrome_src: git repo path of chrome/src
    branch: branch number or 'master'
    commit_position: chrome's commit position number

  Returns:
    git commit id

  Raises:
    core.ExecutionFatalError: unable to find commits with given
        commit_position.
  """
  if branch == 'master':
    ref_path = 'refs/heads/master'
  else:
    ref_path = 'refs/branch-heads/%s' % branch

  rev = util.check_output(
      'git',
      'rev-list',
      '-1',
      '--all',
      '--grep',
      'Cr-Commit-Position: %s@{#%s}' % (ref_path, commit_position),
      cwd=chrome_src).strip()
  if not rev:
    raise ValueError('unable to find commits with given commit_position')
  return rev


def query_git_rev(chrome_src, rev):
  """Guesses git commit by heuristic.

  Args:
    chrome_src: git repo path of chrome/src
    rev: could be
      chrome version, commit position, or git hash

  Returns:
    git commit hash
  """
  if is_chrome_version(rev):
    try:
      # Query via git tag
      return git_util.get_commit_hash(chrome_src, rev)
    except subprocess.CalledProcessError:
      # Failed due to source code not synced yet? Anyway, query by web api
      url = 'https://omahaproxy.appspot.com/deps.json?version=' + rev
      logger.debug('fetch %s', url)
      content = urllib2.urlopen(url).read()
      obj = json.loads(content)
      rev = obj['chromium_commit']
      if not git_util.is_git_rev(rev):
        raise ValueError(
            'The response of omahaproxy, %s, is not a git commit hash' % rev)

  if git_util.is_git_rev(rev):
    return rev

  # Cr-Commit-Position
  m = re.match(r'^#(\d+)$', rev)
  if m:
    return query_git_rev_by_commit_position(chrome_src, 'master', m.group(1))

  raise ValueError, 'unknown rev format: %s' % rev


def build_and_deploy(chrome_src, board, dut, targets):
  """Build and deploy Chrome binaries to ChromeOS DUT

  Args:
    chrome_src: git repo path of chrome/src
    board: ChromeOS board name
    dut: DUT address
    targets: ninja targets. Default to build chrome.
  """
  # The main steps are written in the helper shell script because it is
  # expected to be often local hacked by users.
  dirname = os.path.dirname(os.path.abspath(__file__))
  build_and_deploy_script = os.path.join(dirname, '..',
                                         'build_and_deploy_chrome_helper.sh')

  # To run command inside chrome-sdk shell.
  prefix = [
      'cros',
      'chrome-sdk',
      '--board=%s' % board,
      '--internal',
      '--',
  ]

  # The actual command to run.
  cmd = ['sh', '-ex', build_and_deploy_script] + targets
  full_cmd = prefix + cmd

  env = os.environ.copy()
  env['DUT'] = dut

  util.check_call(*full_cmd, cwd=chrome_src, env=env)
