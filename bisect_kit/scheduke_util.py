# Copyright 2025 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Utility functions to get lease info from Scheduke."""

import urllib.request

from bisect_kit import util
from chromiumos.config.proto.chromiumos.test.scheduling import task_state_pb2
import google.auth.transport.requests
import google.oauth2.id_token


_SCHEDUKE_FRONT_DOOR_URL = 'https://front-door-4vl5zcgwzq-wl.a.run.app'


def get_active_gcloud_user() -> str:
    """Returns the name of the active gcloud user."""
    return util.check_output(
        'gcloud',
        'auth',
        'list',
        '--filter=status:ACTIVE',
        '--format=value(account)',
    ).strip()


def is_dut_leased(dut_name: str) -> bool:
    task_states = get_task_states_from_frontdoor(
        users=[get_active_gcloud_user()], device_names=[dut_name]
    )
    return len(task_states.tasks) != 0


def get_task_states_from_frontdoor(
    ids: list[str] | None = None,
    users: list[str] | None = None,
    device_names: list[str] | None = None,
):
    """Returns a ReadTaskStatesResponse for the given params.
    The information is retrieved from FrontDoor service.
    """
    params = []
    if ids:
        params.append("ids=%s" % (",".join(ids)))
    if users:
        params.append("users=%s" % (",".join(users)))
    if device_names:
        params.append("device_names=%s" % (",".join(device_names)))
    url = "%s/tasks?%s" % (_SCHEDUKE_FRONT_DOOR_URL, "&".join(params))
    req = urllib.request.Request(url)

    audience = _SCHEDUKE_FRONT_DOOR_URL
    auth_req = google.auth.transport.requests.Request()
    id_token = google.oauth2.id_token.fetch_id_token(auth_req, audience)

    req.add_header("Authorization", f"Bearer {id_token}")
    response = urllib.request.urlopen(req)

    proto = task_state_pb2.ReadTaskStatesResponse()
    proto.ParseFromString(response.read())
    return proto
