"""会議の1分前に、会議リンク付きの通知を出す（常駐させる。設定 calendar がオンの時だけ動く）。
対面の会議（shodan_calendar.in_person）も通知し、開始時刻に録音が始まることを知らせる。

予定の選び方は Mica と同じ eligible()（会議URLあり、または参加者2人以上。終日・辞退・空き時間は除外）。
通知を押すと会議リンクを開く。リンクが無い予定はカレンダーの予定を開く。
会議リンクは AI に渡す Mica の共有キャッシュ（calendar.json）には入れず、この処理の中だけで持つ。
"""
from __future__ import annotations
import datetime as dt
import hashlib
import json
import re
import shutil
import subprocess
import time
import urllib.parse
import urllib.request
from pathlib import Path
import mica_config
from shodan_calendar import JST, _token, eligible, epoch, event_key, in_person

PREPARED = mica_config.data_root() / '.next-meeting/meeting-brief.json'
STATE = mica_config.app_support() / 'reminded.json'
LOG = Path(__file__).with_name('reminder.log')
LEAD = 60          # 開始の何秒前に出すか
LATE = 60          # スリープ明けなどで遅れたとき、開始後この秒数までは出す
FETCH_EVERY = 300  # カレンダーを読み直す間隔
TICK = 10
URL = re.compile(r'https://[^\s"<>]*(?:zoom\.us/[jw]/|meet\.google\.com/|teams\.microsoft\.com/l/meetup-join|teams\.live\.com/meet|webex\.com/)[^\s"<>]*')


def log(msg):
    with LOG.open('a') as f:
        f.write(f'{dt.datetime.now(JST):%Y-%m-%d %H:%M:%S} {msg}\n')


def join_url(ev):
    if ev.get('hangoutLink'):
        return ev['hangoutLink']
    for point in (ev.get('conferenceData') or {}).get('entryPoints', []):
        if point.get('entryPointType') == 'video' and point.get('uri'):
            return point['uri']
    found = URL.search(str(ev.get('location', '')) + ' ' + str(ev.get('description', '')).replace('&amp;', '&'))
    return found.group(0) if found else None


def fetch():
    now = dt.datetime.now(JST)
    token = _token()
    events = {}
    for cal in mica_config.calendar_settings()['calendars']:
        q = {'timeMin': (now - dt.timedelta(minutes=5)).isoformat(), 'timeMax': (now + dt.timedelta(hours=2)).isoformat(),
             'singleEvents': 'true', 'maxResults': '50', 'orderBy': 'startTime'}
        url = f'https://www.googleapis.com/calendar/v3/calendars/{urllib.parse.quote(cal, safe="")}/events?' + urllib.parse.urlencode(q)
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={'Authorization': 'Bearer ' + token}), timeout=15) as r:
                items = json.load(r).get('items', [])
        except OSError as exc:
            log(f'カレンダー取得失敗 {cal[:6]}… {exc}')
            continue
        for ev in items:
            if not (eligible(ev) or in_person(ev, cal)):
                continue
            key = f"{ev.get('iCalUID', ev['id'])}|{ev['start']['dateTime']}"
            if key in events:
                continue  # 複数の暦にある同じ予定は、Mica の calendar.json と同じく先の暦（仕事）を使う
            events[key] = {'key': key, 'title': ev.get('summary') or '会議', 'start': ev['start']['dateTime'],
                           'link': join_url(ev), 'calendar_link': ev.get('htmlLink'),
                           'in_person': in_person(ev, cal),
                           'mica_key': event_key({'id': ev['id'], 'calendar': hashlib.sha256(cal.encode()).hexdigest()[:12], 'start': ev['start']['dateTime']})}
    return list(events.values())


def prepared(ev):
    """Mica が会議前に作った目的とやること（この予定のものだけ）。"""
    try:
        brief = json.loads(PREPARED.read_text())
    except (OSError, ValueError):
        return '', []
    if brief.get('auto_preparation', {}).get('event_key') != ev['mica_key']:
        return '', []
    return (brief.get('purpose') or '').strip(), [t for t in brief.get('todos', []) if t][:5]


def next_technique():
    """前の商談の分析が選んだ「次回の1技」。技は設定 negotiation_notes の台帳の番号。"""
    try:
        saved = json.loads(mica_config.next_technique_path().read_text())
    except FileNotFoundError:
        return ''
    name, _, phrase = saved['technique'].partition('｜')
    line = f'今回の1技：{name.strip()}'
    phrase = phrase.strip()
    return line + (f'（前回の{saved["meeting"][:20]}で選んだ一言：{phrase[:60]}{"…" if len(phrase) > 60 else ""}）' if phrase else '')


def notifier():
    found = shutil.which('terminal-notifier')
    if not found:
        raise RuntimeError('terminal-notifier が見つかりません（brew install terminal-notifier）')
    return found


def notify(ev):
    start = dt.datetime.fromisoformat(ev['start']).astimezone(JST)
    target = ev['link'] or ev['calendar_link']
    purpose, todos = prepared(ev)
    lines = [f'目的：{purpose}'] if purpose else []
    lines += ['・' + t for t in todos]
    technique = next_technique() if mica_config.negotiation_notes() else ''
    if technique:
        lines.append(technique)
    if ev['link']:
        lines.append('押すと会議に入ります')
    elif ev['in_person']:
        lines.append('対面の会議：開始時刻に Mica が録音を始めます')  # shodan_watch.py と同じ shodan_calendar.in_person
    else:
        lines.append('会議リンクなし（押すと予定を開く）')
    args = [notifier(), '-title', f'{start:%H:%M} まもなく会議：' + ev['title'][:60], '-message', '\n'.join(lines),
            '-sound', 'Glass', '-group', 'mica-' + ev['key'][:60]]
    if target:
        args += ['-open', target]
    subprocess.run(args, check=True, timeout=15)
    log(f"通知 {start:%H:%M} {ev['title'][:40]} link={'あり' if ev['link'] else 'なし'} 準備={'あり' if purpose else 'なし'}")


def main():
    if not mica_config.calendar_enabled():
        raise SystemExit('設定 calendar.enabled が false です。会議の通知は予定表を読むため、何もしません')
    STATE.parent.mkdir(parents=True, exist_ok=True)
    done = json.loads(STATE.read_text()) if STATE.exists() else {}
    events, fetched = [], 0.0
    log('起動')
    while True:
        now = time.time()
        if now - fetched >= FETCH_EVERY:
            try:
                events, fetched = fetch(), now
            except (OSError, ValueError, KeyError) as exc:
                log(f'カレンダー取得失敗 {exc}')
                fetched = now - FETCH_EVERY + 60  # 1分後にもう一度
        for ev in events:
            left = epoch(ev['start']) - now
            if ev['key'] not in done and -LATE <= left <= LEAD:
                try:
                    notify(ev)
                except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
                    log(f'通知失敗 {exc}')
                    continue
                done[ev['key']] = now
                done = {k: v for k, v in done.items() if now - v < 2 * 86400}
                STATE.write_text(json.dumps(done, ensure_ascii=False))
        time.sleep(TICK)


if __name__ == '__main__':
    main()
