# Copyright 2026 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Unit tests for gemini_util."""

from collections import namedtuple
import copy
import logging
import unittest
from unittest import mock

from bisect_kit.gemini_bisect import gemini_util
from google import genai


# pylint: disable=protected-access

MockFunctionCall = namedtuple(
    'MockFunctionCall',
    ['id', 'name', 'arguments', 'type'],
    defaults=['function_call'],
)
MockText = namedtuple('MockText', ['text', 'type'], defaults=['text'])
MockThought = namedtuple(
    'MockThought',
    ['signature', 'summary', 'type'],
    defaults=[None, None, 'thought'],
)
MockUsage = namedtuple(
    'MockUsage',
    [
        'total_input_tokens',
        'total_tokens',
        'total_cached_tokens',
        'total_output_tokens',
    ],
    defaults=[None, None, None, None],
)


class MockInteraction:
    """Mock Interaction for testing."""

    def __init__(
        self,
        id_str: str,
        steps: list,
        status: str = 'completed',
        output_text: str = '',
        usage=None,
    ):
        self.id = id_str
        self.status = status
        self.steps = steps
        self.output_text = output_text
        self.usage = usage


class TestGeminiAgent(unittest.TestCase):
    """Test GeminiAgent class."""

    @mock.patch('google.auth.transport.requests.AuthorizedSession')
    @mock.patch(
        'google.oauth2.service_account.Credentials.from_service_account_file'
    )
    @mock.patch('google.genai.Client')
    def test_gemini_agent_init_and_reply(
        self,
        mock_genai_client,
        _mock_from_service_account_file,
        mock_auth_session_cls,
    ):
        mock_session = mock.MagicMock()
        mock_response = mock.MagicMock()
        mock_response.json.return_value = {
            'payload': {'data': 'ZmFrZS1rZXk='}  # base64 for 'fake-key'
        }
        mock_session.get.return_value = mock_response
        mock_auth_session_cls.return_value = mock_session

        client_instance = mock.MagicMock()
        mock_genai_client.return_value = client_instance

        mock_interaction_1 = MockInteraction(
            'int_1',
            [MockFunctionCall(id='fc_1', name='get_test_name', arguments={})],
        )
        mock_interaction_2 = MockInteraction(
            'int_2',
            [
                MockFunctionCall(
                    id='fc_2', name='conclude', arguments={'rev_id': 'commit1'}
                )
            ],
        )
        client_instance.interactions.create.side_effect = [
            mock_interaction_1,
            mock_interaction_2,
        ]

        with mock.patch.dict(
            'os.environ', {'SKYLAB_CLOUD_SERVICE_ACCOUNT_JSON': 'dummy_path'}
        ):
            agent = gemini_util.GeminiAgent(
                prompt="Investigate bug",
                functions=[{'name': 'get_test_name', 'description': 'desc'}],
                model='gemini-3.6-flash',
            )

            mock_genai_client.assert_called_once_with(api_key='fake-key')
            self.assertEqual(
                agent._tools,
                [
                    {
                        'type': 'function',
                        'name': 'get_test_name',
                        'description': 'desc',
                        'parameters': {'type': 'object', 'properties': {}},
                    }
                ],
            )
            self.assertEqual(
                agent._generation_config,
                {
                    'thinking_level': 'high',
                    'tool_choice': 'any',
                },
            )

            # Test start
            res1 = agent.start()
            self.assertEqual(res1.id, 'int_1')
            self.assertEqual(agent._last_interaction_id, 'int_1')
            client_instance.interactions.create.assert_called_with(
                model='gemini-3.6-flash',
                input='Start bisection investigation.',
                system_instruction='Investigate bug',
                tools=[
                    {
                        'type': 'function',
                        'name': 'get_test_name',
                        'description': 'desc',
                        'parameters': {'type': 'object', 'properties': {}},
                    }
                ],
                generation_config={
                    'thinking_level': 'high',
                    'tool_choice': 'any',
                },
            )

            # Test reply with previous_interaction_id
            res2 = agent.reply(
                [
                    {
                        'type': 'function_result',
                        'call_id': 'fc_1',
                        'name': 'get_test_name',
                        'result': {'name': 'test1'},
                    }
                ]
            )
            self.assertEqual(res2.id, 'int_2')
            self.assertEqual(agent._last_interaction_id, 'int_2')
            client_instance.interactions.create.assert_called_with(
                model='gemini-3.6-flash',
                input=[
                    {
                        'type': 'function_result',
                        'call_id': 'fc_1',
                        'name': 'get_test_name',
                        'result': {'name': 'test1'},
                    }
                ],
                system_instruction='Investigate bug',
                tools=[
                    {
                        'type': 'function',
                        'name': 'get_test_name',
                        'description': 'desc',
                        'parameters': {'type': 'object', 'properties': {}},
                    }
                ],
                generation_config={
                    'thinking_level': 'high',
                    'tool_choice': 'any',
                },
                previous_interaction_id='int_1',
            )

            # Test deepcopy and branching
            agent_copy = copy.deepcopy(agent)
            self.assertEqual(agent_copy._last_interaction_id, 'int_2')
            self.assertIs(agent_copy._client, agent._client)

            mock_interaction_3a = MockInteraction(
                'int_3a',
                [
                    MockFunctionCall(
                        id='fc_3a',
                        name='run_at',
                        arguments={'rev_id': 'commit2'},
                    )
                ],
            )
            mock_interaction_3b = MockInteraction(
                'int_3b',
                [
                    MockFunctionCall(
                        id='fc_3b',
                        name='run_at',
                        arguments={'rev_id': 'commit3'},
                    )
                ],
            )
            client_instance.interactions.create.side_effect = [
                mock_interaction_3a,
                mock_interaction_3b,
            ]

            # Original agent replies along Branch A
            res_3a = agent.reply(
                [
                    {
                        'type': 'function_result',
                        'call_id': 'fc_2',
                        'name': 'conclude',
                        'result': {'status': 'continue'},
                    }
                ]
            )
            self.assertEqual(res_3a.id, 'int_3a')
            self.assertEqual(agent._last_interaction_id, 'int_3a')
            self.assertEqual(agent_copy._last_interaction_id, 'int_2')
            client_instance.interactions.create.assert_called_with(
                model='gemini-3.6-flash',
                input=[
                    {
                        'type': 'function_result',
                        'call_id': 'fc_2',
                        'name': 'conclude',
                        'result': {'status': 'continue'},
                    }
                ],
                system_instruction='Investigate bug',
                tools=[
                    {
                        'type': 'function',
                        'name': 'get_test_name',
                        'description': 'desc',
                        'parameters': {'type': 'object', 'properties': {}},
                    }
                ],
                generation_config={
                    'thinking_level': 'high',
                    'tool_choice': 'any',
                },
                previous_interaction_id='int_2',
            )

            # Copied agent replies along Branch B from the same parent 'int_2'
            res_3b = agent_copy.reply(
                [
                    {
                        'type': 'function_result',
                        'call_id': 'fc_2',
                        'name': 'conclude',
                        'result': {'status': 'branch_b'},
                    }
                ]
            )
            self.assertEqual(res_3b.id, 'int_3b')
            self.assertEqual(agent_copy._last_interaction_id, 'int_3b')
            self.assertEqual(agent._last_interaction_id, 'int_3a')
            client_instance.interactions.create.assert_called_with(
                model='gemini-3.6-flash',
                input=[
                    {
                        'type': 'function_result',
                        'call_id': 'fc_2',
                        'name': 'conclude',
                        'result': {'status': 'branch_b'},
                    }
                ],
                system_instruction='Investigate bug',
                tools=[
                    {
                        'type': 'function',
                        'name': 'get_test_name',
                        'description': 'desc',
                        'parameters': {'type': 'object', 'properties': {}},
                    }
                ],
                generation_config={
                    'thinking_level': 'high',
                    'tool_choice': 'any',
                },
                previous_interaction_id='int_2',
            )

    def test_is_retriable_gemini_error(self):
        class DummyClientError(genai.errors.ClientError):
            """Dummy ClientError for testing."""

            def __init__(self, code):
                super().__init__(code, {'error': {'message': f'{code}'}})
                self.code = code

        class DummyServerError(genai.errors.ServerError):
            """Dummy ServerError for testing."""

            def __init__(self, code):
                super().__init__(code, {'error': {'message': f'{code}'}})
                self.code = code

        class DummyStatusError(Exception):
            """Dummy StatusError for testing."""

            def __init__(self, status_code):
                super().__init__(f"Status error {status_code}")
                self.status_code = status_code

        self.assertTrue(
            gemini_util._is_retriable_gemini_error(DummyClientError(429))
        )
        self.assertFalse(
            gemini_util._is_retriable_gemini_error(DummyClientError(400))
        )
        self.assertTrue(
            gemini_util._is_retriable_gemini_error(DummyServerError(503))
        )
        self.assertFalse(
            gemini_util._is_retriable_gemini_error(DummyServerError(500))
        )
        self.assertTrue(
            gemini_util._is_retriable_gemini_error(DummyStatusError(429))
        )
        self.assertTrue(
            gemini_util._is_retriable_gemini_error(DummyStatusError(503))
        )
        self.assertFalse(
            gemini_util._is_retriable_gemini_error(DummyStatusError(404))
        )
        self.assertFalse(
            gemini_util._is_retriable_gemini_error(ValueError("error"))
        )


class TestBisectAgentInteractions(unittest.TestCase):
    """Test BisectAgent with Gemini Interactions API."""

    @mock.patch('google.auth.transport.requests.AuthorizedSession')
    @mock.patch(
        'google.oauth2.service_account.Credentials.from_service_account_file'
    )
    @mock.patch('bisect_kit.gemini_bisect.gemini_util.GeminiAgent')
    @mock.patch('bisect_kit.cr_util.build_revlist')
    @mock.patch('bisect_kit.git_util.get_commit_metadata')
    @mock.patch('bisect_kit.git_util.CommitMeta.get_summary')
    @mock.patch('bisect_kit.git_util.git_show')
    def test_bisect_agent_run_loop(
        self,
        mock_git_show,
        mock_get_summary,
        _mock_get_metadata,
        mock_build_revlist,
        mock_gemini_agent_cls,
        _mock_creds,
        _mock_session,
    ):
        mock_build_revlist.return_value = (
            None,
            {
                '1': {'actions': [{'rev': 'commit1'}]},
                '2': {'actions': [{'rev': 'commit2'}]},
                '3': {'actions': [{'rev': 'commit3'}]},
            },
        )
        mock_get_summary.return_value = 'Fix issue'
        mock_git_show.return_value = 'diff --git a/foo b/foo'

        mock_gemini_agent = mock.MagicMock()
        mock_gemini_agent_cls.return_value = mock_gemini_agent

        # Turn 1: Parallel query (get_test_name, get_range, get_revision_detail)
        turn1_interaction = MockInteraction(
            'int_1',
            [
                MockThought(signature='sig1'),
                MockText(text='Let me inspect test and range'),
                MockFunctionCall(id='fc_1', name='get_test_name', arguments={}),
                MockFunctionCall(
                    id='fc_2',
                    name='get_range',
                    arguments={'include_reason': True},
                ),
                MockFunctionCall(
                    id='fc_3',
                    name='get_revision_detail',
                    arguments={'rev_id': '2', 'reason': 'check diff'},
                ),
            ],
        )

        # Turn 2: Run test at revision 2
        turn2_interaction = MockInteraction(
            'int_2',
            [
                MockText(text='I will test revision 2'),
                MockFunctionCall(
                    id='fc_4',
                    name='run_at',
                    arguments={'rev_id': '2', 'reason': 'potential culprit'},
                ),
            ],
        )

        # Turn 3: Read last log and read log
        turn3_interaction = MockInteraction(
            'int_3',
            [
                MockFunctionCall(id='fc_5', name='read_last_log', arguments={}),
                MockFunctionCall(
                    id='fc_6',
                    name='read_log',
                    arguments={'rev_id': '2', 'run_index': 0},
                ),
                MockFunctionCall(
                    id='fc_7', name='conclude', arguments={'rev_id': '2'}
                ),
            ],
        )

        mock_gemini_agent.start.return_value = turn1_interaction
        mock_gemini_agent.reply.side_effect = [
            turn2_interaction,
            turn3_interaction,
        ]

        with mock.patch.dict(
            'os.environ', {'SKYLAB_CLOUD_SERVICE_ACCOUNT_JSON': 'dummy_path'}
        ):
            agent = gemini_util.BisectAgent(
                test_name='test_example',
                repo='test_repo',
                revlist=['1', '2', '3'],
            )

            # First step: next() triggers start() -> turn1 (queries) -> replies -> turn2 (run_at '2')
            next_rev = agent.next()
            self.assertEqual(next_rev, '2')
            self.assertEqual(agent.get_result(), None)

            # Next step: simulate run_at result
            agent.add_sample(
                '2',
                'new',
                result='FAIL',
                reason='AssertionError',
                eval_log=None,
            )
            next_rev2 = agent.next(
                {
                    'result': 'FAIL',
                    'status': 'new',
                    'reason': 'AssertionError',
                    'likelihood_new': 0.95,
                }
            )
            self.assertIsNone(next_rev2)
            self.assertEqual(agent.get_result(), '2')

            # Verify response format sent to Gemini
            turn2_results = mock_gemini_agent.reply.call_args[0][0]
            self.assertEqual(
                turn2_results,
                [
                    {
                        'type': 'function_result',
                        'call_id': 'fc_4',
                        'name': 'run_at',
                        'result': {
                            'result': 'FAIL',
                            'status': 'new',
                            'reason': 'AssertionError',
                            'likelihood_new': 0.95,
                        },
                    }
                ],
            )

    @mock.patch('google.auth.transport.requests.AuthorizedSession')
    @mock.patch(
        'google.oauth2.service_account.Credentials.from_service_account_file'
    )
    @mock.patch('bisect_kit.gemini_bisect.gemini_util.GeminiAgent')
    @mock.patch('bisect_kit.cr_util.build_revlist')
    def test_bisect_agent_invalid_rev_and_no_calls(
        self,
        mock_build_revlist,
        mock_gemini_agent_cls,
        _mock_creds,
        _mock_session,
    ):
        mock_build_revlist.return_value = (
            None,
            {'1': {'actions': [{'rev': 'commit1'}]}},
        )
        mock_gemini_agent = mock.MagicMock()
        mock_gemini_agent_cls.return_value = mock_gemini_agent

        # Return invalid rev_ids for get_revision_detail, run_at, conclude
        turn1_interaction = MockInteraction(
            'int_1',
            [
                MockFunctionCall(
                    id='fc_1',
                    name='get_revision_detail',
                    arguments={'rev_id': 'invalid_rev', 'reason': 'test'},
                ),
                MockFunctionCall(
                    id='fc_2',
                    name='run_at',
                    arguments={'rev_id': 'invalid_rev', 'reason': 'test'},
                ),
                MockFunctionCall(
                    id='fc_3',
                    name='conclude',
                    arguments={'rev_id': 'invalid_rev'},
                ),
                MockFunctionCall(
                    id='fc_4',
                    name='run_at',
                    arguments={'rev_id': '1', 'reason': 'valid'},
                ),
            ],
        )
        mock_gemini_agent.start.return_value = turn1_interaction

        with mock.patch.dict(
            'os.environ', {'SKYLAB_CLOUD_SERVICE_ACCOUNT_JSON': 'dummy_path'}
        ):
            agent = gemini_util.BisectAgent(
                test_name='test_example',
                repo='test_repo',
                revlist=['1'],
            )
            next_rev = agent.next()
            self.assertEqual(next_rev, '1')

            # Verify that error responses were generated for the invalid rev_ids
            self.assertEqual(len(agent._pending_results), 3)
            self.assertIn(
                'Invalid rev_id', agent._pending_results[0]['result']['error']
            )
            self.assertIn(
                'Invalid rev_id', agent._pending_results[1]['result']['error']
            )
            self.assertIn(
                'Invalid rev_id', agent._pending_results[2]['result']['error']
            )

            # Test no function calls raises RuntimeError
            mock_gemini_agent.reply.return_value = MockInteraction(
                'int_2', [MockText(text='I am just chatting.')]
            )
            with self.assertRaises(RuntimeError):
                agent.next({'status': 'PASS'})

    @mock.patch('bisect_kit.cr_util.build_revlist')
    @mock.patch('bisect_kit.gemini_bisect.gemini_util.GeminiAgent')
    def test_bisect_agent_future_build_tree(
        self, mock_gemini_agent_cls, mock_build_revlist
    ):
        mock_build_revlist.return_value = (
            None,
            {
                '1': {'actions': [{'rev': 'c1'}]},
                '2': {'actions': [{'rev': 'c2'}]},
            },
        )
        mock_gemini_agent = mock.MagicMock()
        mock_gemini_agent_cls.return_value = mock_gemini_agent

        tree = {
            'FAIL': {
                'next_revision': '1',
                'reason': 'test fail path',
                'confidence': 0.8,
                'next': {
                    'FAIL': {
                        'next_revision': '2',
                        'reason': 'further isolate',
                        'confidence': 0.9,
                    }
                },
            },
            'PASS': {
                'next_revision': '2',
                'reason': 'test pass path',
                'confidence': 0.2,
            },
        }
        interaction = MockInteraction(
            'int_1',
            [
                MockFunctionCall(
                    id='fc_1',
                    name='run_at',
                    arguments={
                        'rev_id': '1',
                        'reason': 'test reason',
                        'future_build_tree': tree,
                    },
                )
            ],
        )
        mock_gemini_agent.start.return_value = interaction

        with mock.patch.dict(
            'os.environ', {'SKYLAB_CLOUD_SERVICE_ACCOUNT_JSON': 'dummy_path'}
        ):
            agent = gemini_util.BisectAgent(
                test_name='test_example',
                repo='test_repo',
                revlist=['1', '2'],
            )
            next_rev = agent.next()
            self.assertEqual(next_rev, '1')
            self.assertEqual(agent.future_build_tree, tree)

    @mock.patch('google.auth.transport.requests.AuthorizedSession')
    @mock.patch(
        'google.oauth2.service_account.Credentials.from_service_account_file'
    )
    @mock.patch('bisect_kit.gemini_bisect.gemini_util.GeminiAgent')
    @mock.patch('bisect_kit.cr_util.build_revlist')
    def test_bisect_agent_token_logging(
        self,
        mock_build_revlist,
        mock_gemini_agent_cls,
        _mock_creds,
        _mock_session,
    ):
        mock_build_revlist.return_value = (
            None,
            {
                '1': {'actions': [{'rev': 'commit1'}]},
                '2': {'actions': [{'rev': 'commit2'}]},
            },
        )
        mock_gemini_agent = mock.MagicMock()
        mock_gemini_agent_cls.return_value = mock_gemini_agent

        interaction1 = MockInteraction(
            'int_1',
            [
                MockFunctionCall(
                    id='fc_1',
                    name='get_range',
                    arguments={},
                )
            ],
            usage=MockUsage(
                total_input_tokens=500,
                total_tokens=550,
                total_cached_tokens=250,
            ),
        )
        interaction2 = MockInteraction(
            'int_2',
            [
                MockFunctionCall(
                    id='fc_2',
                    name='run_at',
                    arguments={'rev_id': '1', 'reason': 'test reason'},
                )
            ],
            usage=MockUsage(
                total_input_tokens=1200,
                total_tokens=1280,
                total_cached_tokens=600,
            ),
        )
        mock_gemini_agent.start.return_value = interaction1
        mock_gemini_agent.reply.return_value = interaction2

        with mock.patch.dict(
            'os.environ', {'SKYLAB_CLOUD_SERVICE_ACCOUNT_JSON': 'dummy_path'}
        ):
            agent = gemini_util.BisectAgent(
                test_name='test_example',
                repo='test_repo',
                revlist=['1', '2'],
            )
            assert isinstance(agent.logger, logging.LoggerAdapter)
            with self.assertLogs(agent.logger.logger, level='INFO') as log_ctx:
                next_rev = agent.next()
                self.assertEqual(next_rev, '1')
                self.assertEqual(agent.total_input_tokens, 500 + 1200)
                self.assertEqual(agent.total_cached_tokens, 250 + 600)
                self.assertEqual(agent.total_tokens_used, 550 + 1280)
                self.assertAlmostEqual(
                    agent.accumulative_cache_rate, (250 + 600) / (500 + 1200)
                )
                assert agent.total_cost is not None
                self.assertAlmostEqual(agent.total_cost, 0.00039375 + 0.000795)
                log_text = '\n'.join(log_ctx.output)
                self.assertIn(
                    'calling get_range({}) (I500, T550, C50%, $0.0004, AccT550, AccC50%, Acc$0.0004)',
                    log_text,
                )
                self.assertIn(
                    "calling run_at({'rev_id': '1', 'reason': 'test reason'}) (I1.2k, T1.3k, C50%, $0.0008, AccT1.8k, AccC50%, Acc$0.0012)",
                    log_text,
                )

    def test_format_tokens(self):
        self.assertEqual(gemini_util._format_tokens(0), '0')
        self.assertEqual(gemini_util._format_tokens(500), '500')
        self.assertEqual(gemini_util._format_tokens(1000), '1k')
        self.assertEqual(gemini_util._format_tokens(1200), '1.2k')
        self.assertEqual(gemini_util._format_tokens(5000), '5k')
        self.assertEqual(gemini_util._format_tokens(5500), '5.5k')
        self.assertEqual(gemini_util._format_tokens(12000), '12k')
        self.assertEqual(gemini_util._format_tokens(1000000), '1M')
        self.assertEqual(gemini_util._format_tokens(1500000), '1.5M')

    def test_format_cost(self):
        self.assertEqual(gemini_util._format_cost(0), '$0')
        self.assertEqual(gemini_util._format_cost(0.000076875), '$0.00008')
        self.assertEqual(gemini_util._format_cost(0.00039375), '$0.0004')
        self.assertEqual(gemini_util._format_cost(0.000795), '$0.0008')
        self.assertEqual(gemini_util._format_cost(0.0012), '$0.0012')
        self.assertEqual(gemini_util._format_cost(0.015), '$0.015')
        self.assertEqual(gemini_util._format_cost(0.25), '$0.25')
        self.assertEqual(gemini_util._format_cost(1.5), '$1.50')

    def test_estimate_gemini_token_cost(self):
        cost_37_flash = gemini_util.estimate_gemini_token_cost(
            'gemini-3.7-flash',
            input_tokens=500,
            cached_tokens=250,
            output_tokens=50,
        )
        assert cost_37_flash is not None
        self.assertAlmostEqual(cost_37_flash, 0.00039375)

        # Unknown model returns None
        self.assertIsNone(
            gemini_util.estimate_gemini_token_cost(
                'unknown-model',
                input_tokens=1000,
                cached_tokens=0,
                output_tokens=100,
            )
        )

    def test_format_template_prompt(self):
        prompt_regression = gemini_util.format_template(
            gemini_util._PROMPT_TEMPLATE, gemini_util.REGRESSION_REPLACEMENTS
        )
        self.assertIn('investigating test regressions', prompt_regression)
        self.assertIn('culprit range', prompt_regression)
        self.assertIn(
            'FAIL/new behavior on a PASS/old commit', prompt_regression
        )
        self.assertIn('new/FAIL', prompt_regression)
        self.assertIn(
            'unlucky streak of FAILs, causing "likelihood_new" to climb high',
            prompt_regression,
        )
        self.assertIn(
            'unlucky streak of PASSes, causing "likelihood_new" to drop low',
            prompt_regression,
        )
        self.assertIn(
            'cumulative probability that the culprit is at or before this commit',
            prompt_regression,
        )

        prompt_fix = gemini_util.format_template(
            gemini_util._PROMPT_TEMPLATE, gemini_util.FIX_REPLACEMENTS
        )
        self.assertIn('investigating test fixes', prompt_fix)
        self.assertIn('candidate range', prompt_fix)
        self.assertIn('PASS/new behavior on a FAIL/old commit', prompt_fix)
        self.assertIn('new/PASS', prompt_fix)
        self.assertIn(
            'unlucky streak of PASSes, causing "likelihood_new" to climb high',
            prompt_fix,
        )
        self.assertIn(
            'unlucky streak of FAILs, causing "likelihood_new" to drop low',
            prompt_fix,
        )
        self.assertIn('fix CL', prompt_fix)
        self.assertIn(
            'cumulative probability that the fix CL is at or before this commit',
            prompt_fix,
        )
        self.assertNotIn('culprit', prompt_fix)

    def test_format_template_functions(self):
        funcs_regression = gemini_util.format_template(
            gemini_util._FUNCTIONS_TEMPLATE, gemini_util.REGRESSION_REPLACEMENTS
        )
        funcs_by_name = {f['name']: f for f in funcs_regression}
        self.assertIn(
            'regressed test', funcs_by_name['get_test_name']['description']
        )
        self.assertIn(
            'known good to known bad', funcs_by_name['get_range']['description']
        )
        self.assertIn('culprit', funcs_by_name['get_range']['description'])
        self.assertIn('new/FAIL', funcs_by_name['run_at']['description'])
        self.assertIn('culprit', funcs_by_name['conclude']['description'])
        self.assertIn('get_revisions_order_by_utility', funcs_by_name)
        self.assertNotIn('get_future_utility_prediction', funcs_by_name)

        funcs_fix = gemini_util.format_template(
            gemini_util._FUNCTIONS_TEMPLATE, gemini_util.FIX_REPLACEMENTS
        )
        funcs_by_name_fix = {f['name']: f for f in funcs_fix}
        self.assertIn(
            'fixed test', funcs_by_name_fix['get_test_name']['description']
        )
        self.assertIn(
            'known bad to known good',
            funcs_by_name_fix['get_range']['description'],
        )
        self.assertIn('fix CL', funcs_by_name_fix['get_range']['description'])
        self.assertIn('new/PASS', funcs_by_name_fix['run_at']['description'])
        self.assertIn('fix CL', funcs_by_name_fix['conclude']['description'])
        self.assertIn('get_revisions_order_by_utility', funcs_by_name_fix)
        self.assertNotIn('get_future_utility_prediction', funcs_by_name_fix)

    @mock.patch('bisect_kit.cr_util.build_revlist')
    @mock.patch('bisect_kit.gemini_bisect.gemini_util.GeminiAgent')
    def test_get_revisions_order_by_utility(
        self, _mock_gemini_agent, mock_build_revlist
    ):
        mock_build_revlist.return_value = (
            None,
            {
                '1': {'actions': [{'rev': 'c1'}]},
                '2': {'actions': [{'rev': 'c2'}]},
                '3': {'actions': [{'rev': 'c3'}]},
                '4': {'actions': [{'rev': 'c4'}]},
                '5': {'actions': [{'rev': 'c5'}]},
            },
        )
        with (
            mock.patch(
                'bisect_kit.git_util.CommitMeta.get_summary',
                side_effect=lambda meta: f"Summary for {meta}",
            ),
            mock.patch(
                'bisect_kit.git_util.get_commit_metadata',
                side_effect=lambda repo, commit: commit,
            ),
        ):
            agent = gemini_util.BisectAgent(
                test_name='test_example',
                repo='test_repo',
                revlist=['1', '2', '3', '4', '5'],
            )
            # Initially no stats, returns in utility order for default prior [0, 0.25, 0.25, 0.25, 0.25]
            res = agent._get_revisions_order_by_utility()
            self.assertEqual(
                [r['rev_id'] for r in res['revisions']],
                ['3', '4', '2', '1', '5'],
            )
            self.assertNotIn('future_build_tree', res)

            # Update stats with various utilities
            agent.update_stats(
                {
                    '1': {
                        'prob': 0.05,
                        'info_gain': 0.1,
                        'info_gain_per_cost': 0.1,
                        'status': {},
                    },
                    '2': {
                        'prob': 0.40,
                        'info_gain': 0.8,
                        'info_gain_per_cost': 0.8,
                        'status': {},
                    },
                    '3': {
                        'prob': 0.25,
                        'info_gain': 0.5,
                        'info_gain_per_cost': 0.5,
                        'status': {},
                    },
                    '4': {
                        'prob': 0.15,
                        'info_gain': 0.3,
                        'info_gain_per_cost': 0.3,
                        'status': {},
                    },
                    '5': {
                        'prob': 0.15,
                        'info_gain': 0.2,
                        'info_gain_per_cost': 0.2,
                        'status': {},
                    },
                }
            )

            # 1. Order all by utility descending (2: 0.8, 3: 0.5, 4: 0.3, 1: 0.1, 5: 0.0)
            all_ordered = agent._get_revisions_order_by_utility()
            self.assertEqual(
                [r['rev_id'] for r in all_ordered['revisions']],
                ['2', '3', '4', '1', '5'],
            )

            # 2. limit=1 gets only the single highest utility revision
            top_1 = agent._get_revisions_order_by_utility(limit=1)
            self.assertEqual(len(top_1['revisions']), 1)
            self.assertEqual(top_1['revisions'][0]['rev_id'], '2')

            # 3. limit=3 gets top 3
            top_3 = agent._get_revisions_order_by_utility(limit=3)
            self.assertEqual(
                [r['rev_id'] for r in top_3['revisions']], ['2', '3', '4']
            )

            # 4. limit=0 gets empty list
            top_0 = agent._get_revisions_order_by_utility(limit=0)
            self.assertEqual(top_0['revisions'], [])

            # 5. Purpose 2a: Candidate subset specified: return highest within the list
            subset = agent._get_revisions_order_by_utility(
                revisions=['1', '4', '5']
            )
            self.assertEqual(
                [r['rev_id'] for r in subset['revisions']], ['4', '1', '5']
            )

            # 6. Candidate subset with limit=1: get highest within the subset
            subset_top_1 = agent._get_revisions_order_by_utility(
                limit=1, revisions=['1', '4', '5']
            )
            self.assertEqual(len(subset_top_1['revisions']), 1)
            self.assertEqual(subset_top_1['revisions'][0]['rev_id'], '4')

            # 7. Candidate subset with unknown rev_id: ignores unknown
            subset_with_unknown = agent._get_revisions_order_by_utility(
                limit=1, revisions=['1', 'unknown_rev', '5']
            )
            self.assertEqual(len(subset_with_unknown['revisions']), 1)
            self.assertEqual(subset_with_unknown['revisions'][0]['rev_id'], '1')

            # 8. Empty candidate subset: returns empty list
            empty_subset = agent._get_revisions_order_by_utility(revisions=[])
            self.assertEqual(empty_subset['revisions'], [])

            # 9. Tie breaking: preserve relative revlist order for equal utilities
            agent.update_stats(
                {
                    '1': {
                        'prob': 0.5,
                        'status': {},
                        'info_gain': None,
                        'info_gain_per_cost': None,
                    },
                    '2': {
                        'prob': 0.0,
                        'status': {},
                        'info_gain': None,
                        'info_gain_per_cost': None,
                    },
                    '3': {
                        'prob': 0.5,
                        'status': {},
                        'info_gain': None,
                        'info_gain_per_cost': None,
                    },
                }
            )
            tied = agent._get_revisions_order_by_utility(
                revisions=['1', '2', '3']
            )
            self.assertEqual(
                [r['rev_id'] for r in tied['revisions']], ['1', '2', '3']
            )
            self.assertEqual(
                agent._get_revisions_order_by_utility(
                    limit=1, revisions=['1', '2', '3']
                )['revisions'][0]['rev_id'],
                '1',
            )

    @mock.patch('bisect_kit.cr_util.build_revlist')
    @mock.patch('bisect_kit.gemini_bisect.gemini_util.GeminiAgent')
    def test_get_revisions_order_by_utility_suspects(
        self, _mock_gemini_agent, mock_build_revlist
    ):
        mock_build_revlist.return_value = (
            None,
            {
                '1': {'actions': [{'rev': 'c1'}]},
                '2': {'actions': [{'rev': 'c2'}]},
                '3': {'actions': [{'rev': 'c3'}]},
                '4': {'actions': [{'rev': 'c4'}]},
                '5': {'actions': [{'rev': 'c5'}]},
            },
        )
        with (
            mock.patch(
                'bisect_kit.git_util.CommitMeta.get_summary',
                side_effect=lambda meta: f"Summary for {meta}",
            ),
            mock.patch(
                'bisect_kit.git_util.get_commit_metadata',
                side_effect=lambda repo, commit: commit,
            ),
        ):
            agent = gemini_util.BisectAgent(
                test_name='test_example',
                repo='test_repo',
                revlist=['1', '2', '3', '4', '5'],
                old_p=0.05,
                new_p=0.95,
            )
            agent.update_stats(
                {
                    '1': {
                        'prob': 0.2,
                        'status': {},
                        'info_gain': None,
                        'info_gain_per_cost': None,
                    },
                    '2': {
                        'prob': 0.2,
                        'status': {},
                        'info_gain': None,
                        'info_gain_per_cost': None,
                    },
                    '3': {
                        'prob': 0.2,
                        'status': {},
                        'info_gain': None,
                        'info_gain_per_cost': None,
                    },
                    '4': {
                        'prob': 0.2,
                        'status': {},
                        'info_gain': None,
                        'info_gain_per_cost': None,
                    },
                    '5': {
                        'prob': 0.2,
                        'status': {},
                        'info_gain': None,
                        'info_gain_per_cost': None,
                    },
                }
            )

            # Purpose 2b: Only suspects '2' and '5' are suspected (0 prob on 1, 3, 4).
            # Testing middle commits ('2', '3', '4') gives highest utility to discriminate 2 from 5.
            res_suspects = agent._get_revisions_order_by_utility(
                suspects=['2', '5']
            )
            self.assertEqual(len(res_suspects['revisions']), 5)
            # Middle commits have highest info gain
            self.assertIn(
                res_suspects['revisions'][0]['rev_id'], ('2', '3', '4')
            )
            # Commit 1 has info gain 0
            stats_1 = [
                r['stats']
                for r in res_suspects['revisions']
                if r['rev_id'] == '1'
            ][0]
            self.assertEqual(stats_1['prob'], 0.0)
            self.assertEqual(stats_1['info_gain'], 0.0)

            # Commit 3 has high info gain
            stats_3 = [
                r['stats']
                for r in res_suspects['revisions']
                if r['rev_id'] == '3'
            ][0]
            self.assertEqual(stats_3['prob'], 0.0)
            self.assertGreater(stats_3['info_gain'], 0.4)

            # Suspects '1' and '2': commit '1' splits the suspects
            res_1_2 = agent._get_revisions_order_by_utility(suspects=['1', '2'])
            self.assertEqual(res_1_2['revisions'][0]['rev_id'], '1')

            # Invalid suspects
            res_invalid = agent._get_revisions_order_by_utility(
                suspects=['unknown_rev']
            )
            self.assertIn('error', res_invalid)
            self.assertEqual(res_invalid['revisions'], [])

            # Suspects with candidate subset: only rank within revisions=['1', '3', '5']
            res_sub = agent._get_revisions_order_by_utility(
                suspects=['2', '5'], revisions=['1', '3', '5']
            )
            self.assertEqual(
                [r['rev_id'] for r in res_sub['revisions']], ['3', '1', '5']
            )

    @mock.patch('bisect_kit.cr_util.build_revlist')
    @mock.patch('bisect_kit.gemini_bisect.gemini_util.GeminiAgent')
    def test_get_revisions_order_by_utility_future_tree(
        self, _mock_gemini_agent, mock_build_revlist
    ):
        mock_build_revlist.return_value = (
            None,
            {
                '1': {'actions': [{'rev': 'c1'}]},
                '2': {'actions': [{'rev': 'c2'}]},
                '3': {'actions': [{'rev': 'c3'}]},
                '4': {'actions': [{'rev': 'c4'}]},
                '5': {'actions': [{'rev': 'c5'}]},
            },
        )
        with (
            mock.patch(
                'bisect_kit.git_util.CommitMeta.get_summary',
                side_effect=lambda meta: f"Summary for {meta}",
            ),
            mock.patch(
                'bisect_kit.git_util.get_commit_metadata',
                side_effect=lambda repo, commit: commit,
            ),
        ):
            agent = gemini_util.BisectAgent(
                test_name='test_example',
                repo='test_repo',
                revlist=['1', '2', '3', '4', '5'],
                old_p=0.05,
                new_p=0.95,
            )
            agent.update_stats(
                {
                    '1': {
                        'prob': 0.2,
                        'status': {},
                        'info_gain': None,
                        'info_gain_per_cost': None,
                    },
                    '2': {
                        'prob': 0.2,
                        'status': {},
                        'info_gain': None,
                        'info_gain_per_cost': None,
                    },
                    '3': {
                        'prob': 0.2,
                        'status': {},
                        'info_gain': None,
                        'info_gain_per_cost': None,
                    },
                    '4': {
                        'prob': 0.2,
                        'status': {},
                        'info_gain': None,
                        'info_gain_per_cost': None,
                    },
                    '5': {
                        'prob': 0.2,
                        'status': {},
                        'info_gain': None,
                        'info_gain_per_cost': None,
                    },
                }
            )

            # Purpose 3: depth=1 produces 1st-level future tree
            res_d1 = agent._get_revisions_order_by_utility(depth=1)
            self.assertIn('revisions', res_d1)
            self.assertIn('future_build_tree', res_d1)
            tree_d1 = res_d1['future_build_tree']
            self.assertIn('FAIL', tree_d1)
            self.assertIn('PASS', tree_d1)
            self.assertIn('next_revision', tree_d1['FAIL'])
            self.assertIn('confidence', tree_d1['FAIL'])
            self.assertNotIn('next', tree_d1['FAIL'])

            # depth=2 produces 2nd-level 'next' subtrees
            res_d2 = agent._get_revisions_order_by_utility(depth=2, limit=1)
            self.assertEqual(len(res_d2['revisions']), 1)
            tree_d2 = res_d2['future_build_tree']
            self.assertIn('next', tree_d2['FAIL'])
            self.assertIn('FAIL', tree_d2['FAIL']['next'])
            self.assertIn('PASS', tree_d2['FAIL']['next'])
            self.assertIn('next', tree_d2['PASS'])

            # depth=3 produces 3rd-level 'next' subtrees
            res_d3 = agent._get_revisions_order_by_utility(depth=3)
            tree_d3 = res_d3['future_build_tree']
            self.assertIn('next', tree_d3['FAIL']['next']['FAIL'])

            # Specifying root_revision
            res_root = agent._get_revisions_order_by_utility(
                root_revision='4', depth=2
            )
            self.assertIn('future_build_tree', res_root)

            # Combining suspects with depth
            res_suspects_tree = agent._get_revisions_order_by_utility(
                suspects=['1', '5'], depth=2
            )
            self.assertIn('future_build_tree', res_suspects_tree)

    @mock.patch('bisect_kit.cr_util.build_revlist')
    @mock.patch('bisect_kit.gemini_bisect.gemini_util.GeminiAgent')
    def test_get_revisions_order_by_utility_simulated_runs(
        self, _mock_gemini_agent, mock_build_revlist
    ):
        mock_build_revlist.return_value = (
            None,
            {
                '1': {'actions': [{'rev': 'c1'}]},
                '2': {'actions': [{'rev': 'c2'}]},
                '3': {'actions': [{'rev': 'c3'}]},
                '4': {'actions': [{'rev': 'c4'}]},
                '5': {'actions': [{'rev': 'c5'}]},
            },
        )
        with (
            mock.patch(
                'bisect_kit.git_util.CommitMeta.get_summary',
                side_effect=lambda meta: f"Summary for {meta}",
            ),
            mock.patch(
                'bisect_kit.git_util.get_commit_metadata',
                side_effect=lambda repo, commit: commit,
            ),
        ):
            agent = gemini_util.BisectAgent(
                test_name='test_example',
                repo='test_repo',
                revlist=['1', '2', '3', '4', '5'],
                old_p=0.1,
                new_p=0.9,
            )
            agent.update_stats(
                {
                    '1': {
                        'prob': 0.2,
                        'status': {},
                        'info_gain': None,
                        'info_gain_per_cost': None,
                    },
                    '2': {
                        'prob': 0.2,
                        'status': {},
                        'info_gain': None,
                        'info_gain_per_cost': None,
                    },
                    '3': {
                        'prob': 0.2,
                        'status': {},
                        'info_gain': None,
                        'info_gain_per_cost': None,
                    },
                    '4': {
                        'prob': 0.2,
                        'status': {},
                        'info_gain': None,
                        'info_gain_per_cost': None,
                    },
                    '5': {
                        'prob': 0.2,
                        'status': {},
                        'info_gain': None,
                        'info_gain_per_cost': None,
                    },
                }
            )

            # 2nd-level: If we simulate that rev '3' resulted in FAIL (new behavior)
            sim_revs = agent._get_revisions_order_by_utility(
                simulated_runs=[{'rev_id': '3', 'result': 'FAIL'}]
            )
            self.assertEqual(len(sim_revs['revisions']), 5)
            # Under FAIL at '3', candidates <= 3 get higher probability
            self.assertIn(sim_revs['revisions'][0]['rev_id'], ('1', '2'))

            # 3rd-level: If we simulate rev '3' FAIL, then rev '2' PASS
            sim_revs_3rd = agent._get_revisions_order_by_utility(
                simulated_runs=[
                    {'rev_id': '3', 'result': 'FAIL'},
                    {'rev_id': '2', 'result': 'PASS'},
                ]
            )
            self.assertEqual(len(sim_revs_3rd['revisions']), 5)
            # Between 2 and 3, boundary candidates have highest utility
            self.assertIn(sim_revs_3rd['revisions'][0]['rev_id'], ('2', '3'))

    @mock.patch('bisect_kit.cr_util.build_revlist')
    @mock.patch('bisect_kit.gemini_bisect.gemini_util.GeminiAgent')
    def test_bisect_agent_get_revisions_order_by_utility_interaction(
        self, mock_gemini_agent_cls, mock_build_revlist
    ):
        mock_build_revlist.return_value = (
            None,
            {
                '1': {'actions': [{'rev': 'c1'}]},
                '2': {'actions': [{'rev': 'c2'}]},
                '3': {'actions': [{'rev': 'c3'}]},
            },
        )
        mock_gemini_agent = mock.MagicMock()
        mock_gemini_agent_cls.return_value = mock_gemini_agent

        # Interaction: Gemini calls get_revisions_order_by_utility(depth=2, limit=1, revisions=['1', '3'])
        interaction_1 = MockInteraction(
            'int_1',
            [
                MockFunctionCall(
                    id='fc_1',
                    name='get_revisions_order_by_utility',
                    arguments={'depth': 2, 'limit': 1, 'revisions': ['1', '3']},
                ),
            ],
        )
        interaction_2 = MockInteraction(
            'int_2',
            [
                MockFunctionCall(
                    id='fc_2',
                    name='run_at',
                    arguments={
                        'rev_id': '1',
                        'reason': 'highest utility in subset',
                        'future_build_tree': {
                            'FAIL': {
                                'next_revision': '1',
                                'confidence': 0.5,
                                'reason': 'r',
                            }
                        },
                    },
                ),
            ],
        )
        mock_gemini_agent.start.return_value = interaction_1
        mock_gemini_agent.reply.return_value = interaction_2

        with mock.patch.dict(
            'os.environ', {'SKYLAB_CLOUD_SERVICE_ACCOUNT_JSON': 'dummy_path'}
        ):
            agent = gemini_util.BisectAgent(
                test_name='test_example',
                repo='test_repo',
                revlist=['1', '2', '3'],
            )
            agent.update_stats(
                {
                    '1': {
                        'prob': 0.1,
                        'info_gain': 0.2,
                        'info_gain_per_cost': 0.2,
                        'status': {},
                    },
                    '2': {
                        'prob': 0.6,
                        'info_gain': 0.9,
                        'info_gain_per_cost': 0.9,
                        'status': {},
                    },
                    '3': {
                        'prob': 0.3,
                        'info_gain': 0.7,
                        'info_gain_per_cost': 0.7,
                        'status': {},
                    },
                }
            )
            next_rev = agent.next()
            self.assertEqual(next_rev, '1')

            reply_call_args = mock_gemini_agent.reply.call_args[0][0]
            self.assertEqual(len(reply_call_args), 1)
            self.assertEqual(
                reply_call_args[0]['name'], 'get_revisions_order_by_utility'
            )
            result = reply_call_args[0]['result']
            self.assertIn('revisions', result)
            self.assertIn('future_build_tree', result)
            self.assertEqual(len(result['revisions']), 1)
            self.assertEqual(result['revisions'][0]['rev_id'], '1')

    @mock.patch(
        'bisect_kit.gemini_bisect.gemini_util.GeminiAgent',
    )
    def test_bisect_agent_reverse_mode(self, mock_gemini_agent_cls):
        with mock.patch.object(
            gemini_util.cros_util,
            'build_revlist',
            return_value=(None, {}),
        ):
            agent = gemini_util.BisectAgent(
                test_name='fixed_test',
                repo='test_repo',
                revlist=['rev1', 'rev2'],
                term_old='FAIL',
                term_new='PASS',
            )
            mock_gemini_agent_cls.assert_called_once_with(
                prompt=gemini_util.format_template(
                    gemini_util._PROMPT_TEMPLATE, gemini_util.FIX_REPLACEMENTS
                ),
                functions=gemini_util.format_template(
                    gemini_util._FUNCTIONS_TEMPLATE,
                    gemini_util.FIX_REPLACEMENTS,
                ),
                model='gemini-3.7-flash',
            )
            self.assertEqual(agent.get_result(), None)

    @mock.patch('bisect_kit.cr_util.build_revlist')
    @mock.patch('bisect_kit.gemini_bisect.gemini_util.GeminiAgent')
    def test_dynamic_cost_table_generation(
        self, _mock_agent, mock_build_revlist
    ):
        mock_build_revlist.return_value = (
            None,
            {
                '1': {'actions': [{'rev': 'c1'}]},
                '2': {'actions': [{'rev': 'c2'}]},
                '3': {'actions': [{'rev': 'c3'}]},
            },
        )
        with mock.patch.dict(
            'os.environ', {'SKYLAB_CLOUD_SERVICE_ACCOUNT_JSON': 'dummy_path'}
        ):
            agent = gemini_util.BisectAgent(
                test_name='test_cost',
                repo='test_repo',
                revlist=['1', '2', '3'],
            )

            # 1. Default (no cost info) -> uniform (1.0, 1.0)
            self.assertEqual(
                agent._get_cost_table(),
                [(1.0, 1.0), (1.0, 1.0), (1.0, 1.0)],
            )

            # 2. Dynamic cost_func
            agent.update_stats(
                {},
                cost_func=lambda dut: [
                    (10.0 if r == dut else 100.0, 10.0 if r == dut else 100.0)
                    for r in agent._revlist
                ],
            )
            self.assertEqual(
                agent._get_cost_table('2'),
                [(100.0, 100.0), (10.0, 10.0), (100.0, 100.0)],
            )

            # 3. Setting cost_func to None explicitly clears cost function
            agent.update_stats({}, cost_func=None)
            self.assertEqual(
                agent._get_cost_table('2'),
                [(1.0, 1.0), (1.0, 1.0), (1.0, 1.0)],
            )

            # 4. cost_func covers entire rev_stats (5 revs), but _revlist is a subset ['1', '2', '3']
            all_revs = ['0', '1', '2', '3', '4']
            agent.update_stats(
                {
                    r: {
                        'prob': 0.2,
                        'status': {},
                        'info_gain': 0.1,
                        'info_gain_per_cost': 0.1,
                    }
                    for r in all_revs
                },
                cost_func=lambda dut: [
                    (float(i), float(i)) for i, _ in enumerate(all_revs)
                ],
            )
            self.assertEqual(
                agent._get_cost_table('2'),
                [(1.0, 1.0), (2.0, 2.0), (3.0, 3.0)],
            )

    @mock.patch('bisect_kit.cr_util.build_revlist')
    @mock.patch('bisect_kit.gemini_bisect.gemini_util.GeminiAgent')
    def test_dynamic_cost_tree_and_simulation(
        self, _mock_agent, mock_build_revlist
    ):
        mock_build_revlist.return_value = (
            None,
            {
                '1': {'actions': [{'rev': 'c1'}]},
                '2': {'actions': [{'rev': 'c2'}]},
                '3': {'actions': [{'rev': 'c3'}]},
            },
        )
        with mock.patch.dict(
            'os.environ', {'SKYLAB_CLOUD_SERVICE_ACCOUNT_JSON': 'dummy_path'}
        ):
            agent = gemini_util.BisectAgent(
                test_name='test_cost_sim',
                repo='test_repo',
                revlist=['1', '2', '3'],
                old_p=0.0,
                new_p=1.0,
            )

            def cost_func(dut):
                return [
                    (30.0 if r == dut else 630.0, 30.0 if r == dut else 630.0)
                    for r in agent._revlist
                ]

            agent.update_stats(
                {
                    '1': {
                        'prob': 0.0,
                        'status': {},
                        'info_gain': 0.0,
                        'info_gain_per_cost': 0.0,
                    },
                    '2': {
                        'prob': 0.5,
                        'status': {},
                        'info_gain': 0.5,
                        'info_gain_per_cost': 0.5,
                    },
                    '3': {
                        'prob': 0.5,
                        'status': {},
                        'info_gain': 0.5,
                        'info_gain_per_cost': 0.5,
                    },
                },
                old_p=0.0,
                new_p=1.0,
                cost_func=cost_func,
            )

            # With simulated run at 2, DUT becomes 2
            res = agent._get_revisions_order_by_utility(
                simulated_runs=[{'rev_id': '2', 'result': 'FAIL'}],
                depth=1,
            )
            self.assertIn('revisions', res)
            # In future tree branching from 2, DUT is 2 for subsequent evaluations
            self.assertIn('future_build_tree', res)


if __name__ == '__main__':
    unittest.main()
