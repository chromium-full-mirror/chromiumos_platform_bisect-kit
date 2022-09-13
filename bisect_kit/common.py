# -*- coding: utf-8 -*-
# Copyright 2017 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Common functions used by almost of all bisect kit scripts."""

from __future__ import print_function
import argparse
import logging
import logging.config
import os
import sys

from bisect_kit import configure

logger = logging.getLogger(__name__)

DEFAULT_SESSION_BASE = 'bisect.sessions'
DEFAULT_SESSION_NAME = 'default'
DEFAULT_LOG_FILENAME = 'bisect-kit.log'

BISECT_KIT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

common_argument_parser = None
session_optional_parser = None
session_required_parser = None


def determine_session_dir(session):
  if not session:
    session = DEFAULT_SESSION_NAME
  session_dir = os.path.join(DEFAULT_SESSION_BASE, session)
  return session_dir


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
      session_dir = determine_session_dir(opts.session)
      opts.log_file = os.path.join(session_dir, DEFAULT_LOG_FILENAME)
      if not os.path.exists(session_dir):
        os.makedirs(session_dir)
    else:
      opts.log_file = DEFAULT_LOG_FILENAME
  os.environ['LOG_FILE'] = opts.log_file
  if os.path.dirname(opts.log_file):
    os.makedirs(os.path.dirname(opts.log_file), exist_ok=True)
  print('log file = %s' % opts.log_file, file=sys.stderr)

  if getattr(opts, 'debug'):
    os.environ['BISECT_KIT_CONSOLE_LOG_LEVEL'] = 'DEBUG'
  # By default, top level process, i.e. bisectors, output INFO level
  # messages to console.
  # Child processes, i.e. switcher and evaluator, output less verbosely ---
  # only warnings and errors to console.
  console_level = os.environ.get('BISECT_KIT_CONSOLE_LOG_LEVEL', 'INFO')
  os.environ['BISECT_KIT_CONSOLE_LOG_LEVEL'] = 'WARNING'

  file_handler = {
      'class': 'logging.FileHandler',
      'level': 'DEBUG',
      'formatter': 'verbose',
      'filename': opts.log_file,
  }
  # Rotates default log file to prevent it grows unlimitedly.
  if opts.log_file == DEFAULT_LOG_FILENAME:
    file_handler.update({
        'class': 'logging.handlers.TimedRotatingFileHandler',
        'when': 'd',
        'backupCount': 2,
    })

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
  })


def _setup_argument_parsers():
  # pylint: disable=global-statement
  global common_argument_parser
  global session_optional_parser
  global session_required_parser
  if common_argument_parser:
    return  # already done
  common_argument_parser = argparse.ArgumentParser(add_help=False)
  session_optional_parser = argparse.ArgumentParser(add_help=False)
  session_required_parser = argparse.ArgumentParser(add_help=False)
  common_argument_parser.add_argument(
      '--log_file',
      metavar='LOG_FILE',
      default=configure.get('LOG_FILE'),
      help='Override log filename')
  common_argument_parser.add_argument(
      '--debug', action='store_true', help='Output DEBUG log to console')
  common_argument_parser.add_argument(
      '--rc',
      metavar=configure.CONFIG_ENV_NAME,
      help='Specify config file to use. Otherwise searches default '
      'locations. Use "none" to suppress config file searching.')

  session_optional_parser.add_argument(
      '--session',
      default=DEFAULT_SESSION_NAME,
      help='Session name (default: %(default)r)')

  session_required_parser.add_argument(
      '--session', required=True, help='Session name')


def under_luci_context():
  """Returns True if current process is under a luci auth context."""
  return bool(os.getenv('LUCI_CONTEXT'))


def init():
  configure.load_config()
  configure.root.load_plugins()
  _setup_argument_parsers()
