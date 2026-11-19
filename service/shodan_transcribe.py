"""Diarized file chunks; anonymous API labels never persist across requests.

Reference arrays use known_speaker_names[] / known_speaker_references[], per:
https://developers.openai.com/api/reference/resources/audio/subresources/transcriptions/methods/create
"""
from __future__ import annotations

import base64
import hashlib
import http.client
import difflib
import unicodedata
import time
import array
from concurrent.futures import ThreadPoolExecutor
import json
import math
import mimetypes
from pathlib import Path
import re
import subprocess
import urllib.error
import urllib.request
import uuid
import wave

import mica_config
from shodan_evidence import canonical, numeric_keys, vocabulary, mark_verification, disputed, equivalent_text
from shodan_text import assemble_clauses

# ffmpeg の場所は mica_config.ffmpeg()。launchd の最小の PATH では見つからないので、その場合は設定 ffmpeg に絶対パスを書く。


class TransientTranscriptionError(RuntimeError):
    """Retry the retained batch after transport/service recovery, in capture order."""


class LocalRecognitionPending(TransientTranscriptionError):
    kind = 'local_model'


def _inside(path: Path, folder: Path) -> Path:
    resolved = path.resolve()
    if not resolved.is_relative_to(folder):
        raise RuntimeError('音声と話者参照は同じ会議フォルダ内に置いてください。')
    return resolved


def _duration(path: Path) -> float:
    try:
        with wave.open(str(path), 'rb') as audio:
            return audio.getnframes() / audio.getframerate()
    except (OSError, EOFError, wave.Error, ZeroDivisionError):
        raise RuntimeError('話者参照の音声形式が正しくありません。') from None


def _registry(folder: Path) -> list[dict]:
    path = _inside(folder / 'speaker-registry.json', folder)
    if not path.exists():
        return []
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        speakers = value['speakers']
        if not isinstance(speakers, list) or len(speakers) > 4:
            raise ValueError()
        names = set()
        for speaker in speakers:
            name, ref = speaker['name'], speaker['reference']
            if not re.fullmatch(r'相手[1-4]', name) or name in names:
                raise ValueError()
            names.add(name)
            if not isinstance(ref, str) or Path(ref).is_absolute():
                raise ValueError()
            reference = _inside(folder / ref, folder)
            if not reference.is_file() or not 2 <= _duration(reference) <= 10:
                raise ValueError()
        return speakers
    except (OSError, ValueError, TypeError, KeyError):
        raise RuntimeError('この会議の話者参照を読み込めません。') from None


def _multipart(fields: list[tuple[str, str]], path: Path) -> tuple[bytes, str]:
    boundary = 'shodan-' + uuid.uuid4().hex
    parts = []
    for name, value in fields:
        parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
                      + value + '\r\n').encode('utf-8'))
    # Use a generated filename: local filenames and paths are not API metadata.
    suffix = path.suffix.lower()
    content_type = mimetypes.guess_type('audio' + suffix)[0] or 'application/octet-stream'
    parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
                  f'filename="audio{suffix}"\r\nContent-Type: {content_type}\r\n\r\n').encode('utf-8'))
    try:
        parts.append(path.read_bytes())
    except OSError:
        raise RuntimeError('文字起こし用の音声を読み込めません。') from None
    parts.append(f'\r\n--{boundary}--\r\n'.encode('ascii'))
    return b''.join(parts), boundary


def _post(fields, path):
    body, boundary = _multipart(fields, path)
    request = urllib.request.Request(
        'https://api.openai.com/v1/audio/transcriptions', method='POST', data=body,
        headers={'Authorization': 'Bearer ' + mica_config.transcription_check_key(),
                 'Content-Type': 'multipart/form-data; boundary=' + boundary})
    # One transport attempt here. The live worker owns the durable retry queue;
    # nested retries would hide the outage and hold two workers for minutes.
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        code = exc.code
        exc.close()
        error = TransientTranscriptionError if code in (408, 429, 500, 502, 503, 504) else RuntimeError
        raise error(f'OpenAI文字起こしエラー（HTTP {code}）。音声を保持しました。') from None
    except (urllib.error.URLError, OSError, TimeoutError, http.client.IncompleteRead, http.client.RemoteDisconnected):
        raise TransientTranscriptionError('文字起こしの接続を待っています。録音は継続し、復旧後に再処理します。') from None
    except (ValueError, UnicodeError):
        raise RuntimeError('OpenAI文字起こしの応答形式が正しくありません。') from None


def _request(path: Path, speakers: list[dict], folder: Path) -> list[dict]:
    fields = [('model', 'gpt-4o-transcribe-diarize'), ('response_format', 'diarized_json'),
              ('chunking_strategy', 'auto'), ('language', 'ja')]
    for speaker in speakers:
        reference = _inside(folder / speaker['reference'], folder)
        try:
            data = base64.b64encode(reference.read_bytes()).decode('ascii')
        except OSError:
            raise RuntimeError('この会議の話者参照を読み込めません。') from None
        fields.extend([('known_speaker_names[]', speaker['name']),
                       ('known_speaker_references[]', 'data:audio/wav;base64,' + data)])
    result = _post(fields, path)
    try:
        segments = result['segments']
        if not isinstance(segments, list):
            raise ValueError()
        clean = []
        for segment in segments:
            start, end = float(segment['start']), float(segment['end'])
            if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end < start:
                raise ValueError()
            if not isinstance(segment['text'], str):
                raise ValueError()
            speaker = segment.get('speaker')
            if speaker is not None and not isinstance(speaker, str):
                raise ValueError()
            if segment['text'].strip():
                clean.append({'start': start, 'end': end, 'speaker': speaker,
                              'text': segment['text'].strip()})
        return clean
    except (TypeError, ValueError, KeyError):
        raise RuntimeError('OpenAI文字起こしの応答形式が正しくありません。') from None


def _register(path: Path, segment: dict, speakers: list[dict], folder: Path) -> str:
    refs = _inside(folder / 'speaker-references', folder)
    refs.mkdir(mode=0o700, exist_ok=True)
    used = {speaker['name'] for speaker in speakers}
    number = next(n for n in range(1, 5) if f'相手{n}' not in used)
    name = f'相手{number}'
    target = _inside(refs / f'speaker-{number}.wav', folder)
    temporary = _inside(refs / f'.{uuid.uuid4().hex}.wav', folder)
    try:
        result = subprocess.run(
            [mica_config.ffmpeg(), '-nostdin', '-v', 'error', '-y', '-i', str(path),
             '-ss', str(segment['start']), '-t', str(min(8, segment['end'] - segment['start'])),
             '-vn', '-ac', '1', '-ar', '16000', '-c:a', 'pcm_s16le', str(temporary)],
            capture_output=True, timeout=30, check=False)
        if result.returncode != 0:
            raise RuntimeError('話者参照の音声切り出しに失敗しました。')
        temporary.chmod(0o600)
        duration = _duration(temporary)
        if not 2 <= duration <= 10:
            # A truncated source cannot serve as a valid reference.
            return ''
        temporary.replace(target)
        entry = {'name': name, 'reference': str(target.relative_to(folder))}
        registry_path = _inside(folder / 'speaker-registry.json', folder)
        registry_temp = _inside(folder / f'.speaker-registry-{uuid.uuid4().hex}.tmp', folder)
        try:
            registry_temp.write_text(json.dumps({'speakers': speakers + [entry]}, ensure_ascii=False), encoding='utf-8')
            registry_temp.chmod(0o600)
            registry_temp.replace(registry_path)
        finally:
            registry_temp.unlink(missing_ok=True)
        speakers.append(entry)
        return name
    except (OSError, subprocess.SubprocessError):
        raise RuntimeError('話者参照の音声を保存できません。') from None
    finally:
        temporary.unlink(missing_ok=True)


def transcribe_chunk(path, channel, offset, folder, register_new=True) -> list[dict]:
    """Transcribe one completed chunk. offset is seconds from recording start.

    confirmed means API matching against a supplied reference, not verified identity.
    New references remain tentative in this chunk; only subsequent matches confirm.
    At most four remote references are kept. Short speech can match a known voice;
    it cannot create a new reference on its own.
    Calls for the same meeting must be serialized by the worker.
    """
    folder = Path(folder).resolve()
    path = _inside(Path(path), folder)
    if channel not in ('self', 'remote'):
        raise RuntimeError('録音トラックの種類が正しくありません。')
    try:
        offset = float(offset)
        if not math.isfinite(offset) or offset < 0:
            raise ValueError()
    except (ValueError, TypeError):
        raise RuntimeError('録音の開始時刻が正しくありません。') from None
    speakers = _registry(folder) if channel == 'remote' else []
    known = {speaker['name'] for speaker in speakers}
    segments = _request(path, speakers, folder)
    newly_registered = {}
    if channel == 'remote' and register_new:
        # An anonymous A in another request has no relationship to this A.
        candidates = []
        for segment in sorted(segments, key=lambda s: (s['start'], s['end'])):
            if (candidates and candidates[-1]['speaker'] == segment['speaker']
                    and 0 <= segment['start'] - candidates[-1]['end'] <= 1.25):
                candidates[-1]['end'] = max(candidates[-1]['end'], segment['end'])
            else:
                candidates.append(dict(segment))
        candidates.sort(key=lambda s: s['end'] - s['start'], reverse=True)
        for segment in candidates:
            label = segment['speaker']
            if not label or label in known or label in newly_registered or len(speakers) >= 4:
                continue
            if segment['end'] - segment['start'] < 2:
                continue
            if any(other['speaker'] != label and other['start'] < segment['end']
                   and segment['start'] < other['end'] for other in segments):
                continue
            name = _register(path, segment, speakers, folder)
            if name:
                newly_registered[label] = name
    output = []
    for index, segment in enumerate(segments):
        label = segment['speaker']
        if channel == 'self':
            who, status = '自分', 'source'
        elif label in known:
            who, status = label, 'confirmed'
        elif label in newly_registered:
            who, status = newly_registered[label], 'tentative'
        else:
            who, status = '話者未確定', 'tentative'
        output.append({'id': f'{path.stem}-{index}', 'start': offset + segment['start'],
                       'end': offset + segment['end'], 'who': who, 'text': segment['text'],
                       'speaker_status': status})
    return output


def transcribe_text(path):
    path = Path(path)
    folder = path.parent.parent if path.parent.name == '.asr-cache' else path.parent
    terms = vocabulary(folder)
    prompt = '日本語の会話。聞こえた発言だけをそのまま文字起こししてください。'
    if terms:
        prompt += '用語：' + '、'.join(terms) + '。音声にない語句・日時・数字を補わないでください。'
    result = _post([('model', 'gpt-transcribe'), ('response_format', 'json'),
                    ('languages[]', 'ja'),
                    ('prompt', prompt)], Path(path))
    if not isinstance(result.get('text'), str):
        raise RuntimeError('OpenAI文字起こしの応答形式が正しくありません。')
    return result['text'].strip()


def _characters(text):
    chars, positions = [], []
    for index, char in enumerate(text):
        for value in unicodedata.normalize('NFKC', char).lower():
            if not value.isspace() and not unicodedata.category(value).startswith('P'):
                chars.append(value)
                positions.append(index)
    return ''.join(chars), positions


def _canonical(text):
    return canonical(text)


def _mark_verification(item):
    return mark_verification(item)


def align_transcript(segments, text, offset, duration, identity):
    """Use acoustic speaker timestamps; flag disagreement instead of inventing corrections."""
    original = ''.join(s['text'] for s in segments)
    a, positions_a = _characters(original)
    b, positions_b = _characters(text)
    matcher = difflib.SequenceMatcher(None, a, b, autojunk=False)
    if not text:
        return [dict(s, verification_note='本文認識と話者認識が一致していません。音声で確認してください。') for s in segments]
    if not segments or matcher.ratio() < .45:
        return [{'id': identity + '-uncertain', 'start': offset, 'end': offset + duration,
                 'who': '話者未確定', 'speaker_status': 'tentative', 'text': text,
                 'raw_text': original,
                 'verification_note': '認識結果が一致せず、話者と内容は未確認です。別候補: ' + original}]
    blocks = matcher.get_matching_blocks()
    def mapped(raw):
        position = next((i for i, p in enumerate(positions_a) if p >= raw), len(a))
        left = (0, 0)
        for x, y, count in blocks:
            if x <= position <= x + count:
                target = y + position - x
                return positions_b[target] if target < len(positions_b) else len(text)
            if x > position:
                target = round(left[1] + (y-left[1]) * (position-left[0]) / max(1, x-left[0]))
                return positions_b[min(target, len(positions_b)-1)] if target < len(positions_b) else len(text)
            left = (x + count, y + count)
        return len(text)
    # Text outside the acoustic matches has no word timestamp to borrow.
    leading, trailing = mapped(0), mapped(len(original))
    cuts, cursor = [leading], 0
    for segment in segments[:-1]:
        cursor += len(segment['text'])
        cuts.append(max(cuts[-1], mapped(cursor)))
    cuts.append(max(cuts[-1], trailing))
    for i in range(1, len(cuts)-1):
        position = cuts[i]
        while 0 < position < len(text) and text[position-1].isdigit() and text[position].isdigit():
            position -= 1
        cuts[i] = max(cuts[i-1], position)
    output = []
    def uncertain(piece, start, end, side):
        if not piece.strip(): return
        if end <= start: start, end = offset, offset + duration
        item = {'id': identity + '-' + side + '-uncertain', 'start': start, 'end': end,
                'who': '話者未確定', 'speaker_status': 'tentative', 'text': piece.strip(),
                'raw_text': '', 'alignment_review': {'status': 'unresolved',
                    'reason': 'text_without_acoustic_timestamp', 'range': 'batch_bound'},
                'verification_note': '本文候補に発話時刻の裏付けがありません。時刻と話者は未確認です。'}
        output.append(_mark_verification(item))
    uncertain(text[:leading], offset, max(offset, segments[0]['start']), 'prefix')
    for i, segment in enumerate(segments):
        piece = text[cuts[i]:cuts[i+1]].strip()
        if not piece:
            output.append(dict(segment, text='', raw_text=segment['text'], verification_note='本文認識では確認できない発言です。別候補: ' + segment['text']))
            continue
        item = dict(segment, text=piece, raw_text=segment['text'])
        if _canonical(piece) != _canonical(segment['text']):
            item['verification_note'] = '聞き取り要確認。別の認識候補: ' + segment['text']
        output.append(_mark_verification(item))
    uncertain(text[trailing:], min(offset + duration, segments[-1]['end']), offset + duration, 'suffix')
    return output


def _anonymous_segments(path, channel, offset, folder):
    from shodan_local_asr import transcribe, ASRUnavailableError
    try:
        segments = transcribe(Path(path), vocabulary(folder))
    except ASRUnavailableError as error:
        raise LocalRecognitionPending('音声認識の準備待ちです。未処理の録音は保存しています。') from error
    return [{'id': f'{Path(path).stem}-{index}', 'start': offset + segment['start'],
             'end': offset + segment['end'], 'who': '自分' if channel == 'self' else '話者未確定',
             'speaker_status': 'source' if channel == 'self' else 'tentative', 'text': segment['text']}
            for index, segment in enumerate(segments)]


def _relabel_speakers(folder, batches, final=False):
    runtime = mica_config.app_support() / 'speaker-runtime/.venv/bin/python'
    helper = Path(__file__).resolve().with_name('shodan_speakers.py')
    if not runtime.is_file() or not helper.is_file():
        raise RuntimeError('話者判定の実行環境がありません。Micaの音声モデルを導入してください。')
    duration = max((float(segment['end']) - float(batch.get('offset', 0))
                    for batch in batches for segment in batch['segments']), default=0)
    # This Mac measured 135s for 1,212s audio. Give final review a duration-based
    # deadline instead of the old 180s ceiling that cannot cover an hour meeting.
    timeout = max(180, 60 + duration * .3) if final else 30
    try:
        response = subprocess.run([str(runtime), str(helper)], input=json.dumps(
            {'folder': str(folder), 'batches': batches, 'final': final}, ensure_ascii=False), text=True,
            capture_output=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        if final:
            raise
        print(f'ライブ話者判定が{timeout}秒で時間切れ。文字起こしを保持して後続の話者確認に回します。', flush=True)
        return [dict(batch, segments=[
            dict(segment, who='話者未確定', speaker_status='tentative') for segment in batch['segments']
        ]) for batch in batches]
    if response.returncode != 0:
        raise RuntimeError('Mac内の話者判定に失敗しました。音声を保持しました。')
    try:
        result = json.loads(response.stdout)['batches']
        if len(result) != len(batches):
            raise ValueError()
        for before, after in zip(batches, result):
            if len(before['segments']) != len(after['segments']):
                raise ValueError()
            # The acoustic helper sorts chronologically. Arrival order can differ
            # across the two capture streams; identity is the durable segment ID.
            indexed = {item['id']: item for item in after['segments']}
            expected = [item['id'] for item in before['segments']]
            if len(indexed) != len(after['segments']) or len(set(expected)) != len(expected) or set(indexed) != set(expected):
                raise ValueError()
            for a in before['segments']:
                b = indexed[a['id']]
                if any(a.get(key) != b.get(key) for key in ('id', 'start', 'end', 'text')):
                    raise ValueError()
                if b.get('who') not in ('相手1', '相手2', '相手3', '相手4', '話者未確定'):
                    raise ValueError()
            after['segments'] = [indexed[identifier] for identifier in expected]
        return result
    except (KeyError, TypeError, ValueError):
        raise RuntimeError('Mac内の話者判定の応答が不正です。音声を保持しました。') from None


def transcribe_verified(path, channel, offset, folder):
    from shodan_local_asr import ensure_ready
    if not ensure_ready(0):
        raise LocalRecognitionPending('音声認識の準備待ちです。未処理の録音は保存しています。')
    duration = _duration(Path(path))
    if mica_config.transcription_check_key():
        with ThreadPoolExecutor(max_workers=2) as pool:
            text_future = pool.submit(transcribe_text, path)
            voice_future = pool.submit(_anonymous_segments, path, channel, offset, folder)
            segments, text = voice_future.result(), text_future.result()
    else:
        # 2重確認なし：Mac の中の認識が唯一の候補。比べる相手が無いので、食い違いの印は付かない
        segments = _anonymous_segments(path, channel, offset, folder)
        text = ''.join(s['text'] for s in segments)
        if not text:
            return []
    aligned = align_transcript(segments, text, offset, duration, Path(path).stem)
    for item in aligned:
        item['source_channel'] = channel
        if channel == 'self':
            # Uncertain words/timestamps do not change which input supplied them.
            item.update(who='自分', speaker_status='source')
    if aligned and aligned[-1]['end'] >= offset + duration - .6:
        aligned[-1]['boundary_pending'] = True
    from shodan_text import with_capture_health
    issues = []
    timeline_path = Path(path).with_suffix('.timeline.json')
    if timeline_path.exists():
        timeline = json.loads(timeline_path.read_text())
        origin = float(timeline.get('offset_seconds', offset))
        for gap in timeline.get('gaps', []):
            start = origin + gap['start_sample'] / 16000
            end = origin + gap['end_sample'] / 16000
            if gap.get('missing_position_within_previous_chunk_unknown'):
                preceding = [item for item in timeline.get('chunks', []) if item['start_sample'] <= gap['start_sample']]
                if preceding: start = origin + preceding[-1]['start_sample'] / 16000
            issues.append({'start': start, 'end': end, 'channel': channel, 'kind': 'audio_chunk_gap'})
    return with_capture_health(assemble_clauses(aligned), {'issues': issues})


def _batch_audio(paths, folder):
    folder = Path(folder).resolve()
    paths = sorted(_inside(Path(p), folder) for p in paths)
    if not paths:
        raise ValueError('音声がありません')
    chunks = []
    for path in paths:
        offset = int(path.stem.split('-')[0]) / 1000
        metadata_path = _inside(path.with_suffix('.json'), folder)
        metadata = None
        if metadata_path.exists():
            metadata = json.loads(metadata_path.read_text(encoding='utf-8'))
            if not isinstance(metadata, dict):
                raise RuntimeError('録音chunkの時刻情報が正しくありません。')
            offset = metadata.get('start_offset_seconds')
            rate = metadata.get('sample_rate')
            frames = metadata.get('frame_count')
            if (metadata.get('schema_version') != 1
                    or not isinstance(offset, (int, float)) or isinstance(offset, bool)
                    or not math.isfinite(offset) or offset < 0
                    or not isinstance(rate, (int, float)) or isinstance(rate, bool)
                    or not math.isfinite(rate) or rate <= 0
                    or not isinstance(frames, int) or isinstance(frames, bool) or frames < 0):
                raise RuntimeError('録音chunkの時刻情報が正しくありません。')
        chunks.append((float(offset), path, metadata))
    chunks.sort(key=lambda item: item[0])
    cache = _inside(folder / '.asr-cache', folder)
    cache.mkdir(mode=0o700, exist_ok=True)
    target = cache / (chunks[0][1].stem + '-' + uuid.uuid4().hex + '.wav')
    timeline_path = target.with_suffix('.timeline.json')
    origin = chunks[0][0]
    timeline = {'schema_version': 1, 'offset_seconds': origin, 'sample_rate': 16000,
                'missing_audio_recovered': False, 'chunks': [], 'gaps': [], 'status': 'building'}
    cursor = 0
    try:
        with wave.open(str(target), 'wb') as audio:
            audio.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
            for index, (offset, path, metadata) in enumerate(chunks):
                result = subprocess.run([mica_config.ffmpeg(), '-nostdin', '-v', 'error', '-i', str(path),
                                         '-map', '0:a:0', '-ar', '16000', '-ac', '1',
                                         '-c:a', 'pcm_s16le', '-f', 's16le', 'pipe:1'],
                                        capture_output=True, timeout=120, check=False)
                if result.returncode != 0 or len(result.stdout) % 2:
                    raise RuntimeError('文字起こし用の音声を読み込めません。')
                pcm = result.stdout
                frames = len(pcm) // 2
                start = round((offset - origin) * 16000)
                entry = {'audio': str(path.relative_to(folder)), 'offset_seconds': offset,
                         'start_sample': start, 'decoded_frames': frames,
                         'timing_source': 'metadata' if metadata else 'filename_milliseconds'}
                timeline['chunks'].append(entry)
                if metadata:
                    # Round positions on one shared 16kHz clock, not each duration independently.
                    # ffmpeg may round a resampled chunk up by one sample (e.g. 44100 -> 16000).
                    expected = round((offset - origin + metadata['frame_count'] / metadata['sample_rate']) * 16000) - start
                    if abs(expected - frames) == 1:
                        entry['resampling_rounding_samples'] = expected - frames
                        pcm = pcm[:expected * 2] if frames > expected else pcm + (pcm[-2:] if pcm else b'\0\0')
                        frames = expected
                if metadata is None and path.suffix.lower() == '.m4a':
                    entry['legacy_aac_timing_uncertain'] = True
                    if index + 1 < len(chunks):
                        next_start = round((chunks[index + 1][0] - origin) * 16000)
                        excess = start + frames - next_start
                        if excess > 0:
                            entry['legacy_aac_overlap_samples'] = excess
                            if excess > 1024:
                                raise RuntimeError('AAC音声の時刻超過が64msを超えています。結合を停止しました。')
                            # AVAssetWriter AAC has a measured 42ms trailing surplus.
                            # Limit this legacy rule to one 16kHz AAC frame; record every trim.
                            pcm = pcm[:(frames - excess) * 2]
                            frames -= excess
                            entry['legacy_aac_padding_trim_samples'] = excess
                            entry['legacy_aac_padding_trim_seconds'] = excess / 16000
                    else:
                        entry['legacy_aac_terminal_padding_unknown'] = True
                if start < cursor:
                    overlap = cursor - start
                    entry['overlap_samples'] = overlap
                    if overlap > 1:
                        raise RuntimeError('録音chunkの時刻が重なっています。音声を切り捨てず結合を停止しました。')
                    # Independent resampling/rounding can disagree by one 16kHz sample.
                    entry['rounding_adjustment_samples'] = overlap
                    start = cursor
                if start > cursor:
                    gap = start - cursor
                    timeline['gaps'].append({'start_sample': cursor, 'end_sample': start,
                                             'duration_seconds': gap / 16000,
                                             'reason': 'chunk_offset_gap', 'replacement': 'silence',
                                             'missing_position_within_previous_chunk_unknown': bool(
                                                 index and chunks[index - 1][2] is None
                                                 and chunks[index - 1][1].suffix.lower() == '.m4a')})
                    while gap:
                        count = min(gap, 16000)
                        audio.writeframesraw(b'\0\0' * count)
                        gap -= count
                    cursor = start
                audio.writeframesraw(pcm)
                cursor += frames
                if metadata:
                    expected = round((offset - origin + metadata['frame_count'] / metadata['sample_rate']) * 16000) - entry['start_sample']
                    entry['expected_frames'] = expected
                    difference = expected - frames
                    if difference > 0:
                        timeline['gaps'].append({'start_sample': cursor, 'end_sample': cursor + difference,
                                                 'duration_seconds': difference / 16000,
                                                 'reason': 'metadata_frame_shortfall', 'replacement': 'silence'})
                        remaining = difference
                        while remaining:
                            count = min(remaining, 16000)
                            audio.writeframesraw(b'\0\0' * count)
                            remaining -= count
                        cursor += difference
                    elif difference < -1:
                        entry['excess_decoded_frames'] = -difference
                        raise RuntimeError('録音chunkのsample数が時刻情報を超えています。音声を切り捨てず結合を停止しました。')
                entry['end_sample'] = cursor
        target.chmod(0o600)
        timeline['frames'] = cursor
        timeline['status'] = 'complete_with_silence' if timeline['gaps'] else 'complete'
    except BaseException as error:
        timeline['status'] = 'failed'
        timeline['error'] = str(error)
        target.unlink(missing_ok=True)
        raise
    finally:
        timeline_path.write_text(json.dumps(timeline, ensure_ascii=False, indent=2), encoding='utf-8')
        timeline_path.chmod(0o600)
    return target, origin

def _silent(path):
    # Only discard near digital silence; quiet speech is retained.
    with wave.open(str(path), 'rb') as audio:
        samples = array.array('h', audio.readframes(audio.getnframes()))
    return not samples or max(abs(value) for value in samples) < 16


def _repair_boundary(previous, current, audio, offset, folder):
    old = previous['segments']
    if (not old
            or abs(previous['offset'] + previous['duration'] - offset) > .3):
        return None
    # A recognizer can skip the beginning of a cut utterance. Its first returned
    # timestamp is therefore not evidence that there was silence at the cut.
    if (not any(disputed(item) for item in old + current)
            and not old[-1].get('boundary_pending') and old[-1]['end'] < offset - 1
            and current and current[0]['start'] > offset + 1):
        return None
    source = _inside(folder / previous['audio'], folder)
    combined = folder / '.asr-cache' / (uuid.uuid4().hex + '.wav')
    try:
        with wave.open(str(combined), 'wb') as target:
            target.setparams((1, 2, 16000, 0, 'NONE', ''))
            for path, length in ((source, offset - previous['offset']), (audio, None)):
                with wave.open(str(path), 'rb') as reader:
                    if (reader.getnchannels(), reader.getsampwidth(), reader.getframerate()) != (1, 2, 16000):
                        raise RuntimeError('境界確認の音声形式が正しくありません。')
                    expected = round(length * 16000) if length is not None else reader.getnframes()
                    if expected < 0:
                        raise RuntimeError('境界確認の音声時刻が正しくありません。')
                    frames = min(reader.getnframes(), expected)
                    target.writeframes(reader.readframes(frames))
                    if frames < expected:
                        missing = expected - frames
                        gap = {'schema_version': 1, 'reason': 'boundary_context_shortfall',
                               'audio': str(path.relative_to(folder)),
                               'start_offset_seconds': previous['offset'] + frames / 16000,
                               'end_offset_seconds': offset, 'missing_samples': missing,
                               'sample_rate': 16000, 'replacement': 'silence',
                               'missing_audio_recovered': False}
                        diagnostic = combined.with_suffix('.timeline.json')
                        diagnostic.write_text(json.dumps(gap, ensure_ascii=False, indent=2), encoding='utf-8')
                        diagnostic.chmod(0o600)
                        while missing:
                            count = min(missing, 16000)
                            target.writeframes(b'\0\0' * count)
                            missing -= count
        combined.chmod(0o600)
        # Both independent recognizers hear the same complete context. Reusing
        # truncated secondary text would keep negation and dates unresolved.
        updated = transcribe_verified(combined, 'remote', previous['offset'], folder)
        if not updated:
            return None
        if current and max(item['end'] for item in updated) < max(item['end'] for item in current) - .6:
            return None
        # A preceding batch can already own a sentence crossing the left edge.
        # Do not duplicate its tail when this 40-second window starts mid-sentence.
        floor = min(item['start'] for item in old)
        updated = [item for item in updated if item['start'] >= floor - .5]
        # Context recognition can omit a suffix too. It cannot delete already
        # corroborated words (especially a later negative) from either batch.
        revised_text = canonical(''.join(item['text'] for item in updated))
        for item in old + current:
            key = canonical(item['text'])
            # A corroborated '3個' must not match the suffix of '13個' or '-3個'.
            pattern = (r'(?<![\d.+-])' if key[:1] in '0123456789+-' else '') + re.escape(key)
            if key[-1:] in '0123456789': pattern += r'(?![\d.])'
            if (not disputed(item) and not re.search(pattern, revised_text)
                    and not any(equivalent_text(item['text'], revised['text']) for revised in updated)):
                return None
        before = [item for item in updated if item['start'] < offset]
        after = [item for item in updated if item['start'] >= offset]
        if not before:
            return None
        for items, originals in ((before, old), (after, current)):
            prefix = (originals[0]['id'].rsplit('-', 1)[0] if originals else f'{round(offset * 1000):012d}-remote-batch')
            for index, item in enumerate(items):
                item['id'] = f'{prefix}-{index}'
        return before, after
    except (OSError, RuntimeError, wave.Error):
        return None  # Preserve disputed original candidates when review is unavailable.
    finally:
        combined.unlink(missing_ok=True)


def _atomic(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
    temporary.chmod(0o600)
    temporary.replace(path)


def _commit_batch_history(folder, transaction):
    history_path = _inside(folder / '.asr-history.json', folder)
    old = json.loads(history_path.read_text()) if history_path.exists() else []
    recent = transaction['history']
    if max((item['offset'] for item in old), default=-1) <= transaction['offset']:
        _atomic(history_path, recent)
        keep = {item['audio'] for item in recent}
        for item in old:
            if item['audio'] not in keep:
                _inside(folder / item['audio'], folder).unlink(missing_ok=True)


def transcribe_batch(paths, channel, folder):
    folder = Path(folder).resolve()
    prefix = Path(paths[0]).stem + '-batch'
    commits = _inside(folder / '.asr-results', folder)
    commits.mkdir(mode=0o700, exist_ok=True)
    receipt = commits / (prefix + '.json')
    if receipt.exists():
        transaction = json.loads(receipt.read_text())
        if channel == 'remote':
            _commit_batch_history(folder, transaction)
        return transaction['result']
    audio, offset = _batch_audio(paths, folder)
    keep = False
    try:
        if _silent(audio):
            return {'segments': [], 'revisions': []}
        result = transcribe_verified(audio, channel, offset, folder)
        prefix = Path(paths[0]).stem + '-batch'
        for index, segment in enumerate(result):
            segment['id'] = f'{prefix}-{index}'
        revisions = []
        if channel == 'remote':
            # Keep only the recent minute. Revisit earlier labels when new voices become available.
            history_path = _inside(folder / '.asr-history.json', folder)
            history = json.loads(history_path.read_text()) if history_path.exists() else []
            recent = []
            for item in history:
                stored = _inside(folder / item['audio'], folder)
                if item['offset'] + item['duration'] > offset - 60 and stored.exists() and item['prefix'] != prefix:
                    recent.append(item)
            if recent:
                previous = recent[-1]
                repaired = _repair_boundary(previous, result, audio, offset, folder)
                if repaired is not None:
                    old_ids = [item['id'] for item in previous['segments']]
                    previous['segments'], result = repaired
                    previous['text'] = ''.join(item['text'] for item in previous['segments'])
                    revisions.append({'replace_ids': old_ids, 'segments': previous['segments']})
            recent.append({'audio': str(audio.relative_to(folder)), 'offset': offset, 'duration': _duration(audio),
                           'prefix': prefix, 'text': ''.join(seg['text'] for seg in result),
                           'segments': result})
            recent = recent[-3:]
            previous_ids = [[segment['id'] for segment in batch['segments']] for batch in recent]
            recent = _relabel_speakers(folder, recent)
            # Local acoustic clusters may correct earlier labels; apply each revision atomically in the worker.
            for ids, batch in zip(previous_ids[:-1], recent[:-1]):
                revisions.append({'replace_ids': ids, 'segments': batch['segments']})
            result = recent[-1]['segments']
            transaction = {'offset': offset, 'history': recent, 'result': {'segments': result, 'revisions': revisions}}
            # Write-ahead receipt: a crash before live-state commit replays identical revisions without API calls.
            _atomic(receipt, transaction)
            keep = True
            _commit_batch_history(folder, transaction)
        else:
            _atomic(receipt, {'result': {'segments': result, 'revisions': revisions}})
        return {'segments': result, 'revisions': revisions}
    finally:
        if not keep:
            audio.unlink(missing_ok=True)


def transcribe_draft(paths, channel, folder):
    audio, offset = _batch_audio(paths, folder)
    try:
        if _silent(audio) or not mica_config.transcription_check_key():
            return []   # 2重確認なしでは速報（下書き）を出さず、確定の文字起こしだけを出す
        text = transcribe_text(audio)
        return ([{'id': Path(paths[0]).stem + '-draft', 'start': offset,
                  'end': offset + _duration(audio), 'who': '自分' if channel == 'self' else '確認中',
                  'speaker_status': 'draft', 'text': text}] if text else [])
    finally:
        audio.unlink(missing_ok=True)


def finalize_speakers(folder, segments):
    """Full-recording acoustic review before publishing the post-meeting transcript."""
    folder = Path(folder).resolve()
    from shodan_text import is_self_segment
    reviewed = [dict(s, who='自分', speaker_status='source') for s in segments if is_self_segment(s)]
    remote = [s for s in segments if not is_self_segment(s)]
    if not remote:
        return reviewed
    video = _inside(folder / 'video.mov', folder)
    stamp = video.stat()
    payload = [{k: item.get(k) for k in ('id', 'start', 'end', 'text', 'raw_text', 'verification_note', 'numeric_review', 'clause_status', 'text_review')} for item in remote]
    helper = Path(__file__).with_name('shodan_speakers.py')
    engine = hashlib.sha256(helper.read_bytes()).hexdigest()
    fingerprint = hashlib.sha256(json.dumps([payload, stamp.st_size, stamp.st_mtime_ns, engine], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    receipt = folder / 'speaker-final.json'
    previous = json.loads(receipt.read_text()) if receipt.exists() else {}
    if previous.get('fingerprint') == fingerprint:
        updated = previous['segments']
    else:
        cache = folder / '.asr-cache'; cache.mkdir(mode=0o700, exist_ok=True)
        audio = cache / ('final-' + uuid.uuid4().hex + '.wav')
        try:
            result = subprocess.run([mica_config.ffmpeg(), '-nostdin', '-v', 'error', '-y', '-i', str(video),
                '-map', '0:a:0', '-vn', '-ac', '1', '-ar', '16000', '-c:a', 'pcm_s16le', str(audio)],
                capture_output=True, timeout=120, check=False)
            if result.returncode != 0:
                raise RuntimeError('終了後の話者確認用に音声を取り出せません。録画を保持しました。')
            audio.chmod(0o600)
            batch = {'audio': str(audio.relative_to(folder)), 'offset': 0, 'segments': remote}
            updated = _relabel_speakers(folder, [batch], final=True)[0]['segments']
            _atomic(receipt, {'fingerprint': fingerprint, 'segments': updated})
        finally:
            audio.unlink(missing_ok=True)
    # A receipt may predate new evidence about an utterance. Apply acoustic
    # results to the current segment instead of restoring its saved copy.
    labels = {item['id']: item for item in updated}
    for source in remote:
        label = labels[source['id']]
        item = dict(source, who=label['who'], speaker_status=label['speaker_status'])
        overlap = label.get('overlap_review', {})
        if overlap.get('status') == 'unresolved' and item.get('overlap_review', {}).get('status') != 'unresolved':
            item['overlap_review'] = overlap
        if any(item.get(key, {}).get('status') == 'unresolved' for key in ('overlap_review', 'alignment_review')):
            item.update(who='話者未確定', speaker_status='tentative')
        reviewed.append(item)
    return sorted(reviewed, key=lambda s:(s['start'],s['id']))
