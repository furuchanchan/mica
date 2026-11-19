"""One meeting's brief, incremental memory and advice. No capture/UI dependency."""
from __future__ import annotations
import contextlib
import fcntl
import json
import hashlib
import math
import os
from pathlib import Path
import tempfile
import threading
import time
import uuid
from shodan_evidence import POLICY


def load_json(path, default=None):
    path = Path(path)
    return json.loads(path.read_text()) if path.exists() else default


def atomic_json(path, value):
    path = Path(path)
    fd, name = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(value, f, ensure_ascii=False)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def append_json(path, value):
    with Path(path).open('a') as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.write(json.dumps(value, ensure_ascii=False) + '\n')
        f.flush()


def read_jsonl(path):
    path = Path(path)
    if not path.exists():
        return []
    with path.open() as f:
        fcntl.flock(f, fcntl.LOCK_SH)
        return [json.loads(line) for line in f if line.strip()]


@contextlib.contextmanager
def lock(folder, name, blocking=True):
    with (Path(folder) / ('.' + name + '.lock')).open('a') as f:
        fcntl.flock(f, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def read_brief(folder):
    brief = load_json(Path(folder) / 'meeting-brief.json', {
        'purpose': '', 'ends_at': None, 'automatic': True, 'context': {}, 'updated_at': None})
    brief.setdefault('context_mode', 'manual' if brief.get('context', {}).get('source_id') else 'auto')
    return brief


def save_brief(folder, changes):
    folder = Path(folder)
    with lock(folder, 'brief'):
        brief = read_brief(folder)
        manual = set(brief.get('manual_fields', []))
        for field in ('purpose', 'ends_at'):
            if field in changes and changes[field] != brief.get(field):
                manual.add(field)
        if 'purpose' in changes:
            if not isinstance(changes['purpose'], str) or len(changes['purpose']) > 2000:
                raise ValueError('会議の目的は2000文字以内で入力してください')
            brief['purpose'] = changes['purpose']
        if 'ends_at' in changes:
            value = changes['ends_at']
            if value is not None and (isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value <= 0 or value > time.time() + 86400):
                raise ValueError('残り時間を確認してください')
            brief['ends_at'] = value
        if 'automatic' in changes:
            if not isinstance(changes['automatic'], bool):
                raise ValueError('自動ヒントの設定が不正です')
            brief['automatic'] = changes['automatic']
        if 'context' in changes:
            context = changes['context']
            if context != brief.get('context'):
                append_json(folder / 'meeting-context.jsonl', {'captured_at': time.time(), **context})
            brief['context'] = context
        if 'context_mode' in changes:
            if changes['context_mode'] not in ('auto', 'manual', 'off'):
                raise ValueError('背景の設定が不正です')
            brief['context_mode'] = changes['context_mode']
        elif 'context' in changes:
            brief['context_mode'] = 'manual' if changes['context'].get('source_id') else 'off'
        brief['manual_fields'] = sorted(manual)
        brief['updated_at'] = time.time()
        atomic_json(folder / 'meeting-brief.json', brief)
        return brief


def initialize(folder):
    from shodan_calendar import event_key
    folder = Path(folder)
    with lock(folder.parent, 'next-meeting'):
        if not (folder / 'meeting-brief.json').exists():
            draft = folder.parent / '.next-meeting' / 'meeting-brief.json'
            prepared = load_json(draft, {})
            expected = prepared.get('auto_preparation', {}).get('event_key')
            actual = event_key(load_json(folder / 'meta.json', {}))
            # Never carry a different calendar meeting's context into an unplanned call.
            archived = draft.parent / '.prepared-history' / (hashlib.sha256(str(actual).encode()).hexdigest() + '.json')
            changes = prepared if prepared and (not expected or expected == actual) else load_json(archived, {})
            changes.setdefault('purpose', '')
            changes.setdefault('automatic', True)  # 自動ヒントは既定でオン（11/6〜11/19 の13会議で相談は0回。空のアシストを開かせない）
            changes.setdefault('context', {})
            changes.setdefault('context_mode', 'auto')
            changes.setdefault('ends_at', None)
            if changes.get('ends_at') and changes['ends_at'] < time.time():
                changes['ends_at'] = None
            with lock(folder, 'brief'):
                atomic_json(folder / 'meeting-brief.json', changes)
            if draft.exists() and (not expected or expected == actual):
                draft.unlink()
    from shodan_preparation import schedule
    schedule(folder)


def save_preparation(root, expected_active_session, changes):
    """A debounced preparation edit belongs to the meeting that just started."""
    root = Path(root).resolve()
    draft = root / '.next-meeting'
    draft.mkdir(exist_ok=True)
    with lock(root, 'next-meeting'):
        active = load_json(root / '.live-session.json', {}).get('folder')
        current = Path(active).resolve() if active else None
        if current and current.parent != root:
            raise ValueError('会議フォルダが不正です')
        if current and not current.is_dir():
            current = None
        if current and current.name != expected_active_session:
            if not (current / 'meeting-brief.json').exists():
                save_brief(current, read_brief(draft))
            result = save_brief(current, changes)
            (draft / 'meeting-brief.json').unlink(missing_ok=True)
            return result
        return save_brief(draft, changes)


def refresh_context(folder):
    import mica_config
    from shodan_context import store
    brief = read_brief(folder)
    if brief.get('context_mode') == 'auto':
        from shodan_preparation import schedule
        schedule(folder)
        return read_brief(folder)
    if brief.get('context_mode') == 'off' or not mica_config.context_enabled():
        return brief
    context = store().current(brief.get('context', {}).get('source_id'))
    # Re-read even when the fetched context was unchanged: selection may have moved.
    with lock(folder, 'brief'):
        current = read_brief(folder)
        if (current.get('context', {}).get('source_id') == context.get('source_id')
                and context != current.get('context')):
            append_json(Path(folder) / 'meeting-context.jsonl', {'captured_at': time.time(), **context})
            current.update(context=context, updated_at=time.time())
            atomic_json(Path(folder) / 'meeting-brief.json', current)
        brief = current

    return brief


def remaining(brief):
    if not brief.get('ends_at'):
        return '未設定'
    seconds = max(0, int(brief['ends_at'] - time.time()))
    return f'残り{seconds // 60}分{seconds % 60}秒' if seconds else '予定時刻を過ぎています'


def _lane(mode):
    if mode not in ('manual', 'auto'):
        raise ValueError('AI処理の種類が不正です')
    return mode


def ai_status(folder, mode='manual'):
    mode = _lane(mode)
    status = load_json(Path(folder) / f'live-ai-{mode}-status.json', {'phase': 'idle', 'mode': mode})
    if status.get('phase') == 'running':
        try:
            os.kill(status['pid'], 0)
        except (ProcessLookupError, KeyError):
            # The remote result may be unknown; never automatically resend this request.
            status = dict(status, phase='error', error='AI処理が中断しました。必要なら新しい質問として相談できます。')
    return status


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def source_versions(segments):
    return {s['id']: {
        'text': _digest({k: s.get(k) for k in ('text', 'raw_text', 'verification_note', 'numeric_review',
            'clause_status', 'text_review', 'capture_review', 'overlap_review', 'alignment_review')}),
        'speaker': _digest({k: s.get(k) for k in ('who', 'speaker_status')})} for s in segments}


def transcript_fingerprints(segments):
    return {key: _digest(value) for key, value in source_versions(segments).items()}


def answer_view(answer, segments):
    """Attach source notices without modifying a saved answer or calling a model."""
    if not answer:
        return None
    view = dict(answer)
    latest = source_versions(segments)
    previous = answer.get('source_versions', {})
    changed = [key for key, value in previous.items() if latest.get(key) != value]
    text_changed = any(key not in latest or previous[key].get('text') != latest[key].get('text') for key in changed)
    view['changed_source_ids'] = changed
    if changed:
        view['source_notice'] = ('参照した発言が後から訂正されています。回答は質問時点の記録として残しています。'
                                 if text_changed else '参照した発言の話者表示が更新されています。回答は質問時点の記録として残しています。')
    elif not answer.get('request_id'):
        view['source_notice'] = '以前の方式で作成した回答です。参照元の変更は追跡できません。'
    else:
        view['source_notice'] = ''
    view['copy_text'] = (view['source_notice'] + ' 最新の文字起こしを確認してください。') if view['source_notice'] else answer['text']
    return view


def advice_event(folder, request_id, mode, event, **values):
    append_json(Path(folder) / 'advice-events.jsonl', {
        'request_id': request_id, 'mode': mode, 'event': event, 'at': time.time(), **values})


def _accept_advice(folder, question, mode):
    from shodan_text import effective_segments
    folder = Path(folder)
    state = load_json(folder / 'live-state.json', {})
    segments = effective_segments(state)
    if not segments:
        raise ValueError('文字起こしを受信してから相談できます')
    # Context preparation runs separately. Acceptance never waits for a context/AI refresh.
    brief = read_brief(folder)
    if mode == 'auto':
        if not brief.get('automatic', True):
            return None
        cursor = load_json(folder / 'auto-advice-cursor.json', {})
        # Stable IDs survive text/speaker corrections. Revision alone cannot trigger advice.
        seen = set(cursor.get('segment_ids', []))
        if not any(s['id'] not in seen and s.get('end', s.get('start', 0)) > cursor.get('read_through', -1) for s in segments):
            return None
    accepted = time.time()
    request_id = uuid.uuid4().hex
    snapshot = {'session_id': folder.name, 'question': question, 'segments': segments, 'brief': brief,
                'brief_version': _digest(brief), 'transcript_revision': state.get('transcript_revision', 0),
                'source_versions': source_versions(segments),
                'read_through': max((s.get('end', s.get('start', 0)) for s in segments), default=0),
                'timing': '会議は終了済み。会議後の行動を案内してください' if state.get('status') == '録画終了' else remaining(brief)}
    request = {'request_id': request_id, 'mode': mode, 'phase': 'accepted', 'accepted_at': accepted,
               'snapshot': snapshot}
    (folder / 'advice-requests').mkdir(exist_ok=True)
    atomic_json(folder / 'advice-requests' / (request_id + '.json'), request)
    if mode == 'auto':
        atomic_json(folder / 'auto-advice-cursor.json', {'segment_ids': sorted(seen | {s['id'] for s in segments}),
                    'request_id': request_id, 'accepted_at': accepted, 'read_through': snapshot['read_through']})
    status = {'phase': 'running', 'pid': os.getpid(), 'started_at': accepted, 'updated_at': accepted,
              'question': question, 'mode': mode, 'request_id': request_id}
    atomic_json(folder / f'live-ai-{mode}-status.json', status)
    advice_event(folder, request_id, mode, 'accepted', read_through=snapshot['read_through'])
    return request


def _generate_advice(folder, request):
    from shodan_ai import guide
    folder = Path(folder)
    mode, request_id = request['mode'], request['request_id']
    path = folder / 'advice-requests' / (request_id + '.json')
    # Durable start marker enforces one invocation even after restart or an unknown response.
    with lock(folder, 'request-' + request_id, blocking=False):
        saved = load_json(path, {})
        if saved.get('phase') != 'accepted':
            raise RuntimeError('この質問は受付済みです。自動で再送しません。')
        request = dict(saved, phase='running', generation_started_at=time.time())
        atomic_json(path, request)
    snapshot = request['snapshot']
    segments, brief = snapshot['segments'], snapshot['brief']
    status = ai_status(folder, mode)
    try:
        advice_event(folder, request_id, mode, 'generation_started')
        # AI summaries/answers are never fed back as meeting facts.
        result = guide(brief, {}, segments, segments[-150:], snapshot['question'], snapshot['timing'], all_segments=segments)
        advice_event(folder, request_id, mode, 'generation_completed')
        ids = result.get('source_ids')
        if not isinstance(ids, list) or any(not isinstance(key, str) or key not in snapshot['source_versions'] for key in ids):
            raise RuntimeError('AIの参照元を確認できませんでした。必要なら新しい質問として相談してください。')
        ids = list(dict.fromkeys(ids))
        text = result['next_step']
        if result.get('question'):
            text += '\n\n**確認すること**\n' + result['question']
        if result.get('time_plan'):
            text += '\n\n**時間の使い方**\n' + result['time_plan']
        answer = {'text': text, 'question': snapshot['question'], 'mode': mode, 'request_id': request_id,
                  'accepted_at': request['accepted_at'], 'updated_at': time.time(), 'session_id': folder.name,
                  'input_segment_ids': [s['id'] for s in segments], 'source_ids': ids,
                  'source_versions': {key: snapshot['source_versions'][key] for key in ids},
                  'read_through': snapshot['read_through'], 'brief_version': snapshot['brief_version'],
                  'transcript_revision': snapshot['transcript_revision'], 'evidence_policy': POLICY,
                  'purpose': brief.get('purpose', ''), 'context_updated_at': brief.get('context', {}).get('updated_at')}
        # One immutable history record and a distinct latest pointer for each lane.
        append_json(folder / 'advice-history.jsonl', answer)
        atomic_json(folder / f'live-{mode}-advice.json', answer)
        atomic_json(path, dict(request, phase='complete', completed_at=answer['updated_at'], answer=answer))
        atomic_json(folder / f'live-ai-{mode}-status.json', dict(status, phase='idle', updated_at=time.time()))
        advice_event(folder, request_id, mode, 'answer_saved', source_ids=ids)
        return answer
    except Exception as exc:
        message = str(exc) if isinstance(exc, (RuntimeError, ValueError)) else 'AI処理に失敗しました'
        atomic_json(path, dict(request, phase='error', error=message, ended_at=time.time()))
        atomic_json(folder / f'live-ai-{mode}-status.json', dict(status, phase='error', error=message, updated_at=time.time()))
        advice_event(folder, request_id, mode, 'failed', error_type=type(exc).__name__)
        raise


def run_advice(folder, question, mode='manual'):
    """At most one generation per lane; capture and the other lane never wait."""
    mode = _lane(mode)
    with lock(folder, 'advice-' + mode, blocking=False):
        request = _accept_advice(folder, question, mode)
        return _generate_advice(folder, request) if request else None


def submit_advice(folder, question, mode='manual'):
    folder, mode = Path(folder), _lane(mode)
    reservation = lock(folder, 'advice-' + mode, blocking=False)
    reservation.__enter__()
    try:
        # Snapshot and status are durable before returning HTTP 202 or scheduling a worker.
        request = _accept_advice(folder, question, mode)
        if request is None:
            reservation.__exit__(None, None, None)
            return None
        def work():
            try:
                _generate_advice(folder, request)
            except Exception:
                pass  # _generate_advice persists the error for this lane only.
            finally:
                reservation.__exit__(None, None, None)
        threading.Thread(target=work, daemon=False).start()
        return {'phase': 'running', 'request_id': request['request_id'], 'mode': mode,
                'session_id': folder.name, 'accepted_at': request['accepted_at']}
    except BaseException:
        if 'request' in locals() and request is not None:
            message = 'AI処理を開始できませんでした。新しい質問として相談できます。'
            atomic_json(folder / 'advice-requests' / (request['request_id'] + '.json'), dict(request, phase='error', error=message))
            atomic_json(folder / f'live-ai-{mode}-status.json', dict(ai_status(folder, mode), phase='error', error=message, updated_at=time.time()))
            advice_event(folder, request['request_id'], mode, 'start_failed')
        reservation.__exit__(None, None, None)
        raise


def automatic_loop(folder, stop):
    last = 0
    while not stop.wait(2):
        try:
            state = load_json(Path(folder) / 'live-state.json', {})
            if state.get('status') in ('録画終了', 'エラー'):
                return
            refresh_context(folder)
            status = ai_status(folder, 'auto')
            previous = max(last, status.get('started_at', 0))
            if state.get('segments') and time.time() - previous >= 30:
                # No queue: if busy, retain only current transcript for the next eligible tick.
                submitted = submit_advice(folder, '目的に向けて、次に何を確認すべき？', mode='auto')
                if submitted:
                    last = submitted['accepted_at']
        except BlockingIOError:
            pass
        except Exception:
            last = time.time()


def start_automatic(folder):
    initialize(folder)
    stop = threading.Event()
    thread = threading.Thread(target=automatic_loop, args=(folder, stop), daemon=True)
    thread.start()
    return stop, thread
