"""接続・分析・音声の送信境界を確かめる。ネットワークも Keychain も使わない。"""
import copy
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

WORK = tempfile.TemporaryDirectory(prefix='.test-llm-', dir=Path(__file__).resolve().parent)
ROOT = Path(WORK.name)
os.environ.update(MICA_CONFIG=str(ROOT / 'config.json'), MICA_APP_SUPPORT=str(ROOT / 'support'), MICA_NO_CLOUD='1')
(ROOT / 'config.json').write_text('{}')
import mica_config
import mica_http
import mica_llm
import shodan_analyze as analyze
import shodan_transcribe as transcribe
import shodan_calendar as calendar
import shodan_storage as storage
import shodan_live as live
from shodan_grounding import POLICY, render_analysis

SCHEMA = {'type': 'object', 'properties': {'answer': {'type': 'string'}}, 'required': ['answer'], 'additionalProperties': False}
BASE = 'http://127.0.0.1:12345/v1'


class LLMTest(unittest.TestCase):
    def setUp(self):
        for guard in (patch.object(socket, 'socket', side_effect=AssertionError('試験中の通信は禁止')),
                      patch.object(subprocess, 'run', side_effect=AssertionError('試験中の外部コマンドは禁止')),
                      patch.object(mica_config, 'stored_key', return_value='sk-test'),
                      patch.object(urllib.request.OpenerDirector, 'open', side_effect=self.respond)):
            guard.start()
            self.addCleanup(guard.stop)
        os.environ['MICA_NO_CLOUD'] = '1'
        self.seen, self.reply, self.result, self.error = [], {'answer': 'はい'}, None, None
        self.configure()
        self.folder = ROOT / self._testMethodName
        self.folder.mkdir()
        self.ledger = {'policy': POLICY, 'utterances': [{'id': 'u00001', 'text': '見積もりに三日かかる', 'start': 0, 'who': '相手'}]}
        for name, value in [('analysis-evidence.json', self.ledger), ('meta.json', {'title': 'テスト', 'test': True}),
                            ('analysis-reference.json', {'brief': {'purpose': '条件を確かめる'}, 'advice-history.jsonl': '未合意の提案'})]:
            (self.folder / name).write_text(json.dumps(value, ensure_ascii=False))

    def configure(self, **ai):
        providers = {name: {'base_url': BASE, 'api_key_env': 'TEST_KEY'} for name in ('openai', 'anthropic')}
        providers.update({name: {'base_url': BASE, 'api_key_env': None} for name in ('mine', 'ollama', 'lmstudio', 'gemini', 'groq', 'openrouter')})
        ai.setdefault('providers', providers)
        self.write_config({'ai': ai})

    def write_config(self, value):
        (ROOT / 'config.json').write_text(json.dumps(dict(data_root=str(ROOT / 'data'), **value)))
        mica_config.reload()

    def respond(self, request, timeout=None):
        body = json.loads(request.data) if request.data and request.get_header('Content-type') == 'application/json' else request.data
        self.seen.append((request, body))
        if self.error:
            code, detail = self.error
            raise urllib.error.HTTPError(request.full_url, code, 'failed', {}, io.BytesIO(json.dumps({'error': {'message': detail}}).encode()))
        if self.result is not None:
            result = self.result
        elif request.get_method() == 'GET':
            result = {'data': [{'id': 'b'}, {'id': 'a'}]}
        elif request.full_url.endswith('/audio/transcriptions'):
            result = {'text': '日本語の会話'}
        else:
            answer = json.dumps(self.reply, ensure_ascii=False)
            if request.full_url.endswith('/responses'):
                result = {'status': 'completed', 'output': [{'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': answer}]}]}
            elif request.full_url.endswith('/messages'):
                result = {'stop_reason': 'end_turn', 'content': [{'type': 'thinking'}, {'type': 'text', 'text': answer}]}
            else:
                result = {'choices': [{'finish_reason': 'stop', 'message': {'content': answer}}]}
        return io.BytesIO(json.dumps(result).encode())

    def ask(self, role='advice'):
        return json.loads(mica_llm.complete_json(role, '指示', '入力', 'test', SCHEMA))

    def valid_draft(self):
        return {'kind': '仕事', 'sections': [{'number': n, 'source_ids': ['u00001'], 'ai_proposals': ['条件を聞く'], 'visual_observations': []} for n in (1, 2, 7)]}

    def draft(self):
        return analyze.draft_by_api(str(self.folder), '分析の指示', self.folder / 'analysis-draft.json')

    def test_unconfigured_keys_do_not_send(self):
        self.write_config({})
        with self.assertRaisesRegex(mica_llm.NotConnected, 'ai.advice'):
            self.ask()
        with self.assertRaises(mica_llm.NotConnected):
            transcribe._post([], self.folder / 'missing.wav')
        self.assertEqual(self.seen, [])

    def test_provider_wire_shapes(self):
        for provider in ('openai', 'anthropic', 'gemini', 'groq', 'openrouter', 'ollama', 'lmstudio', 'mine'):
            with self.subTest(provider=provider):
                self.configure(advice=provider + '/test-model')
                self.assertEqual(self.ask(), {'answer': 'はい'})
                request, body = self.seen[-1]
                self.assertEqual(body['model'], 'test-model')
                if provider == 'openai':
                    self.assertTrue(request.full_url.endswith('/responses'))
                    self.assertFalse(body['store'])
                    self.assertEqual(body['text']['format'], {'type': 'json_schema', 'name': 'test', 'strict': True, 'schema': SCHEMA})
                    self.assertEqual(body['input'], [{'role': 'user', 'content': '入力'}])
                elif provider == 'anthropic':
                    self.assertTrue(request.full_url.endswith('/messages'))
                    self.assertEqual(request.get_header('Anthropic-version'), mica_llm.ANTHROPIC_VERSION)
                    self.assertEqual(request.get_header('X-api-key'), 'sk-test')
                    self.assertIsNone(request.get_header('Authorization'))
                    self.assertEqual(body['output_config'], {'format': {'type': 'json_schema', 'schema': SCHEMA}})
                else:
                    self.assertTrue(request.full_url.endswith('/chat/completions'))
                    self.assertIsNone(request.get_header('Authorization'))
                    self.assertEqual(body['response_format'], {'type': 'json_schema', 'json_schema': {'name': 'test', 'strict': True, 'schema': SCHEMA}})

    def test_roles_and_slash_model(self):
        self.configure(advice='mine/org/model', analysis='anthropic/other')
        self.ask()
        self.assertEqual(self.seen[-1][1]['model'], 'org/model')
        self.ask('analysis')
        self.assertTrue(self.seen[-1][0].full_url.endswith('/messages'))

    def test_missing_key_does_not_send(self):
        self.configure(advice='openai/model')
        with patch.object(mica_config, 'stored_key', return_value=''), self.assertRaisesRegex(mica_llm.NotConnected, '鍵が見つかりません'):
            self.ask()
        self.assertEqual(self.seen, [])

    def test_no_cloud_at_transport_and_models(self):
        self.configure(advice='groq/model', providers={})
        with self.assertRaisesRegex(mica_llm.NotConnected, 'MICA_NO_CLOUD'):
            self.ask()
        found = dict(mica_llm.PROVIDERS['openai'], key='sk-test', model='x')
        for call in (lambda: mica_llm.models(found), lambda: mica_llm._post(found, '/responses', {}, {}, 1)):
            with self.assertRaisesRegex(RuntimeError, 'MICA_NO_CLOUD'):
                call()
        self.assertEqual(self.seen, [])

    def test_unsafe_urls_and_loopback(self):
        for url in ('file:///secret', 'https://user:secret@example.com/v1', 'http://localhost:99999/v1', 'https://example.com/v1?key=secret',
                    'https://example.com/v1#secret', 'http://localhost\\@evil/v1', 'http://localhost\n/v1', 'http:///v1'):
            with self.subTest(url=url), self.assertRaises(ValueError) as caught:
                mica_llm.provider('mine', {'mine': {'base_url': url}})
            self.assertNotIn('secret', str(caught.exception))
        for host in ('localhost', '127.0.0.1', '[::1]'):
            self.assertTrue(mica_llm.is_local({'base_url': 'http://' + host + '/v1'}))
        for host in ('localhost.evil.invalid', '127.0.0.1.evil.invalid', '192.168.0.1', '2130706433'):
            self.assertFalse(mica_llm.is_local({'base_url': 'http://' + host + '/v1'}))

    def test_redirects_are_not_followed(self):
        for code in (301, 302, 303, 307, 308):
            request = urllib.request.Request(BASE + '/responses', data=b'private', headers={'Authorization': 'Bearer sk-test'})
            with io.BytesIO() as response, self.assertRaises(urllib.error.HTTPError):
                mica_http.NoRedirect().redirect_request(request, response, code, 'moved', {}, 'https://elsewhere.invalid')
        self.assertEqual(self.seen, [])

    def test_local_skips_proxy_and_localhost_pinned(self):
        with patch.dict(os.environ, {'http_proxy': 'http://outside.invalid:80'}), patch.object(urllib.request, 'build_opener', wraps=urllib.request.build_opener) as build:
            with mica_http.urlopen('http://localhost:12345/v1/models', timeout=1):
                pass
            # この Mac の中の宛先は、プロキシを通さない
            self.assertEqual([h.proxies for h in build.call_args.args if isinstance(h, urllib.request.ProxyHandler)], [{}])
            self.assertTrue(any(isinstance(h, mica_http.NoRedirect) for h in build.call_args.args))
            os.environ['MICA_NO_CLOUD'] = ''
            with mica_http.urlopen('https://api.example.invalid/v1/models', timeout=1):
                pass
            # Mac の外の宛先は、使う人のネットワークの設定に従う（プロキシを消さない）。転送には従わない
            self.assertFalse(any(isinstance(h, urllib.request.ProxyHandler) for h in build.call_args.args))
            self.assertTrue(any(isinstance(h, mica_http.NoRedirect) for h in build.call_args.args))
        self.assertTrue(self.seen[0][0].full_url.startswith('http://127.0.0.1:12345/'))

    def test_errors_show_reason_without_key_and_do_not_switch_provider(self):
        self.configure(advice='openai/x')
        for code in (401, 404, 429, 500):
            self.error = (code, 'key sk-test rejected: model-not-found')
            before = len(self.seen)
            with self.assertRaisesRegex(RuntimeError, 'HTTP ' + str(code)) as caught:
                self.ask()
            # 相手の返した理由は見せる。鍵が写っていたら伏せる。別の先へやり直さない
            self.assertNotIn('sk-test', str(caught.exception))
            self.assertIn('model-not-found', str(caught.exception))
            self.assertEqual(len(self.seen) - before, 1)
        with patch.object(urllib.request.OpenerDirector, 'open', side_effect=urllib.error.URLError('Connection refused')):
            with self.assertRaisesRegex(RuntimeError, 'Connection refused'):
                self.ask()

    def test_schema_retry_stays_at_same_destination(self):
        self.configure(advice='mine/x')
        calls = []
        def respond(request, timeout=None):
            calls.append(request)
            if len(calls) == 1:
                raise urllib.error.HTTPError(request.full_url, 400, 'bad', {}, io.BytesIO(b'{"error":{"message":"json_schema unsupported"}}'))
            return self.respond(request, timeout)
        with patch.object(urllib.request.OpenerDirector, 'open', side_effect=respond):
            self.ask()
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0].full_url, calls[1].full_url)
        self.assertEqual(json.loads(calls[1].data)['response_format'], {'type': 'json_object'})

    def test_incomplete_and_malformed_responses_fail(self):
        fixtures = [('openai', {'status': 'incomplete', 'output': []}), ('openai', {'status': 'completed', 'output': 3}),
                    ('openai', {'status': 'completed', 'output': [{'type': 'message', 'role': 'assistant', 'content': 3}]}),
                    ('anthropic', {'stop_reason': 'refusal'}), ('anthropic', {'stop_reason': 'max_tokens'}),
                    ('anthropic', {'stop_reason': 'tool_use', 'content': [{'type': 'text', 'text': '{}'}]}),
                    ('anthropic', {'stop_reason': 'end_turn', 'content': 3}),
                    ('mine', {'choices': [{'finish_reason': 'length', 'message': {'content': '{}'}}]}),
                    ('mine', {'choices': [{'finish_reason': 'stop', 'message': 'bad'}]}),
                    ('mine', {'choices': [{'finish_reason': 'content_filter', 'message': {'content': '{}'}}]}),
                    ('mine', {'choices': [{'finish_reason': 'stop', 'message': {'refusal': 'no'}}]}),
                    ('mine', {'choices': []}), ('mine', [])]
        for provider, result in fixtures:
            with self.subTest(provider=provider, result=result):
                self.configure(advice=provider + '/x')
                self.result = result
                with self.assertRaises(RuntimeError):
                    self.ask()

    def test_models_and_check(self):
        self.configure(advice='openai/x')
        self.assertEqual(mica_llm.models(mica_llm.resolve('advice')), ['a', 'b'])
        self.reply = {'ok': True}
        self.assertGreaterEqual(mica_llm.check('advice'), 0)
        self.reply = {'ok': 'sk-test'}
        with self.assertRaises(RuntimeError) as caught:
            mica_llm.check('advice')
        self.assertNotIn('sk-test', str(caught.exception))
        self.result = {'data': 'not a list'}
        with self.assertRaises(RuntimeError):
            mica_llm.models(mica_llm.resolve('advice'))

    def test_api_draft_contains_reference_and_validates(self):
        self.configure(analysis='mine/x')
        self.reply = self.valid_draft()
        self.draft()
        sent = self.seen[-1][1]['messages'][1]['content']
        for word in ('見積もりに三日かかる', 'テスト', '条件を確かめる', '未合意の提案'):
            self.assertIn(word, sent)
        self.assertEqual(json.loads((self.folder / 'analysis-draft.json').read_text()), self.reply)

    def rendered(self):
        """API の下書きを作り、公開の前の確かめ（analyze と同じ render_analysis）を通す。"""
        self.draft()
        value = json.loads((self.folder / 'analysis-draft.json').read_text())
        render_analysis(value, self.ledger, (), test=True)
        return value

    def test_api_draft_fills_only_skipped_chapters(self):
        self.configure(analysis='mine/x')
        draft = self.valid_draft()
        # 小さいモデルが飛ばした章は「引用なし」の空の章として置く。渡していない画像の観察は消す
        self.reply = dict(draft, sections=[dict(draft['sections'][1], visual_observations=[{'frame': 'not-provided.jpg', 'text': '見た'}])])
        value = self.rendered()
        self.assertEqual([section['number'] for section in value['sections']], [1, 2, 7])
        self.assertEqual([section['source_ids'] for section in value['sections']], [[], ['u00001'], []])
        self.assertTrue(all(section['visual_observations'] == [] for section in value['sections']))

    def test_api_draft_does_not_hide_invalid_evidence(self):
        self.configure(analysis='mine/x')
        draft = self.valid_draft()
        # 同じ章が2つ・決まりに無い章・無い発言ID・余分な項目は、補ったり捨てたりせずに止める
        invalid = [[], {'sections': 'x'}, dict(draft, kind='other'), dict(draft, sections=draft['sections'] + [draft['sections'][0]]),
                   dict(draft, sections=draft['sections'] + [dict(draft['sections'][0], number=9)])]
        for key, value in (('source_ids', ['unknown']), ('number', True), ('number', []), ('extra', 'private')):
            item = copy.deepcopy(draft)
            item['sections'][0][key] = value
            invalid.append(item)
        for item in invalid:
            with self.subTest(item=item):
                self.reply = item
                with self.assertRaises((RuntimeError, ValueError)):
                    self.rendered()

    def test_transcription_endpoint_and_wire(self):
        self.configure(transcription_check='openai')
        audio = self.folder / 'speech.wav'
        audio.write_bytes(b'fake-audio')
        with patch('shodan_transcribe.vocabulary', return_value=[]):
            self.assertEqual(transcribe.transcribe_text(audio), '日本語の会話')
        request, body = self.seen[-1]
        self.assertEqual(request.full_url, BASE + '/audio/transcriptions')
        self.assertEqual(request.get_header('Authorization'), 'Bearer sk-test')
        self.assertIn(b'gpt-transcribe', body)
        self.assertIn(b'name="languages[]"\r\n\r\nja', body)
        self.configure(transcription_check='openai', providers={'openai': {'base_url': BASE, 'api_key_env': None}})
        self.assertTrue(mica_llm.transcription_connected())
        transcribe._post([], audio)
        self.assertIsNone(self.seen[-1][0].get_header('Authorization'))

    def test_transcription_guard_before_audio_read(self):
        for ai in ({}, {'transcription_check': 'openai'}):
            self.write_config({'ai': ai})
            with self.assertRaises(mica_llm.NotConnected):
                transcribe._post([], self.folder / 'missing.wav')
        self.assertEqual(self.seen, [])

    def test_no_cloud_blocks_integrations(self):
        self.write_config({'ai': {'analysis': 'ollama/x'}, 'calendar': {'enabled': True, 'account': 'test'}, 'delivery': {'drive': {'enabled': True}, 'slack': {'enabled': True, 'channel': 'c', 'command': ['not-run']}}})
        for call in (calendar._token, lambda: storage.access_token('仕事'), lambda: storage.api('secret', 'https://www.googleapis.com/x'), lambda: storage.ensure_uploaded(self.folder, 'test', '仕事'), lambda: analyze.slack('meeting')):
            with self.assertRaisesRegex(RuntimeError, 'MICA_NO_CLOUD'):
                call()
        self.assertEqual(calendar.CALENDAR.snapshot()['events'], [])
        self.assertEqual(storage.cleanup_recording(self.folder, apply=True)['status'], 'preserved')
        self.assertEqual(self.seen, [])

    def test_drive_rejects_arbitrary_upload_location(self):
        os.environ['MICA_NO_CLOUD'] = ''
        self.write_config({'ai': {'analysis': 'ollama/x'}, 'delivery': {'drive': {'enabled': True}}})
        with self.assertRaisesRegex(RuntimeError, '保存先を確認できません'):
            storage.api('secret', 'https://other.invalid/upload', data=b'video')
        self.assertEqual(self.seen, [])

    def test_origin_and_host_without_server(self):
        handler = object.__new__(live.Handler)
        for host, origin, write, expected in ((f'127.0.0.1:{live.PORT}', None, False, True), (f'127.0.0.1:{live.PORT}', None, True, False),
                (f'127.0.0.1:{live.PORT}', f'http://127.0.0.1:{live.PORT}', True, True),
                (f'evil.invalid:{live.PORT}', f'http://127.0.0.1:{live.PORT}', False, False),
                (f'127.0.0.1:{live.PORT}', 'https://evil.invalid', False, False), (f'127.0.0.1:{live.PORT}', 'null', True, False)):
            handler.headers = {'Host': host}
            if origin is not None:
                handler.headers['Origin'] = origin
            self.assertEqual(handler.authorized(write), expected)

    def test_claude_guard_before_process(self):
        self.configure(analysis='claude_cli')
        with self.assertRaisesRegex(RuntimeError, 'MICA_NO_CLOUD'):
            analyze.draft_by_claude(str(self.folder), 'private', [], self.folder / 'draft.json')

    def test_claude_stdin_without_tools(self):
        self.configure(analysis='claude_cli')
        os.environ['MICA_NO_CLOUD'] = ''
        events = [{'type': 'system', 'subtype': 'init'}, {'type': 'result', 'subtype': 'success', 'structured_output': self.valid_draft()}]
        result = subprocess.CompletedProcess([], 0, '\n'.join(json.dumps(event) for event in events), '')
        frames = self.folder / 'frames'
        frames.mkdir()
        (frames / 'one.jpg').write_bytes(b'image')
        with patch.object(analyze.shutil, 'which', return_value='/fake/claude'), patch.object(analyze, 'claude_env', return_value={}), patch.object(subprocess, 'run', return_value=result) as run:
            analyze.draft_by_claude(str(self.folder), 'private-meeting', [str(frames / 'one.jpg')], self.folder / 'draft.json')
        args = run.call_args.args[0]
        # 会議の中身は引数に出さず、標準入力で渡す。道具・使う人の設定・フックを持たせない
        self.assertNotIn('private-meeting', ' '.join(args))
        self.assertNotIn('--dangerously-skip-permissions', args)
        self.assertEqual(args[args.index('--tools') + 1], '')
        self.assertEqual(args[args.index('--setting-sources') + 1], '')
        incoming = json.loads(run.call_args.kwargs['input'])['message']['content']
        self.assertIn('private-meeting', incoming[0]['text'])
        self.assertIn('条件を確かめる', incoming[0]['text'])
        self.assertEqual(incoming[-1]['type'], 'image')
        self.assertEqual(json.loads((self.folder / 'draft.json').read_text()), self.valid_draft())

    def test_claude_failure_shows_reason(self):
        self.configure(analysis='claude_cli')
        os.environ['MICA_NO_CLOUD'] = ''
        for result in (subprocess.CompletedProcess([], 1, '', 'Invalid API key · Please run /login'),
                       subprocess.CompletedProcess([], 0, json.dumps({'type': 'result', 'is_error': True, 'result': 'usage limit reached'}), '')):
            with patch.object(analyze.shutil, 'which', return_value='/fake/claude'), patch.object(analyze, 'claude_env', return_value={}), patch.object(subprocess, 'run', return_value=result):
                with self.assertRaisesRegex(RuntimeError, 'Please run /login|usage limit reached'):
                    analyze.draft_by_claude(str(self.folder), 'private', [], self.folder / 'draft.json')
            self.assertFalse((self.folder / 'draft.json').exists())

    def test_offline_transcription_uses_installed_runtime(self):
        self.configure()
        words = [{'start': 0, 'end': 2, 'text': '確認します。'}]
        with patch.object(analyze, 'sh') as shell, patch('mica_config.ffmpeg', return_value='ffmpeg'), patch('shodan_local_asr.LocalASR') as local, patch('shodan_evidence.vocabulary', return_value=[]):
            local.return_value.__enter__.return_value.transcribe.return_value = words
            result = analyze.transcribe(str(self.folder))
        self.assertEqual(local.call_count, 2)
        self.assertEqual(local.call_args.kwargs['timeout'], 3 * 3600)
        self.assertTrue(all(call.args[0][0] == 'ffmpeg' for call in shell.call_args_list))
        self.assertTrue(result)

    def test_no_analysis_does_not_invoke_claude(self):
        self.configure()
        with patch.object(analyze, 'meeting_reference', return_value=''), patch.object(analyze, 'analysis_inputs', return_value={}), patch.object(analyze, 'build_prompt', return_value=''):
            (self.folder / 'analysis-reference.json').write_text(json.dumps({'input_fingerprints': {}}))
            with self.assertRaisesRegex(RuntimeError, 'AI をつないでいません'):
                analyze.analyze(str(self.folder), {})

    def test_failed_frames_are_not_cached(self):
        def fail(args, **kwargs):
            Path(args[-1].replace('%04d', '0001')).write_bytes(b'partial')
            raise subprocess.CalledProcessError(1, args)
        with patch('mica_config.ffmpeg', return_value='ffmpeg'), patch.object(subprocess, 'run', side_effect=fail):
            with self.assertRaises(subprocess.CalledProcessError):
                analyze.extract_frames(str(self.folder))
        self.assertFalse((self.folder / 'frames').exists())
        self.assertEqual(list(self.folder.glob('.frames-*')), [])

    def test_storage_requires_content_receipt(self):
        (self.folder / 'analysis.md').write_text('種別: 仕事\n\n分析は未検証です。録画を保持します。')
        (self.folder / 'transcript.md').write_text('文字起こし')
        (self.folder / 'drive_url.txt').write_text('https://drive.google.com/file/d/test/view')
        self.assertFalse(storage.analysis_complete(self.folder))
        storage.mark_analysis_complete(self.folder)
        self.assertTrue(storage.analysis_complete(self.folder))
        (self.folder / 'transcript.md').write_text('内容が変わった')
        self.assertFalse(storage.analysis_complete(self.folder))

    def test_cross_site_get_without_origin_is_rejected(self):
        handler = object.__new__(live.Handler)
        for site in ('cross-site', 'same-site'):
            handler.headers = {'Host': f'127.0.0.1:{live.PORT}', 'Sec-Fetch-Site': site}
            self.assertFalse(handler.authorized())

    def test_watch_corrupt_request_does_not_stop_recording(self):
        import shodan_watch as watch
        path = self.folder / 'record-request.json'
        for value in ([], 'start', {'action': 'start', 'at': True}):
            path.write_text(json.dumps(value))
            with patch.object(watch, 'REQUEST', str(path)), patch.object(watch, 'log'):
                self.assertIsNone(watch.take_request())
            self.assertFalse(path.exists())

    def test_watch_corrupt_started_record_does_not_stop_watcher(self):
        import shodan_watch as watch
        path = self.folder / 'started.json'
        for value in ('{', '[]', '{"event":"yesterday"}'):
            path.write_text(value)
            with patch.object(watch, 'STARTED', path):
                self.assertEqual(watch.load_started(), {})


if __name__ == '__main__':
    try:
        unittest.main()
    finally:
        WORK.cleanup()
