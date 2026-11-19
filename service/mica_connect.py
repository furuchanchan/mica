"""つなぎ先を見る・つなぐ・外す。ターミナルの ./mica と、Mica.app の「AI をつなぐ」の画面が同じここを通る。

つなぐ時は、試しの呼び出しが通ってから鍵と設定を保存する。失敗した時は何も変えない。
誤りは ValueError で返す（文はそのまま使う人に見せる）。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess

import mica_config
import mica_llm

ROLES = {'advice': '会議中の助言と質問', 'analysis': '会議後の分析'}
# (名前, ターミナルで選ぶ時の説明, 画面で選ぶ時の名前)
CHOICES = [
    ('ollama', 'この Mac の Ollama（鍵なし。Mac の外へ送らない）', 'Ollama（この Mac の中）'),
    ('lmstudio', 'この Mac の LM Studio（鍵なし。Mac の外へ送らない）', 'LM Studio（この Mac の中）'),
    ('openai', 'OpenAI の API の鍵', 'OpenAI'),
    ('anthropic', 'Anthropic（Claude）の API の鍵', 'Anthropic（Claude の API）'),
    ('gemini', 'Google Gemini の API の鍵', 'Google Gemini'),
    ('openrouter', 'OpenRouter の API の鍵', 'OpenRouter'),
    ('groq', 'Groq の API の鍵', 'Groq'),
    ('claude_cli', 'Claude Code（Claude の契約か API の鍵で動く。会議後の分析だけ）', 'Claude Code（会議後の分析だけ）'),
]
DEFAULT_MODELS = {'openai': 'gpt-6-luna', 'anthropic': 'claude-opus-5-5'}
TARGETS = {'advice': ['advice'], 'analysis': ['analysis'], 'check': ['transcription_check'],
           'all': ['advice', 'analysis', 'transcription_check']}
CLAUDE_NOTE = '分析の時、文字起こしと画面の静止画が Anthropic へ送られます。'


def raw_config() -> dict:
    """設定ファイルに書いてある項目だけ（既定を重ねる前）。"""
    path = mica_config.config_path()
    raw = json.loads(path.read_text()) if path.exists() else {}
    mica_config.check(raw, path)
    return raw


def write_config(raw: dict) -> None:
    path = mica_config.config_path()
    mica_config.check(raw, path)  # 誤った設定は書かない
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + '\n')
    os.chmod(temporary, 0o600)  # 鍵は入らないが、つなぎ先の URL が入るので本人だけが読める形にする
    temporary.replace(path)
    mica_config.reload()


def key_source(found: dict) -> str:
    if not found['key_env']:
        return 'この Mac の中' if mica_llm.is_local(found) else '鍵なし'
    return f'鍵: 環境変数 {found["key_env"]}' if os.environ.get(found['key_env'], '').strip() else '鍵: Keychain'


def role_line(role: str) -> tuple:
    """(表示する文, 使えるか, Mac の外の送り先か None)。"""
    spec = mica_config.ai_spec(role)
    if not spec:
        return 'つないでいません', False, None
    if role == 'analysis' and mica_config.analysis_engine() == 'claude_cli':
        if not mica_config.cloud_allowed():
            return f'{spec}（MICA_NO_CLOUD=1 のため使いません）', False, None
        if not shutil.which('claude'):
            return f'{spec}（claude コマンドが見つかりません。Claude Code を入れてください）', False, None
        return f'{spec}（Claude Code）', True, 'Anthropic（Claude Code 経由）'
    try:
        found = mica_llm.connection(spec)
    except (mica_llm.NotConnected, ValueError) as error:
        return f'{spec}（使えません: {error}）', False, None
    return f'{spec}（{key_source(found)}）', True, None if mica_llm.is_local(found) else found['label']


def note(found: dict) -> str:
    """つないだ時に、会議の中身がどこへ行くか。"""
    if mica_llm.is_local(found):
        return 'この Mac の中で動くので、Mac の外へは送りません。'
    return f'使う時、会議の文字起こしが {found["label"]} へ送られます。'


def key_present(name: str, found: dict) -> bool:
    """鍵があるか（中身は読まない）。"""
    if not found['key_env']:
        return False
    if os.environ.get(found['key_env'], '').strip():
        return True
    try:
        return subprocess.run(['security', 'find-generic-password', '-s', f'{name}-api-key'],
                              capture_output=True, timeout=10, check=False).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def overview(choices: bool = True) -> dict:
    """今どの段まで使えるか、Mac の外のどこへ送るか。choices が真なら、つなげる先の一覧（鍵の有無つき）も返す。"""
    value = mica_config.config()
    allowed = mica_config.cloud_allowed()
    stages, outside = [], []
    for role, title in ROLES.items():
        line, usable, destination = role_line(role)
        stages.append({'role': role, 'title': title, 'spec': mica_config.ai_spec(role), 'text': line, 'usable': usable})
        if destination:
            outside.append(destination)
    check = None
    if value['ai']['transcription_check']:
        try:
            audio = mica_llm.transcription_connection()
        except (mica_llm.NotConnected, ValueError):
            audio = None
        check = {'spec': value['ai']['transcription_check'], 'usable': bool(audio)}
        if audio and not mica_llm.is_local(audio):
            outside.append(audio['label'] + '（音声）')
    integrations = []
    for enabled, destination in ((value['calendar']['enabled'], 'Google Calendar'),
                                 (value['delivery']['drive']['enabled'], 'Google Drive'),
                                 (value['delivery']['slack']['enabled'], 'Slack'),
                                 (value['delivery']['knowledge_base']['enabled'], '知識ベースの外部コマンド')):
        if enabled:
            integrations.append({'title': destination, 'active': allowed})
            if allowed:
                outside.append(destination)
    providers = []
    builtin = [name for name, _, _ in CHOICES]
    for name, title in ([(name, title) for name, _, title in CHOICES] + [
            (name, name) for name in value['ai']['providers'] if name not in builtin]) if choices else []:
        if name == 'claude_cli':
            providers.append({'name': name, 'title': title, 'local': False, 'needs_key': False, 'has_key': False,
                              'default_model': '', 'analysis_only': True, 'allowed': allowed, 'note': CLAUDE_NOTE})
            continue
        found = mica_llm.provider(name)
        local = mica_llm.is_local(found)
        if name not in builtin:
            title = found['label']
        providers.append({'name': name, 'title': title, 'local': local, 'needs_key': bool(found['key_env']),
                          'has_key': key_present(name, found), 'default_model': DEFAULT_MODELS.get(name, ''),
                          'analysis_only': False, 'allowed': local or allowed, 'note': note(found)})
    return {'config_path': str(mica_config.config_path()), 'no_cloud': not allowed, 'stages': stages, 'check': check,
            'integrations': integrations, 'outside': list(dict.fromkeys(outside)), 'providers': providers}


def candidate(raw: dict, name: str, base_url=None, key_env=None) -> dict:
    """つなぐ候補（URL・話し方）。base_url がある時は raw の ai.providers に足す（ここでは保存しない）。"""
    if not isinstance(name, str) or not name.strip():
        raise ValueError('つなぐ先を選んでください。')
    if name == 'claude_cli' and (base_url or key_env):
        raise ValueError('claude_cli は会議後の分析だけに使えます。')
    ai = raw.setdefault('ai', {})
    if base_url:
        entry = {'base_url': base_url}
        if key_env:
            entry['api_key_env'] = key_env
        ai.setdefault('providers', {})[name] = entry
    mica_config.check(raw, mica_config.config_path())
    found = mica_llm.provider(name, ai.get('providers', {}))
    found.update(name=name, key='')
    if not mica_llm.is_local(found) and not mica_config.cloud_allowed():
        raise ValueError(f'MICA_NO_CLOUD=1 が付いているので、{found["label"]} にはつなぎません。')
    return found


def _key_env(name: str, base_url, key_env, key):
    """画面から鍵つきで足すサーバーには、鍵の環境変数の名前を名前から決めて付ける（鍵そのものは Keychain に入れる）。"""
    if base_url and key and not key_env:
        return re.sub(r'[^A-Z0-9]', '_', name.upper()) + '_API_KEY'
    return key_env


def save_key(name: str, key: str) -> None:
    """鍵を Keychain（<名前>-api-key）に入れる。鍵をコマンドの引数に出さないよう、security には標準入力で渡す。"""
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]*', name):
        raise ValueError('つなぐ先の名前は、英数字と - _ で書いてください。')
    if not re.fullmatch(r'[!#-&(-\[\]-~]{8,1000}', key):
        raise ValueError('鍵の形が正しくありません（空白や引用符の無い、8文字以上の英数字と記号）。貼り直してください。')
    service = f'{name}-api-key'
    try:
        done = subprocess.run(['security', '-i'], input=f'add-generic-password -U -s {service} -a mica -w "{key}"\n',
                              capture_output=True, text=True, timeout=15, check=False)
        mica_config.stored_key.cache_clear()
        saved = done.returncode == 0 and subprocess.run(['security', 'find-generic-password', '-s', service, '-w'],
                                                        capture_output=True, text=True, timeout=10, check=False).stdout.strip() == key
        if not saved:  # 同じ名前で別の項目が先にあると、読み出しは古い鍵を返す。足した物は残さない
            subprocess.run(['security', 'delete-generic-password', '-s', service, '-a', 'mica'], capture_output=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        saved = False
    mica_config.stored_key.cache_clear()
    if not saved:
        raise ValueError(f'鍵を Keychain に入れられませんでした。キーチェーンアクセスに同じ名前（{service}）の項目があれば消してから、もう一度つないでください。')


def _with_key(found: dict, name: str, key) -> dict:
    if found['key_env']:
        found['key'] = key or mica_config.stored_key(found['key_env'], f'{name}-api-key')
        if not found['key']:
            raise ValueError(f'{found["label"]} の鍵を入れてください。')
    return found


def models(name: str, base_url=None, key_env=None, key=None) -> list:
    """つなぎ先が出しているモデルの名前の一覧。設定は変えない。"""
    if name == 'claude_cli':
        return []
    found = _with_key(candidate(raw_config(), name, base_url, _key_env(name, base_url, key_env, key)), name, key)
    try:
        listed = mica_llm.models(found)
    except RuntimeError as error:
        raise ValueError(str(error)) from None
    if not listed and name == 'ollama':
        raise ValueError('Ollama にモデルが入っていません。先にターミナルで ollama pull <モデル名> を実行してください。')
    return listed


def connect(name: str, model=None, roles=(), base_url=None, key_env=None, key=None,
            transcription_check=False, test=True) -> dict:
    """試してからつなぐ。返す値は {'message': つないだ後に見せる文, 'seconds': 試しの返事までの秒数か None}。"""
    roles = [role for role in ROLES if role in roles]
    if transcription_check and name != 'openai':
        raise ValueError('文字起こしの2重確認は openai の時だけ指定できます。')
    raw = raw_config()
    ai = raw.setdefault('ai', {})
    if name == 'claude_cli':
        if roles != ['analysis'] or base_url or key_env or key:
            raise ValueError('claude_cli は会議後の分析だけに使えます。')
        if not mica_config.cloud_allowed():
            raise ValueError('MICA_NO_CLOUD=1 のため、Claude Code にはつなぎません。')
        if not shutil.which('claude'):
            raise ValueError('claude コマンドが見つかりません。Claude Code（https://claude.com/claude-code）を入れてログインしてから、もう一度つないでください。')
        ai['analysis'] = f'claude_cli/{model}' if model else 'claude_cli'
        write_config(raw)
        return {'message': f'会議後の分析を Claude Code につなぎました（{ai["analysis"]}）。{CLAUDE_NOTE}', 'seconds': None}
    key_env = _key_env(name, base_url, key_env, key)
    found = _with_key(candidate(raw, name, base_url, key_env), name, key)
    check_only = transcription_check and not roles
    if not roles and not check_only:
        raise ValueError('使い道（会議中の助言と質問・会議後の分析）を1つ以上選んでください。')
    if roles and not (isinstance(model, str) and model.strip()):
        raise ValueError('モデルの名前が要ります。')
    for role in roles:
        ai[role] = f'{name}/{model.strip()}'
    if transcription_check:
        ai['transcription_check'] = 'openai'
    mica_config.check(raw, mica_config.config_path())
    seconds = None
    if test and roles:
        try:
            seconds = mica_llm.check_connection(dict(found, model=model.strip()))
        except RuntimeError as error:
            raise ValueError(f'つなげませんでした。設定は変えていません。\n{error}') from None
    if key and found['key_env'] and not os.environ.get(found['key_env'], '').strip() \
            and key != mica_config.stored_key(found['key_env'], f'{name}-api-key'):
        save_key(name, key)
    write_config(raw)
    if check_only:
        return {'message': '文字起こしの2重確認をつなぎました。音声の送信試験は行っていません。', 'seconds': None}
    return {'message': '・'.join(ROLES[role] for role in roles) + f'を {name}/{model.strip()} につなぎました。{note(found)}', 'seconds': seconds}


def disconnect(target: str) -> None:
    """つなぎを外す。鍵は Keychain に残す。"""
    if target not in TARGETS:
        raise ValueError('外す物は advice・analysis・check・all のどれかで指定してください。')
    raw = raw_config()
    ai = raw.setdefault('ai', {})
    if 'analysis' in TARGETS[target] and ai.get('analysis'):
        value = mica_config.config()
        left = [f'delivery.{name}.enabled を false' for name in ('drive', 'slack', 'knowledge_base') if value['delivery'][name]['enabled']]
        if value['negotiation_notes']:
            left.append('negotiation_notes を null')
        if left:
            raise ValueError('会議後の分析を外せません。分析の結果を使う設定が残っています。'
                             f'設定ファイル {mica_config.config_path()} で {"、".join(left)} にしてから、もう一度外してください。')
    for key in TARGETS[target]:
        ai[key] = None
    write_config(raw)
