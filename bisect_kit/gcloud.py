# Copyright 2024 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Common utils for Google cloud platform services."""

import json
import os

from google.api_core.client_options import ClientOptions
from google.cloud import datastore


def get_default_credential_file():
    """Returns default credential file"""
    return os.environ.get('SKYLAB_CLOUD_SERVICE_ACCOUNT_JSON')


class DataStoreClient:
    """Base class for a Datastore client."""

    def __init__(self):
        service_account_json = get_default_credential_file()

        with open(service_account_json) as f:
            service_account_data = json.load(f)
            project = service_account_data['project_id']

        self._client = datastore.Client(
            project=project,
            client_options=ClientOptions(credentials_file=service_account_json),
        )
