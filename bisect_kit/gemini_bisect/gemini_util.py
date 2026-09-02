# Copyright 2025 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Gemini utilities."""

from __future__ import annotations

import base64
from collections import defaultdict
import copy
import dataclasses
import logging
import os
from typing import Any, Callable, TypedDict, TypeVar, Union

from bisect_kit import common
from bisect_kit import cr_util
from bisect_kit import cros_util
from bisect_kit import git_util
from bisect_kit import math_util
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
MODEL_GEMINI_3_7_FLASH = 'gemini-3.7-flash'
MODEL_DEFAULT = MODEL_GEMINI_3_7_FLASH
MODEL_PRICING: dict[str, tuple[float, float, float]] = {
    'gemini-3.7-flash': (0.75, 0.075, 3.75),
}

_PROMPT_TEMPLATE = (
    "You're a Software Engineer investigating {domain}.\n"
    '\n'
    "{intro} "
    "You're a smart engineer and want to do faster than simple bisect, "
    'by reading changes, commits, and diffs within the {range_name} to '
    'find better bisection point than midpoint at each step.\n'
    '\n'
    'Tests may be flaky. The get_range call returns flaky rates "old_p" (the '
    'probability of observing {term_new}/new behavior on a {term_old}/old commit) and '
    '"new_p" (the probability of observing {term_new}/new behavior on a {term_new}/new '
    'commit). Because tests can be flaky, you may choose to run a single commit '
    'multiple times (calling run_at on the same revision again) or test the same '
    'commit later to gain higher statistical confidence. Note that when "old_p" '
    'is non-zero, a {term_old}/old commit can hit an unlucky streak of {term_new_plural}, causing '
    '"likelihood_new" to climb high (e.g. 80-90%). Conversely, when "new_p" is '
    'non-100% (less than 1.0), a {term_new}/new commit can hit an unlucky streak of '
    '{term_old_plural}, causing "likelihood_new" to drop low (e.g. 10-20%). '
    'When "old_p" is zero, a single {term_old} is strong proof that the commit is '
    '{term_old}/old; conversely, when "new_p" is 1.0, a single {term_new} is strong '
    'proof that the commit is {term_new}/new. Therefore, do not blindly trust a '
    'skewed likelihood_new after only a few runs; if you suspect a boundary commit might '
    'be flaky, re-test it on the device (skips rebuild, though still reflashes!) to confirm '
    'its outcome.\n'
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
    'run_results with dictionaries containing the test outcome "result" (e.g., '
    '"PASS", "FAIL"), bisection "status" ("old", "new"), and the test '
    'failure reason (representing the error logs or timeout output from the '
    'test execution if it failed).\n'
    '\n'
    'The run_at function returns a dictionary containing the test outcome "result" '
    '(e.g., "PASS", "FAIL"), bisection "status" ("old", "new"), optional "reason", '
    'and "likelihood_new" (a float between 0.0 and 1.0 '
    'representing the likelihood of this commit having the "new/{term_new}" '
    'behavior based on all past test run history; note that this is NOT the '
    'probability of this commit being the {target_name}, but rather the cumulative '
    'probability that the {target_name} is at or before this commit).\n'
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
    'a commit that obviously {diff_action}), the probability model '
    'can be ignored, and you can test or conclude that commit directly.\n'
    '- If you have zero or low confidence in your code analysis, you '
    'should move a step forward with the probability model (e.g., choose '
    'the revision with high info_gain_per_cost or info_gain) to narrow '
    'down the ranges, which can be better.\n'
    '\n'
    'Speculative parallel builds:\n'
    'Running a test on a physical DUT takes 15-30 minutes, whereas building ChromeOS takes 30-60 minutes. '
    'To avoid idle wait time between iterations, whenever you call run_at, you MUST provide "future_build_tree" '
    'specifying candidate commits to pre-build in parallel on remote builders while this run executes on the DUT. '
    'You should decide the depth and breadth of the tree based on your reasoning and confidence: provide predictions '
    'for the immediate "PASS" and "FAIL" outcomes (unless a conclusion can be made directly after that result, '
    'in which case that branch should be omitted), include your estimated conditional "confidence" (a float between '
    '0.0 and 1.0 representing the conditional probability of reaching this branch given its parent node was reached, '
    'such that sibling branches at each layer add up to 1.0), and arbitrarily deep nested "next" outcomes as appropriate. '
    'If you have no speculative preference for a branch, use the candidate with high info_gain / math utility for that branch. '
    'You can call get_revisions_order_by_utility (with depth=2 or depth=3) to automatically compute optimal candidates '
    'and retrieve a speculative future decision tree.\n'
    'Example:\n'
    '{{\n'
    '  "FAIL": {{\n'
    '    "next_revision": "<rev_id>",\n'
    '    "reason": "<short explanation why this revision is best if current test fails>",\n'
    '    "confidence": 0.8,\n'
    '    "next": {{\n'
    '      "FAIL": {{"next_revision": "<rev_id>", "reason": "...", "confidence": 0.7}},\n'
    '      "PASS": {{"next_revision": "<rev_id>", "reason": "...", "confidence": 0.3}}\n'
    '    }}\n'
    '  }},\n'
    '  "PASS": {{\n'
    '    "next_revision": "<rev_id>",\n'
    '    "reason": "<short explanation why this revision is best if current test passes>",\n'
    '    "confidence": 0.2,\n'
    '    "next": {{\n'
    '      "FAIL": {{"next_revision": "<rev_id>", "reason": "...", "confidence": 0.5}},\n'
    '      "PASS": {{"next_revision": "<rev_id>", "reason": "...", "confidence": 0.5}}\n'
    '    }}\n'
    '  }}\n'
    '}}\n'
    '\n'
    'You have external functions available to use. '
    'You can call multiple independent query functions in parallel in a '
    'single turn (e.g., get_revision_detail, get_test_name, '
    'get_range, get_revisions_order_by_utility, read_last_log, read_log). However, run_at and conclude are blocking, final '
    'actions that MUST only be called individually and cannot be called '
    'in parallel with any other function calls.'
)


@dataclasses.dataclass(frozen=True)
class TemplateReplacements:
    """Replacements table for prompt and function templates."""

    domain: str
    intro: str
    range_name: str
    term_new: str
    term_new_plural: str
    term_old: str
    term_old_plural: str
    target_name: str
    diff_action: str
    test_type: str
    old_quality: str
    new_quality: str
    behavior_noun: str
    action_desc: str


FIX_REPLACEMENTS = TemplateReplacements(
    domain='test fixes',
    intro=(
        'You have a test fix to investigate (finding the commit that '
        'resolved the test failure).'
    ),
    range_name='candidate range',
    term_new='PASS',
    term_new_plural='PASSes',
    term_old='FAIL',
    term_old_plural='FAILs',
    target_name='fix CL',
    diff_action='fixes the test',
    test_type='fixed',
    old_quality='bad',
    new_quality='good',
    behavior_noun='success',
    action_desc='fixes the test failure',
)

REGRESSION_REPLACEMENTS = TemplateReplacements(
    domain='test regressions',
    intro='You have a failed test to investigate.',
    range_name='culprit range',
    term_new='FAIL',
    term_new_plural='FAILs',
    term_old='PASS',
    term_old_plural='PASSes',
    target_name='culprit',
    diff_action='breaks the test',
    test_type='regressed',
    old_quality='good',
    new_quality='bad',
    behavior_noun='failure',
    action_desc='introduces the regression',
)


_T = TypeVar('_T')


def format_template(template: _T, replacements: TemplateReplacements) -> _T:
    """Recursively formats strings within a template using a TemplateReplacements dataclass."""
    mapping = dataclasses.asdict(replacements)
    if isinstance(template, str):
        return template.format(**mapping)  # type: ignore[return-value]
    if isinstance(template, list):
        return [format_template(item, replacements) for item in template]  # type: ignore[return-value]
    if isinstance(template, dict):
        return {
            k: format_template(v, replacements) for k, v in template.items()
        }  # type: ignore[return-value]
    return template


_FUNCTIONS_TEMPLATE = [
    {
        "type": "function",
        "name": "get_test_name",
        "description": "Get name of the {test_type} test",
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
            "for these revisions from known {old_quality} to known {new_quality} (aka oldest to "
            "newest), inclusive on both ends. Returns a dictionary containing "
            "'revisions' (list of dicts), 'old_p' (flaky {behavior_noun} probability on "
            "{old_quality} commits), and 'new_p' ({behavior_noun} probability on {new_quality} commits).\n"
            "Each revision dict contains:\n"
            "- 'rev_id': unique string to identify a revision.\n"
            "- 'subject': first line of commit message.\n"
            "- 'run_results': history of past test runs on this revision. "
            "By default, this is a list of result strings (e.g., 'PASS', 'FAIL'). "
            "If include_reason=True is passed, this will instead be a "
            "list of dictionaries containing 'result', 'status', and 'reason' "
            "(the test's failure output or error log if it failed).\n"
            "- 'stats': dict of probability model parameters:\n"
            "  - 'prob': the probability that this revision is the {target_name} "
            "(the first revision that {action_desc}).\n"
            "  - 'info_gain': the expected decrease in entropy "
            "(uncertainty) of the {range_name} if you test this revision.\n"
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
        "name": "get_revisions_order_by_utility",
        "description": (
            "Query revisions ordered by mathematical utility / information gain "
            "(expected decrease in entropy/uncertainty), highest utility first, "
            "and optionally predict future speculative decision trees for run_at.\n"
            "Returns a dictionary with 'revisions' (list of revision dicts with 'rev_id', 'subject', "
            "'run_results', and 'stats' containing 'prob', 'info_gain', 'info_gain_per_cost') "
            "and optionally 'future_build_tree' if depth > 0.\n"
            "Key capabilities:\n"
            "1. Choose best utility commit instead of center when unsure: call with limit=1 (or without args).\n"
            "2. Choose best utility between candidate suspects (with current prob): pass revisions=['revA', 'revB'].\n"
            "3. Find which commit across the entire range has highest hypothetic max utility when only certain commits are suspected: "
            "pass suspects=['revA', 'revB'] (sets prior probability 0 on non-suspects).\n"
            "4. Generate speculative future build tree for run_at: pass depth=2 (or depth=3)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": (
                        "Maximum number of revisions to return in 'revisions' (e.g., 1 to get only "
                        "the single revision with the highest utility). If omitted, returns all matching revisions."
                    ),
                },
                "revisions": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Optional list of candidate rev_ids to evaluate and rank. If specified, only "
                        "revisions within this list will be evaluated and returned in 'revisions'."
                    ),
                },
                "suspects": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Optional list of suspected culprit rev_ids. When specified, sets a hypothetical "
                        "prior probability distribution where ONLY these revisions have non-zero probability "
                        "(probability is 0 for all other revisions), and evaluates information gain across all "
                        "commits to find which test point best discriminates between these suspects."
                    ),
                },
                "depth": {
                    "type": "integer",
                    "description": (
                        "Optional lookahead depth for future prediction tree (e.g., 1, 2, or 3). "
                        "If depth > 0, the returned dictionary includes 'future_build_tree' formatted "
                        "for direct use in run_at. Defaults to 0 (no future tree)."
                    ),
                },
                "root_revision": {
                    "type": "string",
                    "description": (
                        "Optional root rev_id to build the future_build_tree for. If omitted, "
                        "defaults to the top revision with highest utility."
                    ),
                },
                "simulated_runs": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "rev_id": {
                                "type": "string",
                                "description": "Revision ID of hypothetical test run.",
                            },
                            "result": {
                                "type": "string",
                                "description": (
                                    "Hypothetical test outcome for this revision "
                                    "(e.g. 'PASS', 'FAIL', 'old', 'new')."
                                ),
                            },
                        },
                        "required": ["rev_id", "result"],
                    },
                    "description": (
                        "Optional list of hypothetical test results to simulate updating "
                        "the probability model before ordering revisions by utility."
                    ),
                },
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
            "Run the test at given commit and specify a speculative decision "
            "tree of future candidate commits to pre-build in parallel.\n"
            "Returns a dictionary containing the test outcome 'result' (e.g., 'PASS', 'FAIL'), "
            "the bisection 'status' ('old', 'new'), "
            "optionally a 'reason' key containing the test failure output or error "
            "log if the run failed, and 'likelihood_new' (a float between 0.0 and 1.0) "
            "representing the likelihood of this commit having the 'new/{term_new}' "
            "behavior based on all past test run history (the cumulative "
            "probability that the {target_name} is at or before this commit; this is "
            "NOT the probability of this commit itself being the {target_name})."
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
                "future_build_tree": {
                    "type": "object",
                    "description": (
                        "Hierarchical speculative decision tree of next commits to pre-build in "
                        "parallel while this run executes. Keys are possible outcomes ('PASS', 'FAIL', etc.). "
                        "Each branch contains 'next_revision' (rev_id string), 'reason', 'confidence' (float 0.0-1.0), "
                        "and optional recursive 'next' sub-tree to arbitrary depth. If a conclusion can be made "
                        "immediately upon observing a specific outcome, that branch can be omitted or empty."
                    ),
                    "properties": {
                        "PASS": {
                            "type": "object",
                            "description": (
                                "Speculative branch if current test PASSes (omit if a conclusion can be "
                                "made directly when this test PASSes)."
                            ),
                            "properties": {
                                "next_revision": {
                                    "type": "string",
                                    "description": "rev_id to test if current run PASSes.",
                                },
                                "reason": {
                                    "type": "string",
                                    "description": "Explanation for why this rev_id is chosen if current run PASSes.",
                                },
                                "confidence": {
                                    "type": "number",
                                    "description": (
                                        "Conditional probability/confidence (float between 0.0 and 1.0) "
                                        "that the test will result in PASS given this parent branch was reached. "
                                        "Sibling branches at the same depth level should add up to 1.0."
                                    ),
                                },
                                "next": {
                                    "type": "object",
                                    "description": "Optional nested decision tree of further speculative outcomes to arbitrary depth.",
                                },
                            },
                            "required": [
                                "next_revision",
                                "reason",
                                "confidence",
                            ],
                        },
                        "FAIL": {
                            "type": "object",
                            "description": (
                                "Speculative branch if current test FAILs (omit if a conclusion can be "
                                "made directly when this test FAILs)."
                            ),
                            "properties": {
                                "next_revision": {
                                    "type": "string",
                                    "description": "rev_id to test if current run FAILs.",
                                },
                                "reason": {
                                    "type": "string",
                                    "description": "Explanation for why this rev_id is chosen if current run FAILs.",
                                },
                                "confidence": {
                                    "type": "number",
                                    "description": (
                                        "Conditional probability/confidence (float between 0.0 and 1.0) "
                                        "that the test will result in FAIL given this parent branch was reached. "
                                        "Sibling branches at the same depth level should add up to 1.0."
                                    ),
                                },
                                "next": {
                                    "type": "object",
                                    "description": "Optional nested decision tree of further speculative outcomes to arbitrary depth.",
                                },
                            },
                            "required": [
                                "next_revision",
                                "reason",
                                "confidence",
                            ],
                        },
                    },
                },
            },
            "required": ["rev_id", "reason", "future_build_tree"],
        },
    },
    {
        "type": "function",
        "name": "conclude",
        "description": (
            "Make conclusion on the {target_name}. The conclusion must be "
            "verified on the {target_name} and its previous commit."
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
        assert self.extra is not None
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

    result: str | None
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


def _format_tokens(n: int) -> str:
    """Format token count into compact human readable string (e.g. 500, 1.2k, 12k, 1.5M)."""
    if n >= 1_000_000:
        return f'{n / 1_000_000:.1f}M'.replace('.0M', 'M')
    if n >= 1_000:
        return f'{n / 1_000:.1f}k'.replace('.0k', 'k')
    return str(n)


def _format_cost(cost: float) -> str:
    """Format cost in USD into a concise string."""
    if cost <= 0:
        return '$0'
    if cost >= 1.0:
        return f'${cost:.2f}'
    if cost >= 0.01:
        return f'${cost:.3f}'.rstrip('0').rstrip('.')
    if cost >= 0.0001:
        return f'${cost:.4f}'.rstrip('0').rstrip('.')
    return f'${cost:.5f}'.rstrip('0').rstrip('.')


def estimate_gemini_token_cost(
    model: str | None,
    input_tokens: int,
    cached_tokens: int,
    output_tokens: int,
) -> float | None:
    """Estimates the USD cost of a Gemini interaction based on token counts.

    Returns None if the model's pricing is unknown.
    """
    model_name = (model or '').lower()
    if model_name not in MODEL_PRICING:
        return None
    p_input, p_cached, p_output = MODEL_PRICING[model_name]
    uncached_input = max(0, input_tokens - cached_tokens)
    return (
        uncached_input * p_input
        + cached_tokens * p_cached
        + output_tokens * p_output
    ) / 1_000_000


def _update_prob_vector(
    old_p: float,
    new_p: float,
    prob: list[float],
    idx: int,
    is_new: bool,
) -> list[float]:
    """Updates a probability vector in-memory given an observation at idx."""
    new_prob = list(prob)
    if is_new:
        for i in range(idx + 1):
            new_prob[i] *= new_p
        for i in range(idx + 1, len(new_prob)):
            new_prob[i] *= old_p
    else:
        for i in range(idx + 1):
            new_prob[i] *= 1.0 - new_p
        for i in range(idx + 1, len(new_prob)):
            new_prob[i] *= 1.0 - old_p
    s = sum(new_prob)
    if s <= 0:
        return list(prob)
    return [p / s for p in new_prob]


_UNSET = object()


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
        term_old: str = 'PASS',
        term_new: str = 'FAIL',
    ):
        """Initializes BisectAgent.

        test_name: Name of the bisected test."""
        self._model = model or MODEL_DEFAULT
        self._term_old = term_old
        self._term_new = term_new
        replacements = (
            FIX_REPLACEMENTS
            if term_old == 'FAIL' and term_new == 'PASS'
            else REGRESSION_REPLACEMENTS
        )
        prompt = format_template(_PROMPT_TEMPLATE, replacements)
        functions = format_template(_FUNCTIONS_TEMPLATE, replacements)
        self._agent = GeminiAgent(
            prompt=prompt, functions=functions, model=self._model
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
        self._last_future_build_tree: dict[str, Any] = {}
        self._total_tokens_used: int = 0
        self._total_input_tokens: int = 0
        self._total_cached_tokens: int = 0
        self._total_cost: float | None = (
            0.0 if (self._model.lower() in MODEL_PRICING) else None
        )
        self._cost_func: (
            Callable[[str | None], list[tuple[float, float]] | None] | None
        ) = None

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

    def _get_revision_info(self, x: str, include_reason: bool = False) -> dict:
        """Returns the revision dict for a given revision ID."""
        run_results: list[str] | list[TrialResult]
        if include_reason:
            run_results = [
                TrialResult(
                    result=sample.get('result') or sample['status'],
                    status=sample['status'],
                    reason=sample.get('reason'),
                )
                for sample in self._rev_samples[x]
            ]
        else:
            run_results = [
                sample.get('result') or sample['status']
                for sample in self._rev_samples[x]
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
        return {
            'rev_id': x,
            'repo': self._rev2commit[x]['repo'],
            'subject': subject,
            'run_results': run_results,
            'stats': self._rev_stats.get(x),
        }

    def _get_revisions(self, include_reason: bool = False) -> list[dict]:
        """Returns the revlist and their subjects."""
        return [
            self._get_revision_info(x, include_reason=include_reason)
            for x in self._revlist
        ]

    def _get_revision_utility(self, rev_id: str) -> float:
        """Gets utility (info_gain_per_cost or info_gain) for a revision."""
        stats = self._rev_stats.get(rev_id)
        if stats:
            if stats.get('info_gain_per_cost') is not None:
                return float(stats['info_gain_per_cost'])
            if stats.get('info_gain') is not None:
                return float(stats['info_gain'])
        return float('-inf')

    def _get_current_prob_vector(self) -> list[float]:
        """Gets current probability vector across self._revlist."""
        probs = []
        for x in self._revlist:
            stats = self._rev_stats.get(x)
            p = stats.get('prob') if stats else None
            probs.append(float(p) if p is not None else 0.0)
        s = sum(probs)
        if s > 0:
            return [p / s for p in probs]
        n = len(self._revlist)
        if n <= 1:
            return [1.0] * n
        return [0.0] + [1.0 / (n - 1)] * (n - 1)

    def _is_new_result(self, result: str) -> bool:
        """Determines if a test outcome string indicates new behavior."""
        res_upper = str(result).strip().upper()
        if res_upper in ('NEW', self._term_new.upper()):
            return True
        if res_upper in ('OLD', self._term_old.upper()):
            return False
        return res_upper == self._term_new.upper()

    def _get_cost_table(
        self, dut_rev: str | None = None
    ) -> list[tuple[float, float]]:
        """Generates dynamic cost table for a given DUT revision."""
        if self._cost_func is not None:
            table = self._cost_func(dut_rev)
            if table:
                if len(table) == len(self._revlist):
                    return table
                if len(table) == len(self._rev_stats):
                    rev_to_idx = {
                        rev: i for i, rev in enumerate(self._rev_stats.keys())
                    }
                    if all(r in rev_to_idx for r in self._revlist):
                        return [table[rev_to_idx[r]] for r in self._revlist]
        self.logger.error(
            'No valid cost function available for dut_rev=%s; '
            'falling back to uniform unit cost',
            dut_rev,
        )
        return [(1.0, 1.0)] * len(self._revlist)

    def _calculate_utilities_for_prob(
        self, prob: list[float], dut_rev: str | None = None
    ) -> list[tuple[float, float]]:
        """Calculates information gain and utility for each revision index given prob vector."""
        cost_table = self._get_cost_table(dut_rev)
        orig_stats = math_util.EntropyStats([float(p) for p in prob])
        orig_entropy = orig_stats.entropy()
        new_stats = orig_stats.multiply(self._old_p)
        old_stats = orig_stats.multiply(1.0 - self._old_p)

        results = []
        s = 0.0
        for i, p in enumerate(prob):
            old_stats.replace(p * (1.0 - self._old_p), p * (1.0 - self._new_p))
            new_stats.replace(p * self._old_p, p * self._new_p)
            s += p
            r = s * self._new_p + (1.0 - s) * self._old_p
            entropy = r * new_stats.entropy() + (1.0 - r) * old_stats.entropy()
            information_gain = orig_entropy - entropy
            cost = r * cost_table[i][1] + (1.0 - r) * cost_table[i][0]
            utility = (information_gain / cost) if cost > 0 else 0.0
            results.append((information_gain, utility))

        return results

    def _simulate_prob_vector(
        self,
        current_prob: list[float],
        simulated_runs: list[dict[str, Any]] | None,
    ) -> list[float]:
        """Simulates updating prob vector given hypothetical runs."""
        prob = list(current_prob)
        if not simulated_runs:
            return prob
        for run in simulated_runs:
            if not isinstance(run, dict):
                continue
            rev_id = run.get('rev_id')
            result = run.get('result') or run.get('status')
            if not rev_id or rev_id not in self._rev2commit or not result:
                continue
            idx = self._revlist.index(rev_id)
            is_new = self._is_new_result(str(result))
            prob = _update_prob_vector(
                self._old_p, self._new_p, prob, idx, is_new=is_new
            )
        return prob

    def _get_revisions_order_by_utility(
        self,
        limit: int | None = None,
        revisions: list[str] | None = None,
        suspects: list[str] | None = None,
        depth: int = 0,
        root_revision: str | None = None,
        include_reason: bool = False,
        simulated_runs: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Returns revisions ordered by utility descending and optional future decision tree."""
        prob = self._get_current_prob_vector()

        if suspects is not None:
            suspect_set = set(suspects)
            suspect_indices = [
                i for i, r in enumerate(self._revlist) if r in suspect_set
            ]
            if not suspect_indices:
                return {
                    'error': 'No valid revisions found in suspects.',
                    'revisions': [],
                }
            sum_s = sum(prob[i] for i in suspect_indices)
            if sum_s > 1e-9:
                prob = [
                    (prob[i] / sum_s if i in suspect_indices else 0.0)
                    for i in range(len(self._revlist))
                ]
            else:
                prob = [
                    (
                        1.0 / len(suspect_indices)
                        if i in suspect_indices
                        else 0.0
                    )
                    for i in range(len(self._revlist))
                ]

        if simulated_runs:
            prob = self._simulate_prob_vector(prob, simulated_runs)

        dut_rev = None
        if simulated_runs:
            for run in reversed(simulated_runs):
                if (
                    isinstance(run, dict)
                    and run.get('rev_id') in self._rev2commit
                ):
                    dut_rev = run.get('rev_id')
                    break

        utilities = self._calculate_utilities_for_prob(prob, dut_rev=dut_rev)
        util_map = {
            self._revlist[i]: {
                'prob': prob[i],
                'info_gain': utilities[i][0],
                'info_gain_per_cost': utilities[i][1],
            }
            for i in range(len(self._revlist))
        }

        if revisions is not None:
            rev_set = set(revisions)
            target_revs = [x for x in self._revlist if x in rev_set]
        else:
            target_revs = list(self._revlist)

        sorted_revs = sorted(
            target_revs,
            key=lambda x: util_map[x]['info_gain_per_cost'],
            reverse=True,
        )

        limited_revs = sorted_revs
        if limit is not None and limit >= 0:
            limited_revs = sorted_revs[:limit]

        rev_dicts = []
        for x in limited_revs:
            info = self._get_revision_info(x, include_reason=include_reason)
            info['stats'] = util_map[x]
            rev_dicts.append(info)

        result: dict[str, Any] = {'revisions': rev_dicts}

        if depth and depth > 0:
            root = root_revision
            if not root:
                if sorted_revs:
                    root = sorted_revs[0]
            if root and root in self._revlist:
                max_depth = max(1, min(depth, 5))
                tree = self._build_future_utility_tree(
                    root, prob, cur_depth=1, max_depth=max_depth
                )
                result['future_build_tree'] = tree
            else:
                result['future_build_tree'] = {}

        return result

    def _build_future_utility_tree(
        self,
        cur_rev: str,
        cur_prob: list[float],
        cur_depth: int,
        max_depth: int,
    ) -> dict[str, Any]:
        """Recursively builds future prediction tree for cur_rev."""
        if cur_depth > max_depth or cur_rev not in self._revlist:
            return {}
        idx = self._revlist.index(cur_rev)
        tree: dict[str, Any] = {}

        # Branch outcomes: term_new and term_old
        for is_new, outcome in [
            (True, self._term_new),
            (False, self._term_old),
        ]:
            s = sum(cur_prob[: idx + 1])
            cond_prob = (
                (s * self._new_p + (1.0 - s) * self._old_p)
                if is_new
                else (1.0 - (s * self._new_p + (1.0 - s) * self._old_p))
            )
            branch_prob = _update_prob_vector(
                self._old_p, self._new_p, cur_prob, idx, is_new=is_new
            )
            # When evaluating candidates after testing cur_rev, cur_rev is currently installed on DUT
            utilities = self._calculate_utilities_for_prob(
                branch_prob, dut_rev=cur_rev
            )
            best_i = None
            best_val = float('-inf')
            for i, (_, u) in enumerate(utilities):
                if u > best_val:
                    best_val = u
                    best_i = i

            if best_i is not None:
                next_rev = self._revlist[best_i]
                node: dict[str, Any] = {
                    'next_revision': next_rev,
                    'confidence': round(cond_prob, 4),
                    'reason': (
                        f'Highest utility ({best_val:.3f}) revision if '
                        f'{cur_rev} is {outcome}'
                    ),
                    'stats': {
                        'prob': round(branch_prob[best_i], 4),
                        'info_gain': round(utilities[best_i][0], 4),
                        'info_gain_per_cost': round(utilities[best_i][1], 4),
                    },
                }
                if cur_depth < max_depth and max(branch_prob) < 0.999:
                    sub_tree = self._build_future_utility_tree(
                        next_rev, branch_prob, cur_depth + 1, max_depth
                    )
                    if sub_tree:
                        node['next'] = sub_tree
                tree[outcome] = node

        return tree

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

            usage_str = ''
            if ret.usage:
                round_input = ret.usage.total_input_tokens or 0
                round_total = ret.usage.total_tokens or 0
                round_cached = ret.usage.total_cached_tokens or 0
                round_output = getattr(
                    ret.usage, 'total_output_tokens', None
                ) or max(0, round_total - round_input)
                round_cache_rate = (
                    (round_cached / round_input) if round_input else 0.0
                )
                round_cost = estimate_gemini_token_cost(
                    self._model,
                    round_input,
                    round_cached,
                    round_output,
                )

                self._total_input_tokens += round_input
                self._total_cached_tokens += round_cached
                self._total_tokens_used += round_total
                accumulative_cache_rate = (
                    (self._total_cached_tokens / self._total_input_tokens)
                    if self._total_input_tokens
                    else 0.0
                )

                cost_str = ''
                acc_cost_str = ''
                if round_cost is not None:
                    self._total_cost = (self._total_cost or 0.0) + round_cost
                    cost_str = f', {_format_cost(round_cost)}'
                    acc_cost_str = f', Acc{_format_cost(self._total_cost)}'

                usage_str = (
                    f' (I{_format_tokens(round_input)}, '
                    f'T{_format_tokens(round_total)}, '
                    f'C{round_cache_rate * 100:.0f}%'
                    f'{cost_str}, '
                    f'AccT{_format_tokens(self._total_tokens_used)}, '
                    f'AccC{accumulative_cache_rate * 100:.0f}%'
                    f'{acc_cost_str})'
                )

            self.logger.info(
                'Gemini replied %s, calling %s%s',
                self._short(text),
                ', '.join(
                    [
                        f'{call.name}({self._short(call.arguments)})'
                        for call in function_calls
                    ]
                ),
                usage_str,
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
                elif call.name == 'get_revisions_order_by_utility':
                    limit = call_args.get('limit')
                    revisions = call_args.get('revisions')
                    suspects = call_args.get('suspects')
                    depth = call_args.get('depth', 0)
                    root_revision = call_args.get('root_revision')
                    include_reason = call_args.get('include_reason', False)
                    simulated_runs = (
                        call_args.get('simulated_runs')
                        or call_args.get('simulated_results')
                        or call_args.get('assumed_runs')
                    )
                    util_res = self._get_revisions_order_by_utility(
                        limit=limit,
                        revisions=revisions,
                        suspects=suspects,
                        depth=depth,
                        root_revision=root_revision,
                        include_reason=include_reason,
                        simulated_runs=simulated_runs,
                    )
                    self._pending_results.append(
                        {
                            'type': 'function_result',
                            'call_id': call.id,
                            'name': 'get_revisions_order_by_utility',
                            'result': util_res,
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
                self._last_future_build_tree = (
                    run_at_call.arguments.get('future_build_tree') or {}
                )
                return run_at_call.arguments['rev_id']

    @property
    def future_build_tree(self) -> dict[str, Any]:
        """Returns the last future build tree suggested by Gemini."""
        return self._last_future_build_tree

    @property
    def total_tokens_used(self) -> int:
        """Returns the total number of tokens used across all Gemini interactions."""
        return self._total_tokens_used

    @property
    def total_input_tokens(self) -> int:
        """Returns the total number of input tokens across all Gemini interactions."""
        return self._total_input_tokens

    @property
    def total_cached_tokens(self) -> int:
        """Returns the total number of cached tokens across all Gemini interactions."""
        return self._total_cached_tokens

    @property
    def accumulative_cache_rate(self) -> float:
        """Returns the cumulative cache rate across all Gemini interactions."""
        if not self._total_input_tokens:
            return 0.0
        return self._total_cached_tokens / self._total_input_tokens

    @property
    def total_cost(self) -> float | None:
        """Returns the total estimated USD cost across all Gemini interactions."""
        return self._total_cost

    def add_sample(
        self,
        rev: str,
        status: str,
        result: str | None = None,
        reason: str | None = None,
        eval_log: str | None = None,
    ):
        """Add run result

        rev: revision
        status: status of a run ('old', 'new', etc.).
        result: mapped test result ('PASS', 'FAIL', etc.).
        """
        if result is None:
            result = status
        self._rev_samples[rev].append(
            {
                'result': result,
                'status': status,
                'reason': reason,
                'eval_log': eval_log,
            }
        )

    def update_stats(
        self,
        stats: dict[str, ProbModelResultType],
        old_p: float | None = None,
        new_p: float | None = None,
        cost_func: (
            Callable[[str | None], list[tuple[float, float]] | None]
            | None
            | object
        ) = _UNSET,
    ):
        """Updates prob model stats

        stats: new stats."""
        self._rev_stats = stats
        if old_p is not None:
            self._old_p = old_p
        if new_p is not None:
            self._new_p = new_p
        if cost_func is not _UNSET:
            self._cost_func = cost_func  # type: ignore[assignment]

    def next(self, status: TrialResult | None = None) -> str | None:
        """Returns the next target revision for bisector to run."""
        if self._conclusion:
            return None
        if status is not None:
            assert self._pending_run_at_call is not None
            self._last_run_eval_log = status.get('eval_log')
            response: dict[str, Any] = {
                'result': status.get('result', status.get('status')),
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

    def get_result(self) -> str | None:
        """Returns the concluded revision."""
        return self._conclusion
