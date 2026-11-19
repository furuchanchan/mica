"""Render meeting facts from source utterances, never from model paraphrases.

The model selects source IDs and writes explicitly unagreed proposals. It cannot
supply the text, speaker, time, or agreement status of a meeting fact.
"""
from __future__ import annotations

import html
import math

import mica_config
from shodan_evidence import safe_segment

POLICY = 'verbatim-meeting-facts-v1'
WORK_HEADINGS = {1: '1. 結論（3行）', 2: '2. 決まったこと',
                 7: '7. 次やること（誰が・何を・いつまで）'}
SALES_HEADINGS = {1: '1. 結論（3行）', 2: '2. 相手', 3: '3. 相手の課題と本音',
                  4: '4. 画面で見えたこと', 5: '5. 進み具合',
                  6: mica_config.sales_heading(), 7: '7. 次やること', 8: '8. 次回聞く問い'}


def evidence_ledger(segments):
    """Keep complete safe utterances; exclude uncertain clauses and raw candidates."""
    # Repeated audio fixtures and repeated confirmations do not need to consume
    # model context repeatedly. Keep the latest whole utterance per audio label,
    # with both its stable ledger ID and original segment ID for auditing.
    latest = {}
    for index, segment in enumerate(segments):
        safe = safe_segment(segment)
        if safe['evidence_status'] == 'unconfirmed' or not safe['text'].strip():
            continue
        if segment.get('speaker_status') == 'draft' or segment.get('alignment_review', {}).get('status') == 'unresolved':
            continue
        start = safe.get('start')
        if not isinstance(start, (int, float)) or not math.isfinite(start) or start < 0:
            continue
        who = safe.get('who', '話者未確定')
        latest[(who, safe['text'])] = (index, {'id': f'u{index + 1:05d}', 'segment_id': segment.get('id'),
                     'start': start, 'who': who, 'text': safe['text'],
                     'recognition': safe['evidence_status']})
    return {'policy': POLICY, 'utterances': [row for _, row in sorted(latest.values())]}


def _plain(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError('分析の文章は空でない文字列で指定してください')
    # One inert line: model text must not inject headings or impersonate facts.
    value = ' '.join(value.splitlines()).strip()
    return html.escape(value, quote=False).replace('`', '\\`').replace('*', '\\*').replace('_', '\\_').replace('[', '\\[').replace(']', '\\]')


def render_analysis(draft, ledger, frames=(), test=False):
    """Validate the protocol before publishing. Unknown IDs/fields fail closed."""
    if not isinstance(draft, dict) or set(draft) != {'kind', 'sections'}:
        raise ValueError('分析下書きは kind と sections だけを持つJSONにしてください')
    if ledger.get('policy') != POLICY:
        raise ValueError('分析の発言台帳の版が一致しません')
    kind = draft['kind']
    if kind not in ('仕事', '商談', '私的') or (test and kind != '仕事'):
        raise ValueError('会議の種別が不正です')
    headings = {1: '結論（3行）'} if kind == '私的' else WORK_HEADINGS if kind == '仕事' else SALES_HEADINGS
    sections = draft['sections']
    if (not isinstance(sections, list)
            or any(not isinstance(s, dict) or type(s.get('number')) is not int for s in sections)
            or [s['number'] for s in sections] != list(headings)):
        raise ValueError('分析の章番号・順序が一致しません')
    sources = {row['id']: row for row in ledger['utterances']}
    frames = set(frames)
    output = ['種別: ' + kind, '', '発言記録は文字起こしの全文引用です。自動認識の誤りは残る可能性があります。',
              'AI提案は未合意です。章への配置だけで決定・担当・期限・同意を確定しません。', '']
    for section in sections:
        if set(section) != {'number', 'source_ids', 'ai_proposals', 'visual_observations'}:
            raise ValueError('分析の章に許可されていない項目があります')
        number = section['number']
        refs, proposals, observations = (section[key] for key in ('source_ids', 'ai_proposals', 'visual_observations'))
        if not all(isinstance(value, list) for value in (refs, proposals, observations)):
            raise ValueError('分析の根拠・提案・画像観察は配列で指定してください')
        output.extend(['## ' + headings[number], ''])
        if number == 7 or (kind == '仕事' and number == 2):
            output.extend(['発言記録（合意・担当・期限は引用内の条件を確認。確定タスクの自動判定はしません）', ''])
        if not refs:
            output.extend(['- この章に対応する発言の引用はありません。', ''])
        if any(not isinstance(ref, str) or ref not in sources for ref in refs):
            raise ValueError('分析の根拠に存在しない発言IDがあります')
        for ref in dict.fromkeys(refs):
            row = sources[ref]
            second = int(row['start'])
            output.extend([f"- 発言原文 [{second // 60:02d}:{second % 60:02d}] {_plain(row['who'])} / {ref}",
                           '> ' + html.escape(row['text'], quote=False).replace('\n', '\n> '), ''])
        for proposal in proposals:
            output.extend(['- AI提案（未合意・会話の確定事実ではありません）: ' + _plain(proposal), ''])
        if observations and (kind != '商談' or number != 4):
            raise ValueError('画像の観察は商談の第4章だけに置いてください')
        for observation in observations:
            if (not isinstance(observation, dict) or set(observation) != {'frame', 'text'}
                    or not isinstance(observation['frame'], str) or observation['frame'] not in frames):
                raise ValueError('画像観察の出典が不正です')
            output.extend(['- 画像に関するAI観察（音声事実・合意ではありません） / ' + _plain(observation['frame']) + ': ' + _plain(observation['text']), ''])
    return '\n'.join(output)
