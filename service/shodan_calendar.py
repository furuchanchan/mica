"""Google カレンダーの読み取り（設定 calendar がオンの時だけ使う。呼ぶ側で分岐する）。"""
from __future__ import annotations
import datetime as dt
import hashlib
import html
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
import mica_config
import mica_http
from shodan_context import redact
from shodan_session import atomic_json, load_json, lock

JST=dt.timezone(dt.timedelta(hours=9))
# 対面の会議の印。題に【面談】【商談】【対面】があるか、参加者2人以上で場所がある予定を対面とみなす。
# 「面談の準備」「面談の予約」のような作業の枠は題に【】が無いので拾わない。題に【対面】と書けば、どの予定も対象にできる。
IN_PERSON_TAGS=('【面談】','【商談】','【対面】')
ONLINE_WORDS=re.compile(r'オンライン|online|zoom|teams|meet\.google|google meet|webex',re.I)
CACHE=mica_config.app_support()/'calendar.json'

def _token():
    if not mica_config.calendar_enabled() or not mica_config.cloud_allowed():
        raise RuntimeError('カレンダーへの通信はオフです。calendar.enabled と MICA_NO_CLOUD の設定を確かめてください。')
    with (mica_config.google_credentials_dir()/f"{mica_config.calendar_settings()['account']}.json").open() as f:c=json.load(f)
    body=urllib.parse.urlencode({'client_id':c['client_id'],'client_secret':c['client_secret'],'refresh_token':c['refresh_token'],'grant_type':'refresh_token'}).encode()
    request=urllib.request.Request('https://oauth2.googleapis.com/token',data=body)
    with mica_http.urlopen(request,timeout=15) as response:return json.load(response)['access_token']

def plain(value):
    text=html.unescape(re.sub('<[^>]+>',' ',str(value or '')))
    text=re.sub(r'https?://\S+','[会議リンク]',text)
    return redact(re.sub(r'[ \t]+',' ',text)).strip()

CALL_IDS = (re.compile(r'teams\.microsoft\.com/meet/(\d+)'), re.compile(r'teams\.microsoft\.com/l/meetup-join/([^/?&\s"<>\\]+)'),
            re.compile(r'zoom\.us/j/(\d+)'), re.compile(r'meet\.google\.com/([a-z]{3}-[a-z]{4}-[a-z]{3})'),
            re.compile(r'webex\.com/(?:meet|join)/([\w.-]+)'))

def call_keys(ev):
    """会議リンクの会議番号（ハッシュ）。リンクそのものは AI に渡す calendar.json に入れない。"""
    blob = ' '.join([str(ev.get('location', '')), str(ev.get('description', '')), str(ev.get('hangoutLink', '')), json.dumps(ev.get('conferenceData') or {})])
    return sorted({hashlib.sha256(m.group(0).lower().encode()).hexdigest()[:12] for pattern in CALL_IDS for m in pattern.finditer(blob)})

def has_call(ev):
    return bool(ev.get('hangoutLink') or ev.get('conferenceData') or re.search(r'zoom\.us|teams\.microsoft|meet\.google|webex',str(ev.get('location',''))+str(ev.get('description',''))))

def in_person(ev,cal):
    """対面の会議として、開始時刻に録音を始める予定。個人の暦・会議リンクや「オンライン」の書かれた予定は除く。"""
    me=next((a for a in ev.get('attendees',[]) if a.get('self')), {})
    people=[a for a in ev.get('attendees',[]) if not a.get('resource') and a.get('responseStatus')!='declined']
    title,place=str(ev.get('summary') or ''),str(ev.get('location') or '')
    return (cal!=mica_config.calendar_settings()['personal_calendar'] and not has_call(ev) and not ONLINE_WORDS.search(title+' '+place)
            and ev.get('status')!='cancelled' and me.get('responseStatus')!='declined'
            and ev.get('transparency')!='transparent' and 'dateTime' in ev.get('start',{}) and 'dateTime' in ev.get('end',{})
            and (any(tag in title for tag in IN_PERSON_TAGS) or (len(people)>=2 and bool(place.strip()))))

def eligible(ev):
    me=next((a for a in ev.get('attendees',[]) if a.get('self')), {})
    people=[a for a in ev.get('attendees',[]) if not a.get('resource') and a.get('responseStatus')!='declined']
    return (ev.get('status')!='cancelled' and me.get('responseStatus')!='declined'
            and ev.get('transparency')!='transparent' and 'dateTime' in ev.get('start',{})
            and 'dateTime' in ev.get('end',{}) and (has_call(ev) or len(people)>=2))

def fetch_events():
    now=dt.datetime.now(JST);token=_token();events=[];errors=[];seen=set()
    calendars=mica_config.calendar_settings()['calendars']
    for cal in calendars:
        page=None
        try:
            for _ in range(10):
                q={'timeMin':(now-dt.timedelta(hours=4)).isoformat(),'timeMax':(now+dt.timedelta(hours=36)).isoformat(),'singleEvents':'true','maxResults':'100','orderBy':'startTime'}
                if page:q['pageToken']=page
                url=f'https://www.googleapis.com/calendar/v3/calendars/{urllib.parse.quote(cal,safe="")}/events?'+urllib.parse.urlencode(q)
                with mica_http.urlopen(urllib.request.Request(url,headers={'Authorization':'Bearer '+token}),timeout=15) as response:data=json.load(response)
                for ev in data.get('items',[]):
                    if not (eligible(ev) or in_person(ev,cal)):continue
                    key=(ev.get('iCalUID',ev['id']),ev['start']['dateTime'])
                    if key in seen:continue
                    seen.add(key)
                    events.append({'id':ev['id'],'calendar':hashlib.sha256(cal.encode()).hexdigest()[:12],
                        'title':plain(ev.get('summary') or '会議')[:300],'description':plain(ev.get('description'))[:4000],
                        'start':ev['start']['dateTime'],'end':ev['end']['dateTime'],
                        'in_person':in_person(ev,cal),'call_keys':call_keys(ev),
                        'attendees':[{'name':plain(a.get('displayName'))[:80]} for a in ev.get('attendees',[]) if not a.get('resource') and a.get('responseStatus')!='declined' and a.get('displayName')]})
                page=data.get('nextPageToken')
                if not page:break
            else:errors.append('予定の件数が多く、一部を取得できませんでした')
        except (urllib.error.URLError,TimeoutError,OSError,ValueError,KeyError):
            errors.append('一部のカレンダーを取得できませんでした')
    if len(errors)==len(calendars):raise RuntimeError('カレンダーを取得できませんでした')
    return {'events':sorted(events,key=lambda e:e['start']),'warning':' / '.join(sorted(set(errors))) or None}

def epoch(value):
    return dt.datetime.fromisoformat(value.replace('Z','+00:00')).timestamp()

def event_key(event):
    return hashlib.sha256(json.dumps({k:event.get(k) for k in ('id','calendar','start')},sort_keys=True).encode()).hexdigest()[:24] if event else None

LONG_BLOCK=4*3600  # これ以上の予定は作業の枠（例：9時〜18時の期日つきの枠。参加者が多く会議リンクもある）

def is_block(event):
    return epoch(event['end'])-epoch(event['start'])>=LONG_BLOCK

def same_meeting(events):
    """重なった予定が同じ会議番号を持てば同じ会議（招待と自分で入れた予定など）。参加者の多い方を使う。"""
    shared=set(events[0].get('call_keys') or [])
    for event in events[1:]:shared&=set(event.get('call_keys') or [])
    return max(events,key=lambda e:(len(e.get('attendees') or []),len(e.get('description') or ''))) if shared else None

def pick(events):
    """重なった予定から会議を1つ選ぶ。作業の枠は、ほかに予定があれば会議と見なさない。選べなければ None。"""
    short=[e for e in events if not is_block(e)] or events
    return short[0] if len(short)==1 else same_meeting(short)

def choose_event(events,now=None,upcoming=False):
    now=time.time() if now is None else now
    ongoing=[e for e in events if epoch(e['start'])<=now<epoch(e['end'])]
    if ongoing and all(is_block(e) for e in ongoing) and any(now<epoch(e['start'])<=now+600 and not is_block(e) for e in events):
        ongoing=[]  # 作業の枠の最中に、10分以内に始まる会議へ早めに入った
    if ongoing:
        chosen=pick(ongoing)
        return (chosen,None) if chosen else (None,'同じ時間に複数の予定があるため、予定の自動選択を保留しています')
    ahead=sorted((e for e in events if now<epoch(e['start'])<=now+(36*3600 if upcoming else 600)),key=lambda e:epoch(e['start']))
    ahead=[e for e in ahead if not is_block(e)] or ahead
    if not ahead:return None,None
    first=ahead[0]
    chosen=pick([e for e in ahead if epoch(e['start'])<epoch(first['end'])])
    return (chosen,None) if chosen else (None,'重なった予定があるため、予定の自動選択を保留しています')

def current_event(events):return choose_event(events)[0]

class CalendarStore:
    def __init__(self,path=CACHE):self.path=Path(path);self.thread=None;self.guard=threading.Lock()
    def refresh(self,force=False):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        try:
            with lock(self.path.parent,'calendar-refresh',blocking=False):
                previous=load_json(self.path,{})
                if not force and time.time()-previous.get('checked_at',0)<300:return previous
                try:
                    value=fetch_events();value.update(checked_at=time.time(),updated_at=time.time(),error=None)
                except (OSError,ValueError,KeyError,RuntimeError,TimeoutError):
                    # Retain cached evidence for diagnosis, but never select from failed/stale data.
                    value=dict(previous,checked_at=time.time(),error='カレンダーを取得できません。音声から会議の目的を整理します。')
                atomic_json(self.path,value);return value
        except BlockingIOError:return load_json(self.path,{})
    def snapshot(self):
        if not mica_config.calendar_enabled() or not mica_config.cloud_allowed():
            return {'events': [], 'error': None}
        value=load_json(self.path,{})
        with self.guard:
            if time.time()-value.get('checked_at',0)>=300 and (self.thread is None or not self.thread.is_alive()):
                self.thread=threading.Thread(target=self.refresh,daemon=True);self.thread.start()
        if value.get('error') or time.time()-value.get('updated_at',0)>900:return dict(value,events=[])
        return value

CALENDAR=CalendarStore()
