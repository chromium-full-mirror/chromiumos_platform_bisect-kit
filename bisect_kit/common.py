# -*- coding: utf-8 -*-
# Copyright 2017 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Common functions used by almost of all bisect kit scripts."""

from __future__ import print_function
import logging
import logging.config
import os

# better_exceptions can print beautiful detail for unhandled exceptions, like
# value of variables on the stack frames. This is helpful for debugging and
# optional.
try:
  # pylint: disable=unused-import
  import better_exceptions
except ImportError:
  pass

from bisect_kit import configure

logger = logging.getLogger(__name__)

DEFAULT_SESSION_BASE = 'bisect.sessions'
DEFAULT_LOG_FILENAME = 'bisect-kit.log'

BISECT_KIT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def config_logging(opts):
  """Config logging handlers.

  bisect-kit will write full DEBUG level messages to log file and INFO level
  messages to console.

  If command line argument --session is specified, the log file is stored
  inside session directory; otherwise, stored in current working directory.

  Args:
    opts: An argparse.Namespace to hold command line arguments.
  """
  if hasattr(config_logging, 'configured'):
    return
  config_logging.configured = True

  if opts.log_file is None:
    if getattr(opts, 'session', None):
      session_base = getattr(opts, 'session_base', DEFAULT_SESSION_BASE)
      session_dir = os.path.join(session_base, opts.session)
      opts.log_file = os.path.join(session_dir, DEFAULT_LOG_FILENAME)
      if not os.path.exists(session_dir):
        os.makedirs(session_dir)
    else:
      opts.log_file = DEFAULT_LOG_FILENAME
  os.environ['LOG_FILE'] = opts.log_file

  root_handlers = ['file-log']
  # When running nested, for example bisector calling runner, disable console
  # output to avoid duplication.
  if not os.environ.get('DISABLE_CONSOLE_OUTPUT'):
    root_handlers.append('console')
    os.environ['DISABLE_CONSOLE_OUTPUT'] = '1'

  logging.config.dictConfig({
      'version': 1,
      'disable_existing_loggers': False,
      'formatters': {
          'verbose': {
              'format': '%(asctime)s %(module)s pid=%(process)d %(levelname)s '
                        '%(message)s'
          },
          'simple': {
              'format': '%(asctime)s %(levelname)s %(message)s',
              'datefmt': '%H:%M:%S'
          },
      },
      'handlers': {
          'console': {
              'level': 'DEBUG' if getattr(opts, 'debug') else 'INFO',
              'class': 'logging.StreamHandler',
              'formatter': 'simple',
          },
          'file-log': {
              'level': 'DEBUG',
              'class': 'logging.handlers.TimedRotatingFileHandler',
              'formatter': 'verbose',
              'filename': opts.log_file,
              'when': 'd',
              'backupCount': 2,
          },
      },
      'root': {
          'handlers': root_handlers,
          'level': 'DEBUG',
      },
  })


def add_common_arguments(parser):
  """Add common arguments to command line parser.

  Args:
    parser: An argparse.ArgumentParser instance.
  """
  parser.add_argument(
      '--log_file',
      metavar='LOG_FILE',
      default=configure.get('LOG_FILE'),
      help='Override log filename')
  parser.add_argument(
      '--debug', action='store_true', help='Output DEBUG log to console')

  parser.add_argument(
      '--rc',
      metavar=configure.CONFIG_ENV_NAME,
      help='Specify config file to use. Otherwise searches default '
      'locations. Use "none" to suppress config file searching.')


def init():
  configure.load_config()
  configure.root.load_plugins()
