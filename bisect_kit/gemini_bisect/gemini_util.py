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
from typing import Any, TypedDict, Union

from bisect_kit import common
from bisect_kit import cr_util
from bisect_kit import cros_util
from bisect_kit import git_util
from bisect_kit import util
from google import genai
from google.api_core import retry
import google.auth
import google.auth.transport.requests
import google.oauth2.service_account


GEMINI_API_KEY_SECRET_LOCATION = (
    'https://secretmanager.googleapis.com/'
    'v1/projects/43146777051/secrets/gemini-api-key/versions/latest'
)
MODEL_GEMINI_3_6_FLASH = 'gemini-3.6-flash'
MODEL_DEFAULT = MODEL_GEMINI_3_6_FLASH

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
        "type": "function",
        "name": "get_test_name",
        "description": "Get name of the regressed test",
        "parameters": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "type": "function",
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
        "type": "function",
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
        "type": "function",
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
        "type": "function",
        "name": "read_log",
        "description": (
            "Read the test execution log or a specific collected system log "
            "(e.g. 'system_logs/syslog', 'results.json') of a particular run of a particular "
            "revision. If file_name is empty or omitted, reads default execution log and includes 'available_files'."
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
                "file_name": {
                    "type": "string",
                    "description": (
                        "Optional file name or relative path to read from the "
                        "collected test results for this run (e.g. 'system_logs/syslog', "
                        "'results.json'). Leave empty or omit for default test log."
                    ),
                },
            },
            "required": ["rev_id", "run_index"],
        },
    },
    {
        "type": "function",
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
        "type": "function",
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
    if (
        isinstance(e, genai.errors.ClientError)
        and getattr(e, 'code', None) == 429
    ):
        return True
    if (
        isinstance(e, genai.errors.ServerError)
        and getattr(e, 'code', None) == 503
    ):
        return True
    status_code = getattr(e, 'status_code', None)
    if status_code in (429, 503):
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

    def __init__(
        self,
        prompt: str,
        functions: list[dict[str, Any]],
        model: str | None = None,
    ):
        """Initializes GeminiAgent with prompt and functions.

        prompt: initial prompt to Gemini.
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
        self._tools = []
        for f in functions:
            func_dict = (
                {'type': 'function', **f} if 'type' not in f else dict(f)
            )
            if 'parameters' not in func_dict or not func_dict['parameters']:
                func_dict['parameters'] = {'type': 'object', 'properties': {}}
            self._tools.append(func_dict)
        self._generation_config = {
            'thinking_level': 'high',
            'tool_choice': 'any',
        }
        self._last_interaction_id: str | None = None

    def __deepcopy__(self, memo):
        cls = self.__class__
        result = cls.__new__(cls)
        memo[id(self)] = result
        for k, v in self.__dict__.items():
            if k in ('_client',):
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

    def start(self) -> Any:
        """Starts the agent by sending a start trigger.

        Returns the response from Gemini.
        """
        return self.reply("Start bisection investigation.")

    @retry.Retry(predicate=_is_retriable_gemini_error)
    def reply(self, reply: str | list[dict[str, Any]]) -> Any:
        """Send a reply to Gemini.

        reply: the input string or list of function results to send to Gemini.
        Returns the interaction from Gemini.
        """
        kwargs: dict[str, Any] = {
            'model': self._model,
            'input': reply,
            'system_instruction': self._prompt,
            'tools': self._tools,
            'generation_config': self._generation_config,
        }
        if self._last_interaction_id:
            kwargs['previous_interaction_id'] = self._last_interaction_id
        interaction = self._client.interactions.create(**kwargs)
        if not interaction:
            raise RuntimeError(
                f"No interaction returned from Gemini. Response: {interaction}"
            )
        self._last_interaction_id = interaction.id
        return interaction


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
        self._pending_results: list[dict[str, Any]] = []
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

    def _list_collected_files(self, eval_log: str | None) -> list[str]:
        """Lists relative paths of all files in the test results directory for a run."""
        if not eval_log:
            return []
        eval_step_id = util.extract_step_id_from_eval_log_path(eval_log)
        full_dir = util.get_eval_results_dir(
            self._session, eval_step_id=eval_step_id
        )
        files: list[str] = []
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
            if not eval_log:
                return {'error': 'Log not available for this run.'}
            eval_step_id = util.extract_step_id_from_eval_log_path(eval_log)
            sys_dir = util.get_eval_results_dir(
                self._session, eval_step_id=eval_step_id
            )
            log_full_path = os.path.abspath(os.path.join(sys_dir, file_name))
            sys_dir_abs = os.path.abspath(sys_dir)
            if os.path.commonpath([sys_dir_abs, log_full_path]) != sys_dir_abs:
                return {'error': f'Invalid file name {file_name}.'}

            if os.path.exists(log_full_path):
                log_real_path = os.path.realpath(log_full_path)
                sys_dir_real = os.path.realpath(sys_dir)
                if (
                    os.path.commonpath([sys_dir_real, log_real_path])
                    != sys_dir_real
                ):
                    return {'error': f'Invalid file name {file_name}.'}

            if not os.path.exists(log_full_path) or not os.path.isfile(
                log_full_path
            ):
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

    def _handle_read_log(
        self, rev_id: str | None, run_index: Any, file_name: str | None = None
    ) -> dict:
        """Helper to handle read_log tool execution."""
        if not rev_id or rev_id not in self._rev2commit:
            return {'error': f'Invalid rev_id {rev_id}.'}
        if run_index is None or not isinstance(run_index, int) or run_index < 0:
            return {
                'error': (
                    f'Invalid run_index {run_index}. Must be a'
                    ' non-negative integer.'
                )
            }
        if run_index >= len(self._rev_samples[rev_id]):
            return {
                'error': (
                    f'run_index {run_index} out of range for'
                    f' revision {rev_id}. Total runs:'
                    f' {len(self._rev_samples[rev_id])}.'
                )
            }

        eval_log = self._rev_samples[rev_id][run_index].get('eval_log')
        resp = self._read_log_file(eval_log, file_name=file_name)
        if not file_name or not file_name.strip():
            resp['available_files'] = self._list_collected_files(eval_log)
        return resp

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
                for item in self._pending_results:
                    self.logger.info(
                        'Sending response to Gemini: %s',
                        self._short(item.get('result')),
                    )
                ret = self._agent.reply(self._pending_results)
                self._pending_results = []
            function_calls = [
                s for s in (ret.steps or []) if s.type == 'function_call'
            ]
            text = getattr(ret, 'output_text', '') or ''
            if not function_calls:
                raise RuntimeError(
                    f"Gemini failed to return any function calls. "
                    f"Response text: {text}. Response: {ret}"
                )

            self.logger.info(
                'Gemini replied %s, calling %s',
                self._short(text),
                ', '.join(
                    [
                        f'{call.name}({self._short(call.arguments)})'
                        for call in function_calls
                    ]
                ),
            )

            run_at_call = None
            conclude_call = None

            for call in function_calls:
                call_args = call.arguments or {}

                if call.name == 'get_test_name':
                    self._pending_results.append(
                        {
                            'type': 'function_result',
                            'call_id': call.id,
                            'name': 'get_test_name',
                            'result': {'name': self._test_name},
                        }
                    )
                elif call.name == 'get_range':
                    include_reason = call_args.get('include_reason', False)
                    self._pending_results.append(
                        {
                            'type': 'function_result',
                            'call_id': call.id,
                            'name': 'get_range',
                            'result': {
                                'revisions': self._get_revisions(
                                    include_reason
                                ),
                                'old_p': self._old_p,
                                'new_p': self._new_p,
                            },
                        }
                    )
                elif call.name == 'get_revision_detail':
                    rev_id = call_args.get('rev_id')
                    if not rev_id or rev_id not in self._rev2commit:
                        self.logger.warning(
                            'Gemini requested detail for invalid rev_id: %s',
                            rev_id,
                        )
                        self._pending_results.append(
                            {
                                'type': 'function_result',
                                'call_id': call.id,
                                'name': 'get_revision_detail',
                                'result': {
                                    'error': (
                                        f'Invalid rev_id {rev_id}. '
                                        'Please use a rev_id from '
                                        'get_range.'
                                    )
                                },
                            }
                        )
                    else:
                        self._pending_results.append(
                            {
                                'type': 'function_result',
                                'call_id': call.id,
                                'name': 'get_revision_detail',
                                'result': self._get_revision_detail(rev_id),
                            }
                        )
                elif call.name == 'read_last_log':
                    file_name = call_args.get('file_name')
                    resp = self._read_log_file(
                        self._last_run_eval_log,
                        file_name=file_name,
                    )
                    if not file_name or not file_name.strip():
                        resp['available_files'] = self._list_collected_files(
                            self._last_run_eval_log
                        )
                    self._pending_results.append(
                        {
                            'type': 'function_result',
                            'call_id': call.id,
                            'name': 'read_last_log',
                            'result': resp,
                        }
                    )
                elif call.name == 'read_log':
                    resp = self._handle_read_log(
                        call_args.get('rev_id'),
                        call_args.get('run_index'),
                        file_name=call_args.get('file_name'),
                    )
                    self._pending_results.append(
                        {
                            'type': 'function_result',
                            'call_id': call.id,
                            'name': 'read_log',
                            'result': resp,
                        }
                    )
                elif call.name == 'run_at':
                    rev_id = call_args.get('rev_id')
                    if not rev_id or rev_id not in self._rev2commit:
                        self.logger.warning(
                            'Gemini requested run_at for invalid rev_id: %s',
                            rev_id,
                        )
                        self._pending_results.append(
                            {
                                'type': 'function_result',
                                'call_id': call.id,
                                'name': 'run_at',
                                'result': {
                                    'error': (
                                        f'Invalid rev_id {rev_id}. '
                                        'Please use a rev_id from '
                                        'get_range.'
                                    )
                                },
                            }
                        )
                    else:
                        run_at_call = call
                elif call.name == 'conclude':
                    rev_id = call_args.get('rev_id')
                    if not rev_id or rev_id not in self._rev2commit:
                        self.logger.warning(
                            'Gemini requested conclude for invalid rev_id: %s',
                            rev_id,
                        )
                        self._pending_results.append(
                            {
                                'type': 'function_result',
                                'call_id': call.id,
                                'name': 'conclude',
                                'result': {
                                    'error': (
                                        f'Invalid rev_id {rev_id}. '
                                        'Please use a rev_id from '
                                        'get_range.'
                                    )
                                },
                            }
                        )
                    else:
                        conclude_call = call
                else:
                    raise NotImplementedError(
                        f"Unsupported function call: {call.name}"
                    )

            if conclude_call is not None:
                assert conclude_call.arguments is not None
                self._conclusion = conclude_call.arguments['rev_id']
                self._pending_results.append(
                    {
                        'type': 'function_result',
                        'call_id': conclude_call.id,
                        'name': 'conclude',
                        'result': {'status': 'concluded'},
                    }
                )
                return None

            if run_at_call is not None:
                assert run_at_call.arguments is not None
                self._pending_run_at_call = run_at_call
                return run_at_call.arguments['rev_id']

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
            response: dict[str, Any] = {
                'status': status['status'],
                'reason': status.get('reason'),
            }
            if 'likelihood_new' in status:
                response['likelihood_new'] = status['likelihood_new']
            call_id = (
                getattr(self._pending_run_at_call, 'id', None)
                if not isinstance(self._pending_run_at_call, dict)
                else self._pending_run_at_call.get('id')
            )
            self._pending_results.append(
                {
                    'type': 'function_result',
                    'call_id': call_id,
                    'name': 'run_at',
                    'result': response,
                }
            )
            self._pending_run_at_call = None
        return self._run()

    def get_result(self) -> str:
        """Returns the concluded culprit revision."""
        return self._conclusion
