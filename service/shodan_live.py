#!/usr/bin/env python3
"""Local meeting service. Audio and advice leave the Mac only for the destinations written in the config (ai.*)."""
from __future__ import annotations
import argparse
import json
import math
import os
from pathlib import Path
import subprocess
import time
import threading
import shutil
import tempfile
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
import shodan_session as SESSION
from shodan_context import store as context_store
import mica_config
import mica_llm
from shodan_evidence import POLICY, safe_segment, safe_brief
from shodan_text import assemble_clauses, effective_segments, without_echo
from shodan_display import transcript_turns

HERE = Path(__file__).resolve().parent
ROOT = mica_config.data_root()
ACTIVE = ROOT / '.live-session.json'
RECORD_REQUEST = ROOT / '.record-request.json'  # Mica の［録音］［止める］。shodan_watch.py が受け取る
PORT = mica_config.live_port()
ADVICE_LOCK = threading.Lock()


atomic_json = SESSION.atomic_json
WAITING_LIVE = 'waiting_for_live'
WAITING_CONNECTION = 'waiting_for_connection'


def filtered_segments(segments, final=False):
    # Similar adjacent words can be a real response, not an acoustic echo.
    return assemble_clauses(segments, final=final)


def offline_resume_ready(folder, local_only=False, now=None):
    """Only pre-analysis work can be recovered without replaying external effects."""
    folder = Path(folder)
    audio = folder / 'live-audio'
    video = folder / 'video.mov'
    if not (audio / 'capture.done').is_file() or (audio / 'error.txt').exists() or not video.is_file() or video.is_symlink():
        return False
    if any((folder / name).exists() for name in ('analysis.md', 'analysis_complete.json', 'delivery-receipt.json', 'drive_url.txt')):
        return False
    meta = SESSION.load_json(folder / 'meta.json', {})
    if not meta or (meta.get('test') and not local_only):
        return False
    state = SESSION.load_json(folder / 'live-state.json', {})
    analysis = SESSION.load_json(folder / 'analysis-status.json', {})
    phase = analysis.get('phase')
    if (analysis.get('resume_analysis_claimed_at') is not None or state.get('processing_errors')
            or state.get('status') == 'エラー' or phase not in (None, 'not_started', WAITING_LIVE, WAITING_CONNECTION)):
        return False
    if state.get('status') == '録画終了':
        return phase == WAITING_CONNECTION
    if state.get('status') not in ('準備中', '文字起こし中', '話者を確認中', '復旧待ち'):
        return False
    retries = state.get('retry_batches', {})
    if not (phase == WAITING_CONNECTION or (retries and (state.get('status') == '復旧待ち' or phase == WAITING_LIVE))):
        return False
    now = time.time() if now is None else now
    return not retries or any(retry.get('next_attempt_at', 0) <= now for retry in retries.values())


def transcribe(folder, resume=False):
    folder = Path(folder).resolve()
    with SESSION.lock(folder, 'transcription', blocking=False):
        marker = folder / 'live-worker.pid'
        pid = str(os.getpid())
        temporary = marker.with_name(f'.{marker.name}-{pid}')
        temporary.write_text(pid)
        temporary.replace(marker)
        try:
            _transcribe(folder, resume)
        finally:
            if marker.exists() and marker.read_text().strip() == pid:
                marker.unlink()


def _transcribe(folder, resume):
    audio = folder / 'live-audio'
    state_path = folder / 'live-state.json'
    meta = json.loads((folder / 'meta.json').read_text()) if (folder / 'meta.json').exists() else {}
    state = {'title': meta.get('title', '会議'), 'status': '準備中', 'error': None,
             'segments': [], 'draft_segments': [], 'recorded_at': meta.get('recorded_at'), 'updated_at': time.time(),
             'in_person': bool(meta.get('in_person'))}
    raw, seen = [], set()
    if state_path.exists():
        previous = json.loads(state_path.read_text())
        raw, seen = previous.get('raw_segments', []), set(previous.get('processed_chunks', []))
    state['transcript_revision'] = previous.get('transcript_revision', 0) if state_path.exists() else 0
    drafts, failed, errors = {}, set(), []
    retries = previous.get('retry_batches', {}) if state_path.exists() else {}
    retries = {channel: value for channel, value in retries.items()
               if not set(value.get('chunks', [])).issubset(seen)}
    done_since = None

    def save():
        state['capture_health'] = SESSION.load_json(folder / 'capture-health.json', {})
        state.update(segments=filtered_segments(raw, final=state['status'] == '録画終了'),
                     draft_segments=filtered_segments([segment for value in drafts.values() for segment in value[1]]),
                     updated_at=time.time(), raw_segments=raw, processed_chunks=sorted(seen),
                     failed_chunks=sorted(failed), retry_batches=retries)
        if any(value.get('kind') == 'local_model' for value in retries.values()):
            state['recovery_message'] = '音声認識の再開待ちです。録音を保存しながら、速報を表示します。' if not (audio / 'capture.done').exists() else '録音は保存済みです。音声認識の準備後に、残りを処理します。'
        elif state['status'] == '復旧待ち':
            state['recovery_message'] = '録音は保存済みです。通信が戻り次第、残りの文字起こしを再開します。'
        else:
            state['recovery_message'] = ('接続の復旧待ちです。復旧後に文字起こしを続けます。' if retries else None)
        atomic_json(state_path, state)
        rows = []
        for segment in state['segments']:
            row = f"[{int(segment['start']//60):02d}:{int(segment['start']%60):02d}] {segment['who']}: {segment['text']}"
            if segment.get('verification_note'):
                row += f"\n  ※不確かな候補（AIは断定しない）: {segment['verification_note']}"
            rows.append(row)
        temp = folder / 'live-transcript.md.tmp'
        temp.write_text('# 会議中の文字起こし\n\n話者は音声からの推定です。会議後の分析も、この話者付き記録を引き継ぎます。\n\n' + '\n'.join(rows), encoding='utf-8')
        temp.replace(folder / 'live-transcript.md')

    def pending():
        result = {}
        for chunk in sorted(path for path in audio.iterdir() if path.suffix in ('.m4a', '.caf', '.wav')) if audio.exists() else []:
            if '.partial.' in chunk.name or chunk.name in seen or chunk.name in failed:
                continue
            offset, channel = chunk.stem.split('-', 1)
            int(offset)  # Reject malformed names rather than silently dropping audio.
            result.setdefault(channel, []).append(chunk)
        return result

    def signature(paths):
        return tuple(path.name for path in paths)

    def draft_job(paths, channel, snapshot):
        try:
            return transcribe_draft(paths, channel, folder)
        finally:
            snapshot.cleanup()

    save()
    auto_stop, auto_thread = (None, None)
    if not resume and not (audio / 'capture.done').exists() and mica_llm.connected('advice'):
        auto_stop, auto_thread = SESSION.start_automatic(folder)
    try:
        if resume:
            for retry in retries.values():
                for name in retry['chunks']:
                    if name not in seen and (Path(name).name != name or not (audio / name).is_file()):
                        raise RuntimeError('再開に必要な音声チャンクが見つかりません')
        from shodan_transcribe import transcribe_batch, transcribe_draft, finalize_speakers, TransientTranscriptionError
        state['status'] = '文字起こし中'
        save()
        pool = ThreadPoolExecutor(max_workers=2)
        draft_pool = ThreadPoolExecutor(max_workers=2)
        inflight, draft_inflight, draft_attempted = {}, {}, {}
        waiting_since = time.time()
        while True:
            if not folder.exists():
                return
            if (audio / 'error.txt').exists():
                raise RuntimeError((audio / 'error.txt').read_text())
            done = (audio / 'capture.done').exists()
            if done and done_since is None:
                done_since = time.time()
            # Bound finalization when the meeting ends while still offline.
            # Keep every unprocessed chunk for explicit resumption, never silently skip it.
            if done_since is not None and time.time() - done_since > 180 and retries and not inflight:
                for retry in retries.values():
                    failed.update(retry['chunks'])
                if errors:
                    state['status'] = 'エラー'
                else:
                    state['status'] = '復旧待ち'
                    state['error'] = None
                    for retry in retries.values():
                        retry['next_attempt_at'] = max(retry['next_attempt_at'], time.time() + 30)
                save()
                return
            channels = pending()
            busy = {channel for paths, channel in inflight.values()}
            for channel, paths in channels.items():
                if done_since is not None and time.time() - done_since > 180 and retries:
                    continue  # Drain existing requests before reporting retained offline audio.
                if channel in retries and time.time() < retries[channel]['next_attempt_at']:
                    continue
                if channel not in busy and (len(paths) >= 4 or done):
                    batch = paths[:4]
                    inflight[pool.submit(transcribe_batch, batch, channel, folder)] = (batch, channel)
                    busy.add(channel)
            draft_busy = {channel for names, channel in draft_inflight.values()}
            for channel, paths in channels.items():
                paths = paths[-4:]  # Bound preview work to the newest ~20 seconds during backlog.
                names = signature(paths)
                network_wait = channel in retries and retries[channel].get('kind', 'network') == 'network'
                if done or network_wait or channel in draft_busy or draft_attempted.get(channel) == names:
                    continue
                # Immutable copies prevent successful finalization deleting a queued draft's input.
                snapshot = tempfile.TemporaryDirectory(prefix='.draft-', dir=audio)
                try:
                    copies = []
                    for path in paths:
                        target = Path(snapshot.name) / path.name
                        shutil.copyfile(path, target)
                        if path.with_suffix('.json').exists():
                            shutil.copyfile(path.with_suffix('.json'), target.with_suffix('.json'))
                        copies.append(target)
                    future = draft_pool.submit(draft_job, copies, channel, snapshot)
                except Exception:
                    snapshot.cleanup()
                    raise
                draft_inflight[future] = (names, channel)
                draft_attempted[channel] = names
            completed, _ = wait(inflight, timeout=.2, return_when=FIRST_COMPLETED) if inflight else (set(), set())
            for future in completed:
                paths, channel = inflight.pop(future)
                try:
                    result = future.result()
                    updated = {segment['id']: segment for segment in raw}
                    original = dict(updated)
                    for revision in result['revisions']:
                        for segment_id in revision['replace_ids']:
                            updated.pop(segment_id, None)
                        for segment in revision['segments']:
                            updated[segment['id']] = segment
                    if updated != original:
                        state['transcript_revision'] += 1
                    for segment in result['segments']:
                        updated[segment['id']] = segment
                except Exception as exc:
                    if isinstance(exc, TransientTranscriptionError):
                        previous_retry = retries.get(channel, {})
                        attempt = previous_retry.get('attempts', 0) + 1
                        retries[channel] = {'chunks': list(signature(paths)), 'attempts': attempt,
                                            'kind': getattr(exc, 'kind', 'network'),
                                            'first_failure_at': previous_retry.get('first_failure_at', time.time()),
                                            'next_attempt_at': time.time() + min(30, 2 ** min(attempt, 5))}
                        SESSION.append_json(folder / 'transcription-events.jsonl',
                                             {'event': 'retry_scheduled', 'at': time.time(),
                                              'channel': channel, 'error_type': type(exc).__name__,
                                              'reason': str(exc), **retries[channel]})
                        save()
                        continue
                    failed.update(signature(paths))
                    retries.pop(channel, None)
                    errors.append(f'{channel} [{", ".join(signature(paths))}]: {type(exc).__name__}: {exc}')
                    state['error'] = f'{len(failed)}件の音声を処理できませんでした。録音を保持しています。'
                    state['processing_errors'] = list(errors)
                    drafts.pop(channel, None)
                    save()
                    continue
                raw[:] = updated.values()
                seen.update(signature(paths))
                recovered = retries.pop(channel, None)
                if recovered:
                    SESSION.append_json(folder / 'transcription-events.jsonl',
                                         {'event': 'recovered', 'at': time.time(), 'channel': channel,
                                          'chunks': list(signature(paths)), 'attempts': recovered['attempts']})
                    state.pop('draft_error', None)
                # Never resurrect a draft containing a chunk which is now final.
                drafts.pop(channel, None)
                save()
                for chunk in paths:
                    chunk.unlink(missing_ok=True)  # Original audio remains in video.mov.
                    chunk.with_suffix('.json').unlink(missing_ok=True)
            current = pending()
            for future in list(draft_inflight):
                if not future.done():
                    continue
                names, channel = draft_inflight.pop(future)
                try:
                    segments = future.result()
                except Exception as exc:
                    # A preview failure must not stop final recognition or recording.
                    state['draft_error'] = f'{channel}: {type(exc).__name__}: {exc}'
                    save()
                    continue
                current_names = set(signature(current.get(channel, [])))
                displayed = drafts.get(channel)
                newer = not displayed or names[-1] > displayed[0][-1]
                if newer and set(names).issubset(current_names) and not any(name in seen or name in failed for name in names):
                    drafts[channel] = (names, segments)
                    state.pop('draft_error', None)
                    save()
            # Arrival of new audio does not invalidate a useful preview; finalization does.
            stale = [channel for channel, (names, segments) in drafts.items()
                     if not set(names).issubset(signature(current.get(channel, [])))]
            for channel in stale:
                drafts.pop(channel)
            if stale:
                save()
            if done and not inflight and not current and not draft_inflight:
                if not failed:
                    state['status'] = '話者を確認中'
                    drafts.clear()
                    save()
                    reviewed = finalize_speakers(folder, raw)
                    if reviewed != filtered_segments(raw):
                        state['transcript_revision'] += 1
                    raw[:] = reviewed
                state['status'] = 'エラー' if failed else '録画終了'
                drafts.clear()
                save()
                return
            # A crashed capture must not leave a worker polling forever.
            pid_file = folder / 'video.mov.pid'
            if pid_file.exists():
                try:
                    os.kill(int(pid_file.read_text()), 0)
                except ProcessLookupError:
                    # Capture may finish while this loop waits for transcription.
                    # Its done marker is committed before the recorder exits.
                    if not (audio / 'capture.done').exists():
                        raise RuntimeError('録画プロセスが終了しました。末尾の音声は未確定です。')
            if not seen and not channels and not inflight and not failed and time.time() - waiting_since > 45:
                raise RuntimeError('録画からライブ音声が届きません。対応版の録画アプリか確認してください。')
            time.sleep(.5)
    except Exception as exc:
        state.update(status='エラー', error=f'{type(exc).__name__}: {exc}')
        if folder.exists():
            save()
        raise
    finally:
        if auto_stop is not None:
            auto_stop.set()
            auto_thread.join(timeout=50)
        from shodan_preparation import wait_for
        wait_for(folder)
        if 'pool' in locals():
            pool.shutdown(wait=True)
        if 'draft_pool' in locals():
            draft_pool.shutdown(wait=True)


def session_folder(session_id=None):
    if session_id == '__next__':
        folder = ROOT / '.next-meeting'
        folder.mkdir(parents=True, exist_ok=True)
        return folder
    if session_id is None:
        active = SESSION.load_json(ACTIVE, {})
        if not active.get('folder'):
            return None
        folder = Path(active['folder']).resolve()
        if folder.parent == ROOT.resolve() and not folder.is_dir():
            return None
    else:
        if not isinstance(session_id, str) or '/' in session_id or session_id in ('.', '..'):
            raise ValueError('会議の指定が不正です')
        folder = (ROOT / session_id).resolve()
    if folder.parent != ROOT.resolve() or not folder.is_dir():
        raise ValueError('会議フォルダが見つかりません')
    return folder


def capture_state(folder, health):
    durations = []
    for channel in ('remote', 'self'):
        row = health.get('channels', {}).get(channel, {})
        frames, rate = row.get('received_frames'), row.get('sample_rate')
        if (isinstance(frames, (float, int)) and isinstance(rate, (float, int))
                and math.isfinite(frames) and math.isfinite(rate) and frames > 0 and rate > 0):
            durations.append(frames / rate)
    elapsed = max(durations, default=None)
    updated_at = health.get('updated_at')
    done = folder / 'live-audio/capture.done'
    if (elapsed is None or done.exists() or health.get('complete') is not False
            or health.get('error') or health.get('status') not in ('ok', 'attention')
            or not isinstance(updated_at, (float, int)) or not 0 <= time.time() - updated_at <= 5):
        return False, elapsed
    marker = folder / 'video.mov.pid'
    try:
        pid = int(marker.read_text())
        if pid <= 1 or updated_at < marker.stat().st_mtime:
            return False, elapsed
        os.kill(pid, 0)
        # PID existence alone is not enough after a crash and PID reuse.
        # The recorder must still hold this specific movie open for writing.
        writer = subprocess.run(['/usr/sbin/lsof', '-nP', '-a', '-p', str(pid), '-Ffa', '--', str(folder / 'video.mov')],
                                capture_output=True, text=True, timeout=1)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return False, elapsed
    fields = writer.stdout.splitlines()
    active = (writer.returncode == 0 and not writer.stderr.strip() and f'p{pid}' in fields
              and any(field in ('aw', 'au') for field in fields) and not done.exists())
    return active, elapsed


def active_recording():
    """いま録音している会議（どの会議を表示していても、ボタンはこれを見る）。"""
    active = SESSION.load_json(ACTIVE, {})
    folder = Path(active['folder']) if active.get('folder') else None
    if folder is None or folder.parent.resolve() != ROOT.resolve() or not folder.is_dir():
        return None
    on, _ = capture_state(folder, SESSION.load_json(folder / 'capture-health.json', {}) or {})
    if not on:
        return None
    meta = SESSION.load_json(folder / 'meta.json', {})
    return {'session_id': folder.name, 'mode': meta.get('mode', 'app'), 'title': meta.get('title')}


def read_state(session_id=None):
    value = _read_state(session_id)
    value['recording'] = active_recording()
    # つないだ分だけ機能が増える。画面はこれを見て、使えない欄に「つなぐと使えます」を出す
    value['capabilities'] = {'advice': mica_llm.connected('advice'), 'analysis': mica_config.analysis_engine() != 'none'}
    return value


def _read_state(session_id=None):
    folder = session_folder(session_id)
    if folder is None:
        return {'title': '会議を待っています', 'status': '待機中', 'segments': [], 'draft_segments': [], 'transcript_turns': [],
                'capture_active': False, 'capture_elapsed_seconds': None, 'error': None}
    meta = SESSION.load_json(folder / 'meta.json', {})
    if session_id == '__next__':
        current = session_folder()
        from shodan_preparation import schedule
        schedule(folder)
        prepared = SESSION.read_brief(folder)
        return {'title': prepared.get('auto_preparation', {}).get('calendar_title') or '次の会議（自動準備）', 'status': '準備中', 'segments': [], 'draft_segments': [], 'transcript_turns': [], 'session_id': '__next__',
                'active_session_id': current.name if current else None, 'brief': SESSION.read_brief(folder),
                'analysis': {'phase': 'not_started'}, 'ai_status': {'phase': 'idle'},
                'capture_active': False, 'capture_elapsed_seconds': None}
    state = SESSION.load_json(folder / 'live-state.json', {
        'title': meta.get('title', '会議'), 'status': '録画終了' if (folder / 'analysis.md').exists() else '準備中', 'segments': []})
    health = SESSION.load_json(folder / 'capture-health.json', {}) or {}
    capture_active, capture_elapsed = capture_state(folder, health)
    pid_path = folder / 'live-worker.pid'
    if pid_path.exists() and state.get('status') not in ('録画終了', 'エラー', '復旧待ち'):
        try:
            os.kill(int(pid_path.read_text()), 0)
        except ProcessLookupError:
            message = '文字起こし処理が停止しました。'
            if capture_active:
                message += '録画は別に継続しています。'
            state.update(status='エラー', error=message)
    state['capture_health'] = health or state.get('capture_health', {})
    result = {k: state.get(k) for k in ('title', 'status', 'segments', 'draft_segments', 'error', 'draft_error', 'recovery_message', 'updated_at', 'recorded_at', 'transcript_revision')}
    result.update(capture_active=capture_active, capture_elapsed_seconds=capture_elapsed)
    result['segments'] = [dict(s, ai_text=safe_segment(s)['text']) for s in effective_segments(state)]
    result['draft_segments'] = without_echo(result.get('draft_segments') or [], reference=result['segments'])
    result['transcript_turns'] = transcript_turns(result['segments'], result['draft_segments'])
    if state['capture_health'].get('error'):
        result['capture_warning'] = state['capture_health']['error']
    elif state['capture_health'].get('issues'):
        result['capture_warning'] = '録音に途切れが見つかりました。該当区間はAIの確定情報から除いています。'
    result.update(session_id=folder.name, brief=SESSION.read_brief(folder),
                  ai_status=SESSION.ai_status(folder, 'manual'),
                  auto_ai_status=SESSION.ai_status(folder, 'auto'))
    result['ai_brief'] = safe_brief(result['brief'], result['segments'])
    analysis = SESSION.load_json(folder / 'analysis-status.json', {'phase': 'complete' if (folder / 'analysis.md').exists() else 'not_started'})
    if analysis.get('resume_analysis_claimed_at') is not None and analysis.get('phase') not in ('complete', 'error'):
        try:
            with SESSION.lock(folder, 'processing', blocking=False):
                analysis.update(phase='error', message='再開後の分析が中断しました',
                                error='分析・保存の実行結果を確認してください。自動では再実行しません。')
        except BlockingIOError:
            pass
    if analysis.get('phase') == 'complete' and (folder / 'analysis.md').exists():
        analysis['text'] = (folder / 'analysis.md').read_text()
    result['analysis'] = analysis
    manual = SESSION.load_json(folder / 'live-manual-advice.json')
    automatic = SESSION.load_json(folder / 'live-auto-advice.json')
    legacy = SESSION.load_json(folder / 'live-advice.json') if not manual and not automatic else None
    result['manual_advice'] = SESSION.answer_view(manual, result['segments'])
    result['auto_advice'] = SESSION.answer_view(automatic, result['segments'])
    result['advice'] = result['manual_advice'] or result['auto_advice'] or SESSION.answer_view(legacy, result['segments'])
    result['advice_history'] = [SESSION.answer_view(answer, result['segments'])
        for answer in SESSION.read_jsonl(folder / 'advice-history.jsonl')[-50:]]
    return result


def sessions():
    folders = sorted((p for p in ROOT.iterdir() if p.is_dir() and (p / 'meta.json').exists()), key=lambda p:p.name, reverse=True)[:30] if ROOT.exists() else []
    items = []
    for folder in folders:
        meta = SESSION.load_json(folder / 'meta.json', {})
        phase = SESSION.load_json(folder / 'analysis-status.json', {}).get('phase')
        items.append({'id': folder.name, 'title': meta.get('title', folder.name),
                      'phase': phase or ('complete' if (folder / 'analysis.md').exists() else 'not_started')})
    return {'sessions': items}


class Handler(BaseHTTPRequestHandler):
    def authorized(self, write=False):
        if self.headers.get('Host') not in (f'127.0.0.1:{PORT}', f'localhost:{PORT}'):
            return False
        origin = self.headers.get('Origin')
        return (not origin and not write) or origin in (f'http://127.0.0.1:{PORT}', f'http://localhost:{PORT}')

    def reply(self, value, code=200):
        body = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self.authorized():
            self.send_error(403); return
        url = urlparse(self.path)
        session_id = parse_qs(url.query).get('session_id', [None])[0]
        try:
            if url.path == '/api/health':
                value = {'service': 'shodan-live', 'version': 3}
            elif url.path == '/api/install':
                value = SESSION.load_json(mica_config.app_support() / 'live-install-status.json', {'status': 'pending'})
            elif url.path == '/api/context/sources':
                value = context_store().sources() if mica_config.context_enabled() else {'sources': []}
            elif url.path == '/api/context':
                value = context_store().current() if mica_config.context_enabled() else {'source_id': None, 'title': None, 'updated_at': None, 'text': '', 'error': None}
            elif url.path == '/api/state':
                value = read_state(session_id)
            elif url.path == '/api/sessions':
                value = sessions()
            elif url.path == '/':
                body = (HERE / 'live.html').read_bytes()
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'")
                self.end_headers(); self.wfile.write(body); return
            else:
                self.send_error(404); return
            self.reply(value)
        except (OSError, ValueError, KeyError) as exc:
            self.reply({'error': str(exc)}, 400)

    def do_POST(self):
        if not self.authorized(write=True):
            self.send_error(403); return
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= 16000:
                raise ValueError('入力が長すぎます')
            request = json.loads(self.rfile.read(size))
            if not isinstance(request, dict):
                raise ValueError('入力形式が正しくありません')
            if self.path == '/api/context':
                folder = session_folder(request['session_id']) if request.get('session_id') else None
                mode = request.get('mode', 'manual' if request.get('source_id') else 'off')
                if mode not in ('auto', 'manual', 'off'):
                    raise ValueError('背景の設定が不正です')
                if mode == 'manual' and not mica_config.context_enabled():
                    raise ValueError('過去の会話の参照は設定 context.enabled が false です')
                empty_context = {'source_id': None, 'title': None, 'updated_at': None, 'text': '', 'error': None}
                if mode == 'auto':
                    context = SESSION.read_brief(folder).get('context', {}) if folder else empty_context
                elif mica_config.context_enabled():
                    context = context_store().select(request.get('source_id'))
                else:
                    context = empty_context
                if folder:
                    if request['session_id'] == '__next__':
                        SESSION.save_preparation(ROOT, request.get('active_session_id'), {'context': context, 'context_mode': mode})
                    else:
                        SESSION.save_brief(folder, {'context': context, 'context_mode': mode})
                if folder:
                    from shodan_preparation import schedule
                    schedule(folder, force=True)
                self.reply(context); return
            if self.path == '/api/recording':
                if request.get('action') not in ('start', 'stop'):
                    raise ValueError('録音の操作が不正です')
                atomic_json(RECORD_REQUEST, {'action': request['action'], 'at': time.time()})
                self.reply({'accepted': True, 'action': request['action']}, 202); return
            if self.path not in ('/api/advice', '/api/brief'):
                self.send_error(404); return
            if not request.get('session_id'):
                raise ValueError('会議を指定してください')
            folder = session_folder(request['session_id'])
            if self.path == '/api/brief':
                changes = {k:request[k] for k in ('purpose', 'ends_at', 'automatic') if k in request}
                if request['session_id'] == '__next__':
                    if 'active_session_id' not in request:
                        raise ValueError('会議の準備画面を更新してください')
                    value = SESSION.save_preparation(ROOT, request['active_session_id'], changes)
                else:
                    value = SESSION.save_brief(folder, changes)
                self.reply(value); return
            if not SESSION.load_json(folder / 'live-state.json', {}).get('segments'):
                raise ValueError('文字起こしを受信してから相談できます')
            # Persist purpose even for the existing browser client.
            if 'purpose' in request:
                SESSION.save_brief(folder, {'purpose': str(request['purpose'])[:2000]})
            question = str(request.get('question') or '目的に向けて、次に何を確認すべき？')[:2000]
            accepted = SESSION.submit_advice(folder, question)
            self.reply(accepted, 202)
        except BlockingIOError:
            self.reply({'error': '前の質問に回答中です。自動ヒントとは別に処理しています。'}, 409)
        except (ValueError, TypeError, KeyError) as exc:
            self.reply({'error': str(exc)}, 400)
        except OSError:
            self.reply({'error': '会議の記録を保存できませんでした'}, 500)

    def log_message(self, *args):
        pass


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=['serve', 'transcribe'])
    parser.add_argument('folder', nargs='?')
    args = parser.parse_args()
    if args.mode == 'serve':
        ThreadingHTTPServer(('127.0.0.1', PORT), Handler).serve_forever()
    elif not args.folder:
        parser.error('transcribe requires folder')
    else:
        transcribe(args.folder)
