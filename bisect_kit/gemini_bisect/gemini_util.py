# Copyright 2025 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Gemini utilities."""

from __future__ import annotations

import base64
from collections import defaultdict
import logging
import os
import re
import sys
from typing import Any, TypedDict, Union

from bisect_kit import cr_util
from bisect_kit import git_util
from google import genai
from google.api_core import retry
import google.auth
import google.auth.transport.requests
import google.oauth2.service_account


GEMINI_API_KEY_SECRET_LOCATION = (
    'https://secretmanager.googleapis.com/'
    'v1/projects/43146777051/secrets/gemini-api-key/versions/latest'
)
MODEL = 'gemini-3-pro-preview'

PROMPT = (
    "You're a Software Engineer investigating test regressions.\n"
    '\n'
    'You have a failed test to investigate. '
    "You're a smart engineer and want to do faster than simple bisect, "
    'by reading changes, commits, and diffs within the culprit range to '
    'find better bisection point than midpoint at each step.\n'
    '\n'
    'Each revision returned by get_range has an associated "stats" field '
    'containing parameters from the underlying probability model. These statistics '
    'are dynamic and will change after every test run. You can call get_range '
    'again on-demand if you need to fetch the latest values.\n'
    '\n'
    'You must balance your code diff knowledge with the probability '
    'model:\n'
    '- If you are very confident in your code analysis (e.g., you find '
    'a commit that obviously breaks the test), the probability model '
    'can be ignored, and you can test or conclude that commit directly.\n'
    '- If you have zero or low confidence in your code analysis, you '
    'should move a step forward with the probability model (e.g., choose '
    'the revision with high info_gain_per_cost or info_gain) to narrow '
    'down the ranges, which can be better.\n'
    '\n'
    'You have external functions available to use. '
    'You can call multiple independent query functions in parallel in a '
    'single turn (e.g., get_revision_detail, get_test_name, '
    'get_range). However, run_at and conclude are blocking, final '
    'actions that MUST only be called individually and cannot be called '
    'in parallel with any other function calls.'
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
            "oldest), inclusive on both end. Returns list of dicts.\n"
            "Each dict contains:\n"
            "- 'rev_id': unique string to identify a revision.\n"
            "- 'subject': first line of commit message.\n"
            "- 'run_results': history of past test runs on this revision.\n"
            "- 'stats': dict of probability model parameters:\n"
            "  - 'prob': the probability that this revision is the culprit "
            "(the first revision that introduces the regression).\n"
            "  - 'info_gain': the expected decrease in entropy "
            "(uncertainty) of the culprit range if you test this revision.\n"
            "  - 'info_gain_per_cost': the info_gain divided by the estimated "
            "cost (build + deploy + run time) of testing this revision.\n"
            "Note that the run results and revision stats update dynamically "
            "after every run. Calling get_range again after run_at will "
            "return the updated values."
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
                "reason": {
                    "type": "string",
                    "description": (
                        "A short explanation of 1-2 sentences regarding why "
                        "you choose to run this revision."
                    ),
                },
            },
            "required": ["rev_id", "reason"],
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
                "reason": {
                    "type": "string",
                    "description": (
                        "A short explanation of 1-2 sentences regarding why "
                        "you choose to run this revision."
                    ),
                },
            },
            "required": ["rev_id", "reason"],
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


NestedDict = dict[str, Union[str, int, 'NestedDict']]


class ProbModelResultType(TypedDict):
    """Type annotation for dict values for per-commit prob model result"""

    prob: float
    status: dict[str, int]
    info_gain: float | None
    info_gain_per_cost: float | None


class GeminiAgent:
    """A stub agent to Gemini"""

    def __init__(self, prompt: str, functions: dict, model: str | None = None):
        """Initializes GeminiAgent with prompt and functions.

        promot: initial prompt to Gemini.
        functions: functions that gemini can call.
        """
        self._prompt = prompt
        self._model = model or MODEL
        try:
            credentials = self._get_credentials()
            session = google.auth.transport.requests.AuthorizedSession(
                credentials
            )
            response = session.get(GEMINI_API_KEY_SECRET_LOCATION + ":access")
            response.raise_for_status()
            ret = response.json()
            self._key = base64.b64decode(ret['payload']['data']).decode('ascii')
        except Exception as e:
            raise RuntimeError(
                "Failed to retrieve Gemini API key from Secret Manager. "
                f"Error: {e}"
            ) from e
        self._client = genai.Client(api_key=self._key)
        tools = genai.types.Tool(function_declarations=functions)
        self._config = genai.types.GenerateContentConfig(
            system_instruction=self._prompt,
            tools=[tools],
            thinking_config=genai.types.ThinkingConfig(
                thinking_level='high',
            ),
            tool_config=genai.types.ToolConfig(
                function_calling_config=genai.types.FunctionCallingConfig(
                    mode='ANY'
                )
            ),
        )
        self._contents: list[genai.types.Content] = []

    def _get_credentials(self) -> google.oauth2.service_account.Credentials:
        """Get credentials to Cloud Secret Manager.

        Our Gemini API Key is stored at cloud secret manager so we can use the
        same service account to retrieve it.
        """
        sa_file = os.environ.get('SKYLAB_CLOUD_SERVICE_ACCOUNT_JSON')
        if not sa_file:
            raise ValueError(
                'SKYLAB_CLOUD_SERVICE_ACCOUNT_JSON environment variable '
                'is not set'
            )
        return (
            google.oauth2.service_account.Credentials.from_service_account_file(
                sa_file,
                scopes=['https://www.googleapis.com/auth/cloud-platform'],
            )
        )

    def start(self) -> genai.types.GenerateContentResponse:
        """Starts the agent by sending a start trigger.

        Returns the response from Gemini.
        """
        return self.reply(
            genai.types.Content(
                role='user',
                parts=[genai.types.Part(text="Start bisection investigation.")],
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
        response = self._client.models.generate_content(
            model=self._model, contents=contents, config=self._config
        )
        # Only append to context history after successful call to avoid
        # duplicates caused from retry.
        if not response.candidates:
            raise RuntimeError(
                f"No candidates returned from Gemini. Response: {response}"
            )
        candidate = response.candidates[0]
        if not candidate.content or not candidate.content.parts:
            finish_reason = getattr(candidate, 'finish_reason', 'UNKNOWN')
            raise RuntimeError(
                f"Gemini response candidate has no content or parts. "
                f"Finish reason: {finish_reason}. Response: {response}"
            )
        self._contents = contents + [candidate.content]
        return response


class BisectAgent:
    """The agent that allows Gemini to analyze commit messages, diffs for
    smarter bisection."""

    def __init__(
        self,
        test_name: str,
        repo: str,
        revlist: list[str],
        model: str | None = None,
    ):
        """Initializes BisectAgent.

        test_name: Name of the regressed test."""
        self._agent = GeminiAgent(
            prompt=PROMPT, functions=FUNCTIONS, model=model
        )
        self._test_name = test_name
        self._started = False
        self._repo = repo
        self._revlist = revlist
        self._rev_samples: dict[str, list[str]] = defaultdict(list)
        self._rev_stats: dict[str, ProbModelResultType] = {}
        self._conclusion: str | None = None
        self._pending_parts: list[genai.types.Part] = []
        self._pending_run_at_call: Any = None

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
                'run_results': self._rev_samples[x],
                'stats': self._rev_stats.get(x),
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

    def _short(self, d: Any) -> Any:
        """Create a shorter object for logging purpose.

        d: any object to be logged.
        Returns a shorter version of the object.
        """
        if isinstance(d, dict):
            ret = {}
            for k, v in d.items():
                if k == 'git show' and isinstance(v, str):
                    # Keep only first few lines of git show
                    lines = v.split('\n')
                    if len(lines) > 5:
                        ret[k] = '\n'.join(lines[:5]) + '\n...'
                    else:
                        ret[k] = v
                else:
                    ret[k] = self._short(v)
            return ret
        if isinstance(d, list):
            if len(d) > 3:
                return [
                    self._short(d[0]),
                    f'... and {len(d) - 2} more items ...',
                    self._short(d[-1]),
                ]
            return [self._short(x) for x in d]
        if isinstance(d, str):
            _N = 256
            return d if len(d) <= _N else (d[:_N] + '...')
        return d

    def _run(self) -> str | None:
        """Runs the GeminiAgent loop.

        Runs until a bisection action (run_at or conclude) is requested.
        """
        while True:
            if not self._started:
                ret = self._agent.start()
                self._started = True
            else:
                for part in self._pending_parts:
                    logger.info(
                        'Sending response to Gemini: %s',
                        self._short(part.function_response.response),
                    )
                ret = self._agent.reply(
                    genai.types.Content(
                        role='function',
                        parts=self._pending_parts,
                    )
                )
                self._pending_parts = []

            text = '\n'.join(
                [
                    v.text
                    for v in ret.candidates[0].content.parts
                    if v.text and not getattr(v, 'thought', False)
                ]
            )
            if not ret.function_calls:
                raise RuntimeError(
                    f"Gemini failed to return any function calls. "
                    f"Response text: {text}. Response: {ret}"
                )
            logger.info(
                'Gemini replied %s, calling %s',
                self._short(text),
                ', '.join(
                    [
                        f'{call.name}({self._short(call.args)})'
                        for call in ret.function_calls
                    ]
                ),
            )

            run_at_call = None
            conclude_call = None

            for call in ret.function_calls:
                if call.name == 'get_test_name':
                    self._pending_parts.append(
                        genai.types.Part(
                            function_response=genai.types.FunctionResponse(
                                id=call.id,
                                name='get_test_name',
                                response={'name': self._test_name},
                            )
                        )
                    )
                elif call.name == 'get_range':
                    self._pending_parts.append(
                        genai.types.Part(
                            function_response=genai.types.FunctionResponse(
                                id=call.id,
                                name='get_range',
                                response={'revisions': self._get_revisions()},
                            )
                        )
                    )
                elif call.name == 'get_revision_detail':
                    rev_id = call.args.get('rev_id')
                    if not rev_id or rev_id not in self._rev2commit:
                        logger.warning(
                            'Gemini requested detail for invalid rev_id: %s',
                            rev_id,
                        )
                        self._pending_parts.append(
                            genai.types.Part(
                                function_response=genai.types.FunctionResponse(
                                    id=call.id,
                                    name='get_revision_detail',
                                    response={
                                        'error': (
                                            f'Invalid rev_id {rev_id}. '
                                            'Please use a rev_id from '
                                            'get_range.'
                                        )
                                    },
                                )
                            )
                        )
                    else:
                        self._pending_parts.append(
                            genai.types.Part(
                                function_response=genai.types.FunctionResponse(
                                    id=call.id,
                                    name='get_revision_detail',
                                    response=self._get_revision_detail(rev_id),
                                )
                            )
                        )
                elif call.name == 'run_at':
                    rev_id = call.args.get('rev_id')
                    if not rev_id or rev_id not in self._rev2commit:
                        logger.warning(
                            'Gemini requested run_at for invalid rev_id: %s',
                            rev_id,
                        )
                        self._pending_parts.append(
                            genai.types.Part(
                                function_response=genai.types.FunctionResponse(
                                    id=call.id,
                                    name='run_at',
                                    response={
                                        'error': (
                                            f'Invalid rev_id {rev_id}. '
                                            'Please use a rev_id from '
                                            'get_range.'
                                        )
                                    },
                                )
                            )
                        )
                    else:
                        run_at_call = call
                elif call.name == 'conclude':
                    rev_id = call.args.get('rev_id')
                    if not rev_id or rev_id not in self._rev2commit:
                        logger.warning(
                            'Gemini requested conclude for invalid rev_id: %s',
                            rev_id,
                        )
                        self._pending_parts.append(
                            genai.types.Part(
                                function_response=genai.types.FunctionResponse(
                                    id=call.id,
                                    name='conclude',
                                    response={
                                        'error': (
                                            f'Invalid rev_id {rev_id}. '
                                            'Please use a rev_id from '
                                            'get_range.'
                                        )
                                    },
                                )
                            )
                        )
                    else:
                        conclude_call = call
                else:
                    raise NotImplementedError(
                        f"Unsupported function call: {call.name}"
                    )

            if conclude_call is not None:
                self._conclusion = conclude_call.args['rev_id']
                self._pending_parts.append(
                    genai.types.Part(
                        function_response=genai.types.FunctionResponse(
                            id=conclude_call.id,
                            name='conclude',
                            response={'status': 'concluded'},
                        )
                    )
                )
                return None

            if run_at_call is not None:
                self._pending_run_at_call = run_at_call
                return run_at_call.args['rev_id']

    def add_sample(self, rev: str, status: str):
        """Add run result

        rev: revision
        status: status of a run."""
        self._rev_samples[rev].append(status)

    def update_stats(self, stats: dict[str, ProbModelResultType]):
        """Updates prob model stats

        stats: new stats."""
        self._rev_stats = stats

    def next(self, status: str | None = None) -> str | None:
        """Returns the next target revision for bisector to run.

        good: whether previous run is good or not."""
        if self._conclusion:
            return None
        if status is not None:
            assert self._pending_run_at_call is not None
            self._pending_parts.append(
                genai.types.Part(
                    function_response=genai.types.FunctionResponse(
                        id=self._pending_run_at_call.id,
                        name='run_at',
                        response={'status': status},
                    )
                )
            )
            self._pending_run_at_call = None
        return self._run()

    def get_result(self) -> str:
        """Returns the concluded culprit revision."""
        return self._conclusion


def _get_simulated_cost_table(good, bad, prev_rev):
    cost_table = []
    for x in range(good, bad + 1):
        if x == prev_rev:
            # No build/deploy, only Tast run (PASS: 60s, FAIL: 30s)
            cost_table.append((60.0, 30.0))
        else:
            # Incremental build & deploy (240s) + Tast run (PASS: 60s, FAIL: 30s)
            cost_table.append((300.0, 270.0))
    return cost_table


def demo(testname, good, bad, culprit):
    # pylint: disable=import-outside-toplevel
    from bisect_kit import strategy

    agent = BisectAgent(
        testname,
        os.path.join(os.getenv('HOME'), 'bisect-workdir/test/chrome/src'),
        ['refs/heads/main@{#%d}' % (x) for x in range(good, bad + 1)],
    )
    agent.add_sample('refs/heads/main@{#%d}' % good, 'PASS')
    agent.add_sample('refs/heads/main@{#%d}' % bad, 'FAIL')
    stat = defaultdict(list)
    stat[good].append('PASS')
    stat[bad].append('FAIL')

    prob = [
        1.0 / (bad - good) if good < x <= bad else 0.0
        for x in range(good, bad + 1)
    ]
    # The last run revision was 'bad'
    cost_table = _get_simulated_cost_table(good, bad, bad)
    # pylint: disable=protected-access
    utilities = strategy.NoisyBinarySearch._calculate_utilities(
        0.0, 1.0, prob, cost_table=cost_table
    )

    agent.update_stats(
        {
            'refs/heads/main@{#%d}'
            % (x): {
                'prob': prob[x - good],
                'status': {
                    'PASS': stat[x].count('PASS'),
                    'FAIL': stat[x].count('FAIL'),
                },
                'info_gain': utilities[x - good][0],
                'info_gain_per_cost': utilities[x - good][1],
            }
            for x in range(good, bad + 1)
        }
    )
    attempt = agent.next()
    runs_llm = 0
    now_good, now_bad = good, bad
    while attempt:
        rev = int(re.search(r'#(\d+)', attempt).group(1))
        runs_llm += 1
        if rev >= culprit:
            status = 'FAIL'
            now_bad = min(bad, now_bad)
        else:
            status = 'PASS'
            now_good = max(good, now_good)
        stat[rev].append(status)
        agent.add_sample('refs/heads/main@{#%d}' % rev, status)

        prob = [
            1.0 / (now_bad - now_good) if now_good < x <= now_bad else 0.0
            for x in range(good, bad + 1)
        ]
        # Current revision is 'rev'
        cost_table = _get_simulated_cost_table(good, bad, rev)
        # pylint: disable=protected-access
        utilities = strategy.NoisyBinarySearch._calculate_utilities(
            0.0, 1.0, prob, cost_table=cost_table
        )

        agent.update_stats(
            {
                'refs/heads/main@{#%d}'
                % (x): {
                    'prob': prob[x - good],
                    'status': {
                        'PASS': stat[x].count('PASS'),
                        'FAIL': stat[x].count('FAIL'),
                    },
                    'info_gain': utilities[x - good][0],
                    'info_gain_per_cost': utilities[x - good][1],
                }
                for x in range(good, bad + 1)
            }
        )
        attempt = agent.next(status)
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
    MODEL = 'gemini-3.5-flash'
    logger.setLevel(logging.DEBUG)
    handler = logging.StreamHandler(sys.stderr)
    handler.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    demo('tast.ui.WebUIJSErrors', 1416515, 1416531, 1416520)
    demo('tast.a11y.Smoke', 1513012, 1513038, 1513018)
