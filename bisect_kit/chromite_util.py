# Copyright 2025 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

import logging
import os
from pathlib import Path
import sys
import tempfile
from typing import Callable, cast, Optional, ParamSpec, TypeVar

from bisect_kit import git_util
from bisect_kit import util


logger = logging.getLogger(__name__)


def _find_chromite_dir(path: Path) -> Optional[Path]:
    """Find the chromite dir in a repo, gclient, or submodule checkout.

    The logic is copy-and-pasted from 'cros' script in the chromite repo:
    https://source.chromium.org/chromium/chromium/tools/depot_tools/+/main:cros;drc=0929ef8d842928b1b5e7aa685130a17fe98fa31e;l=39
    """
    path = path.resolve()
    # Depending on the checkout type (whether repo chromeos or gclient chrome)
    # Chromite lives in a different location.
    roots = (
        # CrOS checkout using normal manifest.
        ('.repo', 'chromite/.git'),
        # CrOS checkout using CitC.
        ('../.citc', 'chromite/__init__.py'),
        # Chromium checkout using gclient+DEPS.
        ('.gclient', 'src/third_party/chromite/.git'),
        # Chromium checkout using git submodules (submodules at root).
        ('.gitmodules', 'chromite/.git'),
        # Chromium checkout using git submodules.
        ('src/.gitmodules', 'src/third_party/chromite/.git'),
        # Chromium checkout using CitC.
        ('../.citc', 'third_party/chromite/__init__.py'),
    )

    while path != Path("/"):
        for root, chromite_git_dir in roots:
            if all((path / x).exists() for x in [root, chromite_git_dir]):
                return (path / chromite_git_dir).parent
        path = path.parent
    return None


def _is_ancestor_or_equal(git_dir: Path, ancestor: str, commit: str) -> bool:
    """Checks if a commit is an ancestor of or equal to another commit.

    Args:
        git_dir: Path to the git repository.
        ancestor: Git hash of the ancestor commit.
        commit: Git hash of the commit.

    Returns:
        True if ancestor is an ancestor of or equal to commit.
    """
    if len(
        ancestor
    ) != git_util.GIT_FULL_COMMIT_ID_LENGTH or not git_util.is_git_rev(
        ancestor
    ):
        raise ValueError(f'Invalid ancestor commit hash: {ancestor}')
    if len(
        commit
    ) != git_util.GIT_FULL_COMMIT_ID_LENGTH or not git_util.is_git_rev(commit):
        raise ValueError(f'Invalid commit hash: {commit}')

    ancestor = ancestor.lower()
    commit = commit.lower()
    return ancestor == commit or git_util.is_ancestor_commit(
        git_dir, ancestor, commit
    )


def _get_required_python_version(
    chromite_dir: Optional[Path],
) -> Optional[list[str]]:
    """Get required python version for the given chromite checkout.

    Args:
        chromite_dir: Path to the chromite checkout.

    Returns:
        A list of required python versions, or None if no specific version is
        required.
    """
    # The first commits in the chromite repo supporting python 3.12 and 3.13.
    MIN_REV_FOR_3_12 = 'f220bbd3d92693db4a51c2d5e5d0575f231127e2'
    MIN_REV_FOR_3_13 = '822b9e35d8052a39b83c08cf44748defce0e3e2d'

    if not chromite_dir or not git_util.is_git_root(chromite_dir):
        return None

    try:
        current_commit_hash = git_util.get_commit_hash(chromite_dir, 'HEAD')
    except ValueError:
        # Sometimes the git repo gets broken for some reason. In that case, it
        # proceeds without doing the hack.
        # TODO(yoshiki): Investigate why it breaks and find altanative way to
        # check the commit hadh
        logger.error(
            'Chromite git repo seems to be broken, but git comannd failed.'
        )
        return None

    # If the current python is 3.12+ but the chromite doesn't support it, use
    # python3.11.
    if sys.version_info >= (3, 12) and not _is_ancestor_or_equal(
        chromite_dir, MIN_REV_FOR_3_12, current_commit_hash
    ):
        logger.info(
            'Current chromite checkout %s requires Python 3.11 or older.',
            current_commit_hash,
        )
        return ['python3.11']

    # If the current python is 3.13+ but the chromite doesn't support it, use
    # python3.12.
    if sys.version_info >= (3, 13) and not _is_ancestor_or_equal(
        chromite_dir, MIN_REV_FOR_3_13, current_commit_hash
    ):
        logger.info(
            'Current chromite checkout %s requires Python 3.12 or older.',
            current_commit_hash,
        )
        return ['python3.12', 'python3.11']

    return None


FuncParam = ParamSpec("FuncParam")
FuncRetType = TypeVar("FuncRetType")


def _run_with_python_version(
    func: Callable[FuncParam, FuncRetType],
    *args: FuncParam.args,
    **kwargs: FuncParam.kwargs,
) -> FuncRetType:
    """Run a function with a specific python version if needed.

    It checks the chromite version and if the current python is not supported,
    it tries to use an older python version by creating a temporary symlink
    and modifying PATH.
    """
    cwd = kwargs.get('cwd', os.getcwd())

    # Retrieve the python name appropriate to the chromite revision.
    chromite_dir = _find_chromite_dir(Path(cwd))
    desired_python_names = _get_required_python_version(chromite_dir)

    if not desired_python_names:
        # It's ok to use the current python.
        return func(*args, **kwargs)

    for desired_python_name in desired_python_names:
        desired_python_path = Path('/usr/bin') / desired_python_name
        if not os.path.exists(desired_python_path):
            continue

        with tempfile.TemporaryDirectory() as temp_dir:
            # Create a symlink to the desired python3 in the temp_dir.
            os.symlink(desired_python_path, Path(temp_dir) / 'python3')

            # Add the temp_dir to PATH.
            env = cast(dict[str, str], kwargs.get('env', os.environ)).copy()
            env['PATH'] = f'{temp_dir}:{env.get("PATH")}'
            kwargs['env'] = env

            # Execute the function with the modified PATH.
            logger.info(
                'Executing chromite command %r with %s.',
                args[0],
                desired_python_name,
            )
            return func(*args, **kwargs)

    logger.error(
        'Current chromite revision requires Python %s, but none of them exist'
        ' on the system.',
        ' or '.join(desired_python_names),
    )
    logger.error(
        'Executing the script with the default Python. This may cause issues.'
    )

    return func(*args, **kwargs)


def check_call(*args, **kwargs) -> None:
    """Wrapper for util.check_call to run with appropriate python version."""
    return _run_with_python_version(util.check_call, *args, **kwargs)


def check_output(*args, **kwargs) -> str:
    """Wrapper for util.check_output to run with appropriate python version."""
    return _run_with_python_version(util.check_output, *args, **kwargs)
