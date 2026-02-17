# Copyright 2020 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Cache mechanism to speed up heavy operations."""

import contextlib
import enum
import functools
import hashlib
import json
import logging
import os
import tempfile

from bisect_kit import errors
from bisect_kit import gs_util
from bisect_kit import util


logger = logging.getLogger(__name__)


class Cache:
    """A simple cache decorator with enable/disable control.

    Sample usage:
      @Cache
      def foo(x):
        return ...

      @Cache.default_disabled
      def bar(x):
        return ...

      foo.disable_cache()
      bar.enable_cache()
    """

    def __init__(self, func, enable=True):
        self.wrapped = func
        self.enabled = enable
        self.data = {}
        functools.update_wrapper(self, func)

    @staticmethod
    def default_disabled(func):
        return Cache(func, enable=False)

    def __call__(self, *args):
        if not self.enabled:
            return self.wrapped(*args)

        result = self.data.get(args)
        if result is None:
            result = self.wrapped(*args)
            self.data[args] = result
        return result

    def __name__(self):
        return self.wrapped.__name__

    def enable_cache(self):
        self.enabled = True

    def disable_cache(self):
        self.enabled = False
        self.data = {}


class BuildArtifactsCache:
    """A Cloud Storage based cache for build artifacts."""

    class BuildType(enum.StrEnum):
        """Build types the cache supports."""

        CHROME = 'chrome'

    _BUCKET_SUFFIX = 'builds'

    _GAC_JSON = os.environ.get('SKYLAB_CLOUD_SERVICE_ACCOUNT_JSON')

    _EXCLUDES_PATTERNS_MAP = {
        BuildType.CHROME: [
            'obj',
            'gen',
            'android_clang*',
            '.reproxy_tmp',
        ],
    }

    def _get_bucket_name(self):
        with open(self._GAC_JSON) as f:
            data = json.load(f)
        project_id = data.get('project_id')
        assert project_id
        return '%s-%s-%s' % (
            project_id.removeprefix('google.com:'),
            self._build_type,
            self._BUCKET_SUFFIX,
        )

    def __init__(
        self,
        build_type: 'BuildArtifactsCache.BuildType',
        variant: str,
        rev: str,
        targets: list[str] | None = None,
    ):
        """Initializer.

        Args:
          self: self.
          build_type: the type of the build artifacts (e.g., chrome).
          variant: the variant (e.g., board or cpu architecture).
          rev: the revision.
          targets: the targets (usually binaries) to build.

        The remote path would be like
          gs://<project>-<build_type>-builds/<variant>/<rev>_<hash_of_targets>.tar.xz
        Example:
          gs://crosperf-dev-chrome-builds/grunt/120.0.6064.0_2aff8a6fa2bd9d89491720ca0849b129.tar.xz
        """
        self._build_type = build_type
        self._variant = variant
        self._rev = rev
        self._targets = None
        if isinstance(targets, list):
            self._targets = sorted(targets)

        self._bucket = self._get_bucket_name()

        logger.debug(
            'tar_file_name: %s, remote_path: %s',
            self._tar_file_name,
            self._remote_path,
        )

    def __str__(self):
        return '%s cache of variant: %s, rev: %s, targets: %s' % (
            self._build_type,
            self._variant,
            self._rev,
            self._targets or '(default)',
        )

    @property
    def _tar_file_name(self):
        rev = util.escape_rev(self._rev)
        identifier = 'default'
        if self._targets:
            # Hash of the targets is used as an identifier of the file content.
            identifier = hashlib.md5(
                '_'.join(self._targets).encode('utf8')
            ).hexdigest()

        return '%s_%s.tar.xz' % (rev, identifier)

    @property
    def _remote_path(self):
        return 'gs://' + os.path.join(
            self._bucket, self._variant, self._tar_file_name
        )

    def put(self, src_dir: str):
        with tempfile.TemporaryDirectory() as tmpdir:
            try:
                tar_file_path = os.path.join(tmpdir, self._tar_file_name)
                util.check_call(
                    'tar',
                    *[
                        "--exclude=%s" % p
                        for p in self._EXCLUDES_PATTERNS_MAP.get(
                            self._build_type, []
                        )
                    ],
                    '-Ipixz',
                    '-cf',
                    tar_file_path,
                    '.',
                    cwd=src_dir,
                )
                gs_util.cp(tar_file_path, self._remote_path)
            except Exception:
                # non-fatal error. log but continue.
                logger.exception('failed to upload %s', self)
            else:
                logger.info('uploaded %s', self)

    @contextlib.contextmanager
    def get(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            try:
                gs_util.cp(self._remote_path, tmpdir)
                util.check_call(
                    'tar', '-Ipixz', '-xf', self._tar_file_name, cwd=tmpdir
                )
            except Exception as e:
                logger.exception('failed to download %s', self)
                raise errors.BisectRetriableError(
                    'failed to download build artifacts from cache'
                ) from e
            yield tmpdir

    def cache_hit(self) -> bool:
        try:
            gs_util.ls(self._remote_path)
        except Exception:
            logger.info('cache miss: %s', self)
            return False
        return True
