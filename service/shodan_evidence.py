"""Keep disputed recognition candidates out of decisions, summaries and AI copies."""
from __future__ import annotations
from decimal import Decimal, InvalidOperation
from functools import lru_cache
import json
from pathlib import Path
import re
import unicodedata

import mica_config

POLICY = 2
_DIGITS = dict(zip('零〇一二三四五六七八九', '00123456789'))
_SMALL = {'十': 10, '百': 100, '千': 1000}
_LARGE = {'万': 10000, '億': 100000000, '兆': 1000000000000}
_NUMBER = re.compile(r'[0-9零〇一二三四五六七八九十百千万億兆]+(?:\.[0-9]+)?(?:[十百千万億兆][0-9零〇一二三四五六七八九十百千万億兆]*(?:\.[0-9]+)?)*')
_QUANTITY = re.compile(r'(午前|午後)?\s*([+\-]?\d+(?:\.\d+)?)\s*(セット|パーセント|万円|億円|年|月|日|時|分|秒|円|%|％|箱|個|本|枚|台|件|名|人|社|回|つ|度|℃|歳|箇所|週間|週|か月|ヶ月|キロ|kg|g|km|m|L|ml)')


def normalize_numbers(text):
    text = unicodedata.normalize('NFKC', str(text))
    # Group separators must be removed BEFORE interpreting Japanese large units.
    text = re.sub(r'(?<=\d),(?=\d{3}(?:,|\D|$))', '', text)
    def convert(match):
        value = match.group(); total = Decimal(0); section = Decimal(0); current = ''
        try:
            for char in value:
                if char in _DIGITS or char.isdigit() or char == '.':
                    current += _DIGITS.get(char, char)
                elif char in _SMALL:
                    section += Decimal(current or '1') * _SMALL[char]; current = ''
                else:
                    total += (section + Decimal(current or '0') or Decimal(1)) * _LARGE[char]
                    section = Decimal(0); current = ''
            result = format(total + section + Decimal(current or '0'), 'f')
            return result.rstrip('0').rstrip('.') if '.' in result else result
        except (InvalidOperation, KeyError):
            return value
    return _NUMBER.sub(convert, text)


def canonical(text):
    value = normalize_numbers(text).lower()
    return ''.join(c for i, c in enumerate(value) if not c.isspace() and (
        not unicodedata.category(c).startswith('P') or c in '-+/%' or (
            c == '.' and i > 0 and i + 1 < len(value) and value[i-1].isdigit() and value[i+1].isdigit())))


def quantity_text(text):
    value = normalize_numbers(text)
    value = re.sub(r'(?<!\d)(\d{4})[-/](\d{1,2})[-/](\d{1,2})(?!\d)',
                   lambda m: f'{int(m[1])}年{int(m[2])}月{int(m[3])}日', value)
    value = re.sub(r'(?<![\d/])(\d{1,2})/(\d{1,2})(?![\d/])',
                   lambda m: f'{int(m[1])}月{int(m[2])}日', value)
    value = re.sub(r'[¥￥]\s*(\d+(?:\.\d+)?)', r'\1円', value)
    # A date written without its unit must not bypass the same check.
    value = re.sub(r'((?:発送日|納品日|締切|期限|期日|日付|納期)(?:は|が|[:：])?\s*)(\d{1,2})(?![\d年月日])',
                   r'\1\2日', value)
    return value


def _numeric_spans(text):
    value = quantity_text(text)
    spans = [(m.start(), m.end(), ''.join(m.groups(default='')).replace('％', '%')) for m in _QUANTITY.finditer(value)]
    for m in re.finditer(r'[+\-]?\d+(?:\.\d+)?', value):
        if not any(a <= m.start() < b for a, b, _ in spans):
            spans.append((m.start(), m.end(), 'number:' + m.group()))
    return value, sorted(spans)


def numeric_keys(text):
    return {key for _, _, key in _numeric_spans(text)[1]}


def equivalent_text(text, raw):
    if canonical(text) == canonical(raw): return True
    # Reading equivalence cannot override a changed unit, date, sign or value.
    if numeric_keys(text) != numeric_keys(raw): return False
    number = r'[+\-]?\d+(?:\.\d+)?'
    if re.findall(number, quantity_text(text)) != re.findall(number, quantity_text(raw)): return False
    from shodan_text import reading_key
    return reading_key(canonical(text)) == reading_key(canonical(raw))


_LIGHT = re.compile(r'[ぁ-ゖー]*')
_NEGATION = re.compile(r'ない|ません|なかっ|無[いくし]|ずに')


def near_text(text, raw):
    return _near_text(text or '', raw or '')


@lru_cache(maxsize=50000)
def _near_text(text, raw):
    """2つの認識結果は、つなぎの語（えっと・はい・まあ）や語尾で必ず少し違う。違う所がひらがなの短い語だけなら、
    同じ発言として AI に渡す（完全一致だけを通すと、実際の会議では AI 用の文字起こし531行のうち
    371行が［聞き取り未確認］になり、助言と自動準備が中身を読めなかった）。
    通さない物：数字・日付の違い、否定の有無の違い、漢字・カタカナ・英字の語に掛かる違い（日本／見本、今日中／教授）、
    ひらがなでも13字以上の違い。発言の引用（_can_quote）は、これまでどおり完全一致だけを通す。"""
    if not text or not raw: return False
    if numeric_keys(text) != numeric_keys(raw): return False
    number = r'[+\-]?\d+(?:\.\d+)?'
    if re.findall(number, quantity_text(text)) != re.findall(number, quantity_text(raw)): return False
    if len(_NEGATION.findall(text)) != len(_NEGATION.findall(raw)): return False
    import difflib
    from shodan_text import reading_tokens
    def spread(value):
        tokens = reading_tokens(canonical(value))
        return ''.join(r for _, r in tokens), [i for i, (_, r) in enumerate(tokens) for _ in r], tokens
    a, owner_a, tokens_a = spread(text); b, owner_b, tokens_b = spread(raw)
    if not a or not b: return False
    for tag, a1, a2, b1, b2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag == 'equal': continue
        for owner, tokens, lo, hi in ((owner_a, tokens_a, a1, a2), (owner_b, tokens_b, b1, b2)):
            surface = ''.join(tokens[i][0] for i in sorted(set(owner[lo:hi])))
            if len(surface) > 12 or not _LIGHT.fullmatch(surface): return False
    return True


def mark_verification(item):
    text, raw = item.get('text', ''), item.get('raw_text', '')
    item.pop('numeric_review', None); item.pop('text_review', None)
    if item.get('capture_review', {}).get('status') == 'unresolved':
        item['verification_note'] = '録音が途切れた区間を含みます。認識候補が一致していても、AIは決定・数字・否定の根拠に使いません。'
    elif item.get('overlap_review', {}).get('status') == 'unresolved':
        item['verification_note'] = '声が重なった可能性があります。話者・数字・条件は確認するまでAIの確定情報に使いません。'
    elif item.get('clause_status') in ('pending', 'incomplete'):
        item['verification_note'] = '文の続きが未確認です。数字・条件・否定を一文として確認してからAIへ渡します。'
    elif equivalent_text(text, raw):
        item.pop('verification_note', None)
        item['text_review'] = {'status': 'equivalent', 'method': 'exact' if canonical(text) == canonical(raw) else 'reading', 'policy': POLICY}
    elif near_text(text, raw):
        item.pop('verification_note', None)
        item['text_review'] = {'status': 'near', 'method': 'reading_similarity', 'policy': POLICY}
    elif numeric_keys(text) != numeric_keys(raw):
        item['numeric_review'] = {'status': 'unresolved'}
        item['verification_note'] = '日時・数字が認識候補と一致しません。AIにはこの数字を伏せて渡します。別候補: ' + raw
    else:
        item['verification_note'] = '聞き取り要確認。AIには不確かな発言として渡します。別候補: ' + raw
    return item


def disputed(segment):
    if any(segment.get(key, {}).get('status') == 'unresolved' for key in ('capture_review', 'overlap_review')): return True
    if segment.get('clause_status') in ('pending', 'incomplete'): return True
    if segment.get('numeric_review', {}).get('status') == 'unresolved':
        return True
    if not segment.get('verification_note'):
        return False
    raw = segment.get('raw_text')
    return raw is None or not (equivalent_text(segment.get('text', ''), raw) or near_text(segment.get('text', ''), raw))


def text_only_dispute(segment):
    """録音の途切れ・声の重なり・文の途中ではなく、2つの認識結果の文が食い違うだけの発言。"""
    if any(segment.get(key, {}).get('status') == 'unresolved' for key in ('capture_review', 'overlap_review')): return False
    if segment.get('clause_status') in ('pending', 'incomplete'): return False
    return bool(segment.get('text', '').strip() and segment.get('raw_text') and disputed(segment))


def safe_segment(segment):
    # Explicit allow-list: raw_text and candidate-bearing annotations never enter an AI request.
    result = {k: segment[k] for k in ('id', 'start', 'end', 'who', 'speaker_status') if k in segment}
    if text_only_dispute(segment):
        # 2つの認識が食い違うだけの発言は、伏せ字にしない（伏せると AI は会議の半分を読めない）。本文は第1候補のまま渡し、食い違った数字だけ［数値未確認］に替え、不確かと印を付ける。
        text, raw = segment.get('text', ''), segment.get('raw_text', '')
        result.update(text=scrub_text(text, numeric_keys(text).symmetric_difference(numeric_keys(raw))), evidence_status='uncertain',
                      verification_note='2つの音声認識が食い違った発言です。固有名詞・用語は誤りの可能性があります。')
    elif disputed(segment):
        values = numeric_keys(segment.get('text', '')) | numeric_keys(segment.get('raw_text', ''))
        kind = '録音が途切れた区間' if segment.get('capture_review', {}).get('status') == 'unresolved' else '声が重なった発言' if segment.get('overlap_review', {}).get('status') == 'unresolved' else '文の続き・条件・否定' if segment.get('clause_status') in ('pending', 'incomplete') else ('日時・金額・数量を含む発言' if values else '用語・発言内容')
        result.update(text='［聞き取り未確認：' + kind + '］', evidence_status='unconfirmed',
                      verification_note='認識候補が一致しません。発言者に確認するまで、決定・担当・期限の根拠に使わないでください。')
    else:
        result.update(text=segment.get('text', ''), evidence_status=('corroborated' if equivalent_text(segment.get('text', ''), segment['raw_text']) else 'near_match') if 'raw_text' in segment else 'single_recognition')
    return result


def blocked_values(segments):
    disputed_values = set()
    for segment in segments:
        values = numeric_keys(segment.get('text', ''))
        if disputed(segment):
            raw = segment.get('raw_text')
            # Agreement on the number itself is retained despite spelling differences elsewhere.
            # A different statement using the same value cannot confirm this disputed statement.
            disputed_values |= (values | numeric_keys(raw or '')) if segment.get('clause_status') in ('pending', 'incomplete') or any(segment.get(key, {}).get('status') == 'unresolved' for key in ('capture_review', 'overlap_review')) else (values.symmetric_difference(numeric_keys(raw)) if raw is not None else values)
    return disputed_values


def scrub_text(text, blocked):
    if not blocked or not (numeric_keys(text) & blocked):
        return text
    value, spans = _numeric_spans(text)
    for start, end, key in reversed(spans):
        if key in blocked: value = value[:start] + '［数値未確認］' + value[end:]
    return value


def scrub_tree(value, blocked):
    if isinstance(value, str): return scrub_text(value, blocked)
    if isinstance(value, list): return [scrub_tree(x, blocked) for x in value]
    if isinstance(value, dict): return {k: scrub_tree(v, blocked) for k, v in value.items()}
    return value


def safe_brief(brief, segments):
    clean = json.loads(json.dumps(brief))
    prep = clean.get('auto_preparation', {})
    # Old speech-derived summaries may already contain an unverified interpretation.
    if prep.get('evidence_policy') != POLICY and any(s.get('kind') == 'conversation' for s in prep.get('sources', [])):
        if 'purpose' not in clean.get('manual_fields', []): clean['purpose'] = ''
        if clean.get('context_mode', 'auto') == 'auto': clean['context'] = {}
    return scrub_tree(clean, blocked_values(segments))


def _can_quote(segment):
    from shodan_text import terminal_boundary
    value = segment.get('text', '').strip()
    return bool(value and terminal_boundary(value) and 'raw_text' in segment
                and not disputed(segment) and equivalent_text(value, segment.get('raw_text', ''))
                and segment.get('speaker_status') != 'draft'
                and segment.get('alignment_review', {}).get('status') != 'unresolved')


def corroborated_quotes(segments, limit=16000):
    """Keep whole source clauses available after they leave the recent window."""
    seen = set(); result = []; size = 0
    for segment in reversed(segments):
        value = segment.get('text', '').strip()
        if value in seen or not _can_quote(segment):
            continue
        if size + len(value) > limit: continue
        seen.add(value); size += len(value); result.append(safe_segment(segment))
    return list(reversed(result))


def _output_parts(text, segments):
    """Protect exact whole corroborated utterances only inside explicit quotes."""
    quotes = {}
    for segment in segments:
        if not _can_quote(segment): continue
        quote = '「' + segment['text'].strip() + '」'
        second = int(segment.get('start', 0))
        quotes.setdefault(quote, set()).add(f'[{second//60:02d}:{second%60:02d}]')
        quotes[quote].add(f'[{second//60}:{second%60:02d}]')
    if not quotes: return [(False, text)]
    expression = '|'.join(re.escape(value) for value in sorted(quotes, key=len, reverse=True))
    parts = []; cursor = 0
    for match in re.finditer(expression, text):
        line_start = text.rfind('\n', 0, match.start()) + 1
        line_end = text.find('\n', match.end())
        if line_end < 0: line_end = len(text)
        # A quote embedded in a new assertion cannot lend its date to that assertion.
        suffix = text[match.end():line_end].strip()
        if (not re.fullmatch(r'\s*(?:[-*]\s*)?', text[line_start:match.start()])
                or (not re.fullmatch(r'\s*。?\s*', suffix) and suffix not in quotes[match.group()])):
            continue
        # A timestamp is exempt only when it points to this exact source quote.
        parts.extend([(False, text[cursor:match.start()]), (True, text[match.start():line_end])])
        cursor = line_end
    parts.append((False, text[cursor:]))
    return parts


def _blocked_output(text, blocked, segments):
    # Removing a quote is local to this occurrence; no numeric value is whitelisted.
    return bool(numeric_keys(' '.join(value for quoted, value in _output_parts(text, segments) if not quoted)) & blocked)


def _scrub_output(text, blocked, segments):
    return ''.join(value if quoted else scrub_text(value, blocked)
                   for quoted, value in _output_parts(text, segments))


def guard_guide(result, blocked, segments=()):
    """A deterministic last check; only verbatim corroborated quotes are exempt."""
    result = dict(result)
    for key in ('decisions', 'actions'):
        if key in result: result[key] = [x for x in result[key] if not _blocked_output(x, blocked, segments)]
    if 'open_questions' in result: result['open_questions'] = [_scrub_output(x, blocked, segments) for x in result['open_questions']]
    if 'summary' in result and _blocked_output(result['summary'], blocked, segments):
        result['summary'] = _scrub_output(result['summary'], blocked, segments)
    # 回答を丸ごと定型文に替えない：長い会議では「1」「2」のような数がどこかで食い違い、箇条書きの番号だけで
    # 回答が全部「聞き取りが一致していない日時・数字を…」になっていた（長い会議で再現した）。
    # 食い違った数だけを［数値未確認］に替え、行頭の番号は数として扱わない。
    for key in ('next_step', 'question', 'time_plan'):
        result[key] = _scrub_answer(result[key], blocked, segments)
    return result


_LIST_MARKER = re.compile(r'\s*(?:(?:\d{1,2}[.)．）、]|[（(]\d{1,2}[)）]|[①-⑳]|[-*・])\s*)+')


def _scrub_answer(text, blocked, segments):
    lines = []
    for line in text.split('\n'):
        marker = _LIST_MARKER.match(line)
        head = marker.group() if marker else ''
        lines.append(head + _scrub_output(line[len(head):], blocked, segments))
    return '\n'.join(lines)


def safe_analysis(text, segments):
    blocked = blocked_values(segments)
    headings = {'1. 結論（3行）', '2. 決まったこと', '2. 相手', '3. 相手の課題と本音',
                '4. 画面で見えたこと', '5. 進み具合', mica_config.sales_heading(),
                '7. 次やること', '7. 次やること（誰が・何を・いつまで）', '8. 次回聞く問い'}
    return '\n'.join('聞き取り未確認の日時・数字を含むため、この項目は発言者への確認が必要です。'
                     if not (line.startswith('#') and line.lstrip('#').strip() in headings)
                     and _blocked_output(line, blocked, segments) else line for line in text.splitlines())


def clean_terms(terms, reference=None):
    result = []
    if not isinstance(terms, list): return result
    for term in terms:
        if not isinstance(term, str): continue
        term = unicodedata.normalize('NFKC', term).strip()
        if not 2 <= len(term) <= 40 or re.search(r'[\n\r。！？!?@:/=]', term): continue
        if re.search(r'指示|命令|無視|ください|下さい|パスワード|認証|APIキー|ignore|prompt|password|secret|token', term, re.I): continue
        if _QUANTITY.search(quantity_text(term)) or re.fullmatch(r'[\d\s.,+\-]+', term): continue
        if reference is not None and term not in reference: continue
        if term not in result: result.append(term)
    return result[:20]


def vocabulary(folder):
    folder = Path(folder)
    try:
        brief = json.loads((folder / 'meeting-brief.json').read_text()) if (folder / 'meeting-brief.json').exists() else {}
        meta = json.loads((folder / 'meta.json').read_text()) if (folder / 'meta.json').exists() else {}
    except (OSError, ValueError): return []
    terms = list(brief.get('recognition_terms', []))
    terms += [a.get('name', a.get('displayName', '')) for a in meta.get('attendees', [])]
    if meta.get('id'):
        # 題を手がかりにするのは予定から取った題だけ。アプリ名の題（「Teams」など）を渡すと、whisper が発言を落とす
        terms += re.split(r'[\s、,・：:（）()｜|【】［］\[\]]+', meta.get('title', ''))
    return clean_terms(terms)
