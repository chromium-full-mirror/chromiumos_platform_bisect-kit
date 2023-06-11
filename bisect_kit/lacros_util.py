# Copyright 2022 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Lacros related util functions."""

from __future__ import annotations

import logging
import multiprocessing
import os
import pathlib
import shutil
import subprocess
import tempfile
import time

from bisect_kit import common
from bisect_kit import cr_util
from bisect_kit import errors
from bisect_kit import gs_util
from bisect_kit import locking
from bisect_kit import util


logger = logging.getLogger(__name__)

LACROS_ZIP_PATH = (
    'gs://chrome-unsigned/desktop-5c0tCh/{version}/'
    '{lacros_folder_name}/lacros.zip'
)
LACROS_FILENAME = 'lacros.zip'
BOARD_ARCHITECTURES = {
    'x86_64': {
        'lacros_dir': 'lacros64',
        'arg_for_gn': 'amd64',
    },
    'aarch64': {
        'lacros_dir': 'lacros-arm32',
        'arg_for_gn': 'arm',
    },
}
_DEFAULT_OUT_DEVICE_LACROS_DIR = os.path.join('out_device_lacros', 'Release')


def get_lacros_gs_path_list(
    old_milestone: int = 88,
    new_milestone: int = 150,
    lacros_folder_name: str = BOARD_ARCHITECTURES['x86_64']['lacros_dir'],
) -> list[str]:
    """Get all the lacros gs paths from bucket between 2 milestones.

    For every milestone the gsutil is called in a seperate worker process.
    Without mutli process the query takes around 8 minutes for 88 to 106
      milestones.
    With multi process it takes less than 20 seconds.
    Example query for one worker process:
    `gsutil ls gs://chrome-unsigned/desktop-5c0tCh/100.**/lacros64/lacros.zip`
    Usually if lacros64 is available, lacros-arm32 is also available.

    Args:
      old_milestone: Old milestone of chrome version.
      new_milestone: New milestone of chrome version.
      lacros_folder_name: either 'lacros-64' or 'lacros-arm32' depending on DUT
        architecture.

    Returns:
      A list of gs path
    """
    version_prefixes = []
    for milestone in range(old_milestone, new_milestone + 1):
        version_prefixes.append('%d.**' % milestone)

    with multiprocessing.Pool() as pool:
        async_results = []
        for version_prefix in version_prefixes:
            lacros_zip_path = LACROS_ZIP_PATH.format(
                version=version_prefix, lacros_folder_name=lacros_folder_name
            )
            async_results.append(
                pool.apply_async(
                    gs_util.ls,
                    [lacros_zip_path],
                    {'ignore_errors': True},
                )
            )

        result = []
        for async_result in async_results:
            for lacros_zip_path in async_result.get():
                result.append(lacros_zip_path)
        return result


def _get_all_lacros_prebuilt_between_two_milestone(
    old_milestone: str, new_milestone: str, lacros_folder_name: str
) -> list[str]:
    """Returns a list of lacros prebuilt version between 2 ChromeOS milestone.

    Args:
      old_milestone: Old ChromeOS milestone
      new_milestone: New ChromeOS milestone
      lacros_folder_name: either 'lacros-64' or 'lacros-arm32' depending on DUT
        architecture

    Returns:
      A list of lacros version. (list[str]).
    """
    assert lacros_folder_name in [
        x['lacros_dir'] for x in BOARD_ARCHITECTURES.values()
    ]

    lacros_gs_path_list = get_lacros_gs_path_list(
        old_milestone, new_milestone, lacros_folder_name
    )
    lacros_version_list: list[str] = []

    for gs_path in lacros_gs_path_list:
        seperated_gs_path = gs_path.split('/')
        if len(seperated_gs_path) < 3:
            continue
        lacros_version = seperated_gs_path[-3]
        lacros_version_list.append(lacros_version)

    return lacros_version_list


def get_lacros_prebuilt_versions_between(
    old_version: str, new_version: str, lacros_folder_name: str
) -> list[str]:
    """Returns a list of lacros prebuilt versions between old and new (included).

    Args:
      old_version: Old lacros version
      new_version: New lacros version
      lacros_folder_name: either 'lacros-64' or 'lacros-arm32' depending on DUT
        architecture

    Returns:
      A list of lacros versions. (list[str]).
    """
    old_milestone = int(cr_util.extract_milestone_from_version(old_version))
    new_milestone = int(cr_util.extract_milestone_from_version(new_version))

    lacros_version_list = _get_all_lacros_prebuilt_between_two_milestone(
        old_milestone, new_milestone, lacros_folder_name
    )

    filtered_lacros_versions: list[str] = []
    for lacros_version in lacros_version_list:
        assert cr_util.is_chrome_version(lacros_version)
        if (
            lacros_version != new_version
            and not cr_util.is_direct_relative_version(
                lacros_version, new_version
            )
        ):
            continue
        if not (
            cr_util.is_version_lesseq(old_version, lacros_version)
            and cr_util.is_version_lesseq(lacros_version, new_version)
        ):
            continue
        filtered_lacros_versions.append(lacros_version)
    filtered_lacros_versions.sort(key=util.version_key_func)
    return filtered_lacros_versions


def get_lca_lacros_prebuilt(
    old_version: str, new_version: str, lacros_folder_name: str
) -> str:
    """Get the lowest common ancestor of 2 lacros prebuilt version

    Args:
      old_version: Old lacros version
      new_version: New lacros version
      lacros_folder_name: either 'lacros-64' or 'lacros-arm32' depending on DUT
        architecture.
    """
    old_milestone = int(cr_util.extract_milestone_from_version(old_version))
    new_milestone = int(cr_util.extract_milestone_from_version(new_version))
    version_list_lacros = _get_all_lacros_prebuilt_between_two_milestone(
        old_milestone, new_milestone, lacros_folder_name
    )
    return cr_util.get_lca(old_version, new_version, version_list_lacros)


def is_prebuilt_path_available(lacros_bucket_url: str) -> bool:
    """Check if a lacros prebuilt image is available

    Args:
      lacros_bucket_url: lacros prebuilt image bucket url

    Retunrs:
      True if the lacros version is available to fetch.
      False if not available to fetch.
    """
    gsutil_ls_result = gs_util.ls(lacros_bucket_url, ignore_errors=True)
    return (
        len(gsutil_ls_result) == 1 and gsutil_ls_result[0] == lacros_bucket_url
    )


def is_prebuilt_version_available(
    lacros_version: str, board_cpu_arch: str
) -> bool:
    """Check if a lacros version is available to fetch for a specific dut

    Args:
      lacros_version: lacros prebuilt version to fetch
      board_cpu_arch: CPU architecture of the board

    Retunrs:
      True if the lacros version is available to fetch.
      False if not available to fetch.
    """
    lacros_folder_name = get_lacros_folder_name(board_cpu_arch)
    lacros_bucket_url = LACROS_ZIP_PATH.format(
        version=lacros_version, lacros_folder_name=lacros_folder_name
    )
    return is_prebuilt_path_available(lacros_bucket_url)


def get_lacros_folder_name(board_cpu_arch: str) -> str:
    """Get a lacros prebuilt to determine the folder name in the GS bucket.

    Currently there are 2 folder names: 'lacros-64' and 'lacros-arm32' depending
      on architecture.

    Args:
      board_cpu_arch: CPU architecture of the board

    Returns:
      the appropriate folder name
    """
    if BOARD_ARCHITECTURES.get(board_cpu_arch) is not None:
        return BOARD_ARCHITECTURES[board_cpu_arch]['lacros_dir']

    raise errors.InternalError('Unknown board architecture %s' % board_cpu_arch)


def _get_device_architecture_arg_for_gn(board_cpu_arch: str) -> str:
    """Get the device architecture argument for args.gn file

    Currently there are 2 supported acchitecture: 'amd64' and 'arm' depending
      on architecture.

    Args:
      board_cpu_arch: CPU architecture of the board

    Returns:
      the appropriate architecture name
    """
    if BOARD_ARCHITECTURES.get(board_cpu_arch) is not None:
        return BOARD_ARCHITECTURES[board_cpu_arch]['arg_for_gn']

    raise errors.InternalError('Unknown board architecture %s' % board_cpu_arch)


def fetch_lacros_prebuilt(lacros_version: str, board_cpu_arch: str) -> str:
    """Fetch and unzip a specific lacros version.

    Args:
      lacros_version: lacros prebuilt version to fetch
      board_cpu_arch: CPU architecture of the board

    Returns:
      A temp directory where the lacros prebuilt file is unzipped
    """

    try:
        lacros_folder_name = get_lacros_folder_name(board_cpu_arch)
        lacros_bucket_url = LACROS_ZIP_PATH.format(
            version=lacros_version, lacros_folder_name=lacros_folder_name
        )
        assert is_prebuilt_path_available(lacros_bucket_url)

        # Creates a temp directory with a prefix bisect_tmp
        temp_dir = tempfile.mkdtemp(prefix='bisect_tmp')
        logger.debug('Temp directory for lacros prebuilt: %s', temp_dir)

        # Downloading lacros.zip to the temp directory

        gs_util.cp(lacros_bucket_url, temp_dir)

        # unzip lacros.zip
        logger.debug('fetch and unpack lacros.zip to %s', temp_dir)
        lacros_zip_path = os.path.join(temp_dir, LACROS_FILENAME)
        util.check_call('unzip', lacros_zip_path, cwd=temp_dir)

        return temp_dir
    except:
        logger.error('Fetch lacros prebuilt failed')
        raise


def _get_deploy_chrome_path(
    chrome_src: str | None = None, chromeos_root: str | None = None
):
    """Get deploy_chrome script path

    Args:
      chrome_src: Chrome source root directory
      chromeos_root: ChromeOS source root directory

    Returns:
      Deploy_chrome script path
    """
    if chromeos_root:
        return os.path.join(chromeos_root, 'chromite/bin/deploy_chrome')
    if chrome_src:
        return os.path.join(
            chrome_src, 'third_party/chromite/bin/deploy_chrome'
        )

    return os.path.join(
        common.DEFAULT_CHROMEOS_ROOT, 'chromite/bin/deploy_chrome'
    )


def deploy_lacros_to_dut(
    lacros_build_dir: str, dut_ip: str, deploy_chrome_script_path: str
) -> int:
    """Deploy a lacros build to a DUT

    Args:
      lacros_build_dir: Lacros build directory
      dut_ip: IP of the DUT
      deploy_chrome_script_path: Deploy_chrome script path

    Returns:
      0 for successful deployment. Raises exception for failures.
    """
    try:
        assert os.path.exists(deploy_chrome_script_path)

        lacros_deploy_command = [
            deploy_chrome_script_path,
            '--build-dir=%s' % lacros_build_dir,
            '--device=%s' % dut_ip,
            '--nostrip',
            '--lacros',
            '--reset-lacros',
            '--force',
        ]
        logger.debug('Lacros deploy command:')
        logger.debug(' '.join(lacros_deploy_command))
        util.check_output(*lacros_deploy_command)
        return 0

    except:
        logger.error('Lacros deploy failed')
        raise


def fetch_and_deploy_lacros_prebuilt_to_dut(
    lacros_version: str, board_cpu_arch: str, dut_ip: str, chromeos_root: str
) -> int:
    """Fetch and deploy a specific lacros prebuilt version to a DUT.

    Args:
      lacros_version: lacros prebuilt version to fetch
      board_cpu_arch, CPU architecture of the board
      dut_ip: IP of the DUT
      chromeos_root: ChromeOS source root directory

    Returns:
      0 for successful deployment. Raises exception for failures.
    """
    assert cr_util.is_chrome_version(lacros_version)
    try:
        lacros_prebuilt_directory = fetch_lacros_prebuilt(
            lacros_version, board_cpu_arch
        )

        deploy_chrome_path = _get_deploy_chrome_path(
            chromeos_root=chromeos_root
        )
        deploy_lacros_to_dut(
            lacros_prebuilt_directory, dut_ip, deploy_chrome_path
        )

        return 0
    except:
        logger.error('Lacros prebuilt deploy failed')
        raise
    finally:
        logger.debug(
            'clean up lacros prebuilt directory %s', lacros_prebuilt_directory
        )
        shutil.rmtree(lacros_prebuilt_directory, True)


def _get_args_gn_config(device_architecture: str) -> str:
    """Get GN arguments for lacros build

    Args:
      device_architecture: The device architecture for lacros
      build ("amd64" or "arm")

    Returns:
      A multi line config which can be written to args.gn file
    """
    args_gn_config = """
import("//build/args/chromeos/%s-generic-crostoolchain.gni")
target_os="chromeos"
is_chromeos_device=true
chromeos_is_browser_only=true
use_remoteexec = true
is_chrome_branded=true
is_official_build=false
is_debug=false
use_thin_lto = false
is_cfi = false
is_component_build = false
""" % (
        device_architecture
    )
    return args_gn_config


def _generate_gn_arguments(
    chrome_src: str, board_cpu_arch: str, output_directory: str
):
    """Generate GN arguments for lacros build

    Args:
      chrome_src: Chrome source root directory
      board_cpu_arch: CPU architecture of the board
      output_directory: The directory where the arg.gn file will be created
    """
    device_architecture_arg_for_gn = _get_device_architecture_arg_for_gn(
        board_cpu_arch
    )

    logger.debug(
        'Generating lacros gn arguments for %s', device_architecture_arg_for_gn
    )

    args_gn_path = os.path.join(output_directory, 'args.gn')
    with open(args_gn_path, 'w') as f:
        f.write(_get_args_gn_config(device_architecture_arg_for_gn))

    gn_gen_command = ['gn', 'gen', output_directory]
    util.check_output(*gn_gen_command, cwd=chrome_src)


def _get_output_directory(chrome_src: str) -> str:
    output_directory = os.path.join(chrome_src, _DEFAULT_OUT_DEVICE_LACROS_DIR)
    return output_directory


def build(chrome_src: str, board_cpu_arch: str):
    """Build lacros from chrome source tree

    Args:
      chrome_src: Chrome source root directory
      board_cpu_arch: CPU architecture of the board

    Returns:
      The output directory of lacros build
    """
    output_directory = _get_output_directory(chrome_src)
    pathlib.Path(output_directory).mkdir(parents=True, exist_ok=True)
    _generate_gn_arguments(chrome_src, board_cpu_arch, output_directory)

    logger.debug('Building lacros from source')
    with locking.lock_file(locking.LOCK_FILE_FOR_BUILD):
        lacros_build_cmd = [
            'autoninja',
            '-C',
            _DEFAULT_OUT_DEVICE_LACROS_DIR,
            'chrome',
        ]
        logger.debug('Lacros Build Command: %s', ' '.join(lacros_build_cmd))
        env = cr_util.get_RBE_environment_variables()
        try:
            util.check_output(*lacros_build_cmd, cwd=chrome_src, env=env)
        except subprocess.CalledProcessError:
            logger.warning('build failed, delete out directory and retry again')
            # Some compiler processes are still terminating. Short delay is
            # necessary.
            time.sleep(10)
            shutil.rmtree(output_directory)
            util.check_output(*lacros_build_cmd, cwd=chrome_src, env=env)
        return


def deploy(chrome_src: str, dut_ip: str):
    """Deploy lacros

    Args:
      chrome_src: Chrome source root directory
      dut_ip: IP of the DUT
    """
    output_directory = _get_output_directory(chrome_src)
    deploy_chrome_script_path = _get_deploy_chrome_path(chrome_src=chrome_src)
    deploy_lacros_to_dut(output_directory, dut_ip, deploy_chrome_script_path)
