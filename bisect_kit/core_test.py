# Copyright 2017 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Test core module."""

import json
import os
import tempfile
import unittest

from bisect_kit import core
from bisect_kit import strategy_states
import experiment


class TestRevInfo(unittest.TestCase):
    """Test core.RevInfo class."""

    def test_simple(self):
        rev_info = core.RevInfo('foo')
        self.assertEqual(rev_info.rev, 'foo')
        self.assertEqual(rev_info['old'], 0)
        self.assertEqual(rev_info['new'], 0)
        self.assertEqual(rev_info['skip'], 0)

    def test_add_sample(self):
        rev_info = core.RevInfo('foo')
        rev_info.add_sample(status='old')
        self.assertEqual(rev_info['old'], 1)
        rev_info.add_sample(status='old', times=2)
        self.assertEqual(rev_info['old'], 3)

    def test_averages(self):
        rev_info = core.RevInfo('foo')
        rev_info.add_sample(status='old', values=[1, 3])
        self.assertEqual(rev_info.averages(), [2])
        rev_info.add_sample(status='old', values=[6, 6])
        self.assertEqual(rev_info.averages(), [2, 6])

    def test_reclassify(self):
        rev_info = core.RevInfo('foo')
        rev_info.add_sample(status='value', values=[1, 3])
        rev_info.add_sample(status='value', values=[10, 10])
        rev_info.add_sample(status='value', values=[1, 100])
        rev_info.add_sample(status='skip')
        rev_info.reclassify(1, 3, 5)
        self.assertEqual(rev_info['old'], 1)
        self.assertEqual(rev_info['new'], 2)
        self.assertEqual(rev_info['skip'], 1)

    def test_reclassify_reverse(self):
        rev_info = core.RevInfo('foo')
        rev_info.add_sample(status='value', values=[1, 3])
        rev_info.add_sample(status='value', values=[10, 10])
        rev_info.add_sample(status='value', values=[1, 100])
        rev_info.reclassify(5, 3, 1)
        self.assertEqual(rev_info['old'], 2)
        self.assertEqual(rev_info['new'], 1)


class TestDiagnoseStates(unittest.TestCase):
    """Test core.Diagnose class."""

    def setUp(self):
        self.session_file = tempfile.mktemp()
        self._fake_config = core.DiagnoserConfig(
            session='session1',
            old_value=1.0,
            new_value=2.0,
        )

    def tearDown(self):
        if os.path.exists(self.session_file):
            os.unlink(self.session_file)

    def test_init_states(self):
        states = core.DiagnoseStates(self.session_file)
        states.init_states(self._fake_config)

        self.assertTrue(states.inited)
        self.assertEqual(states.config, self._fake_config)
        self.assertEqual(states.history, [])
        self.assertEqual(states.statistics, core.DiagnoseStatistics())

    def test_save_and_load(self):
        states = core.DiagnoseStates(self.session_file)
        states.init_states(self._fake_config)

        states.config.board = "board1"
        states.statistics.dut_leases_log_path = (
            '/some/other/path/to/dut_leases_log'
        )
        states.save()

        loaded_states = core.DiagnoseStates(self.session_file)
        loaded_states.load_states()

        self.assertTrue(loaded_states.inited)
        self.assertEqual(
            loaded_states.config,
            core.DiagnoserConfig(
                session='session1',
                old_value=1.0,
                new_value=2.0,
                board='board1',
            ),
        )
        self.assertEqual(loaded_states.history, [])
        self.assertEqual(
            loaded_states.statistics,
            core.DiagnoseStatistics(
                dut_leases_log_path='/some/other/path/to/dut_leases_log'
            ),
        )

    def test_load_empty_json_failed(self):
        with open(self.session_file, 'w') as f:
            f.write(json.dumps({}, indent=4, sort_keys=True))
        states = core.DiagnoseStates(self.session_file)
        states.load_states()

        self.assertFalse(states.inited)

    def test_add_history(self):
        states = core.DiagnoseStates(self.session_file)
        states.init_states(self._fake_config)

        states.add_history(event='event1', text='text1')
        states.add_history(event='event2', text='text2')

        loaded_states = core.DiagnoseStates(self.session_file)
        loaded_states.load_states()

        self.assertTrue(loaded_states.inited)
        history = loaded_states.history
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]['event'], 'event1')
        self.assertEqual(history[0]['text'], 'text1')
        self.assertEqual(history[1]['event'], 'event2')
        self.assertEqual(history[1]['text'], 'text2')

    def test_reset(self):
        states = core.DiagnoseStates(self.session_file)
        states.init_states(self._fake_config)
        states.save()

        states = core.DiagnoseStates(self.session_file)
        states.reset()
        self.assertFalse(states.inited)

        states = core.DiagnoseStates(self.session_file)
        # failed because the file doesn't exist.
        self.assertFalse(states.load_states())
        self.assertFalse(states.inited)

    def test_init_from_dict_converts_config(self):
        states = core.DiagnoseStates(self.session_file)
        fake_data = {
            'config': {
                'session': 'session1',
                'old_value': 1.0,
                'new_value': 2.0,
            },
            'history': [],
            'statistics': {},
        }
        with open(self.session_file, 'w') as f:
            json.dump(fake_data, f)
        self.assertTrue(states.load_states())
        self.assertTrue(states.inited)
        self.assertIsInstance(states.config, core.DiagnoserConfig)
        self.assertEqual(states.config.session, 'session1')
        self.assertEqual(states.config.old_value, 1.0)
        self.assertEqual(states.config.new_value, 2.0)


class TestBisectStates(unittest.TestCase):
    """Test core.BisectStates class."""

    def setUp(self):
        self.session_file = tempfile.mktemp()

        self.fake_config = core.BisectConfig(
            dut='fake_dut', test_name='fake_test'
        )
        self.fake_details = {'x': 'y'}

    def tearDown(self):
        if os.path.exists(self.session_file):
            os.unlink(self.session_file)

    def test_init_states(self):
        states = core.BisectStates(self.session_file)
        states.init_states(self.fake_config, ['a', 'b', 'c'], self.fake_details)

        self.assertTrue(states.inited)
        self.assertEqual(states.config, self.fake_config)
        self.assertEqual(states.details, self.fake_details)
        self.assertEqual(states.idx2rev(1), 'b')
        self.assertEqual(states.rev2idx('b'), 1)

    def test_strategy_states(self):
        states = core.BisectStates(self.session_file)
        states.init_states(self.fake_config, ['a', 'b', 'c'], self.fake_details)

        # Before setting.
        self.assertEqual(states.strategy_states, None)

        s = strategy_states.States(state='blah')
        states.strategy_states = s
        # modification after strategy_states() is not propagated.
        s.state = 'bar'
        # After setting.
        self.assertEqual(
            states.strategy_states, strategy_states.States(state='blah')
        )

        # Modification on the returned value doesn't affect the internal
        # value.
        st = states.strategy_states
        assert st is not None
        st.state = 'foo'
        self.assertEqual(
            states.strategy_states, strategy_states.States(state='blah')
        )

    def test_save_and_load(self):
        states = core.BisectStates(self.session_file)
        revlist = [str(i) for i in range(10)]
        states.init_states(core.BisectConfig(), revlist, self.fake_details)
        states.config.board = 'bar'
        strategy_states_to_save = strategy_states.States(
            state='blah',
        )
        states.strategy_states = strategy_states_to_save
        states.save()

        loaded_states = core.BisectStates(self.session_file)
        loaded_states.load_states()

        self.assertTrue(loaded_states.inited)
        self.assertEqual(loaded_states.config.board, 'bar')
        self.assertEqual(loaded_states.details, self.fake_details)
        self.assertEqual(loaded_states.rev2idx('5'), 5)
        self.assertEqual(loaded_states.strategy_states, strategy_states_to_save)

    def test_load_failed(self):
        with open(self.session_file, 'w') as f:
            f.write(json.dumps({}, indent=4, sort_keys=True))

        loaded_states = core.BisectStates(self.session_file)
        # failed because 'revlist' is not in the loaded dict.
        self.assertFalse(loaded_states.load_states())
        self.assertFalse(loaded_states.inited)

    def test_load_dict_config(self):
        session_file_content = {
            'config': {'term_old': 'bad', 'term_new': 'good'},
            'revlist': ['a', 'b'],
            'details': {},
            'history': [],
        }
        with open(self.session_file, 'w') as f:
            f.write(json.dumps(session_file_content, indent=4, sort_keys=True))

        states = core.BisectStates(self.session_file)
        states.load_states()

        self.assertTrue(states.inited)
        self.assertEqual(states.config.term_old, 'bad')
        self.assertEqual(states.config.term_new, 'good')

    def test_reset(self):
        states = core.BisectStates(self.session_file)
        revlist = [str(i) for i in range(10)]
        states.init_states(core.BisectConfig(), revlist)
        states.config.board = 'bar'
        states.save()

        states = core.BisectStates(self.session_file)
        states.reset()
        self.assertFalse(states.inited)

        states = core.BisectStates(self.session_file)
        # failed because the file doesn't exist.
        self.assertFalse(states.load_states())
        self.assertFalse(states.inited)

    def test_get_rev_info(self):
        session_file_content = {
            'config': {},
            'revlist': [str(i) for i in range(5)],
            'details': {},
            'history': [
                {
                    'rev': '0',
                    'event': 'sample',
                    'status': 'old',
                },
                {
                    'rev': '0',
                    'event': 'sample',
                    'status': 'skip',
                },
                {
                    'rev': '4',
                    'event': 'sample',
                    'status': 'skip',
                },
                {
                    'rev': '4',
                    'event': 'sample',
                    'status': 'new',
                },
            ],
        }
        with open(self.session_file, 'w') as f:
            f.write(json.dumps(session_file_content, indent=4, sort_keys=True))
        states = core.BisectStates(self.session_file)
        states.load_states()

        expected_rev_info = [core.RevInfo(rev=str(i)) for i in range(5)]
        expected_rev_info[0].result_counter = {
            'old': 1,
            'skip': 1,
        }
        expected_rev_info[4].result_counter = {
            'new': 1,
            'skip': 1,
        }
        for want, got in zip(expected_rev_info, states.get_rev_info()):
            self.assertEqual(want.to_dict(), got.to_dict())

    def test_get_rev_info_ignore_skip(self):
        session_file_content = {
            'config': {},
            'revlist': [str(i) for i in range(5)],
            'details': {},
            'history': [
                {
                    'rev': '0',
                    'event': 'sample',
                    'status': 'old',
                },
                {
                    'rev': '0',
                    'event': 'sample',
                    'status': 'skip',
                },
                {
                    'rev': '4',
                    'event': 'sample',
                    'status': 'skip',
                },
                {
                    'rev': '4',
                    'event': 'sample',
                    'status': 'new',
                },
            ],
        }
        with open(self.session_file, 'w') as f:
            f.write(json.dumps(session_file_content, indent=4, sort_keys=True))
        states = core.BisectStates(self.session_file)
        states.load_states()

        expected_rev_info = [core.RevInfo(rev=str(i)) for i in range(5)]
        expected_rev_info[0].result_counter = {
            'old': 1,
        }
        expected_rev_info[4].result_counter = {
            'new': 1,
        }
        for want, got in zip(
            expected_rev_info, states.get_rev_info(ignore_skip=True)
        ):
            self.assertEqual(want.to_dict(), got.to_dict())

    def test_get_rev_info_ignore_history(self):
        session_file_content = {
            'config': {},
            'revlist': [str(i) for i in range(5)],
            'details': {},
            'history': [
                {
                    'rev': '0',
                    'event': 'sample',
                    'status': 'old',
                },
                {
                    'rev': '0',
                    'event': 'sample',
                    'status': 'old',
                },
                {
                    'rev': '4',
                    'event': 'sample',
                    'status': 'new',
                },
                {
                    'rev': '4',
                    'event': 'sample',
                    'status': 'new',
                },
            ],
        }
        with open(self.session_file, 'w') as f:
            f.write(json.dumps(session_file_content, indent=4, sort_keys=True))
        states = core.BisectStates(self.session_file)
        states.load_states()

        expected_rev_info = [core.RevInfo(rev=str(i)) for i in range(5)]
        for want, got in zip(
            expected_rev_info, states.get_rev_info(ignore_history=True)
        ):
            self.assertEqual(want.to_dict(), got.to_dict())


class TestConfigFromDict(unittest.TestCase):
    """Test BisectConfig.from_dict() and DiagnoserConfig.from_dict()."""

    def test_empty_dict_returns_default_config(self):
        self.assertEqual(core.BisectConfig.from_dict({}), core.BisectConfig())
        self.assertEqual(
            core.DiagnoserConfig.from_dict({}), core.DiagnoserConfig()
        )

    def test_bisect_config_assigns_known_keys(self):
        config = core.BisectConfig.from_dict(
            {
                'board': 'eve',
                'old': '1.2.3',
                'new': '4.5.6',
                'is_public_build': True,
                'confidence': 0.99,
                'switch': [['./switch.py']],
            }
        )

        self.assertEqual(config.board, 'eve')
        self.assertEqual(config.old, '1.2.3')
        self.assertEqual(config.new, '4.5.6')
        self.assertTrue(config.is_public_build)
        self.assertEqual(config.confidence, 0.99)
        self.assertEqual(config.switch, [['./switch.py']])
        # Untouched fields keep their default value.
        self.assertEqual(config.eval, [])
        self.assertIsNone(config.dut)

    def test_diagnoser_config_assigns_known_keys(self):
        config = core.DiagnoserConfig.from_dict(
            {
                'session': 'session1',
                'board': 'eve',
                'old_value': 1.0,
                'chromium_patch_cls': ['crrev.com/c/123'],
            }
        )

        self.assertEqual(config.session, 'session1')
        self.assertEqual(config.board, 'eve')
        self.assertEqual(config.old_value, 1.0)
        self.assertEqual(config.chromium_patch_cls, ['crrev.com/c/123'])
        self.assertEqual(config.extra_test_variables, [])

    def test_unknown_key_raises(self):
        for cls in (core.BisectConfig, core.DiagnoserConfig):
            with self.subTest(cls=cls.__name__):
                with self.assertRaises(ValueError) as ctx:
                    cls.from_dict({'board': 'eve', 'no_such_key': 'value'})
                self.assertIn('no_such_key', str(ctx.exception))

    def test_experiments_restored_as_enum(self):
        for cls in (core.BisectConfig, core.DiagnoserConfig):
            with self.subTest(cls=cls.__name__):
                config = cls.from_dict(
                    {'experiments': ['stateless', 'vm']},
                )

                experiments = config.experiments
                self.assertEqual(
                    experiments,
                    [experiment.ID.STATELESS, experiment.ID.VM],
                )
                assert experiments is not None
                for value in experiments:
                    self.assertIsInstance(value, experiment.ID)

    def test_empty_experiments(self):
        for cls in (core.BisectConfig, core.DiagnoserConfig):
            with self.subTest(cls=cls.__name__):
                config = cls.from_dict({'experiments': []})

                self.assertEqual(config.experiments, [])

    def test_none_experiments(self):
        # DiagnoserConfig.experiments is optional, so a session file may store
        # null. It should be loaded as-is instead of raising TypeError.
        config = core.DiagnoserConfig.from_dict({'experiments': None})

        self.assertIsNone(config.experiments)

    def test_unknown_experiment_raises(self):
        with self.assertRaises(ValueError):
            core.DiagnoserConfig.from_dict({'experiments': ['no_such_exp']})

    def _write_session_file(self, data: dict) -> str:
        """Writes `data` to a temporary session file and returns its path."""
        session_file = tempfile.mktemp()
        self.addCleanup(os.unlink, session_file)
        with open(session_file, 'w') as f:
            json.dump(data, f)
        return session_file

    def test_diagnose_states_load_converts_experiments(self):
        session_file = self._write_session_file(
            {
                'config': {'session': 'session1', 'experiments': ['vm']},
                'history': [],
                'statistics': {},
            }
        )

        states = core.DiagnoseStates(session_file)

        self.assertTrue(states.load_states())
        experiments = states.config.experiments
        self.assertEqual(experiments, [experiment.ID.VM])
        assert experiments is not None
        self.assertIsInstance(experiments[0], experiment.ID)

    def test_bisect_states_load_converts_experiments(self):
        session_file = self._write_session_file(
            {
                'config': {'board': 'eve', 'experiments': ['vm']},
                'revlist': ['a', 'b'],
                'history': [],
            }
        )

        states = core.BisectStates(session_file)

        self.assertTrue(states.load_states())
        experiments = states.config.experiments
        self.assertEqual(experiments, [experiment.ID.VM])
        self.assertIsInstance(experiments[0], experiment.ID)

    def test_diagnose_states_load_unknown_key_raises(self):
        session_file = self._write_session_file(
            {
                'config': {'obsolete_option': True},
                'history': [],
                'statistics': {},
            }
        )

        states = core.DiagnoseStates(session_file)

        with self.assertRaises(ValueError):
            states.load_states()


class TestSerialization(unittest.TestCase):
    """Test serialization of dataclasses."""

    def test_bisect_config_serialization(self):
        config = core.BisectConfig(board='foo', term_old='bad')

        # (1) Convert BisectConfig with StatesEncoder then with rebuild_from_json
        serialized_json = json.dumps(config, cls=core.StatesEncoder)
        loaded_dict = json.loads(serialized_json)
        rebuilt_config = core.rebuild_from_json(loaded_dict)

        self.assertIsInstance(rebuilt_config, core.BisectConfig)
        self.assertEqual(rebuilt_config.board, 'foo')
        self.assertEqual(rebuilt_config.term_old, 'bad')

    def test_rebuild_from_json(self):
        # (2) Convert the json with rebuild_from_json and confirm it back to BisectConfig
        json_data = {
            '__class__': {
                '__module__': 'bisect_kit.core',
                '__name__': 'BisectConfig',
            },
            'board': 'bar',
            'term_old': 'old_term',
        }
        rebuilt_config = core.rebuild_from_json(json_data)

        self.assertIsInstance(rebuilt_config, core.BisectConfig)
        self.assertEqual(rebuilt_config.board, 'bar')
        self.assertEqual(rebuilt_config.term_old, 'old_term')


if __name__ == '__main__':
    unittest.main()
