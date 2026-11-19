"""Japanese spelling comparison and complete clauses for the Mac meeting app."""
from __future__ import annotations
import ctypes as c
from functools import lru_cache
import re


class _Range(c.Structure):
    _fields_ = [('location', c.c_long), ('length', c.c_long)]


@lru_cache(maxsize=1)
def _tokenizer_api():
    cf = c.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
    definitions = {
        'CFStringCreateWithCString': (c.c_void_p, c.c_void_p, c.c_char_p, c.c_uint32),
        'CFStringGetLength': (c.c_long, c.c_void_p),
        'CFLocaleCreate': (c.c_void_p, c.c_void_p, c.c_void_p),
        'CFStringTokenizerCreate': (c.c_void_p, c.c_void_p, c.c_void_p, _Range, c.c_ulong, c.c_void_p),
        'CFStringTokenizerAdvanceToNextToken': (c.c_ulong, c.c_void_p),
        'CFStringTokenizerCopyCurrentTokenAttribute': (c.c_void_p, c.c_void_p, c.c_ulong),
        'CFStringTokenizerGetCurrentTokenRange': (_Range, c.c_void_p),
        'CFStringGetCString': (c.c_bool, c.c_void_p, c.c_char_p, c.c_long, c.c_uint32),
        'CFRelease': (None, c.c_void_p),
    }
    for name, (result, *args) in definitions.items():
        f = getattr(cf, name); f.restype = result; f.argtypes = args
    return cf


@lru_cache(maxsize=4096)
def reading_key(text):
    """Use the OS Japanese reading dictionary, never an LLM correction of facts."""
    return ''.join(reading for _, reading in reading_tokens(text))


@lru_cache(maxsize=4096)
def reading_tokens(text):
    """語ごとの（表記, 読み）。読みは OS の辞書のローマ字で、英数字だけを残す。"""
    cf = _tokenizer_api(); encoding = 0x08000100
    string = cf.CFStringCreateWithCString(None, text.encode('utf-8'), encoding)
    locale_id = cf.CFStringCreateWithCString(None, b'ja', encoding)
    locale = cf.CFLocaleCreate(None, locale_id)
    tokenizer = cf.CFStringTokenizerCreate(None, string, _Range(0, cf.CFStringGetLength(string)), 1 << 16, locale)
    values = []
    try:
        if not tokenizer: raise RuntimeError('日本語の表記比較を開始できません。')
        utf16 = text.encode('utf-16-le')
        while cf.CFStringTokenizerAdvanceToNextToken(tokenizer):
            span = cf.CFStringTokenizerGetCurrentTokenRange(tokenizer)
            value = cf.CFStringTokenizerCopyCurrentTokenAttribute(tokenizer, 1 << 16)
            surface = utf16[span.location * 2:(span.location + span.length) * 2].decode('utf-16-le')
            if value:
                try:
                    buffer = c.create_string_buffer(cf.CFStringGetLength(value) * 4 + 16)
                    if not cf.CFStringGetCString(value, buffer, len(buffer), encoding):
                        raise RuntimeError('日本語の読みを取得できません。')
                    reading = buffer.value.decode('utf-8')
                finally: cf.CFRelease(value)
            else:
                reading = surface
            values.append((surface, ''.join(ch for ch in reading.lower() if ch.isalnum())))
        return tuple(values)
    finally:
        for value in (tokenizer, locale, locale_id, string):
            if value: cf.CFRelease(value)


def terminal_boundary(text):
    """A full stop inside a quotation does not end the surrounding assertion."""
    stack = []; last = None
    pairs = {'「': '」', '『': '』', '（': '）', '(': ')', '[': ']', '“': '”'}
    for i, char in enumerate(text):
        if char == '"':
            if stack and stack[-1] == '"': stack.pop()
            else: stack.append('"')
        elif char in pairs: stack.append(pairs[char])
        elif stack and char == stack[-1]: stack.pop()
        elif char in '。！？!?' and not stack: last = i
    return last is not None and not text[last+1:].strip()


def sentence_end(text):
    text = text.rstrip()
    return terminal_boundary(text) or bool(re.search(r'(?:です|ます|ません|ました|ない|ください|了解|はい|いいえ)$', text))


IN_PERSON_WHO = '対面'


def in_person_view(segments, in_person):
    """対面はマイク1本に部屋の全員の声が入る。マイク側を「自分」と決めつけず、話者を分けない印にする。

    対面で始めた録音でも、Macから出る相手の声が20秒以上あればオンラインの会議として扱い、ラベルを変えない。
    """
    if not in_person:
        return segments
    remote = sum(float(s['end']) - float(s['start']) for s in segments
                 if not is_self_segment(s) and not str(s.get('id', '')).startswith('capture-gap-'))
    if remote >= 20:
        return segments
    return [dict(s, who=IN_PERSON_WHO) if s.get('who') == '自分' else s for s in segments]


_ECHO_PUNCT = re.compile(r'[\s、。,.!?！？「」…]')


def without_echo(segments, reference=None):
    """スピーカーで聞くと、相手の声が自分のマイクにも入り、同じ発言が「相手」と「自分」の2回出る。
    相手側と同じ時刻に同じ文がある自分側の発言を除く（実際の会議では自分側395件のうち約150件がこれだった）。

    相づちや挨拶の返事は消さない：短い発言（12字未満）は、時刻が実際に重なり、文がほぼ同じときだけ除く。
    5字以上の短い発言は、時刻が重なる相手の発言の中に同じ並びがあるときも除く。
    長い発言は、自分側の字の7割以上が同じ順で相手側にあり、時刻が2秒以内に重なるときに除く。
    reference には、照らす相手側の発言を足せる（速報を確定済みの発言と照らすとき）。
    """
    theirs = [(float(t['start']), float(t.get('end') or t['start']), _ECHO_PUNCT.sub('', t.get('text') or ''))
              for t in list(segments) + list(reference or [])
              if not is_self_segment(t) and not str(t.get('id', '')).startswith('capture-gap-')]
    if not theirs:
        return segments
    import difflib

    def echo(mine):
        text = _ECHO_PUNCT.sub('', mine.get('text') or '')
        if not text:
            return False
        start, end = float(mine['start']), float(mine.get('end') or mine['start'])
        long = len(text) >= 12
        slack = 2 if long else .5
        for their_start, their_end, their_text in theirs:
            if not (their_start < end + slack and start < their_end + slack) or not their_text:
                continue
            matcher = difflib.SequenceMatcher(None, their_text, text, autojunk=False)
            if long:
                if sum(block.size for block in matcher.get_matching_blocks()) >= .7 * len(text):
                    return True
            elif matcher.ratio() >= .8:
                return True
            elif len(text) >= 5:
                # 長い発言の回り込みが節で切れて、頭だけ短く残ることがある。相手側の同じ長さの並びと比べる
                width = len(text)
                if any(difflib.SequenceMatcher(None, their_text[i:i + width], text, autojunk=False).ratio() >= .8
                       for i in range(max(1, len(their_text) - width + 1))):
                    return True
        return False
    return [segment for segment in segments if not is_self_segment(segment) or not echo(segment)]


def effective_segments(state):
    clauses = without_echo(assemble_clauses(state.get('segments') or [], final=state.get('status') == '録画終了'))
    segments = with_capture_health(clauses, state.get('capture_health') or {})
    # 保存済みの「聞き取り要確認」の印は、いまの判定（shodan_evidence.near_text）で通る発言からは外す
    from shodan_evidence import disputed
    for segment in segments:
        if str(segment.get('verification_note', '')).startswith('聞き取り要確認') and not disputed(segment):
            segment.pop('verification_note')
    return in_person_view(segments, state.get('in_person'))


def is_self_segment(segment):
    """Input provenance is independent of a tentative acoustic speaker label.

    Saved pre-provenance records encode their input in the durable capture ID.
    Imported records without that capture ID retain their explicit source label.
    """
    channel = segment.get('source_channel')
    if channel in ('self', 'remote'):
        return channel == 'self'
    match = re.match(r'^\d+-(self|remote)(?:-|$)', segment.get('id', ''))
    return match.group(1) == 'self' if match else segment.get('who') == '自分'



def with_capture_health(segments, health):
    """A missing recording cannot be corroborated by two recognizers hearing the same gap."""
    from shodan_evidence import mark_verification
    result = [dict(segment) for segment in segments]
    for index, issue in enumerate(health.get('issues') or []):
        start, end = float(issue['start']), float(issue['end'])
        channel = issue.get('channel', 'remote')
        touched = False
        for segment in result:
            if is_self_segment(segment) != (channel == 'self'): continue
            if float(segment.get('start', 0)) < end + .25 and float(segment.get('end', 0)) > start - .25:
                segment['capture_review'] = {'status': 'unresolved', 'kind': issue.get('kind')}
                mark_verification(segment); touched = True
        if not touched:
            result.append(mark_verification({'id': 'capture-gap-' + str(index), 'start': start, 'end': end,
                'who': '自分' if channel == 'self' else '話者未確定', 'text': '［録音が途切れた区間］',
                'raw_text': '', 'clause_status': 'complete', 'speaker_status': 'unknown',
                'capture_review': {'status': 'unresolved', 'kind': issue.get('kind')}}))
    return sorted(result, key=lambda segment: (segment['start'], segment['id']))

def assemble_clauses(segments, final=False):
    """Keep a number, its condition and its negation in the same evidence unit.

    Acoustic fragments remain available as raw_segments. A trailing unfinished
    clause is visible but cannot enter the AI until the next audio completes it.
    """
    from shodan_evidence import mark_verification
    if not segments: return []
    if all(s.get('speaker_status') == 'draft' for s in segments):
        return sorted(segments, key=lambda s: (s['start'], s['id']))
    result = []
    # Microphone/system audio are independent streams; never join their clauses.
    for own in (True, False):
        stream = sorted((s for s in segments if is_self_segment(s) == own), key=lambda s: (s['start'], s['id']))
        group = []
        def emit(pending=False):
            if not group: return
            first = group[0]; item = dict(first)
            for key in ('verification_note', 'numeric_review', 'text_review', 'clause_status', 'boundary_pending'):
                item.pop(key, None)
            for key in ('capture_review', 'overlap_review'):
                flagged = next((part[key] for part in group if part.get(key, {}).get('status') == 'unresolved'), None)
                if flagged: item[key] = flagged
            texts = []
            for i, part in enumerate(group):
                value = part.get('text', '')
                if i < len(group)-1 and part.get('boundary_pending') and not re.search(r'(です|ます|ません|ました|ない|ください)[。！？!?]*$', value):
                    value = value.rstrip('。！？!?')
                texts.append(value)
            item['text'] = ''.join(texts).strip()
            item['raw_text'] = ''.join(s.get('raw_text', s.get('text', '')) for s in group).strip()
            item['start'] = min(s['start'] for s in group)
            item['end'] = max(s['end'] for s in group)
            item['component_ids'] = [identifier for s in group for identifier in s.get('component_ids', [s['id']])]
            # First acoustic id is stable when a trailing clause gains more words.
            item['id'] = first['id']
            speakers = {s.get('who') for s in group if s.get('text') and s.get('who') != '話者未確定'}
            if own:
                item.update(who='自分', speaker_status='source')
            elif len(speakers) == 1:
                item['who'] = next(iter(speakers))
                item['speaker_status'] = 'source' if own else ('confirmed' if any(s.get('speaker_status') == 'confirmed' for s in group) else 'tentative')
            elif len(speakers) > 1:
                item.update(who='話者未確定', speaker_status='tentative')
            # Ending a recording cannot finish an explicitly truncated clause.
            unresolved_tail = bool(group[-1].get('boundary_pending')) and not terminal_boundary(item['text'])
            item['clause_status'] = 'pending' if pending else ('complete' if sentence_end(item['text']) and not unresolved_tail else 'incomplete')
            if pending: item['boundary_pending'] = True
            mark_verification(item)
            result.append(item)
            group.clear()
        for i, segment in enumerate(stream):
            group.append(segment)
            at_end = i == len(stream)-1
            # "あります" can still be followed by "が、使いません". Only primary
            # sentence punctuation closes a unit while more words are available.
            if terminal_boundary(''.join(s.get('text', '') for s in group)) and not segment.get('boundary_pending'):
                emit()
            elif at_end:
                emit(pending=not final and (bool(segment.get('boundary_pending')) or not sentence_end(''.join(s.get('text', '') for s in group))))
        emit(pending=not final)
    return sorted(result, key=lambda s: (s['start'], s['id']))
