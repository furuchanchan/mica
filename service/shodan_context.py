"""Match calendar events to direct, recent Claude Code user context without executing logs.

設定 context.enabled がオンの時だけ使う（呼ぶ側で分岐する）。読む場所は設定 context.projects_dir。
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import time
import unicodedata

import mica_config

SELECTION = mica_config.app_support() / 'context-selection.json'
MAX_BYTES = 256 * 1024


def redact(text):
    text = re.sub(r'-----BEGIN [^-]*(?:PRIVATE KEY|SECRET)[^-]*-----[\s\S]*?-----END [^-]+-----', '[秘密非表示]', text)
    text = re.sub(r'\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b', '[秘密非表示]', text)
    text = re.sub(r'(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b', '[メール非表示]', text)
    text = re.sub(r'(?<!\w)(?:\+?\d[\d ()-]{8,}\d)(?!\w)', '[電話非表示]', text)
    text = re.sub(r'(?i)\b(?:bearer\s+\S+|(?:sk-|ghp_|github_pat_|xox[baprs]-)[A-Za-z0-9_\-]+)', '[秘密非表示]', text)
    text = re.sub(r'''(?i)((?:api[_ -]?key|access[_ -]?token|token|password|secret|パスワード|秘密鍵)\s*[=:：]\s*)(["'])(.*?)\2''', r'\1[秘密非表示]', text)
    text = re.sub(r'(?i)((?:api[_ -]?key|access[_ -]?token|token|password|secret|パスワード|秘密鍵)\s*[=:：]\s*)[^\s,;\"\']+', r'\1[秘密非表示]', text)
    return re.sub(r'(?<![\w])(?=[A-Za-z0-9_+/=\-]{32,}(?![\w]))(?=[A-Za-z0-9_+/=\-]*[A-Za-z])(?=[A-Za-z0-9_+/=\-]*[0-9])[A-Za-z0-9_+/=\-]+', '[秘密非表示]', text)


def _stamp(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


MATCH_AGE_DAYS = 45
MATCH_FILES = 256
MATCH_READ_BYTES = 128 * 1024
HISTORY_READ_BYTES = 2 * 1024 * 1024
# These words describe meeting operations, not a client or a case identifier.
_COMMON = set("meeting meetings call calls sync weekly monthly daily agenda discussion review project projects customer client team sales training workshop session consultation proposal follow up initial regular status update planning with and the for about from our your intro introduction kickoff online zoom google teams microsoft notes purpose background context test minutes inc ltd llc corporation corp co company limited followup renewal onboarding interview business ai support internal external development implementation".split())
_COMMON_JA = tuple(sorted({"株式会社", "有限会社", "合同会社", "フォローアップ", "キックオフ", "ミーティング", "打合せ", "打合", "打ち合せ", "定例会", "様", "御中", "お打ち合わせ", "打ち合わせ", "オンライン", "定例会議", "定例", "会議", "商談", "面談", "相談", "初回", "導入", "営業", "研修", "会話", "プロジェクト", "顧客", "案件", "提案", "見積", "検討", "確認", "共有", "進捗", "背景", "目的", "議題", "次回", "本日", "明日", "本件", "資料", "について", "進め方", "MTG", "レビュー", "会社", "お客様", "担当者", "社内", "社外", "更新", "契約", "予算", "費用", "見積もり", "作業", "開発", "運用", "改善", "説明", "サービス", "面接", "採用", "対応"}, key=len, reverse=True))
_INJECTED = ('<system-reminder', '<local-command', '<command-name', '<command-message', '<task-notification', '<user_instructions', '<environment_context', '<instructions>', '# agents.md instructions', '<teammate-message', '<tool_result', '<tool-use', '<system>')
_NON_CASE_NAMES = {'sato', 'suzuki', 'tanaka', 'ito', 'kato', 'yamada', '佐藤', '鈴木', '高橋', '田中', '伊藤', '渡辺', '山本', '中村', '小林', '加藤', '吉田', '山田'}


def _normalized(value):
    return unicodedata.normalize('NFKC', redact(str(value or ''))).lower()


def _terms(value):
    value = _normalized(value)
    value = re.sub(r'https?://[^\s]+|\[[^\]]*非表示\]', ' ', value)
    for word in _COMMON_JA:
        value = value.replace(word.lower(), ' ')
    result = set()
    for token in re.findall(r'[a-z][a-z0-9_-]{2,}|[ぁ-んァ-ヶ一-龯ー]{2,}', value):
        token = re.sub(r'(?:との|への|の|と|に|を|へ|で|は|が|する|です)+$', '', token)
        if len(token) >= 2 and token not in _COMMON and not re.fullmatch(r'[ぁ-ん]+', token):
            result.add(token)
    return result


def _contains(text, term):
    # Latin identifiers require word boundaries: Acme must not match AcmeOther.
    if re.fullmatch(r'[a-z][a-z0-9_-]*', term):
        return bool(re.search(r'(?<![a-z0-9])' + re.escape(term) + r'(?![a-z0-9])', text))
    return term in text


def _user_messages(data, partial=False):
    lines = data.splitlines()
    if partial and lines:
        lines = lines[1:]
    messages = []
    for line in lines:
        try:
            row = json.loads(line)
        except (ValueError, UnicodeError):
            continue
        if not isinstance(row, dict) or row.get('type') != 'user' or row.get('isMeta') or row.get('isSidechain'):
            continue
        message = row.get('message')
        if not isinstance(message, dict) or message.get('role') != 'user':
            continue
        content = message.get('content')
        if isinstance(content, list):
            if any(not isinstance(part, dict) or part.get('type') != 'text' for part in content):
                continue
            content = '\n'.join(part.get('text', '') for part in content if isinstance(part.get('text'), str))
        if not isinstance(content, str) or not content.strip() or any(marker in content.lower() for marker in _INJECTED):
            continue
        timestamp = row.get('timestamp')
        try:
            stamp = datetime.fromisoformat(timestamp.replace('Z', '+00:00')) if isinstance(timestamp, str) else None
            if stamp and stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
        except ValueError:
            stamp = None
        messages.append((stamp, redact(content.strip())))
    return messages


def _is_followup_correction(previous, current):
    """A short, immediate referential correction is evidence from the same conversation."""
    before, previous_text = previous
    stamp, text = current
    if before is None or stamp is None or not 0 <= (stamp - before).total_seconds() <= 600 or len(text) > 500:
        return False
    if not re.match(r'^(?:やっぱり|やはり|先ほど|さっき|今の|訂正|変更|修正)', text):
        return False
    if not re.search(r'変更|訂正|修正|ではなく|でなく|じゃなく|にして|にする', text):
        return False
    if re.search(r'別件|別の案件|別会社|別のお客|ところで|話を変', text):
        return False
    # New named Latin identifiers or an explicitly named other company start another case.
    identifiers = set(re.findall(r'[a-z][a-z0-9_-]{2,}', _normalized(text))) - _COMMON
    if any(not _contains(_normalized(previous_text), value) for value in identifiers):
        return False
    if any(value not in previous_text and value not in _COMMON_JA
           for value in re.findall(r'[ァ-ヶー]{2,}', text)):
        return False
    companies = re.findall(r'(?:株式会社|合同会社|有限会社)[ぁ-んァ-ヶ一-龯ー]+|[ァ-ヶー]{3,}(?:社|様|さん)', text)
    return all(value in previous_text for value in companies)


class ContextStore:
    def __init__(self, projects=None, selection=SELECTION):
        self.projects, self.selection = Path(projects) if projects else mica_config.context_projects_dir(), Path(selection)
        self.lock = threading.RLock()
        self.cached_key, self.cached, self.checked_at = None, None, 0
        self.match_cache = {}

    def _sources(self):
        if not self.projects.exists():
            return []
        sources = []
        for directory in self.projects.iterdir():
            if directory.is_symlink() or not directory.is_dir():
                continue
            files = [p for p in directory.glob('*.jsonl') if p.is_file() and not p.is_symlink()]
            if not files:
                continue
            latest = max(p.stat().st_mtime for p in files)
            title = directory.name
            prefix = str(Path.home()).replace('/', '-') + '-'
            if title.startswith(prefix):
                title = title[len(prefix):]
            title = re.sub(r'[^\w .\-ぁ-んァ-ヶ一-龯]', ' ', redact(title))[-90:].strip() or 'Claude Codeプロジェクト'
            sources.append({'id': hashlib.sha256(directory.name.encode()).hexdigest()[:24], 'title': title,
                            'updated_at': _stamp(latest), '_directory': directory})
        return sorted(sources, key=lambda s: s['updated_at'], reverse=True)

    def sources(self):
        try:
            return {'sources': [{k: s[k] for k in ('id', 'title', 'updated_at')} for s in self._sources()]}
        except OSError:
            return {'sources': [], 'error': 'Claude Codeプロジェクト一覧を読み取れません'}

    def match(self, event):
        """Return one unambiguous recent conversation; an empty result keeps calendar context only.

        No selection is read or changed. Matching is local; no model/API or persisted index is used.
        Only the matched direct user messages (not neighbouring unrelated content) are returned.
        """
        empty = {'source_id': None, 'title': None, 'updated_at': None, 'text': '',
                 'error': None, 'sources': [], 'match_reason': '関連する最近のClaude Code会話なし'}
        if not isinstance(event, dict):
            return empty
        title_terms = _terms(event.get('title'))
        description_terms = _terms(event.get('description'))
        attendees = event.get('attendees')
        names = ' '.join(str(person.get('displayName') or person.get('name') or '')
                         for person in attendees if isinstance(person, dict)) if isinstance(attendees, list) else ''
        excluded_names = _NON_CASE_NAMES | _terms(mica_config.user_name()) | _terms(names)
        title_terms -= excluded_names
        description_terms -= excluded_names
        anchors = title_terms or description_terms
        if not anchors:
            return dict(empty, match_reason='予定に案件を特定できる語がないためカレンダーのみ')
        supporting = (description_terms | _terms(names)) - anchors
        cutoff = time.time() - MATCH_AGE_DAYS * 86400
        with self.lock:
            try:
                # Claude's existing prompt history locates relevant sessions outside the latest 256.
                # Read only its bounded tail; pastedContents/project values are never used.
                session_hits = set()
                history = self.projects.parent / 'history.jsonl'
                if history.is_file() and not history.is_symlink():
                    with history.open('rb') as handle:
                        offset = max(0, history.stat().st_size - HISTORY_READ_BYTES)
                        handle.seek(offset)
                        lines = handle.read(HISTORY_READ_BYTES).splitlines()
                    for line in lines[1:] if offset else lines:
                        try:
                            row = json.loads(line)
                        except (ValueError, UnicodeError):
                            continue
                        if not isinstance(row, dict) or not isinstance(row.get('display'), str):
                            continue
                        session = row.get('sessionId')
                        if isinstance(session, str) and re.fullmatch(r'[a-fA-F0-9-]{8,64}', session):
                            display = _normalized(row['display'])
                            if all(_contains(display, term) for term in anchors):
                                session_hits.add(session)
                files = []
                for source in self._sources():
                    project_hit = any(_contains(_normalized(source['title']), term) for term in anchors)
                    for path in source['_directory'].glob('*.jsonl'):
                        if path.is_symlink() or not path.is_file():
                            continue
                        stat = path.stat()
                        if stat.st_mtime >= cutoff:
                            files.append((path.stem in session_hits, project_hit, stat.st_mtime_ns, path, stat, source))
                # Project metadata can promote relevant older conversations; unrelated latest projects cannot win.
                files.sort(key=lambda row: (row[0], row[1], row[2]), reverse=True)
                candidates, active = [], set()
                for _, project_hit, _, path, stat, source in files[:MATCH_FILES]:
                    cache_key = (str(path), stat.st_mtime_ns, stat.st_size)
                    active.add(cache_key)
                    messages = self.match_cache.get(cache_key)
                    if messages is None:
                        with path.open('rb') as handle:
                            head = handle.read(MATCH_READ_BYTES // 2)
                            offset = max(len(head), stat.st_size - MATCH_READ_BYTES // 2)
                            handle.seek(offset)
                            tail = handle.read(MATCH_READ_BYTES // 2)
                        messages = _user_messages(head) + _user_messages(tail, partial=offset > len(head))
                        # Keep only bounded user content in memory, never raw transcript/tool output.
                        self.match_cache[cache_key] = messages
                    matched, hits, topic_hits = [], set(), set()
                    previous_included = None
                    for stamp, content in messages:
                        if stamp is None or stamp.timestamp() < cutoff or stamp.timestamp() > time.time() + 300:
                            previous_included = None
                            continue
                        normalized = _normalized(content)
                        row_hits = {term for term in anchors if _contains(normalized, term)}
                        direct_match = row_hits == anchors
                        correction = previous_included is not None and _is_followup_correction(previous_included, (stamp, content))
                        if not direct_match and not correction:
                            previous_included = None
                            continue
                        matched.append((stamp, content))
                        previous_included = (stamp, content)
                        hits.update(row_hits)
                        topic_hits.update(term for term in supporting if _contains(normalized, term))
                    if not matched:
                        continue
                    # Stronger agenda overlap can distinguish two conversations with the same client name.
                    score = 4 * len(hits) + 3 * len(topic_hits)
                    candidates.append((score, max(stamp for stamp, _ in matched), path, source, matched))
                self.match_cache = {key: value for key, value in self.match_cache.items() if key in active}
                if not candidates:
                    return empty
                candidates.sort(key=lambda row: (row[0], row[1]), reverse=True)
                winner = candidates[0]
                if len(candidates) > 1 and winner[0] - candidates[1][0] < 3:
                    return dict(empty, match_reason='同程度に一致する会話が複数あるためカレンダーのみ')
                _, updated, path, source, messages = winner
                conversation = path.stem if re.fullmatch(r'[a-fA-F0-9-]{8,64}', path.stem) else hashlib.sha256(path.name.encode()).hexdigest()[:12]
                source_id = 'claude-code:' + hashlib.sha256(str(path).encode()).hexdigest()[:24]
                label = f"Claude Code / {source['title']} / 会話 {conversation}"
                text = label + '\n'
                pieces, remaining = [], 3000 - len(text)
                for stamp, content in reversed(messages[-4:]):
                    prefix = f'\n[{stamp.isoformat()}] ユーザー: '
                    if remaining <= len(prefix):
                        break
                    piece = prefix + content[:remaining - len(prefix)]
                    pieces.append(piece)
                    remaining -= len(piece)
                text += ''.join(reversed(pieces))
                provenance = {'kind': 'claude_code', 'id': source_id, 'title': label, 'updated_at': updated.isoformat()}
                return dict(empty, source_id=source_id, title=label, updated_at=updated.isoformat(),
                            text=text, sources=[provenance], match_reason='予定の案件名・議題と直接のユーザー発言が一致')
            except (OSError, ValueError, TypeError, KeyError):
                return dict(empty, error='Claude Code会話を自動照合できません', match_reason='取得できないためカレンダーのみ')

    def _selected(self):
        if not self.selection.exists():
            return None
        if self.selection.is_symlink():
            raise ValueError('コンテキスト設定を読み取れません')
        with self.selection.open('rb') as f:
            data = f.read(1025)
        if len(data) > 1024:
            raise ValueError('コンテキスト設定が不正です')
        value = json.loads(data)
        source_id = value.get('source_id')
        if source_id is not None and (not isinstance(source_id, str) or not re.fullmatch('[a-f0-9]{24}', source_id)):
            raise ValueError('コンテキスト設定が不正です')
        return source_id

    def select(self, source_id):
        with self.lock:
            if source_id is not None and (not isinstance(source_id, str) or source_id not in {s['id'] for s in self._sources()}):
                raise ValueError('選択したプロジェクトが見つかりません')
            self.selection.parent.mkdir(parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(prefix='.context-', dir=self.selection.parent)
            try:
                with os.fdopen(fd, 'w', encoding='utf-8') as f:
                    os.fchmod(f.fileno(), 0o600)
                    json.dump({'source_id': source_id}, f)
                os.replace(name, self.selection)
            finally:
                if os.path.exists(name):
                    os.unlink(name)
            self.cached = None
            return self.current()

    def current(self, source_id='selected'):
        with self.lock:
            empty = {'source_id': None, 'title': None, 'updated_at': None, 'text': '', 'error': None}
            try:
                if source_id == 'selected':
                    source_id = self._selected()
                empty['source_id'] = source_id
                if source_id is None:
                    return empty
                now = time.monotonic()
                if self.cached is not None and self.cached.get('source_id') == source_id and now - self.checked_at < 2:
                    return dict(self.cached)
                source = next((s for s in self._sources() if s['id'] == source_id), None)
                if source is None:
                    return dict(empty, error='選択したプロジェクトが見つかりません。別のプロジェクトへは切り替えません。')
                empty['title'] = source['title']
                files = [p for p in source['_directory'].glob('*.jsonl') if p.is_file() and not p.is_symlink()]
                latest = max(files, key=lambda p: (p.stat().st_mtime_ns, p.name))
                stat = latest.stat()
                cache_key = (source_id, latest.name, stat.st_mtime_ns, stat.st_size)
                self.checked_at = now
                if self.cached is not None and self.cached_key == cache_key:
                    return dict(self.cached)
                with latest.open('rb') as f:
                    start = max(0, stat.st_size - MAX_BYTES)
                    f.seek(start)
                    data = f.read(MAX_BYTES)
                messages = [(stamp.isoformat() if stamp else '時刻不明', content)
                            for stamp, content in _user_messages(data, partial=bool(start))]
                # Include provenance within the same 3000 character cap.
                conversation = latest.stem if re.fullmatch(r'[a-fA-F0-9-]{8,64}', latest.stem) else hashlib.sha256(latest.name.encode()).hexdigest()[:12]
                text = f"Claude Code / {source['title']} / 会話 {conversation}\n"
                remaining = 3000 - len(text)
                chosen = []
                for timestamp, content in reversed(messages[-3:]):
                    prefix = f'\n[{timestamp}] ユーザー: '
                    if remaining <= len(prefix):
                        break
                    piece = prefix + content[:remaining - len(prefix)]
                    chosen.append(piece)
                    remaining -= len(piece)
                text += ''.join(reversed(chosen))
                result = dict(empty, updated_at=_stamp(stat.st_mtime), text=text if chosen else '',
                              error=None if chosen else '最新の会話に直接のユーザー指示がありません')
                self.cached_key, self.cached = cache_key, result
                return dict(result)
            except (OSError, ValueError, TypeError, KeyError):
                return dict(empty, error='選択したコンテキストを読み取れません')


_store = None


def store():
    """設定 context.projects_dir を読むので、使う時に作る。"""
    global _store
    if _store is None:
        _store = ContextStore()
    return _store
