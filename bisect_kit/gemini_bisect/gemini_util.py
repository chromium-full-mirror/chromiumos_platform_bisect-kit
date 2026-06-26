# Copyright 2025 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Gemini utilities."""

from __future__ import annotations

import base64
from collections import defaultdict
import copy
import logging
import os
import random
import sys
from typing import Any, TypedDict, Union

from bisect_kit import common
from bisect_kit import core
from bisect_kit import cr_util
from bisect_kit import cros_util
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
MODEL_GEMINI_3_5_FLASH = 'gemini-3.5-flash'
MODEL_DEFAULT = MODEL_GEMINI_3_5_FLASH

PROMPT = (
    "You're a Software Engineer investigating test regressions.\n"
    '\n'
    'You have a failed test to investigate. '
    "You're a smart engineer and want to do faster than simple bisect, "
    'by reading changes, commits, and diffs within the culprit range to '
    'find better bisection point than midpoint at each step.\n'
    '\n'
    'Tests may be flaky. The get_range call returns flaky rates "old_p" (the '
    'probability of observing FAIL/new behavior on a good/old commit) and '
    '"new_p" (the probability of observing FAIL/new behavior on a bad/new '
    'commit). Because tests can be flaky, you may choose to run a single commit '
    'multiple times (calling run_at on the same revision again) or test the same '
    'commit later to gain higher statistical confidence. Note that when "old_p" '
    'is non-zero, a good commit can hit an unlucky streak of FAILs, causing '
    '"likelihood_new" to climb high (e.g. 80-90%). Conversely, a single PASS '
    'is strong proof that the commit is good/old. Therefore, do not blindly '
    'trust a high likelihood_new after a few FAILs; if you suspect a boundary '
    'commit might be a flaky good commit, re-test it on the device (skips '
    'rebuild, though still reflashes!) to see if it can produce a PASS.\n'
    '\n'
    'Be aware of execution cost: switching to a different commit requires '
    'rebuilding and deploying code, which takes significantly more time '
    'than re-running the test on the same revision (which skips rebuild, '
    'though still reflashes).\n'
    '\n'
    'Each revision returned by get_range has an associated "stats" field '
    'containing parameters from the underlying probability model. These '
    'statistics are dynamic and will change after every test run. You '
    'can call get_range again on-demand if you need to fetch the latest '
    'values. You can also optionally request the test failure log / reasons '
    'by passing include_reason=True to get_range, which will populate '
    'run_results with dictionaries containing the status and the test '
    'failure reason (representing the error logs or timeout output from the '
    'test execution if it failed).\n'
    '\n'
    'The run_at function returns a dictionary containing the test run "status", '
    'optional "reason", and "likelihood_new" (a float between 0.0 and 1.0 '
    'representing the likelihood of this commit having the "new/FAIL" '
    'behavior based on all past test run history; note that this is NOT the '
    'probability of this commit being the culprit, but rather the cumulative '
    'probability that the regression was introduced at or before this commit).\n'
    'Log contains the full log from ssh to DUT until test completes. But '
    'does not contain any system log like chrome log or syslog grabbed on DUT. '
    'You can call read_last_log to read the execution log of the last run_at. '
    'read_last_log can also take a file_name parameter (e.g., "system_logs/syslog" '
    'or "results.json") to read other files collected in the test results directory. '
    'You can call read_log to read the execution log of a particular run of a particular '
    'revision.\n'
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
    'get_range, read_last_log, read_log). However, run_at and conclude are blocking, final '
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
            "for these revisions from known good to known bad (aka oldest to "
            "newest), inclusive on both ends. Returns a dictionary containing "
            "'revisions' (list of dicts), 'old_p' (flaky failure probability on "
            "good commits), and 'new_p' (failure probability on bad commits).\n"
            "Each revision dict contains:\n"
            "- 'rev_id': unique string to identify a revision.\n"
            "- 'subject': first line of commit message.\n"
            "- 'run_results': history of past test runs on this revision. "
            "By default, this is a list of status strings (e.g., 'PASS', 'FAIL'). "
            "If include_reason=True is passed, this will instead be a "
            "list of dictionaries containing 'status' and 'reason' "
            "(the test's failure output or error log if it failed).\n"
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
        "parameters": {
            "type": "object",
            "properties": {
                "include_reason": {
                    "type": "boolean",
                    "description": (
                        "Whether to include the test failure reason/error "
                        "log for each revision's run results if they failed."
                    ),
                }
            },
        },
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
                        "Your justification / explanation (1-2 sentences) "
                        "regarding why you choose to inspect this revision."
                    ),
                },
            },
            "required": ["rev_id", "reason"],
        },
    },
    {
        "name": "read_last_log",
        "description": (
            "Read logs from the last test run. "
            "If file_name is empty or not set, reads the default test execution log "
            "and includes 'available_files' listing all files collected in the test results directory. "
            "Otherwise, reads the specified file (e.g. 'system_logs/syslog', 'results.json')."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "file_name": {
                    "type": "string",
                    "description": (
                        "Optional file name or relative path to read from the "
                        "collected test results (e.g. 'system_logs/syslog', "
                        "'results.json'). Leave empty or omit for default test log."
                    ),
                },
            },
        },
    },
    {
        "name": "read_log",
        "description": (
            "Read the test execution log of a particular run of a particular "
            "revision. Log contains the full log from ssh to DUT until test completes, "
            "excluding system logs. Returns a dictionary containing 'log'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "rev_id": {
                    "type": "string",
                    "description": "Rev_id of the revision to inspect log for.",
                },
                "run_index": {
                    "type": "integer",
                    "description": (
                        "0-based index of the test run for this revision "
                        "(e.g., 0 for the first run, 1 for the second run)."
                    ),
                },
            },
            "required": ["rev_id", "run_index"],
        },
    },
    {
        "name": "run_at",
        "description": (
            "Run the test at given commit. Returns a dictionary containing "
            "the status (e.g., 'PASS', 'FAIL'), optionally a 'reason' "
            "key containing the test failure output or error log if the "
            "run failed, and 'likelihood_new' (a float between 0.0 and 1.0) "
            "representing the likelihood of this commit having the 'new/FAIL' "
            "behavior based on all past test run history (the cumulative "
            "probability that the culprit is at or before this commit; this is "
            "NOT the probability of this commit itself being the culprit)."
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
                        "Your justification / explanation (1-2 sentences) "
                        "regarding why you choose to run the test at this "
                        "revision."
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


class PrefixLoggerAdapter(logging.LoggerAdapter):
    """LoggerAdapter that prepends prefix to log messages."""

    def process(self, msg, kwargs):
        prefix = self.extra.get('prefix', '')
        return f'{prefix}{msg}', kwargs


def _is_retriable_gemini_error(e) -> bool:
    if isinstance(e, genai.errors.ClientError) and e.code == 429:
        return True
    if isinstance(e, genai.errors.ServerError) and e.code == 503:
        return True
    return False


class TrialResult(TypedDict, total=False):
    """Type annotation for a single run trial result."""

    status: str
    reason: str | None
    likelihood_new: float | None
    eval_log: str | None


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
        self._model = model or MODEL_DEFAULT
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

    def __deepcopy__(self, memo):
        cls = self.__class__
        result = cls.__new__(cls)
        memo[id(self)] = result
        for k, v in self.__dict__.items():
            if k in ('_client', '_config'):
                setattr(result, k, v)
            else:
                setattr(result, k, copy.deepcopy(v, memo))
        return result

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


def _get_git_repo_path(root: str, action_path: str) -> str:
    if os.path.exists(os.path.join(root, '.git')):
        return root
    return os.path.join(root, action_path)


class BisectAgent:
    """The agent that allows Gemini to analyze commit messages, diffs for
    smarter bisection."""

    def __init__(
        self,
        test_name: str,
        repo: str,
        revlist: list[str],
        model: str | None = None,
        chromeos_mirror: str | None = None,
        board: str | None = None,
        old_p: float = 0.0,
        new_p: float = 1.0,
        session: str | None = None,
        custom_logger: logging.Logger | logging.LoggerAdapter | None = None,
    ):
        """Initializes BisectAgent.

        test_name: Name of the regressed test."""
        self._agent = GeminiAgent(
            prompt=PROMPT, functions=FUNCTIONS, model=model
        )
        self.logger = custom_logger or PrefixLoggerAdapter(
            logging.getLogger(__name__), {'prefix': ''}
        )
        self._test_name = test_name
        self._started = False
        self._repo = repo
        self._revlist = revlist
        self._rev_samples: dict[str, list[TrialResult]] = defaultdict(list)
        self._rev_stats: dict[str, ProbModelResultType] = {}
        self._old_p = old_p
        self._new_p = new_p
        self._conclusion: str | None = None
        self._pending_parts: list[genai.types.Part] = []
        self._pending_run_at_call: Any = None
        self._session = session
        self._last_run_eval_log: str | None = None

        is_chrome = (
            '@' in revlist[0]
            or revlist[0].startswith('refs/')
            or revlist[0].isdigit()
        )
        rev2commit = {}
        if is_chrome:
            _, details = cr_util.build_revlist(
                self._repo, revlist[0], revlist[-1]
            )
            for x in revlist:
                rev2commit[x] = {
                    'repo': self._repo,
                    'commit': (
                        details[x]['actions'][0]['rev']
                        if x in details
                        # In case of any bug, try to recover commit individually
                        # build_revlist doesn't have this item
                        else cr_util.query_git_rev_by_commit_position(
                            self._repo, x
                        )
                    ),
                }
        else:
            _, details = cros_util.build_revlist(
                self._repo,
                revlist[0],
                revlist[-1],
                chromeos_mirror=chromeos_mirror,
                board=board,
            )
            for x in revlist:
                rev2commit[x] = {
                    'repo': (
                        _get_git_repo_path(
                            self._repo, details[x]['actions'][0]['path']
                        )
                        if x in details and details[x]['actions']
                        else self._repo
                    ),
                    'commit': (
                        details[x]['actions'][0]['rev']
                        if x in details and details[x]['actions']
                        else x
                    ),
                }
        self._rev2commit = rev2commit

    def _get_revisions(self, include_reason: bool = False) -> list[dict]:
        """Returns the revlist and their subjects."""
        revisions = []
        for x in self._revlist:
            run_results: list[str] | list[TrialResult]
            if include_reason:
                run_results = [
                    TrialResult(
                        status=sample['status'], reason=sample['reason']
                    )
                    for sample in self._rev_samples[x]
                ]
            else:
                run_results = [
                    sample['status'] for sample in self._rev_samples[x]
                ]
            if self._rev2commit[x]['commit'] == x:
                subject = f"Endpoint revision: {x}"
            else:
                try:
                    subject = git_util.CommitMeta.get_summary(
                        git_util.get_commit_metadata(
                            self._rev2commit[x]['repo'],
                            self._rev2commit[x]['commit'],
                        )
                    )
                except Exception as e:
                    self.logger.warning(
                        'Failed to get commit metadata for %s: %s',
                        self._rev2commit[x]['commit'],
                        e,
                    )
                    subject = f"Revision {x}"
            rev_dict = {
                'rev_id': x,
                'repo': self._rev2commit[x]['repo'],
                'subject': subject,
                'run_results': run_results,
                'stats': self._rev_stats.get(x),
            }
            revisions.append(rev_dict)
        return revisions

    def _get_revision_detail(self, rev_id: str) -> dict:
        """Returns the commit details of given rev_id.

        rev_id: the revision id.

        Returns the dict of revision detail."""
        if self._rev2commit[rev_id]['commit'] == rev_id:
            return {
                'rev_id': rev_id,
                'repo': self._rev2commit[rev_id]['repo'],
                'git show': f"Endpoint revision {rev_id}",
            }
        try:
            git_show_content = git_util.git_show(
                self._rev2commit[rev_id]['repo'],
                self._rev2commit[rev_id]['commit'],
            )
        except Exception as e:
            self.logger.warning(
                'Failed to git show for %s: %s',
                self._rev2commit[rev_id]['commit'],
                e,
            )
            git_show_content = f"Failed to git show: {e}"
        return {
            'rev_id': rev_id,
            'repo': self._rev2commit[rev_id]['repo'],
            'git show': git_show_content,
        }

    def _list_collected_files(self) -> list[str]:
        """Lists relative paths of all files in the last run test results directory."""
        files: list[str] = []
        if not self._repo:
            return files
        for target_dir in ['tmp/tast_results_tmp', 'tmp/autotest_results_tmp']:
            full_dir = os.path.join(self._repo, target_dir)
            if os.path.exists(full_dir) and os.path.isdir(full_dir):
                for root, _, filenames in os.walk(full_dir):
                    for fname in filenames:
                        full_path = os.path.join(root, fname)
                        rel_path = os.path.relpath(full_path, full_dir)
                        files.append(rel_path)
        return sorted(list(set(files)))

    def _read_log_file(
        self, eval_log: str | None, file_name: str | None = None
    ) -> dict:
        """Reads the evaluation log file or a specific collected file adhoc from disk."""
        if file_name and file_name.strip():
            file_name = file_name.strip()
            if not self._repo:
                return {
                    'error': 'Repository root not available to locate test results.'
                }
            candidate_paths = [
                os.path.join(self._repo, 'tmp/tast_results_tmp', file_name),
                os.path.join(self._repo, 'tmp/autotest_results_tmp', file_name),
            ]

            log_full_path = None
            for path in candidate_paths:
                if os.path.exists(path) and os.path.isfile(path):
                    log_full_path = path
                    break

            if not log_full_path:
                return {
                    'error': (
                        f'File {file_name} not found in collected test results.'
                    )
                }
        else:
            if not eval_log:
                return {'error': 'Log not available for this run.'}
            if self._session:
                log_full_path = common.get_session_log_path(
                    self._session, eval_log
                )
            else:
                log_full_path = eval_log
            if not os.path.exists(log_full_path):
                return {'error': f'Log file {eval_log} does not exist.'}

        try:
            with open(
                log_full_path, 'r', encoding='utf-8', errors='replace'
            ) as f:
                content = f.read()
            return {'log': content}
        except Exception as e:
            return {'error': f'Failed to read log file: {e}'}

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
            if len(d) > 5:
                mid = len(d) // 2
                return [
                    self._short(d[0]),
                    f'... and {mid - 1} more items ...',
                    self._short(d[mid]),
                    f'... and {len(d) - mid - 2} more items ...',
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
                    self.logger.info(
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
            self.logger.info(
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
                    include_reason = call.args.get('include_reason', False)
                    self._pending_parts.append(
                        genai.types.Part(
                            function_response=genai.types.FunctionResponse(
                                id=call.id,
                                name='get_range',
                                response={
                                    'revisions': self._get_revisions(
                                        include_reason
                                    ),
                                    'old_p': self._old_p,
                                    'new_p': self._new_p,
                                },
                            )
                        )
                    )
                elif call.name == 'get_revision_detail':
                    rev_id = call.args.get('rev_id')
                    if not rev_id or rev_id not in self._rev2commit:
                        self.logger.warning(
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
                elif call.name == 'read_last_log':
                    file_name = call.args.get('file_name')
                    resp = self._read_log_file(
                        self._last_run_eval_log,
                        file_name=file_name,
                    )
                    if not file_name or not file_name.strip():
                        resp['available_files'] = self._list_collected_files()
                    self._pending_parts.append(
                        genai.types.Part(
                            function_response=genai.types.FunctionResponse(
                                id=call.id,
                                name='read_last_log',
                                response=resp,
                            )
                        )
                    )
                elif call.name == 'read_log':
                    rev_id = call.args.get('rev_id')
                    run_index = call.args.get('run_index')
                    if not rev_id or rev_id not in self._rev2commit:
                        resp = {'error': f'Invalid rev_id {rev_id}.'}
                    elif (
                        run_index is None
                        or not isinstance(run_index, int)
                        or run_index < 0
                    ):
                        resp = {
                            'error': (
                                f'Invalid run_index {run_index}. Must be a'
                                ' non-negative integer.'
                            )
                        }
                    elif run_index >= len(self._rev_samples[rev_id]):
                        resp = {
                            'error': (
                                f'run_index {run_index} out of range for'
                                f' revision {rev_id}. Total runs:'
                                f' {len(self._rev_samples[rev_id])}.'
                            )
                        }
                    else:
                        eval_log = self._rev_samples[rev_id][run_index].get(
                            'eval_log'
                        )
                        resp = self._read_log_file(eval_log)
                    self._pending_parts.append(
                        genai.types.Part(
                            function_response=genai.types.FunctionResponse(
                                id=call.id,
                                name='read_log',
                                response=resp,
                            )
                        )
                    )
                elif call.name == 'run_at':
                    rev_id = call.args.get('rev_id')
                    if not rev_id or rev_id not in self._rev2commit:
                        self.logger.warning(
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
                        self.logger.warning(
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

    def add_sample(
        self,
        rev: str,
        status: str,
        reason: str | None = None,
        eval_log: str | None = None,
    ):
        """Add run result

        rev: revision
        status: status of a run."""
        self._rev_samples[rev].append(
            {'status': status, 'reason': reason, 'eval_log': eval_log}
        )

    def update_stats(
        self,
        stats: dict[str, ProbModelResultType],
        old_p: float | None = None,
        new_p: float | None = None,
    ):
        """Updates prob model stats

        stats: new stats."""
        self._rev_stats = stats
        if old_p is not None:
            self._old_p = old_p
        if new_p is not None:
            self._new_p = new_p

    def next(self, status: TrialResult | None = None) -> str | None:
        """Returns the next target revision for bisector to run."""
        if self._conclusion:
            return None
        if status is not None:
            assert self._pending_run_at_call is not None
            self._last_run_eval_log = status.get('eval_log')
            self._pending_parts.append(
                genai.types.Part(
                    function_response=genai.types.FunctionResponse(
                        id=self._pending_run_at_call.id,
                        name='run_at',
                        response={
                            'status': status['status'],
                            'reason': status.get('reason'),
                        },
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
            # Skips rebuild (still reflashes & runs test)
            cost_table.append((60.0, 30.0))
        else:
            # Incremental build & deploy (240s) + Tast run (PASS: 60s, FAIL: 30s)
            cost_table.append((300.0, 270.0))
    return cost_table


def demo(
    testname,
    good: str,
    bad: str,
    culprit: str,
    disabled: bool = False,
    board: str | None = None,
    sim_old_p: float = 0.0,
    sim_new_p: float = 1.0,
):
    if disabled:
        return

    # pylint: disable=import-outside-toplevel
    import uuid

    from bisect_kit import strategy

    def get_reason() -> str | None:
        if testname == 'tast.ui.WebUIJSErrors':
            h = uuid.uuid4().hex
            return (
                'checkUnhandledPromiseRejectionError failed: WaitForCrashFiles '
                f'failed for directory [/run/daemon-store/crash/{h}]: '
                'timed out while waiting for crash files: no file matched '
                'jserror\\.\\d{8}\\.\\d{6}\\.\\d+\\.\\d+\\.meta, '
                'jserror\\.\\d{8}\\.\\d{6}\\.\\d+\\.\\d+\\.js_stack, '
                'jserror\\.\\d{8}\\.\\d{6}\\.\\d+\\.\\d+\\.chrome.txt.gz '
                f'(dirs /run/daemon-store/crash/{h}) (found )'
            )
        if testname == 'tast.a11y.Smoke':
            return (
                'Failed to find the UI element (Browser: New Tab) in the browser: '
                'Uncaught (in promise): context deadline exceeded during a poll with '
                'timeout 15s; "failed to find node with properties: {className: '
                '/\\\\bBrowserFrame\\\\b/, role: window}"'
            )
        if testname == 'tast.arc.RemovableMedia.vm':
            return (
                'Failed to test Files app integration: failed to open a file '
                'from Files app: failed to open Files app: launching the '
                'Files App failed: Uncaught (in promise): context deadline '
                'exceeded during a poll with timeout 60s; "failed to find '
                'node with properties: {name: /^Downloads$/, role: treeItem, '
                'ancestor: {name: /^Files/, className: '
                '/\\\\bBrowserWidget\\\\b/, role: window, first: true}}"'
            )
        if testname == 'tast.video.CDMOEMCrypto.ce_cdm':
            return (
                'widevine_ce_cdm_hw_tests failed: signal: aborted (core dumped)'
            )
        return None

    is_chrome = '@' in good or good.startswith('refs/') or good.isdigit()
    chromeos_mirror = os.path.join(
        os.getenv('HOME'), 'git-mirrors-ext4/chromeos'
    )
    if is_chrome:
        repo = os.path.join(os.getenv('HOME'), 'bisect-workdir/test/chrome/src')
        revlist, _ = cr_util.build_revlist(repo, good, bad)
    else:
        repo = os.path.join(os.getenv('HOME'), 'bisect-workdir/test/chromeos')
        revlist, _ = cros_util.build_revlist(
            repo,
            good,
            bad,
            chromeos_mirror=chromeos_mirror,
            board=board,
        )

    culprit_idx = revlist.index(culprit)

    agent = BisectAgent(
        testname,
        repo,
        revlist,
        model=MODEL_GEMINI_3_5_FLASH,
        chromeos_mirror=chromeos_mirror if not is_chrome else None,
        board=board if not is_chrome else None,
        old_p=sim_old_p,
        new_p=sim_new_p,
    )
    agent.add_sample(good, 'PASS')
    agent.add_sample(bad, 'FAIL', reason=get_reason())
    stat = defaultdict(list)
    stat[good].append('PASS')
    stat[bad].append('FAIL')

    bsearch = strategy.NoisyBinarySearch(
        [core.RevInfo(x) for x in revlist],
        old_idx=0,
        new_idx=len(revlist) - 1,
        oracle=(sim_old_p, sim_new_p),
    )
    bsearch.add_sample(0, 'old')
    bsearch.add_sample(len(revlist) - 1, 'new')
    prob = bsearch.prob

    # The last run revision was 'bad' (index len(revlist) - 1)
    cost_table = _get_simulated_cost_table(
        0, len(revlist) - 1, len(revlist) - 1
    )
    # pylint: disable=protected-access
    utilities = strategy.NoisyBinarySearch._calculate_utilities(
        sim_old_p, sim_new_p, prob, cost_table=cost_table
    )

    agent.update_stats(
        {
            revlist[x]: {
                'prob': prob[x],
                'status': {
                    'PASS': stat[revlist[x]].count('PASS'),
                    'FAIL': stat[revlist[x]].count('FAIL'),
                },
                'info_gain': utilities[x][0],
                'info_gain_per_cost': utilities[x][1],
            }
            for x in range(len(revlist))
        },
        old_p=sim_old_p,
        new_p=sim_new_p,
    )
    attempt = agent.next()
    runs_llm = 0
    while attempt:
        attempt_idx = revlist.index(attempt)
        runs_llm += 1
        run_reason = None
        is_fail = (
            random.random() < sim_new_p
            if attempt_idx >= culprit_idx
            else random.random() < sim_old_p
        )
        if is_fail:
            status = 'FAIL'
            run_reason = get_reason()
        else:
            status = 'PASS'
        stat[attempt].append(status)
        agent.add_sample(attempt, status, reason=run_reason)

        bsearch.add_sample(attempt_idx, 'old' if status == 'PASS' else 'new')
        prob = bsearch.prob
        # Current revision is 'attempt_idx'
        cost_table = _get_simulated_cost_table(0, len(revlist) - 1, attempt_idx)
        # pylint: disable=protected-access
        utilities = strategy.NoisyBinarySearch._calculate_utilities(
            sim_old_p, sim_new_p, prob, cost_table=cost_table
        )

        agent.update_stats(
            {
                revlist[x]: {
                    'prob': prob[x],
                    'status': {
                        'PASS': stat[revlist[x]].count('PASS'),
                        'FAIL': stat[revlist[x]].count('FAIL'),
                    },
                    'info_gain': utilities[x][0],
                    'info_gain_per_cost': utilities[x][1],
                }
                for x in range(len(revlist))
            },
            old_p=sim_old_p,
            new_p=sim_new_p,
        )
        likelihood_new = float(sum(prob[: attempt_idx + 1]))
        attempt = agent.next(
            {
                'status': status,
                'reason': run_reason,
                'likelihood_new': likelihood_new,
            }
        )
    agent_confidence = float(max(bsearch.prob))
    bsearch_std = strategy.NoisyBinarySearch(
        [core.RevInfo(x) for x in revlist],
        old_idx=0,
        new_idx=len(revlist) - 1,
        oracle=(sim_old_p, sim_new_p),
        confidence=min(agent_confidence - 1e-5, 0.999),
    )
    bsearch_std.add_sample(0, 'old')
    bsearch_std.add_sample(len(revlist) - 1, 'new')
    runs_bisect = 0
    while not bsearch_std.is_done():
        runs_bisect += 1
        idx = bsearch_std.next_idx()
        bsearch_std.add_sample(idx, 'new' if idx >= culprit_idx else 'old')

    print(
        'Concluded culprit',
        agent.get_result(),
        'in',
        runs_llm,
        'runs instead of',
        runs_bisect,
        f'runs by bisect (confidence: {agent_confidence:.2%}).',
    )


if __name__ == '__main__':
    # PoC purporse only (should replace with unittest)
    test_logger = logging.getLogger(__name__)
    test_logger.setLevel(logging.DEBUG)
    handler = logging.StreamHandler(sys.stderr)
    handler.setLevel(logging.DEBUG)
    test_logger.addHandler(handler)
    demo(
        'tast.ui.WebUIJSErrors',
        'refs/heads/main@{#1416515}',
        'refs/heads/main@{#1416531}',
        'refs/heads/main@{#1416520}',
    )
    demo(
        'tast.a11y.Smoke',
        'refs/heads/main@{#1513012}',
        'refs/heads/main@{#1513038}',
        'refs/heads/main@{#1513018}',
    )
    demo(
        'tast.arc.RemovableMedia.vm',
        'refs/heads/main@{#1634593}',
        'refs/heads/main@{#1634613}',
        'refs/heads/main@{#1634610}',
    )
    demo(
        'tast.video.CDMOEMCrypto.ce_cdm',
        'R149-16656.0.0-118296',
        'R149-16656.0.0-118297',
        'R149-16656.0.0-118296~R149-16656.0.0-118297/111',
        board='volteer',
        sim_old_p=0.5,
        sim_new_p=1.0,
    )
