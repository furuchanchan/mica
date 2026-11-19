"""Automatic, provenance-bearing meeting preparation; human corrections win."""
from __future__ import annotations
import datetime as dt
import hashlib
import json
from pathlib import Path
import threading
import time
from shodan_evidence import POLICY, safe_segment, clean_terms
from shodan_text import effective_segments
import mica_config
import mica_llm
from shodan_context import store, redact
from shodan_calendar import CALENDAR, choose_event, epoch, event_key, plain
from shodan_session import atomic_json, load_json, read_brief, lock, append_json

_threads={};_guard=threading.Lock()

def event_for(folder):
    folder=Path(folder)
    if folder.name=='.next-meeting':
        snap=CALENDAR.snapshot() if mica_config.calendar_enabled() else {};events=snap.get('events',[])
        active=load_json(folder.parent/'.live-session.json',{}).get('folder')
        if active:
            active_path=Path(active)
            if active_path.parent.resolve()==folder.parent.resolve() and load_json(active_path/'live-state.json',{}).get('status') in ('準備中','文字起こし中'):
                active_key=event_key(load_json(active_path/'meta.json',{}))
                events=[e for e in events if event_key(e)!=active_key]
        # 始まって10分を過ぎた予定（終日の作業枠など）に次の会議の準備を取られない。
        events=[e for e in events if epoch(e['start'])>time.time()-600]
        event,warning=choose_event(events,upcoming=True)
        return event,warning or snap.get('error') or snap.get('warning')
    meta=load_json(folder/'meta.json',{})
    if meta.get('id') and meta.get('start') and meta.get('end'):return meta,None
    if meta.get('test'):return None,None
    snap=CALENDAR.snapshot() if mica_config.calendar_enabled() else {}
    at=epoch(meta['recorded_at']) if meta.get('recorded_at') else time.time()
    event,warning=choose_event(snap.get('events',[]),now=at)
    return event,warning or snap.get('error') or snap.get('warning')

def calendar_context(event):
    if not event:return ''
    return '\n'.join(['カレンダー: '+plain(event.get('title')),
        '開始: '+str(event.get('start','')), '終了: '+str(event.get('end','')),
        '説明: '+plain(event.get('description'))[:4000],
        '参加者: '+', '.join(plain(a.get('name',a.get('displayName',''))) for a in event.get('attendees',[]) if a.get('name') or a.get('displayName'))])

def merge(folder,event,context,purpose,ends_at,preparation,terms=None,todos=None):
    """Publish completed evidence atomically, without overwriting a concurrent human edit."""
    folder=Path(folder)
    with lock(folder,'brief'):
        brief=read_brief(folder);previous=brief.get('auto_preparation',{});derived=previous.get('derived',{});manual=set(brief.get('manual_fields',[]))
        for key,value in [('purpose',purpose),('ends_at',ends_at)]:
            if key not in manual and (not brief.get(key) or brief.get(key)==derived.get(key)):
                brief[key]=value
        # Explicit background selection/off must survive an in-flight automatic search.
        if brief.get('context_mode','auto')=='auto':
            if context!=brief.get('context'):append_json(folder/'meeting-context.jsonl',{'captured_at':time.time(),**context})
            brief['context']=context
        if terms is not None: brief['recognition_terms'] = clean_terms(terms)
        if todos is not None: brief['todos'] = todos
        brief['auto_preparation']=dict(preparation,evidence_policy=POLICY,calendar_title=plain(event.get('title')) if event else None,derived={'purpose':purpose,'ends_at':ends_at},event_key=event_key(event))
        brief['updated_at']=time.time();atomic_json(folder/'meeting-brief.json',brief)
        return brief

def run_once(folder,force=False):
    folder=Path(folder);folder.mkdir(parents=True,exist_ok=True)
    try:
        with lock(folder,'preparation',blocking=False):
            status=load_json(folder/'preparation-status.json',{})
            if not force and time.time()-status.get('checked_at',0)<90:return
            event,warning=event_for(folder);key=event_key(event);brief=read_brief(folder)
            # A prepared future meeting is not silently retargeted after a human correction.
            old=brief.get('auto_preparation',{}).get('event_key')
            if folder.name=='.next-meeting' and old and old!=key and brief.get('manual_fields'):
                if key is None:
                    atomic_json(folder/'preparation-status.json',{'checked_at':time.time(),'status':'manual','message':'手で直した会議の準備を保持しています'})
                    return
                # Keep the correction bound to its meeting; the next event can still prepare itself.
                with lock(folder.parent, 'next-meeting'), lock(folder, 'brief'):
                    brief=read_brief(folder)
                    history=folder/'.prepared-history';history.mkdir(exist_ok=True)
                    atomic_json(history/(hashlib.sha256(str(old).encode()).hexdigest()+'.json'),brief)
                    brief={'purpose':'','ends_at':None,'automatic':brief.get('automatic',True),'context':{},'context_mode':'auto','manual_fields':[]}
                    atomic_json(folder/'meeting-brief.json',brief)
            state=load_json(folder/'live-state.json',{});segments=effective_segments(state)
            conversation=''
            if not event and folder.name!='.next-meeting' and len(segments)>=3:
                conversation=redact('\n'.join(safe_segment(s)['text'] for s in segments[:12]))[:3000]
            if not event and not conversation:
                context={'source_id':None,'title':None,'updated_at':None,'text':'','error':None}
                prep={'status':'waiting','message':warning or ('次の会議の予定を自動で探します。予定がなければ会話から整理します。' if folder.name=='.next-meeting' else '会話が始まると、発言から目的を整理します。'),'sources':[]}
                if brief.get('auto_preparation',{}).get('message')!=prep['message']:merge(folder,event,context,'',None,prep)
                atomic_json(folder/'preparation-status.json',{'checked_at':time.time()-75,'status':'waiting'})
                return
            reference=event or {'title':'予定のない会議','description':conversation}
            mode=brief.get('context_mode','auto')
            if not mica_config.context_enabled():matched={'text':''}
            else:matched=store().match(reference if event else {'title':'','description':conversation}) if mode=='auto' else (store().current(brief.get('context',{}).get('source_id')) if mode=='manual' else {'text':''})
            calendar_text=calendar_context(event)
            sources=([{'kind':'calendar','title':plain(event.get('title')),'start':event.get('start'),'end':event.get('end')}] if event else [{'kind':'conversation','title':'会議の冒頭の発言'}])
            if matched.get('text'):sources+=matched.get('sources') or [{'kind':'claude_code','title':matched.get('title'),'source_id':matched.get('source_id')}]
            basis='予定・Claudeから自動準備' if event and matched.get('text') else ('予定から自動準備' if event else '会話から自動準備')
            fingerprint=hashlib.sha256(json.dumps({'event':event,'context':matched,'conversation':conversation,'mode':mode,'evidence_policy':POLICY},ensure_ascii=False,sort_keys=True).encode()).hexdigest()
            if not force and status.get('fingerprint')==fingerprint and status.get('status')=='ready':
                atomic_json(folder/'preparation-status.json',dict(status,checked_at=time.time()));return
            ends=epoch(event['end']) if event and event.get('end') and epoch(event['end'])>time.time() else None
            if ends and ends>time.time()+86400:ends=None  # imminent meeting will be refreshed before it starts
            raw='\n\n'.join(x for x in [calendar_text,matched.get('text',''),('冒頭の発言:\n'+conversation) if conversation else ''] if x)
            context={'source_id':matched.get('source_id'),'title':basis,'updated_at':dt.datetime.now(dt.timezone.utc).isoformat(),'text':raw[:7500],'error':None,'sources':sources}
            prep={'status':'preparing','message':'目的と背景を自動で整理しています','sources':sources}
            merge(folder,event,context,plain(event.get('title')) if event else '',ends,prep)
            atomic_json(folder/'preparation-status.json',{'checked_at':time.time(),'status':'preparing','fingerprint':fingerprint})
            try:
                from shodan_ai import prepare_meeting
                # Do not send event identifiers, attendee emails, meeting links or authorization data.
                result=prepare_meeting({'title':plain(reference.get('title')),'description':plain(reference.get('description')),'start':reference.get('start'),'end':reference.get('end'),'attendees':[{'name':plain(a.get('name',a.get('displayName','')))} for a in reference.get('attendees',[])]},matched.get('text',''),conversation)
                if read_brief(folder).get('context_mode', 'auto') != mode:
                    atomic_json(folder/'preparation-status.json',{'checked_at':0,'status':'changed'})
                    return
                context['text']=redact('自動で整理した背景（要約）:\n'+result['background']+'\n\n参照元:\n'+raw)[:9500]
                prep.update(status='ready',message=basis+('。'+warning if warning else ''))
                if matched.get('match_reason') and not matched.get('text'):prep['message']+='。関連するClaude会話は特定できませんでした'
                merge(folder,event,context,redact(result['purpose']),ends,prep,result.get('recognition_terms', []),[redact(t) for t in result.get('todos', [])])
                atomic_json(folder/'preparation-status.json',{'checked_at':time.time(),'status':'ready','fingerprint':fingerprint})
            except mica_llm.NotConnected:
                # AI をつないでいない時は、予定と背景をそのまま使う（失敗として見せない）
                prep.update(status='ready',message=basis+'。目的の自動整理は、AI をつなぐと使えます')
                merge(folder,event,context,plain(reference.get('title')),ends,prep)
                atomic_json(folder/'preparation-status.json',{'checked_at':time.time(),'status':'ready','fingerprint':fingerprint})
            except (OSError,RuntimeError,ValueError,KeyError):
                prep.update(status='error',message='目的の自動整理を完了できませんでした。取得済みの予定・背景は利用できます。')
                merge(folder,event,context,plain(reference.get('title')),ends,prep)
                atomic_json(folder/'preparation-status.json',{'checked_at':time.time(),'status':'error','fingerprint':fingerprint})
    except BlockingIOError:return
    except (OSError,ValueError,KeyError,TypeError):
        atomic_json(folder/'preparation-status.json',{'checked_at':time.time(),'status':'error','message':'会議の準備情報を読み取れませんでした'})

def schedule(folder,force=False):
    folder=Path(folder);key=str(folder.resolve())
    with _guard:
        previous=_threads.get(key)
        if previous is not None and previous.is_alive():return
        status=load_json(folder/'preparation-status.json',{}) if folder.exists() else {}
        if not force and time.time()-status.get('checked_at',0)<90:return
        thread=threading.Thread(target=run_once,args=(folder,force),daemon=True);_threads[key]=thread;thread.start()
        for path,task in list(_threads.items()):
            if path!=key and not task.is_alive():del _threads[path]

def schedule_next(root):
    root=Path(root);root.mkdir(parents=True,exist_ok=True);schedule(root/'.next-meeting')


def wait_for(folder, timeout=50):
    with _guard:
        thread = _threads.get(str(Path(folder).resolve()))
    if thread is not None:
        thread.join(timeout=timeout)
