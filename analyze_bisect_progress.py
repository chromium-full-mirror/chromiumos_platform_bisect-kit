#!/usr/bin/env python3
# Copyright 2024 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Analyze advanced bisection statistics.

- Generate view.json at each 'failed' event and count the reason/number of
  retries.

  $ export SKYLAB_CLOUD_SERVICE_ACCOUNT_JSON=<PATH_TO_PRODUCT_BISECT_RUNNER.JSON>

  $ ./analyze_bisect_progress.py --workdir=<WORK_DIR> gen --bisect_id <BISECT_ID>
    or
  $ ./analyze_bisect_progress.py --workdir=<WORK_DIR> gen --after <DATE_TIME> [--before <BEFORE_TIME>]

  For each bisection, it would generate view_<timestamp>.json at each failed
  timestamp and an index file "progress.json" in <WORK_DIR>/<BISECT_ID>.
  Also it would generate a "retries.json" for the reason/number of retries.

- Get the counter of total retries.

  $ ./analyze_bisect_progress.py --workdir=<WORK_DIR> count_retries

  It would aggregate "retries.json" in all sub directories under <WORK_DIR>.
  It should be run after the sub command "gen".

- Geterate the DUT waiting time.

  $ export SKYLAB_CLOUD_SERVICE_ACCOUNT_JSON=<PATH_TO_PRODUCT_BISECT_RUNNER.JSON>

  $ ./analyze_bisect_progress.py --workdir=<WORK_DIR> gen_waiting_time --bisect_id <BISECT_ID>
    or
  $ ./analyze_bisect_progress.py --workdir=<WORK_DIR> gen_waiting_time --after <DATE_TIME> [--before <BEFORE_TIME>]

  For each bisection , it would generate "waiting_time.json" under <WORK_DIR>/<BISECT_ID>.
  - If the bisect is unable to complete due to DUT leasing issue (reach retry
    limit or timeout), output "failed".
  - If the bisect is failed due to reasons other than DUT leasing issues while
    waiting for a DUT, output "unknown" because the waiting time data is incomplete.
  - If the bisect is completed (either success or failed due to reason other

- Get the total waiting time.

  $ ./analyze_bisect_progress.py --workdir=<WORK_DIR> count_waiting_time

  It would aggregate "waiting_time.json" in all sub directories under <WORK_DIR>.
  It should be run after the sub command "gen_waiting_time".
"""

from __future__ import annotations

import argparse
import collections
import datetime
import json
import logging
import os
import re
import subprocess
import sys

from bisect_kit import bisect_db
from bisect_kit import common
from bisect_kit import gs_util
from google.cloud import datastore


logger = logging.getLogger(__name__)


_GS_DIR_PATTTERN = r'gs://crosperf/b/{bisect_id}'

_GS_FILE_PATTERN = r'gs://crosperf/b/[^/]+/([^/]+)'

_DOMAIN_SESSION_FILE_SUFFIX = 'Domain'

_MAIN_SESSION_FILE_SUFFIX = 'CommandLine'

_SUFFIXES_OF_INTERESTS = [
    _MAIN_SESSION_FILE_SUFFIX,
    _DOMAIN_SESSION_FILE_SUFFIX,
]


_MAIN_SCRIPT_MAP = {
    'DiagnoseAutotestCommandLine': 'diagnose_cros_autotest.py',
    'DiagnoseTastCommandLine': 'diagnose_cros_tast.py',
    'DiagnoseCustomEvalCommandLine': 'diagnose_cros_custom_eval.py',
}


def output_view_json(
    records: dict[str, str],
    bisect_id: str,
    main_script: str,
    bisect_dir: str,
    timestamp: float | None = None,
):
    """Generate a view.json for a bisect at some timestamp.

    Args:
        records: A list of dict. The last dict should be filled with {'view_json': <output file>}
        bisect_id: the bisect ID
        main_script: the main diagnose script to execute to generate view.json
        bisect_dir: the output bisect dir
        timestamp: the timestamp or None
    """
    logger.info(
        'generating view.json for %s at timestamp %s', bisect_id, timestamp
    )
    try:
        cmd = [
            './%s' % main_script,
            'view',
            '--log-base-dir',
            os.path.dirname(bisect_dir),
            '--session',
            bisect_id,
            '--json',
        ]
        if timestamp is not None:
            cmd.extend(
                [
                    '--timestamp',
                    str(timestamp),
                ]
            )
        # pylint: disable=subprocess-run-check
        # The return code is checked manually below.
        # check=True would raise an exception immediately so stderr has no
        # chance to output.
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
        )
    except Exception as e:
        logger.error(
            'unable to execute %s for %s: %s', main_script, bisect_id, e
        )
        return

    if result.returncode != 0:
        logger.error(
            'failed to generate view.json with returncode %s', result.returncode
        )
        if result.stderr:
            logger.error('stderr: %s', result.stderr)
        return

    # To save disk size, do not write a new file if the view.json is the same as
    # in the previous timestamp.
    should_write = True
    if len(records) >= 2:
        previous_output_file = records[-2]['view_json']
        with open(os.path.join(bisect_dir, previous_output_file)) as f:
            previous_content = f.read()
        if previous_content == result.stdout:
            should_write = False
            logger.info(
                'skip writing file at timestamp %s because it is the same as %s',
                timestamp,
                previous_output_file,
            )
            records[-1]['view_json'] = previous_output_file

    if should_write:
        output_file = 'view_%s.json' % (timestamp or 'final')
        with open(os.path.join(bisect_dir, output_file), 'w') as f:
            f.write(result.stdout)
        records[-1]['view_json'] = output_file
        logger.info('%s generated successfully', output_file)


def download_session_from_gs(
    bisect_id: str, bisect_dir: str, suffix_of_interests: list[str]
):
    """Download session files from gs bucket to local directory.

    Args:
        bisect_id: bisect ID.
        bisect_dir: local directory path.
        suffix_of_interests: a list of suffix to download.
    """
    try:
        gs_paths = [
            line.strip()
            for line in gs_util.ls(_GS_DIR_PATTTERN.format(bisect_id=bisect_id))
        ]
    except Exception as e:
        logger.error('failed to list gsbucket files of %s: %s', bisect_id, e)
        return

    for gs_path in gs_paths:
        matched = re.fullmatch(_GS_FILE_PATTERN, gs_path)
        if not matched:
            continue
        file_name = matched.group(1)
        logger.debug('matched file %s', file_name)
        for suffix in suffix_of_interests:
            if file_name.endswith(suffix):
                file_path = os.path.join(bisect_dir, file_name)
                logger.info('downloading %s to %s', gs_path, file_path)
                try:
                    gs_util.cp(gs_path, file_path)
                except Exception as e:
                    logger.error('failed to download %s: %s', gs_path, e)
                break


def gen_view(
    bisect_id: str,
    out_dir: str,
    regenerate: bool = False,
    download: bool = True,
) -> None:
    """Generates view.json for a single bisect.

    Args:
        bisect_id: bisect ID.
        out_dir: local dir name.
        regenerate: whether to analyze it again if the bisect directory already exists.
        downoload: whether to download session files from gsbucket. If False, assume the files are downloaded before.

    Returns:
        None
    """
    logger.info('processing %s', bisect_id)

    bisect_dir = os.path.join(out_dir, bisect_id)
    try:
        os.makedirs(bisect_dir, exist_ok=False)
    except FileExistsError as e:
        logger.warning('directory %s already exists: %s', bisect_dir, e)
        if not regenerate:
            logger.info('skip %s since the directory already exists', bisect_id)
            return
        logger.info('re-generate %s', bisect_id)
    except Exception as e:
        logger.error('failed to create directory %s: %s', bisect_dir, e)
        return

    if download:
        download_session_from_gs(
            bisect_id,
            bisect_dir,
            [
                _MAIN_SESSION_FILE_SUFFIX,
                _DOMAIN_SESSION_FILE_SUFFIX,
            ],
        )

    main_session_file = None
    for file_name in os.listdir(bisect_dir):
        if file_name.endswith(_MAIN_SESSION_FILE_SUFFIX):
            main_session_file = file_name

    logger.info('main session file for %s: %s', bisect_id, main_session_file)
    if not main_session_file:
        logger.error('Can not find main session file for %s, abort', bisect_id)
        return

    main_script = _MAIN_SCRIPT_MAP.get(main_session_file)
    logger.info('main script for %s: %s', bisect_id, main_script)
    if not main_script:
        logger.error(
            'Can not find corresponding main script name for file %s',
            main_session_file,
        )
        return

    with open(os.path.join(bisect_dir, main_session_file)) as f:
        data = json.load(f)

    retries: collections.Counter[str] = collections.Counter()
    records = []
    previous_entry = None
    for entry in data.get('history'):
        event = entry.get('event')
        if event == 'failed':
            timestamp = entry.get('timestamp')
            records.append(entry.copy())
            output_view_json(
                records, bisect_id, main_script, bisect_dir, timestamp
            )
        elif event == 'start':
            if (
                previous_entry
                and previous_entry.get('error_type') == 'Retriable'
            ):
                retries[previous_entry.get('exception')] += 1

        previous_entry = entry

    records.append({})
    output_view_json(records, bisect_id, main_script, bisect_dir)

    with open(os.path.join(bisect_dir, 'progress.json'), 'w') as f:
        json.dump(records, f, indent=4)

    with open(os.path.join(bisect_dir, 'retries.json'), 'w') as f:
        json.dump(retries, f, indent=4)


def cmd_count_retries(opts):
    total_retries: collections.Counter[str] = collections.Counter()
    count = 0
    for dir_name in os.listdir(opts.work_dir):
        if not os.path.isdir(os.path.join(opts.work_dir, dir_name)):
            continue
        retries_file = os.path.join(opts.work_dir, dir_name, 'retries.json')
        try:
            with open(retries_file) as f:
                data = json.load(f)
        except Exception as e:
            logger.error('failed to load %s: %s', retries_file, e)
            continue
        count += 1
        total_retries += collections.Counter(data)
    logger.info('total %s bisections', count)
    with open(os.path.join(opts.work_dir, 'total_retires.json'), 'w') as f:
        json.dump(total_retries, f, indent=4)
    logger.info('%s total retries: %s', total_retries.total(), total_retries)


def cmd_gen_view(opts):
    if opts.after:
        if not opts.before:
            opts.before = datetime.datetime.now()
        logger.info('query bisections in [%s, %s)', opts.after, opts.before)
        ds_client = bisect_db.Client()
        bisect_ids = [
            entity.key.id_or_name
            for entity in ds_client.query_finished_bisects(
                opts.after, opts.before
            )
        ]
        num_bisects = len(bisect_ids)
        logger.info('total %s bisections found', num_bisects)
        for i, bisect_id in enumerate(bisect_ids):
            logger.info('processing %s of %s', i + 1, num_bisects)
            gen_view(
                bisect_id,
                opts.work_dir,
                opts.regenerate,
                opts.download,
            )
    elif opts.bisect_id:
        gen_view(
            opts.bisect_id,
            opts.work_dir,
            opts.regenerate,
            opts.download,
        )


def cmd_gen_waiting_time(opts):
    ds_client = bisect_db.Client()
    if opts.after:
        if not opts.before:
            opts.before = datetime.datetime.now()
        logger.info('query bisections in [%s, %s)', opts.after, opts.before)
        ds_client = bisect_db.Client()
        bisects = [
            entity
            for entity in ds_client.query_finished_bisects(
                opts.after,
                opts.before,
            )
            if 'stateless'
            in entity.get(
                'Experiments', []
            )  # and 'shared_dut_pool' in eneity.get('Experiments', [])
        ]
        num_bisects = len(bisects)
        logger.info('total %s bisections found', num_bisects)
        for i, bisect in enumerate(bisects):
            logger.info('procesing %s of %s', i + 1, num_bisects)
            gen_waiting_time(
                bisect.key.id_or_name,
                bisect,
                opts.work_dir,
                opts.regenerate,
                opts.download,
            )
    elif opts.bisect_id:
        entity = ds_client.get_bisect(opts.bisect_id)
        gen_waiting_time(
            opts.bisect_id,
            entity,
            opts.work_dir,
            opts.regenerate,
            opts.download,
        )


_DUT_FAILURES_MESSAGES = [
    'cannot allocate a lab DUT more than 30 times',
    'reached retry limit of 30 times',
    'Bisection exceeds timeout',
]


def is_dut_failure(message: str) -> bool:
    for dut_failure_message in _DUT_FAILURES_MESSAGES:
        if dut_failure_message in message:
            return True
    return False


def gen_waiting_time(
    bisect_id: str,
    entity: datastore.Entity,
    out_dir: str,
    regenerate: bool = False,
    download: bool = True,
):
    """Generates waiting_time.json for a single bisection.

    Args:
        bisect_id: the bisect ID.
        entity: a bisect entity from the Datastore.
        out_dir: the output directory.
        regenerate: whether to regenerate if the directory already exists.
        downoload: whether to download session files from gsbucket. If False, assume the files are downloaded before.
    """
    logger.info('processing %s', bisect_id)

    bisect_dir = os.path.join(out_dir, bisect_id)
    try:
        os.makedirs(bisect_dir, exist_ok=False)
    except FileExistsError as e:
        logger.warning('directory %s already exists: %s', bisect_dir, e)
        if not regenerate:
            logger.info('skip %s since the directory already exists', bisect_id)
            return
        logger.info('re-generate %s', bisect_id)
    except Exception as e:
        logger.error('failed to create directory %s: %s', bisect_dir, e)
        return

    if download:
        download_session_from_gs(
            bisect_id,
            bisect_dir,
            [
                _MAIN_SESSION_FILE_SUFFIX,
            ],
        )

    main_session_file = None
    for file_name in os.listdir(bisect_dir):
        if file_name.endswith(_MAIN_SESSION_FILE_SUFFIX):
            main_session_file = file_name

    logger.info('main session file for %s: %s', bisect_id, main_session_file)
    if not main_session_file:
        logger.error('Can not find main session file for %s, abort', bisect_id)
        return

    try:
        with open(os.path.join(bisect_dir, main_session_file)) as f:
            data = json.load(f)
    except Exception as e:
        logger.error('failed to load %s: %s', main_session_file, e)

    dut_leases = []
    dut_lease_file_path = data.get('statistics', {}).get('dut_leases_log_path')
    if not dut_lease_file_path:
        logger.info('No DUT lease log file')
    else:
        dut_lease_file_name = os.path.basename(dut_lease_file_path)
        dut_lease_local_path = os.path.join(bisect_dir, dut_lease_file_name)

        if download:
            gs_path = os.path.join(
                _GS_DIR_PATTTERN.format(bisect_id=bisect_id),
                'log',
                dut_lease_file_name,
            )
            try:
                gs_util.cp(gs_path, dut_lease_local_path)
            except Exception as e:
                logger.error('failed to download %s: %s', gs_path, e)
                return

        try:
            with open(dut_lease_local_path) as f:
                dut_leases = json.load(f)
        except Exception as e:
            logger.error('failed to load %s: %s', dut_lease_local_path, e)
            return

    fail_timestamps = []
    for entry in data.get('history'):
        event = entry.get('event')
        exception = entry.get('exception')
        if event == 'failed':
            if exception in ['DutLeaseTimeout', 'NoDutAvailable']:
                fail_timestamps.append(entry.get('timestamp'))

    records = []
    waiting_time = 0
    previous_lease = None
    summary = None
    for fail_timestamp in fail_timestamps:
        if previous_lease and fail_timestamp < previous_lease:
            continue
        found = False
        for lease in dut_leases:
            start_timestamp = lease.get('start_timestamp')
            if start_timestamp > fail_timestamp:
                duration = start_timestamp - fail_timestamp
                waiting_time += duration
                records.append(
                    {
                        'fail': fail_timestamp,
                        'next_lease': start_timestamp,
                        'duration': duration,
                    }
                )
                previous_lease = start_timestamp
                found = True
                break
        if not found:
            reason = entity.get('RunInfo.ErrDesc', '')
            if is_dut_failure(reason):
                logger.info('is dut failure: %s %s', bisect_id, reason)
                summary = {
                    'result': 'failed',
                    'reason': reason,
                }
            else:
                # Otherwise, the error is not caused by DUT lease error.
                # We exclude the data point from calculation.
                summary = {
                    'result': 'unknown',
                    'reason': reason,
                }
            break

    if not summary:
        summary = {
            'result': 'success',
            'leases': records,
            'waiting_time': waiting_time,
        }

    with open(os.path.join(bisect_dir, 'waiting_time.json'), 'w') as f:
        json.dump(
            summary,
            f,
            indent=4,
        )
    logger.info('waiting_time.json generated for %s', bisect_id)


def cmd_count_waiting_time(opts):
    count = 0
    failed_count = 0
    success_count = 0
    total_waiting_time = 0.0

    for dir_name in os.listdir(opts.work_dir):
        if not os.path.isdir(os.path.join(opts.work_dir, dir_name)):
            continue
        waiting_time_file = os.path.join(
            opts.work_dir, dir_name, 'waiting_time.json'
        )
        try:
            with open(waiting_time_file) as f:
                data = json.load(f)
        except Exception as e:
            logger.error('failed to load %s: %s', waiting_time_file, e)
            continue
        count += 1
        result = data.get('result')
        if result == 'failed':
            failed_count += 1
        elif result == 'success':
            success_count += 1
            total_waiting_time += float(data.get('waiting_time'))

    logger.info('total %s bisections', count)
    with open(os.path.join(opts.work_dir, 'total_waiting_time.json'), 'w') as f:
        json.dump(
            {
                'success': success_count,
                'failed': failed_count,
                'total_waiting_time': total_waiting_time,
            },
            f,
            indent=4,
        )


def main(args):
    parser = argparse.ArgumentParser(description='analyze bisect progress')
    parser.add_argument(
        '--debug', action='store_true', help='Output DEBUG log to console'
    )
    parser.add_argument(
        '--work_dir',
        required=True,
        help='output directory',
    )
    subparsers = parser.add_subparsers(required=True)

    parser_gen_view = subparsers.add_parser('gen_view')
    parser_gen_view.add_argument('--bisect_id', help='bisection id')
    parser_gen_view.add_argument(
        '--regenerate',
        action=argparse.BooleanOptionalAction,
        default=False,
        help='whether to regenerate if the bisect has been processed',
    )
    parser_gen_view.add_argument(
        '--download',
        action=argparse.BooleanOptionalAction,
        default=True,
        help='whether to download files from gs bucket',
    )
    parser_gen_view.add_argument(
        '--before',
        type=datetime.datetime.fromisoformat,
        help='query bisections < the date',
    )
    parser_gen_view.add_argument(
        '--after',
        type=datetime.datetime.fromisoformat,
        help='query bisections >= the date',
    )
    parser_gen_view.set_defaults(func=cmd_gen_view)

    parser_count_retries = subparsers.add_parser('count_retries')
    parser_count_retries.set_defaults(func=cmd_count_retries)

    parser_gen_waiting_time = subparsers.add_parser('gen_waiting_time')
    parser_gen_waiting_time.add_argument('--bisect_id', help='bisection id')
    parser_gen_waiting_time.add_argument(
        '--regenerate',
        action=argparse.BooleanOptionalAction,
        default=False,
        help='whether to regenerate if the bisect has been processed',
    )
    parser_gen_waiting_time.add_argument(
        '--download',
        action=argparse.BooleanOptionalAction,
        default=True,
        help='whether to download files from gs bucket',
    )
    parser_gen_waiting_time.add_argument(
        '--before',
        type=datetime.datetime.fromisoformat,
        help='query bisections < the date',
    )
    parser_gen_waiting_time.add_argument(
        '--after',
        type=datetime.datetime.fromisoformat,
        help='query bisections >= the date',
    )
    parser_gen_waiting_time.set_defaults(func=cmd_gen_waiting_time)

    parser_count_waiting_time = subparsers.add_parser('count_waiting_time')
    parser_count_waiting_time.set_defaults(func=cmd_count_waiting_time)

    opts = parser.parse_args(args)
    opts.log_file = os.path.join(opts.work_dir, 'log')
    common.config_logging(opts)

    opts.func(opts)


if __name__ == '__main__':
    main(sys.argv[1:])
