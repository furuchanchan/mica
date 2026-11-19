"""外の AI（または Mac の中の LLM）を1か所から呼ぶ。標準ライブラリだけで動く。

設定 ai.advice / ai.analysis に「会社/モデル」を書く（aider・LiteLLM と同じ書き方）。
  openai/gpt-6-luna              OpenAI
  anthropic/claude-opus-5-5      Anthropic（Claude の API）
  gemini/<モデル>                Google Gemini（OpenAI 互換の口）
  openrouter/<モデル>            OpenRouter
  groq/<モデル>                  Groq
  ollama/qwen3:4b                この Mac の Ollama（鍵なし）
  lmstudio/<モデル>              この Mac の LM Studio（鍵なし）
  <自分で付けた名前>/<モデル>    設定 ai.providers に書いた OpenAI 互換のサーバー

鍵は、環境変数（OPENAI_API_KEY など各社の標準の名前）→ Keychain の「<会社>-api-key」の順に探す。
送るのは設定に書いた先だけ。つながらない時に、別の会社やクラウドへ勝手に切り替えない。
MICA_NO_CLOUD=1 の時は、この Mac の中（localhost）のサーバーにだけ送る。
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request

import mica_config

# wire＝話し方。responses＝OpenAI の Responses API、messages＝Anthropic の Messages API、chat＝OpenAI 互換の Chat Completions
PROVIDERS = {
    'openai': {'label': 'OpenAI', 'wire': 'responses', 'base_url': 'https://api.openai.com/v1', 'key_env': 'OPENAI_API_KEY'},
    'anthropic': {'label': 'Anthropic', 'wire': 'messages', 'base_url': 'https://api.anthropic.com/v1', 'key_env': 'ANTHROPIC_API_KEY'},
    'gemini': {'label': 'Gemini', 'wire': 'chat', 'base_url': 'https://generativelanguage.googleapis.com/v1beta/openai', 'key_env': 'GEMINI_API_KEY'},
    'openrouter': {'label': 'OpenRouter', 'wire': 'chat', 'base_url': 'https://openrouter.ai/api/v1', 'key_env': 'OPENROUTER_API_KEY'},
    'groq': {'label': 'Groq', 'wire': 'chat', 'base_url': 'https://api.groq.com/openai/v1', 'key_env': 'GROQ_API_KEY'},
    'ollama': {'label': 'Ollama', 'wire': 'chat', 'base_url': 'http://localhost:11434/v1', 'key_env': None},
    'lmstudio': {'label': 'LM Studio', 'wire': 'chat', 'base_url': 'http://localhost:1234/v1', 'key_env': None},
}
ANTHROPIC_VERSION = '2026-10-21'


class NotConnected(RuntimeError):
    """設定に書いていない、または鍵が無い。"""


def provider(name: str) -> dict:
    """会社の名前から、つなぎ先を返す。設定 ai.providers に同じ名前があれば、そちらが組み込みを上書きする。"""
    custom = mica_config.config()['ai']['providers'].get(name)
    if custom is not None:
        if not isinstance(custom, dict) or set(custom) - {'base_url', 'api_key_env'} or not isinstance(custom.get('base_url'), str):
            raise ValueError(f'設定 ai.providers.{name} は {{"base_url": "http://…/v1", "api_key_env": "環境変数の名前"}} の形で書いてください')
        base = dict(PROVIDERS.get(name) or {'label': name, 'wire': 'chat', 'key_env': None})
        base.update(base_url=custom['base_url'].rstrip('/'), key_env=custom.get('api_key_env', base['key_env']))
        return base
    if name not in PROVIDERS:
        raise ValueError(f'知らない会社の名前です: {name}（使える名前: {"、".join(PROVIDERS)}。ほかは ./mica connect 名前 --base-url http://…/v1 で足す）')
    return dict(PROVIDERS[name])


def is_local(connection: dict) -> bool:
    host = urllib.parse.urlparse(connection['base_url']).hostname or ''
    return host in ('localhost', '127.0.0.1', '::1')


def connection(spec: str) -> dict:
    """"会社/モデル" から、つなぎ先（URL・鍵・話し方）を組む。使えなければ NotConnected を、理由つきで出す。"""
    name, _, model = spec.partition('/')
    if not model:
        raise NotConnected(f'つなぎ先は "会社/モデル" の形で書いてください（{spec!r}）。')
    found = provider(name)
    found.update(name=name, model=model, key='')
    if not is_local(found) and not mica_config.cloud_allowed():
        raise NotConnected(f'MICA_NO_CLOUD=1 のため、{found["label"]} へは送りません。')
    if found['key_env']:
        found['key'] = mica_config.stored_key(found['key_env'], f'{name}-api-key')
        if not found['key']:
            raise NotConnected(f'{found["label"]} の鍵が見つかりません（環境変数 {found["key_env"]} か、Keychain の {name}-api-key）。'
                               'ターミナルで ./mica connect を実行してください。')
    return found


def resolve(role: str) -> dict:
    """role（advice / analysis）の今のつなぎ先。"""
    spec = mica_config.ai_spec(role)
    if not spec:
        raise NotConnected(f'設定 ai.{role} が空です。つなぐには、ターミナルで ./mica connect を実行してください。')
    return connection(spec)


def _auth(found: dict) -> dict:
    if found['wire'] == 'messages':
        return {'x-api-key': found['key'], 'anthropic-version': ANTHROPIC_VERSION}
    return {'Authorization': 'Bearer ' + found['key']} if found['key'] else {}


def models(found: dict, timeout: float = 15) -> list:
    """つなぎ先が出しているモデルの名前の一覧（./mica connect で選ぶ時に使う）。"""
    request = urllib.request.Request(found['base_url'] + '/models', headers=_auth(found))
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            listed = json.load(response)
    except urllib.error.HTTPError as error:
        error.close()
        raise RuntimeError(f'{found["label"]} のモデルの一覧を取れませんでした（HTTP {error.code}）。鍵を確かめてください。') from None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as error:
        hint = '（サーバーが起きているか確かめてください）' if is_local(found) else ''
        raise RuntimeError(f'{found["label"]} につながりませんでした{hint}: {getattr(error, "reason", error)}') from None
    items = listed.get('data') if isinstance(listed, dict) else None
    return sorted(item['id'] for item in items or [] if isinstance(item, dict) and isinstance(item.get('id'), str))


def connected(role: str) -> bool:
    try:
        resolve(role)
        return True
    except (NotConnected, ValueError):
        return False


def _post(connection: dict, path: str, payload: dict, headers: dict, timeout: float) -> dict:
    label = connection['label']
    request = urllib.request.Request(connection['base_url'] + path, method='POST',
                                     data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
                                     headers={'Content-Type': 'application/json', **headers})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result = json.load(response)
    except urllib.error.HTTPError as error:
        # 相手の返した理由（モデル名の誤りなど）を見せる。鍵は相手の返事に入らない
        detail = error.read(2000).decode('utf-8', 'replace')
        error.close()
        try:
            message = json.loads(detail).get('error')
            detail = message.get('message', '') if isinstance(message, dict) else str(message or detail)
        except (ValueError, AttributeError):
            pass
        raise APIError(label, error.code, ' '.join(str(detail).split())[:300]) from None
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        hint = '（サーバーが起きているか確かめてください）' if is_local(connection) else ''
        raise RuntimeError(f'{label} との通信に失敗しました{hint}: {getattr(error, "reason", error)}') from None
    except (ValueError, UnicodeError):
        raise RuntimeError(f'{label} の応答を読めませんでした。') from None
    if not isinstance(result, dict):
        raise RuntimeError(f'{label} の応答の形が正しくありません。')
    return result


class APIError(RuntimeError):
    def __init__(self, label: str, status: int, detail: str):
        super().__init__(f'{label} のエラー（HTTP {status}）: {detail or "理由の記載なし"}')
        self.status, self.detail = status, detail


def _responses(connection, instructions, user, name, schema, max_tokens, timeout):
    """OpenAI の Responses API。"""
    result = _post(connection, '/responses', {
        'model': connection['model'], 'store': False, 'max_output_tokens': max_tokens, 'instructions': instructions,
        'input': [{'role': 'user', 'content': user}],
        'text': {'format': {'type': 'json_schema', 'name': name, 'strict': True, 'schema': schema}},
    }, _auth(connection), timeout)
    if result.get('status') != 'completed' or result.get('error') or result.get('incomplete_details'):
        raise RuntimeError('OpenAI の回答が完了しませんでした。再試行してください。')
    texts = []
    for item in result.get('output') or []:
        if isinstance(item, dict) and item.get('type') == 'message' and item.get('role') == 'assistant':
            texts += [part['text'] for part in item.get('content') or []
                      if isinstance(part, dict) and part.get('type') == 'output_text' and isinstance(part.get('text'), str)]
    return '\n'.join(texts)


def _messages(connection, instructions, user, name, schema, max_tokens, timeout):
    """Anthropic の Messages API。考える分も max_tokens に数えられるので、上限は広めに取る。"""
    result = _post(connection, '/messages', {
        'model': connection['model'], 'max_tokens': max(max_tokens, 16000), 'system': instructions,
        'messages': [{'role': 'user', 'content': user}],
        'output_config': {'format': {'type': 'json_schema', 'schema': schema}},
    }, _auth(connection), timeout)
    if result.get('stop_reason') == 'refusal':
        raise RuntimeError('Anthropic がこの依頼への回答を断りました。')
    if result.get('stop_reason') == 'max_tokens':
        raise RuntimeError('Anthropic の回答が長さの上限で途中で切れました。')
    return '\n'.join(block['text'] for block in result.get('content') or []
                     if isinstance(block, dict) and block.get('type') == 'text' and isinstance(block.get('text'), str))


def _chat(connection, instructions, user, name, schema, max_tokens, timeout):
    """OpenAI 互換の Chat Completions（Gemini・OpenRouter・Groq・Ollama・LM Studio・自分のサーバー）。"""
    payload = {
        'model': connection['model'], 'max_tokens': max(max_tokens, 4096),
        'messages': [{'role': 'system', 'content': instructions}, {'role': 'user', 'content': user}],
        'response_format': {'type': 'json_schema', 'json_schema': {'name': name, 'strict': True, 'schema': schema}},
    }
    headers = _auth(connection)
    try:
        result = _post(connection, '/chat/completions', payload, headers, timeout)
    except APIError as error:
        # JSON スキーマの指定を受け付けないサーバー・モデルがある。その時だけ、スキーマを指示文に書いて1回やり直す
        if error.status != 400 or not re.search(r'response_format|json_schema|schema', error.detail, re.I):
            raise
        payload['response_format'] = {'type': 'json_object'}
        payload['messages'][0]['content'] = (instructions + '\n\n次の JSON スキーマに合う JSON だけを返してください。\n'
                                             + json.dumps(schema, ensure_ascii=False))
        result = _post(connection, '/chat/completions', payload, headers, timeout)
    choices = result.get('choices')
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise RuntimeError(f'{connection["label"]} から回答を取り出せませんでした。')
    if choices[0].get('finish_reason') == 'length':
        raise RuntimeError(f'{connection["label"]} の回答が長さの上限で途中で切れました。')
    content = (choices[0].get('message') or {}).get('content')
    return content if isinstance(content, str) else ''


WIRES = {'responses': _responses, 'messages': _messages, 'chat': _chat}


def complete_json(role: str, instructions: str, user: str, name: str, schema: dict, max_tokens: int = 1600, timeout: float = 45) -> str:
    """指示文と入力を送り、スキーマに合う JSON の文字列を返す。中身の確かめは呼ぶ側がする。"""
    connection = resolve(role)
    text = WIRES[connection['wire']](connection, instructions, user, name, schema, max_tokens, timeout).strip()
    fenced = re.fullmatch(r'```(?:json)?\s*(.*?)\s*```', text, re.S)  # コードの囲みを付けて返すモデルがある
    text = fenced.group(1) if fenced else text
    if not text:
        raise RuntimeError(f'{connection["label"]} から回答の本文を取り出せませんでした。')
    return text


def check(role: str) -> float:
    """つなぎ先を1回だけ試す（./mica connect と ./mica doctor）。成功したら、かかった秒数を返す。"""
    import time
    schema = {'type': 'object', 'properties': {'ok': {'type': 'boolean'}}, 'required': ['ok'], 'additionalProperties': False}
    started = time.time()
    reply = complete_json(role, 'JSON で {"ok": true} とだけ返してください。', '接続の確認です。', 'connection_check', schema, 200, 120)
    try:
        ok = json.loads(reply).get('ok')
    except (ValueError, AttributeError):
        ok = None
    if ok is not True:
        raise RuntimeError(f'返事を JSON として読めませんでした。このモデルは Mica の用途に向きません（返事の先頭: {reply[:80]!r}）')
    return time.time() - started
