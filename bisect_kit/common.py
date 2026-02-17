# Copyright 2017 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Common functions used by almost of all bisect kit scripts."""

import collections.abc
import logging
import logging.config
import os
import pathlib
import sys
import typing
import uuid


logger = logging.getLogger(__name__)

PathLike = os.PathLike | str

DEFAULT_SESSION_NAME = 'default'
DEFAULT_TEMPLATE_NAME = 'template'
DEFAULT_WORK_BASE = str(pathlib.Path('~/bisect-workdir').expanduser())
DEFAULT_MIRROR_BASE = str(pathlib.Path('~/git-mirrors-ext4').expanduser())

DEFAULT_EXT4_CHROME_WORK_BASE = str(
    pathlib.Path('~/chrome-workdir').expanduser()
)

DEFAULT_LOG_BASE = 'bisect.sessions'
DEFAULT_LOG_FILENAME = 'bisect-kit.log'

DEFAULT_CACHE_DIR = 'cache'

_DEFAULT_CHROMEOS_ROOT = str(pathlib.Path('~/chromiumos').expanduser())


class WorkBasePathFactory:
    """Factory for generating session paths."""

    def __init__(self, work_base: str | None = None):
        if not work_base:
            work_base = DEFAULT_WORK_BASE
        self._work_base = pathlib.Path(work_base).absolute()

    @property
    def work_base(self) -> str:
        return str(self._work_base)

    def iter_session_names(
        self,
        exclude_non_uuid: bool = True,
        exclude_template: bool = True,
    ) -> collections.abc.Iterator[str]:
        for path in self._work_base.iterdir():
            if not path.is_dir():
                continue
            if is_valid_uuid(str(path.name)):
                yield path.name
                continue
            if exclude_non_uuid:
                continue
            if exclude_template and path.name == DEFAULT_TEMPLATE_NAME:
                continue
            yield path.name

    def get_session_workdir(self, session_name: str | None) -> str:
        if not session_name:
            session_name = DEFAULT_SESSION_NAME
        return str(self._work_base / session_name)


class ProjectPathFactory(WorkBasePathFactory):
    """Factory for generating chromeos/chrome/android source tree paths."""

    CHROMEOS_SRC = 'chromeos'
    CHROME_SRC = 'chrome'
    ANDROID_SRC_PREFIX = 'android.'

    def __init__(
        self,
        session_name: str | None = None,
        work_base: PathLike | None = None,
        mirror_base: PathLike | None = None,
        chrome_work_base: PathLike | None = None,
    ):
        super().__init__(work_base=work_base)

        if not session_name:
            session_name = DEFAULT_SESSION_NAME
        self._session_name = session_name
        self._session_workdir = self._work_base / self._session_name

        if not mirror_base:
            mirror_base = DEFAULT_MIRROR_BASE
        self._mirror_base = pathlib.Path(mirror_base).absolute()

        if not chrome_work_base:
            chrome_work_base = self._work_base
        self._chrome_workdir = (
            pathlib.Path(chrome_work_base).absolute() / self._session_name
        )

    @property
    def session_name(self) -> str:
        return self._session_name

    @property
    def session_workdir(self) -> str:
        return str(self._session_workdir)

    @property
    def mirror_base(self) -> str:
        return str(self._mirror_base)

    def get_session_file(self, file_path: PathLike) -> str:
        return str(self._session_workdir / file_path)

    def get_chromeos_mirror(self) -> str:
        return str(self._mirror_base / self.CHROMEOS_SRC)

    def get_chromeos_tree(self) -> str:
        return str(self._session_workdir / self.CHROMEOS_SRC)

    def get_android_mirror(self, branch: str) -> str:
        return str(self._mirror_base / f'{self.ANDROID_SRC_PREFIX}{branch}')

    def get_android_tree(self, branch: str) -> str:
        return str(self._session_workdir / f'{self.ANDROID_SRC_PREFIX}{branch}')

    def iter_android_branches(self) -> collections.abc.Iterator[str]:
        for path in self._mirror_base.iterdir():
            if path.name.startswith(self.ANDROID_SRC_PREFIX):
                yield path.name.removeprefix(self.ANDROID_SRC_PREFIX)

    def get_chrome_cache(self) -> str:
        return str(self._mirror_base / self.CHROME_SRC)

    def get_chrome_workdir(self) -> str:
        return str(self._chrome_workdir)

    def get_chrome_tree(self) -> str:
        return str(self._chrome_workdir / self.CHROME_SRC)


class TemplatePathFactory(ProjectPathFactory):
    """Factory for generating template paths."""

    def __init__(
        self,
        work_base: PathLike | None = None,
        mirror_base: PathLike | None = None,
    ):
        super().__init__(DEFAULT_TEMPLATE_NAME, work_base, mirror_base)


def get_default_chromeos_root() -> str:
    return os.environ.get('DEFAULT_CHROMEOS_ROOT', _DEFAULT_CHROMEOS_ROOT)


def get_session_log_path(
    session_name: str | None = None, file_path: PathLike = ''
) -> str:
    path_factory = ProjectPathFactory(session_name, DEFAULT_LOG_BASE)
    return path_factory.get_session_file(file_path)


def get_session_cache_dir(
    session_name: str | None = None,
) -> str:
    return get_session_log_path(session_name, DEFAULT_CACHE_DIR)


def check_dir_existence(path: PathLike, raise_error=True) -> bool:
    path = pathlib.Path(path).expanduser().absolute()
    if not path.exists():
        if raise_error:
            raise FileNotFoundError(path)
        return False
    if not path.is_dir():
        if raise_error:
            raise NotADirectoryError(path)
        return False
    return True


def is_valid_uuid(uuid_str: str) -> bool:
    try:
        uuid_obj = uuid.UUID(uuid_str, version=4)
        return str(uuid_obj) == uuid_str
    except ValueError:
        return False


# Global variable to store the logging initialization state
_logging_configured = False


def config_logging(opts):
    """Config logging handlers.

    bisect-kit will write full DEBUG level messages to log file and INFO level
    messages to console.

    If command line argument --session is specified, the log file is stored
    inside session directory; otherwise, stored in current working directory.

    Args:
      opts: An argparse.Namespace to hold command line arguments.
    """
    global _logging_configured  # pylint: disable=global-statement
    if _logging_configured:
        return
    _logging_configured = True

    log_file: str
    if opts.log_file is not None:
        log_file = opts.log_file
    elif getattr(opts, 'session', None):
        log_file = get_session_log_path(opts.session, DEFAULT_LOG_FILENAME)
    else:
        log_file = DEFAULT_LOG_FILENAME

    pathlib.Path(log_file).parent.mkdir(parents=True, exist_ok=True)

    print(f'log file = {log_file}', file=sys.stderr)
    os.environ['LOG_FILE'] = log_file
    opts.log_file = log_file

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
