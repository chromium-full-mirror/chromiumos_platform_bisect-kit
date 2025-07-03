#!/usr/bin/env python3
# Copyright 2025 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

import json
import logging
import socket  # For timeout
import sys
import urllib.error
import urllib.request


# Gerrit API responses starts with )]}' to prevent XSSI.
GERRIT_JSON_PREFIX = ")]}'"

CHROMIUM_GERRIT_SERVER = "https://chromium-review.googlesource.com"

logger = logging.getLogger(__name__)


def get_gerrit_cl_info(change_id: str) -> dict | None:
    """
    Retrieves information for a specific CL from Gerrit using urllib.

    Args:
        change_id: The Change ID or legacy change number.

    Returns:
        A dictionary containing the CL information if successful, None otherwise.
    """

    # Construct the API URL for change details
    api_url = f"{CHROMIUM_GERRIT_SERVER}/changes/{change_id}/detail?o=CURRENT_REVISION&o=DETAILED_LABELS&o=MESSAGES"

    try:
        req = urllib.request.Request(api_url)
        req.add_header('User-Agent', 'cros-bisect-kit/1.0')

        with urllib.request.urlopen(req, timeout=30) as response:
            if not (200 <= response.status and response.status < 300):
                logger.error(
                    "HTTP error occurred: %d %s",
                    response.status,
                    response.reason,
                )
                try:
                    error_content = response.read().decode(
                        'utf-8', errors='ignore'
                    )
                    logger.error("Response content: %s", error_content)
                except Exception as e:
                    logger.error("Could not read error response body: %s", e)
                return None

            content_bytes = response.read()
            content_str = content_bytes.decode('utf-8')

            if content_str.startswith(GERRIT_JSON_PREFIX):
                content_str = content_str[len(GERRIT_JSON_PREFIX) :]

            cl_info = json.loads(content_str)
            return cl_info

    except urllib.error.HTTPError as http_err:
        logger.error(
            "HTTP error occurred: %d %s", http_err.code, http_err.reason
        )
        try:
            error_content = http_err.read().decode('utf-8', errors='ignore')
            logger.error("Response content: %s", error_content)
        except Exception as e:
            logger.error("Could not read error response body: %s", e)
    except urllib.error.URLError as url_err:
        if isinstance(url_err.reason, socket.timeout):
            logger.error("Timeout Error: %s", url_err.reason)
        else:
            logger.error(
                "URL Error (e.g., connection issue): %s", url_err.reason
            )
    except socket.timeout:
        logger.error("Timeout Error: The request timed out.")
    except json.JSONDecodeError as json_err:
        logger.error("Error decoding JSON response: %s", json_err)
        # content_str must be defined at the timing of json parse.
        logger.error("Response content that failed to parse: %s", content_str)
    except Exception as e:
        logger.exception("An unexpected error occurred: %s", e)

    return None


def main():
    """Main function to run the utility method from the command line."""
    if len(sys.argv) > 1:
        cl_number = sys.argv[1]
    else:
        cl_number = input("Enter the Change ID (e.g., 1234567s): ")

    if not cl_number:
        sys.stderr.write("Change ID cannot be empty.")
        return 1

    cl_data = get_gerrit_cl_info(cl_number)

    if not cl_data:
        sys.stderr.write(
            "Failed to retrieve CL information.",
        )
        return 1

    print(json.dumps(cl_data, indent=4))
    return 0


if __name__ == "__main__":
    sys.exit(main())
