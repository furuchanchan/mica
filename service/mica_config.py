"""Mica の設定。設定ファイルが無くても、既定（外へ何もつながない）で録音と Mac の中の文字起こしは動く。

設定ファイル: ~/.config/mica/config.json（環境変数 MICA_CONFIG で場所を替えられる）。
書いた項目だけが既定を上書きする。知らない項目・型の違う値は、原因が分かる文で失敗する。
外の AI へ送るのは、設定 ai に書いた先だけ。鍵が Mac にあるだけでは何も送らない（つなぎ方は service/mica_llm.py）。
MICA_NO_CLOUD=1 を付けると、設定に書いてあっても、この Mac の外の AI へは送らない。

入っている Mica と別にもう1つ動かす時（開発・確認）は、次の環境変数で置き場とポートを分ける。
MICA_APP_SUPPORT（既定 ~/Library/Application Support/Mica）、MICA_LIVE_PORT（既定 18768）、MICA_APP_PORT（既定 18770）。
"""
from __future__ import annotations
import copy, json, os, shutil, subprocess
from functools import lru_cache
from pathlib import Path

DEFAULTS = {
    'data_root': '~/Movies/Mica',
    'ffmpeg': None,                       # None なら PATH から探す
    'user_name': '自分',                   # 分析の文で「自分」を指す呼び名
    'google_credentials_dir': '~/.google_workspace_mcp/credentials',  # <account>.json（client_id・client_secret・refresh_token）
    'calendar': {'enabled': False, 'account': None, 'calendars': ['primary'], 'personal_calendar': None},
    'delivery': {
        'drive': {'enabled': False, 'work': {'account': None, 'folder': None},
                  'private': {'account': None, 'folder': None}},
        'slack': {'enabled': False, 'channel': None, 'command': None},   # command: 実行する argv の配列。末尾にチャンネルを足し、標準入力に本文を渡す
        'knowledge_base': {'enabled': False, 'ingest_command': None, 'search_command': None},  # ingest_command: argv の配列。末尾に分析ページのパスを足す
    },
    'context': {'enabled': False, 'projects_dir': None},   # 過去の会話ログ（Claude Code の projects フォルダ）から会議の背景を探す
    'ai': {
        'advice': None,                   # 会議中の助言と質問、会議前の準備。「会社/モデル」（例 openai/gpt-6-luna、ollama/qwen3:4b）。None なら使わない
        'analysis': None,                 # 会議後の分析。「claude_cli」（Claude Code。claude_cli/モデル名 も可）か「会社/モデル」。None なら文字起こしの保存まで
        'transcription_check': None,      # 「openai」で、会議中の音声を OpenAI でも文字に起こし、Mac の中の認識と比べる。None なら Mac の中だけ
        'providers': {},                  # 自分で足す OpenAI 互換のサーバー。{"名前": {"base_url": "http://…/v1", "api_key_env": "環境変数の名前"}}
    },
    'negotiation_notes': None,            # 商談の技の台帳（Markdown。「## 技」以降を使う）。None なら使わない
    'ignore_mic_apps': [],                # マイクを使っても会議と見なさないアプリの bundle id（前方一致）。音声入力アプリなど
}


def _merge(base: dict, over: dict, path: str = '') -> dict:
    result = copy.deepcopy(base)
    for key, value in over.items():
        if key not in base:
            moved = '（0.12 で ai.analysis に替わりました。README の「設定」を見てください）' if path + key == 'analysis' else ''
            raise ValueError(f'設定ファイルに知らない項目があります: {path}{key}{moved}')
        if path + key == 'ai.providers':  # 名前は使う人が決めるので、中身は mica_llm が確かめる
            result[key] = value
        elif isinstance(base[key], dict):
            if not isinstance(value, dict):
                raise ValueError(f'設定の {path}{key} は {{...}} の形で書いてください')
            result[key] = _merge(base[key], value, f'{path}{key}.')
        else:
            result[key] = value
    return result


def config_path() -> Path:
    return Path(os.environ.get('MICA_CONFIG') or '~/.config/mica/config.json').expanduser()


_loaded = {'stamp': None, 'value': None}


def config() -> dict:
    """設定を返す。設定ファイルが書き換わったら読み直す（./mica connect の後に、常駐を起こし直さなくて済むように）。"""
    path = config_path()
    try:
        stamp = (str(path), path.stat().st_mtime_ns)
    except FileNotFoundError:
        stamp = (str(path), 0)
    if _loaded['stamp'] != stamp:
        value = _read(path)
        _loaded.update(stamp=stamp, value=value)
        stored_key.cache_clear()  # 鍵も、つなぎ直した後の物を読む
    return _loaded['value']


def _read(path: Path) -> dict:
    loaded = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text())
        except ValueError as error:
            raise ValueError(f'設定ファイル {path} を JSON として読めません: {error}') from None
        if not isinstance(loaded, dict):
            raise ValueError(f'設定ファイル {path} の最上位は {{...}} の形で書いてください')
    return check(loaded, path)


def check(loaded: dict, path: Path) -> dict:
    """書いた項目を既定に重ね、確かめて返す。誤りは、原因が分かる文で失敗する。"""
    value = _merge(DEFAULTS, loaded)
    _validate(value, path)
    return value


def reload() -> None:
    _loaded['stamp'] = None


def _validate(value: dict, path: Path) -> None:
    ai = value['ai']
    for role in ('advice', 'analysis'):
        spec = ai[role]
        if spec is not None and (not isinstance(spec, str) or not spec.strip()):
            raise ValueError(f'設定 ai.{role} は "会社/モデル" の文字列か null です（{spec!r}）: {path}')
    if ai['advice'] and '/' not in ai['advice']:
        raise ValueError(f'設定 ai.advice は "会社/モデル" の形で書いてください（例 openai/gpt-6-luna。{ai["advice"]!r}）: {path}')
    if ai['analysis'] and '/' not in ai['analysis'] and ai['analysis'] != 'claude_cli':
        raise ValueError(f'設定 ai.analysis は claude_cli か "会社/モデル" です（{ai["analysis"]!r}）: {path}')
    if ai['transcription_check'] not in (None, 'openai'):
        raise ValueError(f'設定 ai.transcription_check は "openai" か null です（{ai["transcription_check"]!r}）: {path}')
    if not isinstance(ai['providers'], dict):
        raise ValueError(f'設定 ai.providers は {{...}} の形で書いてください: {path}')
    delivery = value['delivery']
    enabled = [name for name in ('drive', 'slack', 'knowledge_base') if delivery[name]['enabled']]
    if enabled and not ai['analysis']:
        raise ValueError(f'設定 delivery.{enabled[0]} は分析の結果を届ける機能です。ai.analysis を書いてください: {path}')
    if value['negotiation_notes'] and not ai['analysis']:
        raise ValueError(f'設定 negotiation_notes は分析で使います。ai.analysis を書いてください: {path}')
    search = delivery['knowledge_base']['enabled'] and delivery['knowledge_base']['search_command']
    if search and not (ai['analysis'] or '').startswith('claude_cli'):
        raise ValueError(f'設定 delivery.knowledge_base.search_command は、ai.analysis が claude_cli の時だけ使えます: {path}')


def _need(value, name: str):
    if not value:
        raise RuntimeError(f'設定 {name} が空です。設定ファイル {config_path()} に書いてください')
    return value


def app_support() -> Path:
    return Path(os.environ.get('MICA_APP_SUPPORT') or '~/Library/Application Support/Mica').expanduser()


def _port(name: str, default: int) -> int:
    value = os.environ.get(name, '').strip()
    if not value:
        return default
    if not value.isdigit():
        raise ValueError(f'環境変数 {name} はポート番号で書いてください（{value!r}）')
    return int(value)


def live_port() -> int:
    """見張り役が起こす文字起こしのサーバー。"""
    return _port('MICA_LIVE_PORT', 18768)


def app_port() -> int:
    """Mica.app が起こすサーバー（画面はここを読む）。"""
    return _port('MICA_APP_PORT', 18770)


def ignore_mic_apps() -> tuple[str, ...]:
    value = config()['ignore_mic_apps']
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise ValueError(f'設定 ignore_mic_apps は bundle id の配列で書いてください: {config_path()}')
    return tuple(value)


def data_root() -> Path:
    return Path(config()['data_root']).expanduser()


def ffmpeg() -> str:
    found = config()['ffmpeg'] or shutil.which('ffmpeg')
    if not found:
        raise RuntimeError('ffmpeg が見つかりません。入れる（brew install ffmpeg）か、設定 ffmpeg にパスを書いてください')
    return str(Path(found).expanduser())


def user_name() -> str:
    return config()['user_name']


def sales_heading() -> str:
    """商談の分析の第6章の題。"""
    return f'6. {user_name()}の進め方'


def google_credentials_dir() -> Path:
    return Path(config()['google_credentials_dir']).expanduser()


def calendar_enabled() -> bool:
    return bool(config()['calendar']['enabled'])


def calendar_settings() -> dict:
    """予定表の設定。オンなのにアカウントが無ければ失敗する。"""
    calendar = config()['calendar']
    return {'account': _need(calendar['account'], 'calendar.account'),
            'calendars': _need(calendar['calendars'], 'calendar.calendars'),
            'personal_calendar': calendar['personal_calendar']}


def drive_enabled() -> bool:
    return bool(config()['delivery']['drive']['enabled'])


def drive_destination(kind: str) -> tuple[str, str]:
    """(アカウント, フォルダ名)。私的はprivate、それ以外はwork。"""
    side = 'private' if kind == '私的' else 'work'
    drive = config()['delivery']['drive'][side]
    return (_need(drive['account'], f'delivery.drive.{side}.account'),
            _need(drive['folder'], f'delivery.drive.{side}.folder'))


def slack_enabled() -> bool:
    return bool(config()['delivery']['slack']['enabled'])


def slack_settings() -> tuple[list[str], str]:
    """(実行する argv, チャンネル)。"""
    slack = config()['delivery']['slack']
    return (list(_need(slack['command'], 'delivery.slack.command')), _need(slack['channel'], 'delivery.slack.channel'))


def knowledge_base_enabled() -> bool:
    return bool(config()['delivery']['knowledge_base']['enabled'])


def knowledge_base_ingest() -> list[str]:
    return list(_need(config()['delivery']['knowledge_base']['ingest_command'], 'delivery.knowledge_base.ingest_command'))


def knowledge_base_search() -> str | None:
    """分析のモデルに「過去の文脈はこれで引く」と伝えるコマンドの例。無ければ None。"""
    return config()['delivery']['knowledge_base']['search_command']


def context_enabled() -> bool:
    return bool(config()['context']['enabled'])


def context_projects_dir() -> Path:
    return Path(_need(config()['context']['projects_dir'], 'context.projects_dir')).expanduser()


def ai_spec(role: str) -> str | None:
    """設定 ai.advice / ai.analysis の値（"会社/モデル"、"claude_cli"、None）。"""
    return config()['ai'][role]


def analysis_engine() -> str:
    """none＝文字起こしの保存まで、claude_cli＝Claude Code、api＝設定に書いた会社の API かローカルの LLM。"""
    spec = ai_spec('analysis')
    if not spec:
        return 'none'
    return 'claude_cli' if spec.split('/', 1)[0] == 'claude_cli' else 'api'


def analysis_model() -> str | None:
    """claude_cli の時に claude --model へ渡す名前（claude_cli/名前 と書いた時だけ）。"""
    spec = ai_spec('analysis') or ''
    return spec.split('/', 1)[1] if spec.startswith('claude_cli/') else None


def negotiation_notes() -> Path | None:
    value = config()['negotiation_notes']
    return Path(value).expanduser() if value else None


def next_technique_path() -> Path:
    """前の会議の分析が選んだ「次回の1技」の置き場（negotiation_notes を設定した時だけ使う）。"""
    return data_root() / '.next-technique.json'


def cloud_allowed() -> bool:
    return os.environ.get('MICA_NO_CLOUD', '').strip() not in ('1', 'true', 'yes')


@lru_cache(maxsize=None)
def stored_key(env_name: str, keychain_service: str) -> str:
    """鍵を、環境変数 → Keychain の順に探す。無ければ空文字。"""
    key = os.environ.get(env_name, '').strip()
    if key:
        return key
    try:
        result = subprocess.run(['security', 'find-generic-password', '-s', keychain_service, '-w'],
                                capture_output=True, text=True, timeout=10, check=False)
        if result.returncode == 0:
            return result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return ''


def transcription_check_key() -> str:
    """会議中の音声を OpenAI でも文字に起こす時の鍵。設定 ai.transcription_check が openai で、鍵がある時だけ返す。"""
    if config()['ai']['transcription_check'] != 'openai' or not cloud_allowed():
        return ''
    return stored_key('OPENAI_API_KEY', 'openai-api-key')
