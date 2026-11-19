"""つなぎ先ごとの話し方（OpenAI の Responses・Anthropic の Messages・OpenAI 互換の Chat Completions）を、
この Mac の中に立てた偽のサーバーで確かめる。外へは何も送らない。
python3 test_llm.py"""
import http.server
import json
import os
import tempfile
import threading
import time
from pathlib import Path

work = Path(tempfile.mkdtemp())
os.environ.update(MICA_CONFIG=str(work / 'config.json'), MICA_NO_CLOUD='1', TEST_KEY='sk-test')
for name in ('OPENAI_API_KEY', 'ANTHROPIC_API_KEY', 'GROQ_API_KEY'):
    os.environ.pop(name, None)
import mica_config  # noqa: E402
import mica_llm  # noqa: E402

SEEN = []
DRAFT = []  # 入っていれば、偽サーバーはこれを分析の下書きとして返す
SCHEMA = {'type': 'object', 'properties': {'answer': {'type': 'string'}}, 'required': ['answer'], 'additionalProperties': False}


class Fake(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self.reply(200, {'data': [{'id': 'model-b'}, {'id': 'model-a'}]})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        SEEN.append((self.path, dict(self.headers), body))
        answer = json.dumps(DRAFT[0] if DRAFT else {'answer': 'はい'}, ensure_ascii=False)
        if self.path == '/v1/responses':
            assert self.headers['Authorization'] == 'Bearer sk-test' and body['text']['format']['type'] == 'json_schema'
            self.reply(200, {'status': 'completed', 'output': [{'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': answer}]}]})
        elif self.path == '/v1/messages':
            assert self.headers['x-api-key'] == 'sk-test' and self.headers['anthropic-version'] == mica_llm.ANTHROPIC_VERSION
            assert body['output_config']['format'] == {'type': 'json_schema', 'schema': SCHEMA} and body['system'] and 'Authorization' not in self.headers
            stop = {'refuse': 'refusal', 'long': 'max_tokens'}.get(body['model'], 'end_turn')
            self.reply(200, {'stop_reason': stop, 'content': [{'type': 'thinking', 'thinking': '…'}, {'type': 'text', 'text': answer}]})
        elif self.path == '/v1/chat/completions':
            if body['model'] == 'no-schema' and body['response_format']['type'] == 'json_schema':
                self.reply(400, {'error': {'message': 'response_format json_schema is not supported'}})
            elif body['model'] == 'bad-model':
                self.reply(404, {'error': {'message': 'model "bad-model" not found'}})
            else:
                text = '```json\n' + answer + '\n```' if body['model'] == 'fenced' else answer
                self.reply(200, {'choices': [{'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': text}}]})
        else:
            self.reply(404, {'error': 'unknown path'})


server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Fake)
threading.Thread(target=server.serve_forever, daemon=True).start()
base = f'http://127.0.0.1:{server.server_port}/v1'


def configure(**ai):
    providers = {name: {'base_url': base, 'api_key_env': 'TEST_KEY'} for name in ('openai', 'anthropic')}
    providers['mine'] = {'base_url': base}
    (work / 'config.json').write_text(json.dumps({'ai': dict(ai, providers=providers)}))
    mica_config.reload()


def fails(call, *words):
    try:
        call()
    except (RuntimeError, ValueError) as error:
        assert all(word in str(error) for word in words), error
        return
    raise AssertionError('失敗するはずの呼び出しが通った')


def ask(role='advice'):
    return json.loads(mica_llm.complete_json(role, '指示', '入力', 'test', SCHEMA))


# 1. 何も書いていなければ、どこにもつながない
(work / 'config.json').write_text('{}')
assert not mica_llm.connected('advice') and mica_config.analysis_engine() == 'none'
fails(ask, 'ai.advice', './mica connect')

# 2. 3つの話し方で、スキーマつきの JSON が返る
for spec, path in (('openai/gpt-x', '/v1/responses'), ('anthropic/claude-x', '/v1/messages'), ('mine/local-model', '/v1/chat/completions')):
    configure(advice=spec)
    assert mica_llm.connected('advice') and ask() == {'answer': 'はい'}, spec
    assert SEEN[-1][0] == path and SEEN[-1][2]['model'] == spec.split('/', 1)[1]
assert 'Authorization' not in SEEN[-1][1]  # 鍵の要らないサーバーには、鍵を付けない

# 3. 互換サーバーのくせ：スキーマ指定を断る物はやり直し、コードの囲みは外す。モデル名の誤りは相手の文を見せる
configure(advice='mine/no-schema')
assert ask() == {'answer': 'はい'} and SEEN[-1][2]['response_format'] == {'type': 'json_object'} and 'answer' in SEEN[-1][2]['messages'][0]['content']
configure(advice='mine/fenced')
assert ask() == {'answer': 'はい'}
configure(advice='mine/bad-model')
fails(ask, 'HTTP 404', 'bad-model')
configure(advice='anthropic/refuse')
fails(ask, '断りました')
configure(advice='anthropic/long')
fails(ask, '途中で切れました')

# 4. 役割ごとに別の先を選べる。分析は claude_cli か API
configure(advice='mine/a', analysis='anthropic/b')
assert ask('analysis') == {'answer': 'はい'} and SEEN[-1][0] == '/v1/messages' and mica_config.analysis_engine() == 'api'
configure(analysis='claude_cli/opus')
assert mica_config.analysis_engine() == 'claude_cli' and mica_config.analysis_model() == 'opus' and not mica_llm.connected('advice')

# 5. 送るのは設定に書いた先だけ：MICA_NO_CLOUD=1 は Mac の外を止める。鍵が無ければつながない。知らない名前は誤り
(work / 'config.json').write_text(json.dumps({'ai': {'advice': 'groq/some-model'}}))
mica_config.reload()
fails(ask, 'MICA_NO_CLOUD')
os.environ['MICA_NO_CLOUD'] = ''
mica_config.stored_key.cache_clear()
(work / 'config.json').write_text(json.dumps({'ai': {'advice': 'mine/x', 'providers': {'mine': {'base_url': 'https://example.invalid/v1', 'api_key_env': 'NO_SUCH_KEY'}}}}))
mica_config.reload()
fails(ask, '鍵が見つかりません', 'NO_SUCH_KEY')
os.environ['MICA_NO_CLOUD'] = '1'
(work / 'config.json').write_text(json.dumps({'ai': {'advice': 'nowhere/x'}}))
mica_config.reload()
fails(ask, '知らない会社の名前')
assert not mica_llm.connected('advice')

# 6. 設定の誤りは、原因の分かる文で止まる。書き換えは、起こし直さなくても読み直す
for bad, word in (({'ai': {'advice': 'gpt-x'}}, '会社/モデル'), ({'ai': {'analysis': 'codex'}}, 'claude_cli'), ({'analysis': {'engine': 'claude_cli'}}, '知らない項目'),
                  ({'delivery': {'slack': {'enabled': True}}}, 'ai.analysis'), ({'ai': {'transcription_check': 'groq'}}, 'transcription_check')):
    (work / 'config.json').write_text(json.dumps(bad))
    mica_config.reload()
    fails(mica_config.config, word)
configure(advice='mine/a')
assert mica_llm.connected('advice')
time.sleep(0.01)
(work / 'config.json').write_text('{}')
assert not mica_llm.connected('advice')

# 7. モデルの一覧と、試しの1回
configure(advice='mine/a')
assert mica_llm.models(mica_llm.resolve('advice')) == ['model-a', 'model-b']
fails(lambda: mica_llm.check('advice'), 'JSON として読めません')  # 偽サーバーは {"ok": true} を返さない

# 8. 会議後の分析（API の道）：材料を全文で渡し、返った下書きをファイルに置く
import shodan_analyze  # noqa: E402
folder = work / 'meeting'
folder.mkdir()
(folder / 'analysis-evidence.json').write_text(json.dumps({'segments': [{'id': 's1', 'text': '見積もりに三日かかる'}]}, ensure_ascii=False))
(folder / 'meta.json').write_text(json.dumps({'title': 'テスト'}, ensure_ascii=False))
configure(analysis='mine/a')
DRAFT.append({'kind': '仕事', 'sections': [{'number': 2, 'source_ids': ['u00001'], 'ai_proposals': ['期日を聞く'], 'visual_observations': [{'frame': 'a.jpg', 'text': '見た'}]}]})
shodan_analyze.draft_by_api(str(folder), '分析の指示', folder / 'analysis-draft.json')
draft = json.loads((folder / 'analysis-draft.json').read_text())
# 飛ばした章（1 と 7）は空の章として置き、渡していない画像の観察は消す
assert [section['number'] for section in draft['sections']] == [1, 2, 7] and draft['sections'][1]['source_ids'] == ['u00001']
assert all(section['visual_observations'] == [] for section in draft['sections']) and draft['sections'][0]['source_ids'] == []
sent = SEEN[-1][2]['messages'][1]['content']
assert '見積もりに三日かかる' in sent and 'テスト' in sent and '分析の指示' in sent
DRAFT[0] = {'sections': 'x'}
fails(lambda: shodan_analyze.draft_by_api(str(folder), '分析の指示', folder / 'analysis-draft.json'), '下書きを読めませんでした')
print('ok')
