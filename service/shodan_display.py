"""Read-only paragraphs for the transcript views."""
from __future__ import annotations


def transcript_turns(segments, drafts):
    """Group nearby text from the same speaker without changing evidence units."""
    entries = [(segment, False) for segment in segments] + [(segment, True) for segment in drafts]
    turns = []
    previous_key = None
    previous_end = 0
    for segment, draft in sorted(entries, key=lambda entry: entry[0]['start']):
        who = segment.get('who')
        status = segment.get('speaker_status')
        if not who or who in ('話者未確定', '話者未確認', '確認中'):
            who = '相手'
        label = who + '（仮）' if status == 'tentative' and who.startswith('相手') and who != '相手' else who
        key = (draft, who, status, segment.get('source_channel'))
        start = segment['start']
        end = segment.get('end') or start
        body = segment.get('text') or '［聞き取り未確認］'
        note = segment.get('verification_note')

        turn = turns[-1] if turns else None
        if (turn is not None and key == previous_key
                and 0 <= start - previous_end <= 5 and start > turn['start']
                and end - turn['start'] <= 30 and len(turn['text']) + len(body) + 1 <= 160):
            separator = '' if turn['text'].endswith(('。', '！', '？', '\n')) else ' '
            turn['text'] += separator + body
        else:
            turn = {'id': ('draft:' if draft else 'segment:') + segment['id'],
                    'start': start, 'who': label, 'draft': draft, 'text': body, 'notes': []}
            turns.append(turn)
        if note:
            turn['notes'].append({'id': segment['id'], 'start': start, 'text': note})
        previous_key, previous_end = key, end
    return turns
