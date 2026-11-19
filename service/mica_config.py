"""Mica の設定。設定ファイルが無くても、既定（外へ何もつながない）で録音と Mac の中の文字起こしは動く。

設定ファイル: ~/.config/mica/config.json（環境変数 MICA_CONFIG で場所を替えられる）。
書いた項目だけが既定を上書きする。知らない項目・型の違う値は、原因が分かる文で失敗する。
OpenAI の鍵の探し方：環境変数 OPENAI_API_KEY → Keychain の「openai-api-key」。
MICA_NO_CLOUD=1 を付けると、鍵があっても外へ音声・文字を送らない。

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
    'analysis': {'engine': 'none', 'model': None},         # engine: claude_cli か none
    'negotiation_notes': None,            # 商談の技の台帳（Markdown。「## 技」以降を使う）。None なら使わない
    'ignore_mic_apps': [],                # マイクを使っても会議と見なさないアプリの bundle id（前方一致）。音声入力アプリなど
}


def _merge(base: dict, over: dict, path: str = '') -> dict:
    result = copy.deepcopy(base)
    for key, value in over.items():
        if key not in base:
            raise ValueError(f'設定ファイルに知らない項目があります: {path}{key}')
        if isinstance(base[key], dict):
            if not isinstance(value, dict):
                raise ValueError(f'設定の {path}{key} は {{...}} の形で書いてください')
            result[key] = _merge(base[key], value, f'{path}{key}.')
        else:
            result[key] = value
    return result


def config_path() -> Path:
    return Path(os.environ.get('MICA_CONFIG') or '~/.config/mica/config.json').expanduser()


@lru_cache(maxsize=1)
def config() -> dict:
    path = config_path()
    loaded = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text())
        except ValueError as error:
            raise ValueError(f'設定ファイル {path} を JSON として読めません: {error}') from None
        if not isinstance(loaded, dict):
            raise ValueError(f'設定ファイル {path} の最上位は {{...}} の形で書いてください')
    value = _merge(DEFAULTS, loaded)
    _validate(value, path)
    return value


def reload() -> None:
    config.cache_clear()


def _validate(value: dict, path: Path) -> None:
    engine = value['analysis']['engine']
    if engine not in ('claude_cli', 'none'):
        raise ValueError(f'設定 analysis.engine は claude_cli か none です（{engine!r}）: {path}')
    delivery = value['delivery']
    enabled = [name for name in ('drive', 'slack', 'knowledge_base') if delivery[name]['enabled']]
    if enabled and engine == 'none':
        raise ValueError(f'設定 delivery.{enabled[0]} は分析の結果を届ける機能です。analysis.engine を claude_cli にしてください: {path}')
    if value['negotiation_notes'] and engine == 'none':
        raise ValueError(f'設定 negotiation_notes は分析で使います。analysis.engine を claude_cli にしてください: {path}')


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


def analysis_engine() -> str:
    return config()['analysis']['engine']


def analysis_model() -> str | None:
    return config()['analysis']['model']


def negotiation_notes() -> Path | None:
    value = config()['negotiation_notes']
    return Path(value).expanduser() if value else None


def next_technique_path() -> Path:
    """前の会議の分析が選んだ「次回の1技」の置き場（negotiation_notes を設定した時だけ使う）。"""
    return data_root() / '.next-technique.json'


def cloud_allowed() -> bool:
    return os.environ.get('MICA_NO_CLOUD', '').strip() not in ('1', 'true', 'yes')


@lru_cache(maxsize=1)
def _stored_key() -> str:
    key = os.environ.get('OPENAI_API_KEY', '').strip()
    if key:
        return key
    try:
        result = subprocess.run(['security', 'find-generic-password', '-s', 'openai-api-key', '-w'],
                                capture_output=True, text=True, timeout=10, check=False)
        if result.returncode == 0:
            return result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return ''


def openai_key() -> str:
    """鍵。つないでいなければ空文字。"""
    return _stored_key() if cloud_allowed() else ''


def require_openai_key() -> str:
    key = openai_key()
    if not key:
        raise RuntimeError('OpenAI の鍵がつながっていません。会議中の助言と質問は、鍵をつなぐと使えます。')
    return key
