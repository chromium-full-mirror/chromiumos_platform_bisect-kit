# Copyright 2017 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Common functions used by almost of all bisect kit scripts."""

import logging
import logging.config
import os
import pathlib
import sys
import typing


logger = logging.getLogger(__name__)

DEFAULT_SESSION_BASE = 'bisect.sessions'
DEFAULT_SESSION_NAME = 'default'
DEFAULT_LOG_FILENAME = 'bisect-kit.log'
DEFAULT_MIRROR_BASE = str(pathlib.Path('~/git-mirrors').expanduser())
DEFAULT_WORK_BASE = str(pathlib.Path('~/bisect-workdir').expanduser())
DEFAULT_CHROMEOS_ROOT = str(pathlib.Path('~/chromiumos').expanduser())


def determine_session_dir(
    session_name: str | None = None,
    session_base: str | None = None,
) -> pathlib.Path:
    if not session_name:
        session_name = DEFAULT_SESSION_NAME
    if not session_base:
        session_base = DEFAULT_SESSION_BASE
    return pathlib.Path(session_base) / session_name


def config_logging(opts):
    """Config logging handlers.

    bisect-kit will write full DEBUG level messages to log file and INFO level
    messages to console.

    If command line argument --session is specified, the log file is stored
    inside session directory; otherwise, stored in current working directory.

    Args:
      opts: An argparse.Namespace to hold command line arguments.
    """
    if getattr(config_logging, 'configured', False):
        return
    config_logging.configured = True

    log_file: pathlib.Path
    if opts.log_file is not None:
        log_file = pathlib.Path(opts.log_file)
    elif getattr(opts, 'session', None):
        log_file = determine_session_dir(opts.session) / DEFAULT_LOG_FILENAME
    else:
        log_file = pathlib.Path(DEFAULT_LOG_FILENAME)
    log_file.parent.mkdir(parents=True, exist_ok=True)

    print(f'log file = {log_file}', file=sys.stderr)
    os.environ['LOG_FILE'] = str(log_file)
    opts.log_file = str(log_file)

    if getattr(opts, 'debug'):
        os.environ['BISECT_KIT_CONSOLE_LOG_LEVEL'] = 'DEBUG'
    # By default, top level process, i.e. bisectors, output INFO level
    # messages to console.
    # Child processes, i.e. switcher and evaluator, output less verbosely ---
    # only warnings and errors to console.
    console_level = os.environ.get('BISECT_KIT_CONSOLE_LOG_LEVEL', 'INFO')
    os.environ['BISECT_KIT_CONSOLE_LOG_LEVEL'] = 'WARNING'

    file_handler: dict[str, typing.Any] = {
        'class': 'logging.FileHandler',
        'level': 'DEBUG',
        'formatter': 'verbose',
        'filename': opts.log_file,
    }
    # Rotates default log file to prevent it grows unlimitedly.
    if opts.log_file == DEFAULT_LOG_FILENAME:
        file_handler.update(
            {
                'class': 'logging.handlers.TimedRotatingFileHandler',
                'when': 'd',
                'backupCount': 2,
            }
        )

    logging.config.dictConfig(
        {
            'version': 1,
            'disable_existing_loggers': False,
            'formatters': {
                'verbose': {
                    'format': '%(asctime)s %(module)s pid=%(process)d %(levelname)s '
                    '%(message)s'
                },
                'simple': {
                    'format': '%(asctime)s %(levelname)s %(message)s',
                    'datefmt': '%H:%M:%S',
                },
            },
            'handlers': {
                'console': {
                    'level': console_level,
                    'class': 'logging.StreamHandler',
                    'formatter': 'simple',
                },
                'file-log': file_handler,
            },
            'root': {
                'handlers': ['file-log', 'console'],
                'level': 'DEBUG',
            },
        }
    )


def under_luci_context():
    """Returns True if current process is under a luci auth context."""
    return bool(os.getenv('LUCI_CONTEXT'))
