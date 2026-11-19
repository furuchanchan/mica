"""設定とCLIの回帰試験。外部通信・Keychain・利用者の設定には触れない。"""
import argparse
import contextlib
import copy
import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import mica_config
import mica_connect
import mica_llm

ROOT = Path(__file__).resolve().parent.parent
loader = importlib.machinery.SourceFileLoader('mica_cli_test', str(ROOT / 'mica'))
spec = importlib.util.spec_from_loader(loader.name, loader)
cli = importlib.util.module_from_spec(spec)
loader.exec_module(cli)


class ConfigCLITest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='.test-config-', dir=str(ROOT))
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'config.json'
        self.env = patch.dict(os.environ, {'MICA_CONFIG': str(self.path), 'HOME': self.tmp.name}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        for target in ('subprocess.run', 'urllib.request.urlopen'):
            guard = patch(target, side_effect=AssertionError('外部操作は禁止'))
            guard.start()
            self.addCleanup(guard.stop)
        mica_config.reload()
        mica_config.stored_key.cache_clear()
        self.addCleanup(mica_config.reload)
        self.addCleanup(mica_config.stored_key.cache_clear)
        self.output = io.StringIO()
        self.redirect = contextlib.redirect_stdout(self.output)
        self.redirect.__enter__()
        self.addCleanup(self.redirect.__exit__, None, None, None)

    def args(self, **changes):
        values = dict(provider='ollama', model='example', advice=True, analysis=False,
                      transcription_check=False, base_url=None, key_env=None, no_check=False)
        values.update(changes)
        return argparse.Namespace(**values)

    def seed(self, value):
        self.path.write_text(json.dumps(value), encoding='utf-8')
        mica_config.reload()

    def test_top_level_and_nested_types(self):
        cases = [[], None, {'ai': []}, {'calendar': {'enabled': 'false'}},
                 {'context': {'enabled': 1}}, {'data_root': []}, {'user_name': 2},
                 {'calendar': {'calendars': 'primary'}},
                 {'delivery': {'slack': {'command': 'send'}}},
                 {'delivery': {'knowledge_base': {'ingest_command': [1]}}},
                 {'ignore_mic_apps': [None]}, {'ai': {'providers': []}},
                 {'ai': {'providers': {'bad': {'base_url': 'http://localhost/v1', 'api_key_env': 12}}}}]
        for raw in cases:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                mica_config.check(raw, self.path)

    def test_bad_roles_and_models(self):
        for role, value in [('advice', 'claude_cli/x'), ('advice', '/model'),
                            ('analysis', 'ollama/'), ('analysis', 'ollama'),
                            ('analysis', 'claude_cli/')]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                mica_config.check({'ai': {role: value}}, self.path)

    def test_invalid_values_not_in_error(self):
        with self.assertRaises(ValueError) as error:
            mica_config.check({'ai': {'analysis': 'SECRET'}}, self.path)
        self.assertNotIn('SECRET', str(error.exception))

    def test_success_atomic_private_file(self):
        with patch.object(mica_llm, 'check_connection', return_value=.1) as check:
            self.assertEqual(cli.connect(self.args()), 0)
        self.assertEqual(json.loads(self.path.read_text())['ai']['advice'], 'ollama/example')
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        check.assert_called_once()
        self.assertEqual(list(self.path.parent.glob('config.json.*')), [])  # 書きかけの一時ファイルを残さない

    def test_failed_check_preserves_exact_previous_file(self):
        self.seed({'ai': {'advice': 'ollama/old'}})
        before = self.path.read_bytes()
        with patch.object(mica_llm, 'check_connection', side_effect=RuntimeError('REASON')):
            with self.assertRaises(SystemExit) as error:
                cli.connect(self.args(base_url='http://localhost:11434/v1'))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertIn('REASON', str(error.exception))  # つながらなかった理由は見せる

    def test_model_failure_does_not_create_custom_provider(self):
        with patch.object(cli, 'choose_model', side_effect=SystemExit('モデル不明')):
            with self.assertRaises(SystemExit):
                cli.connect(self.args(provider='custom', model=None, base_url='http://localhost:1234/v1'))
        self.assertFalse(self.path.exists())

    def test_no_cloud_failure_does_not_write(self):
        os.environ['MICA_NO_CLOUD'] = '1'
        with self.assertRaises(SystemExit):
            cli.connect(self.args(provider='custom', base_url='https://example.invalid/v1'))
        self.assertFalse(self.path.exists())

    def test_transcription_only_does_not_choose_model_or_roles(self):
        with patch.object(mica_config, 'stored_key', return_value='test-key'), \
             patch.object(cli, 'choose_model', side_effect=AssertionError('モデル選択は禁止')), \
             patch.object(mica_llm, 'check_connection', side_effect=AssertionError('会話試験は禁止')):
            self.assertEqual(cli.connect(self.args(provider='openai', model=None, advice=False,
                                                  transcription_check=True)), 0)
        self.assertEqual(json.loads(self.path.read_text()), {'ai': {'transcription_check': 'openai'}})
        self.assertIn('音声の送信試験は行っていません', self.output.getvalue())

    def test_invalid_claude_role_is_rejected_before_probe(self):
        with patch.object(cli.shutil, 'which', side_effect=AssertionError('探索は禁止')):
            with self.assertRaises(SystemExit):
                cli.connect(self.args(provider='claude_cli'))
        self.assertFalse(self.path.exists())

    def test_claude_status_respects_no_cloud(self):
        self.seed({'ai': {'analysis': 'claude_cli'}})
        os.environ['MICA_NO_CLOUD'] = 'TRUE'
        with patch.object(cli.shutil, 'which', side_effect=AssertionError('探索は禁止')):
            line, usable, outside = cli.role_line('analysis')
        self.assertFalse(usable)
        self.assertIsNone(outside)
        self.assertIn('MICA_NO_CLOUD', line)

    def test_status_lists_enabled_external_integrations(self):
        self.seed({'calendar': {'enabled': True},
                   'ai': {'analysis': 'ollama/model'},
                   'delivery': {'drive': {'enabled': True}, 'slack': {'enabled': True},
                                'knowledge_base': {'enabled': True}}})
        cli.status(None)
        text = self.output.getvalue()
        for name in ('Google Calendar', 'Google Drive', 'Slack', '知識ベースの外部コマンド'):
            self.assertIn(name, text)
        self.assertNotIn('なし（音声も文字も', text)

    def test_status_external_integrations_stop_under_no_cloud(self):
        self.seed({'calendar': {'enabled': True}, 'ai': {'analysis': 'ollama/model'},
                   'delivery': {'drive': {'enabled': True}}})
        os.environ['MICA_NO_CLOUD'] = '1'
        cli.status(None)
        text = self.output.getvalue()
        self.assertIn('Google Calendar', text)
        self.assertIn('ため停止', text)
        self.assertIn('Mac の外へ送る先: なし', text)

    def test_port_range(self):
        for value in ('0', '65536', '-1', 'SECRET'):
            os.environ['MICA_APP_PORT'] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                mica_config.app_port()

    def test_invalid_raw_config_is_rejected(self):
        self.seed([])
        with self.assertRaises(ValueError):
            cli.raw_config()

    def test_disconnect_conflict_guard(self):
        self.seed({'ai': {'advice': 'ollama/old', 'analysis': 'ollama/model'}})
        self.assertEqual(cli.disconnect(argparse.Namespace(target='advice')), 0)
        self.assertIsNone(json.loads(self.path.read_text())['ai']['advice'])
        self.assertEqual(json.loads(self.path.read_text())['ai']['analysis'], 'ollama/model')


    # ここから下は、画面（Mica.app の「AI をつなぐ」）とターミナルが共に通る mica_connect
    def test_app_connect_tests_before_saving_key_and_config(self):
        order = []
        with patch.object(mica_connect, 'key_present', return_value=False), \
             patch.object(mica_config, 'stored_key', return_value=''), \
             patch.object(mica_llm, 'check_connection', side_effect=lambda found: order.append(('test', found['key'], found['model'])) or .2), \
             patch.object(mica_connect, 'save_key', side_effect=lambda name, key: order.append(('key', name, key))):
            done = mica_connect.connect('openai', 'gpt-x', ['advice'], key='KEY-12345678')
        self.assertEqual(order, [('test', 'KEY-12345678', 'gpt-x'), ('key', 'openai', 'KEY-12345678')])
        self.assertEqual(json.loads(self.path.read_text()), {'ai': {'advice': 'openai/gpt-x'}})
        self.assertNotIn('KEY-12345678', self.path.read_text() + done['message'])
        self.assertIn('OpenAI へ送られます', done['message'])

    def test_app_connect_failure_changes_nothing(self):
        self.seed({'ai': {'advice': 'ollama/old'}})
        before = self.path.read_bytes()
        with patch.object(mica_config, 'stored_key', return_value=''), \
             patch.object(mica_llm, 'check_connection', side_effect=RuntimeError('REASON')), \
             patch.object(mica_connect, 'save_key', side_effect=AssertionError('鍵の保存は禁止')):
            with self.assertRaises(ValueError) as error:
                mica_connect.connect('openai', 'gpt-x', ['advice', 'analysis'], key='KEY-12345678')
        self.assertIn('REASON', str(error.exception))
        self.assertEqual(self.path.read_bytes(), before)

    def test_app_connect_requires_key_model_and_role(self):
        with patch.object(mica_config, 'stored_key', return_value=''):
            for arguments, word in ((('openai', 'gpt-x', ['advice']), '鍵'), (('ollama', '', ['advice']), 'モデル'),
                                    (('ollama', 'm', []), '使い道'), (('claude_cli', None, ['advice', 'analysis']), '分析だけ')):
                with self.subTest(arguments=arguments), self.assertRaises(ValueError) as error:
                    mica_connect.connect(*arguments)
                self.assertIn(word, str(error.exception))
        self.assertFalse(self.path.exists())

    def test_app_custom_server_with_key_gets_env_name(self):
        with patch.object(mica_config, 'stored_key', return_value=''), \
             patch.object(mica_llm, 'check_connection', return_value=.1), \
             patch.object(mica_connect, 'save_key') as saved:
            mica_connect.connect('my-server', 'm', ['analysis'], base_url='http://192.168.1.10:8000/v1', key='KEY-12345678')
        saved.assert_called_once_with('my-server', 'KEY-12345678')
        self.assertEqual(json.loads(self.path.read_text())['ai'], {
            'providers': {'my-server': {'base_url': 'http://192.168.1.10:8000/v1', 'api_key_env': 'MY_SERVER_API_KEY'}},
            'analysis': 'my-server/m'})

    def test_save_key_rejects_shapes_that_could_break_the_command(self):
        for key in ('short', 'has space 12345', 'quote"12345678', "quote'12345678", 'back\\slash123', 'line\nbreak12345'):
            with self.subTest(key=key), self.assertRaises(ValueError):
                mica_connect.save_key('openai', key)  # subprocess.run は setUp で禁止している（呼べば落ちる）
        with self.assertRaises(ValueError):
            mica_connect.save_key('bad name', 'KEY-12345678')

    def test_save_key_goes_through_stdin_not_arguments(self):
        calls = []

        def fake(arguments, **options):
            calls.append((arguments, options.get('input')))
            return argparse.Namespace(returncode=0, stdout='KEY-12345678\n')
        with patch('subprocess.run', side_effect=fake):
            mica_connect.save_key('openai', 'KEY-12345678')
        self.assertEqual(calls[0][0], ['security', '-i'])
        self.assertIn('KEY-12345678', calls[0][1])
        self.assertTrue(all('KEY-12345678' not in ' '.join(arguments) for arguments, _ in calls))

    def test_disconnect_analysis_says_what_to_turn_off(self):
        seeded = {'ai': {'advice': 'ollama/a', 'analysis': 'ollama/model'}, 'delivery': {'slack': {'enabled': True}},
                  'negotiation_notes': '/tmp/notes.md'}
        self.seed(seeded)
        for target in ('analysis', 'all'):
            with self.subTest(target=target), self.assertRaises(ValueError) as error:
                mica_connect.disconnect(target)
            self.assertIn('delivery.slack.enabled を false', str(error.exception))
            self.assertIn('negotiation_notes を null', str(error.exception))
        self.assertEqual(json.loads(self.path.read_text()), seeded)
        mica_connect.disconnect('advice')
        self.assertIsNone(json.loads(self.path.read_text())['ai']['advice'])

    def test_overview_lists_stages_and_hides_cloud_under_no_cloud(self):
        self.seed({'ai': {'advice': 'ollama/a', 'providers': {'mine': {'base_url': 'http://localhost:9/v1'}}}})
        os.environ['MICA_NO_CLOUD'] = '1'
        with patch.object(mica_connect, 'key_present', return_value=False):
            seen = mica_connect.overview()
        self.assertEqual([(stage['role'], stage['usable']) for stage in seen['stages']], [('advice', True), ('analysis', False)])
        self.assertEqual(seen['outside'], [])
        allowed = {item['name'] for item in seen['providers'] if item['allowed']}
        self.assertEqual(allowed, {'ollama', 'lmstudio', 'mine'})

    def test_http_ai_request_shapes(self):
        import shodan_live
        with patch.object(mica_connect, 'key_present', return_value=False):
            with patch.object(mica_llm, 'check_connection', return_value=1.25):
                done = shodan_live.ai_request('/api/ai/connect', {'provider': 'ollama', 'model': ' m ', 'roles': ['advice']})
            self.assertIn('返事が来ました（1.2秒）', done['message'])
            self.assertEqual(done['overview']['stages'][0]['spec'], 'ollama/m')
            self.assertIsNone(shodan_live.ai_request('/api/ai/disconnect', {'target': 'advice'})['overview']['stages'][0]['spec'])
        for path, body in (('/api/ai/connect', {'provider': 'ollama', 'model': 'm', 'roles': 'advice'}),
                           ('/api/ai/connect', {'provider': 5, 'roles': []}), ('/api/ai/disconnect', {'target': 'everything'}),
                           ('/api/ai/unknown', {})):
            with self.subTest(path=path, body=body), self.assertRaises(ValueError):
                shodan_live.ai_request(path, body)


    def test_http_ai_changes_need_the_app_token(self):
        import http.client
        import threading
        from http.server import ThreadingHTTPServer
        import shodan_live
        server = ThreadingHTTPServer(('127.0.0.1', 0), shodan_live.Handler)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        def post(token):
            headers = {'Origin': f'http://127.0.0.1:{port}', 'Content-Type': 'application/json'}
            if token is not None:
                headers['X-Mica-Token'] = token
            link = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
            link.request('POST', '/api/ai/disconnect', json.dumps({'target': 'advice'}), headers)
            answer = link.getresponse()
            answer.read()
            link.close()
            return answer.status
        self.seed({'ai': {'advice': 'ollama/a'}})
        with patch.object(shodan_live, 'PORT', port), patch.object(mica_connect, 'key_present', return_value=False):
            with patch.object(shodan_live, 'APP_TOKEN', ''):  # 見張り役の側のサーバー（合言葉なし）では、正しい形の要求でも通さない
                self.assertEqual(post(''), 403)
            with patch.object(shodan_live, 'APP_TOKEN', 'app-token'):
                self.assertEqual([post(None), post('wrong'), post('tökén')], [403, 403, 403])
                self.assertEqual(json.loads(self.path.read_text())['ai']['advice'], 'ollama/a')
                self.assertEqual(post('app-token'), 200)
        self.assertIsNone(json.loads(self.path.read_text())['ai']['advice'])


if __name__ == '__main__':
    unittest.main()
