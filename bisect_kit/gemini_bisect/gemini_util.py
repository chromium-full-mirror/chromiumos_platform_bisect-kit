# Copyright 2025 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Gemini utilities."""

import base64
import json
import logging
import os
import re
import sys
from typing import Optional

from bisect_kit import cr_util
from bisect_kit import git_util
from google import genai
from google.api_core import retry
import google.auth.transport.requests


GEMINI_API_KEY_SECRET_LOCATION = (
    'https://secretmanager.googleapis.com/'
    'v1/projects/43146777051/secrets/gemini-api-key/versions/1'
)
MODEL = 'gemini-3-pro'

PROMPT = (
    "You're a Software Engineer investigating test regressions.\n"
    '\n'
    'You have a failed test to investigate. '
    "You're a smart engineer and want to do faster than simple bisect, "
    'by reading changes, commits, and diffs within the culprit range to '
    'find better bisection point than midpoint at each step.\n'
    '\n'
    'You have external functions available to use. '
    'You can only call 1 external function once. '
    'If you want to call multiple functions, '
    'please call interactively after previous function returned.'
)

FUNCTIONS = [
    {
        "name": "get_test_name",
        "description": "Get name of the regressed test",
    },
    {
        "name": "get_range",
        "description": (
            "Get all revisions and first line of commit message (aka subject) "
            "for these revisions from known bad to known good (aka newest to "
            "oldest), inclusive on both end. Returns list of (rev_id, subject)."
            " rev_id are unique string to identify a revision, similar to git "
            "commit hash, but in different format."
        ),
    },
    {
        "name": "get_revision_detail",
        "description": (
            "Get all details, like commit messages, code diffs for a specific "
            "revision."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "rev_id": {
                    "type": "string",
                    "description": "Rev_id of the revision to run at.",
                },
            },
            "required": ["rev_id"],
        },
    },
    {
        "name": "run_at",
        "description": "Run the test at given commit",
        "parameters": {
            "type": "object",
            "properties": {
                "rev_id": {
                    "type": "string",
                    "description": "Rev_id of the revision to run at.",
                },
            },
            "required": ["rev_id"],
        },
    },
    {
        "name": "conclude",
        "description": (
            "Make conclusion on the culprit CL. The conclusion must be "
            "verified on the culprit and its previous commit."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "rev_id": {
                    "type": "string",
                    "description": "Rev_id of the revision to run at.",
                },
            },
            "required": ["rev_id"],
        },
    },
]

logger = logging.getLogger(__name__)


def _is_retriable_gemini_error(e) -> bool:
    if isinstance(e, genai.errors.ClientError) and e.code == 429:
        return True
    if isinstance(e, genai.errors.ServerError) and e.code == 503:
        return True
    return False


class GeminiAgent:
    """A stub agent to Gemini"""

    def __init__(self, prompt: str, functions: dict):
        """Initializes GeminiAgent with prompt and functions.

        promot: initial prompt to Gemini.
        functions: functions that gemini can call.
        """
        self._prompt = prompt
        credentials = self._get_credentials()
        session = google.auth.transport.requests.AuthorizedSession(credentials)
        ret = json.loads(
            session.get(GEMINI_API_KEY_SECRET_LOCATION + ":access").text
        )
        self._key = base64.b64decode(ret['payload']['data']).decode('ascii')
        self._client = genai.Client(api_key=self._key)
        tools = genai.types.Tool(function_declarations=functions)
        self._config = genai.types.GenerateContentConfig(
            tools=[tools],
            thinking_config=genai.types.ThinkingConfig(
                thinking_budget=-1,
                include_thoughts=True,
            ),
            tool_config=genai.types.ToolConfig(
                function_calling_config=genai.types.FunctionCallingConfig(
                    mode='ANY'
                )
            ),
        )
        self._contents = []

    def _get_credentials(self) -> google.oauth2.service_account.Credentials:
        """Get credentials to Cloud Secret Manager.

        Our Gemini API Key is stored at cloud secret manager so we can use the
        same service account to retrieve it.
        """
        credentials = (
            google.oauth2.service_account.Credentials.from_service_account_file(
                os.environ.get('SKYLAB_CLOUD_SERVICE_ACCOUNT_JSON'),
                scopes=['https://www.googleapis.com/auth/cloud-platform'],
            )
        )
        return credentials

    def start(self) -> genai.types.GenerateContentResponse:
        """Starts the agent by sending initial prompt.

        Returns the response from Gemini.
        """
        return self.reply(
            genai.types.Content(
                role='user', parts=[genai.types.Part(text=self._prompt)]
            )
        )

    @retry.Retry(predicate=_is_retriable_gemini_error)
    def reply(
        self, reply: genai.types.Content
    ) -> genai.types.GenerateContentResponse:
        """Send a reply content to Gemini.

        reply: the content reply to Gemini.
        Returns the response from Gemini.
        """
        contents = self._contents + [reply]
        logger.info('Replying %s', reply)
        response = self._client.models.generate_content(
            model=MODEL, contents=contents, config=self._config
        )
        # Only append to context history after successful call to avoid
        # duplicates caused from retry.
        self._contents = contents + [response.candidates[0].content]
        return response


class BisectAgent:
    """The agent that allows Gemini to analyze commit messages, diffs for
    smarter bisection."""

    def __init__(self, test_name: str, repo: str, revlist: list[str]):
        """Initializes BisectAgent.

        test_name: Name of the regressed test."""
        self._agent = GeminiAgent(prompt=PROMPT, functions=FUNCTIONS)
        self._test_name = test_name
        self._started = False
        self._repo = repo
        self._revlist = revlist
        self._conclusion = None

        # TODO(fqj): add support for ChromeOS CLs.
        rev2commit = {}
        # Use build_revlist to generate revlist and commits fast.
        _, details = cr_util.build_revlist(self._repo, revlist[0], revlist[-1])
        for x in revlist:
            rev2commit[x] = {
                'repo': self._repo,
                'commit': (
                    details[x]['actions'][0]['rev']
                    if x in details
                    # In case of any bug, try to recover commit individually
                    # build_revlist doesn't have this item
                    else cr_util.query_git_rev_by_commit_position(self._repo, x)
                ),
            }
        self._rev2commit = rev2commit

    def _get_revisions(self) -> list[dict]:
        """Returns the revlist and their subjects."""
        return [
            {
                'rev_id': x,
                'repo': self._rev2commit[x]['repo'],
                'subject': git_util.CommitMeta.get_summary(
                    git_util.get_commit_metadata(
                        self._rev2commit[x]['repo'],
                        self._rev2commit[x]['commit'],
                    )
                ),
            }
            for x in self._revlist
        ]

    def _get_revision_detail(self, rev_id: str) -> dict:
        """Returns the commit details of given rev_id.

        rev_id: the revision id.

        Returns the dict of revision detail."""
        return {
            'rev_id': rev_id,
            'repo': self._rev2commit[rev_id]['repo'],
            'git show': git_util.git_show(
                self._rev2commit[rev_id]['repo'],
                self._rev2commit[rev_id]['commit'],
            ),
        }

    def _run(self, reply: Optional[genai.types.Part] = None) -> str:
        """Call GeminiAgent recursively until a target revision is determined.

        reply: reply from previous run.
        Returns the decided revision to run.
        """
        if not self._started:
            ret = self._agent.start()
            self._started = True
        else:
            ret = self._agent.reply(
                genai.types.Content(
                    role='function',
                    parts=[reply],
                )
            )
        thoughts = '\n'.join(
            [
                v.text
                for v in ret.candidates[0].content.parts
                if v.text and v.thought
            ]
        )
        text = '\n'.join(
            [
                v.text
                for v in ret.candidates[0].content.parts
                if v.text and not v.thought
            ]
        )
        logger.debug('Gemini thought %s', thoughts)
        logger.info(
            'Gemini replied %s, calling %s',
            text,
            ret.function_calls,
        )
        call = ret.function_calls[0]
        if call.name == 'get_test_name':
            return self._run(
                genai.types.Part.from_function_response(
                    name='get_test_name', response={'name': self._test_name}
                )
            )
        if call.name == 'get_range':
            return self._run(
                genai.types.Part.from_function_response(
                    name='get_range',
                    response={'revisions': self._get_revisions()},
                )
            )
        if call.name == 'get_revision_detail':
            return self._run(
                genai.types.Part.from_function_response(
                    name='get_revision_detail',
                    response=self._get_revision_detail(call.args['rev_id']),
                )
            )
        if call.name == 'run_at':
            return call.args['rev_id']
        if call.name == 'conclude':
            self._conclusion = call.args['rev_id']
            return None
        raise NotImplementedError()

    def next(self, good: Optional[bool] = None) -> str:
        """Returns the next target revision for bisector to run.

        good: whether previous run is good or not."""
        if self._conclusion:
            return None
        if good is None:
            return self._run()
        return self._run(
            genai.types.Part.from_function_response(
                name='run_at', response={'is_good': good}
            )
        )

    def get_result(self) -> str:
        """Returns the concluded culprit revision."""
        return self._conclusion


def demo(testname, good, bad, culprit):
    agent = BisectAgent(
        testname,
        os.path.join(os.getenv('HOME'), 'bisect-workdir/test/chrome/src'),
        ['refs/heads/main@{#%d}' % (x) for x in range(good, bad + 1)],
    )
    attempt = agent.next()
    runs_llm = 0
    while attempt:
        attempt = int(re.search(r'#(\d+)', attempt).group(1))
        runs_llm += 1
        if attempt >= culprit:
            attempt = agent.next(False)
        else:
            attempt = agent.next(True)
    runs_bisect = 0
    low, high = good, bad
    while high - low > 1:
        runs_bisect += 1
        mid = (low + high) // 2
        if mid < culprit:
            low = mid
        else:
            high = mid

    print(
        'Concluded culprit',
        agent.get_result(),
        'in',
        runs_llm,
        'runs instead of',
        runs_bisect,
        'runs.',
    )


if __name__ == '__main__':
    # PoC purporse only (should replace with unittest)
    MODEL = 'gemini-3-flash-preview'
    logger.setLevel(logging.DEBUG)
    handler = logging.StreamHandler(sys.stderr)
    handler.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    demo('tast.ui.WebUIJSErrors', 1416515, 1416531, 1416520)
    demo('tast.a11y.Smoke', 1513012, 1513038, 1513018)
