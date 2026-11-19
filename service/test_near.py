"""AI に渡す前の判定：つなぎの語だけの違いは通し、数字・否定・漢字の語の違いは伏せる。
python3 test_near.py"""
from shodan_evidence import near_text, disputed, safe_segment

PASS = [('あ、お疲れ様です。', 'お疲れさまです'),
        ('はい。で、創業に関しては2023年なので、今創業3期目になっております。', '創業に関しては2023年なので今創業3期目になっております'),
        ('えっと、そうですね。', 'そうですね')]
BLOCK = [('日本を送ります', '見本を送ります'),            # 漢字の語が違う
         ('できます', 'できません'),                    # 否定の有無
         ('納期は19日です', '納期は12日です'),            # 数字
         ('こちらは今日中にちょっとまとめて送らせていただきます。', 'こちらは教授にまとめて送らせていただきます'),
         ('西田と申します', 'だと申します')]
for a, b in PASS: assert near_text(a, b), (a, b)
for a, b in BLOCK: assert not near_text(a, b), (a, b)
seg = {'id': 'a', 'start': 0, 'end': 1, 'who': '相手1', 'text': 'あ、お疲れ様です。', 'raw_text': 'お疲れさまです', 'verification_note': '聞き取り要確認。別候補: お疲れさまです'}
assert not disputed(seg) and safe_segment(seg)['text'] == 'あ、お疲れ様です。' and safe_segment(seg)['evidence_status'] == 'near_match'
seg = dict(seg, text='日本を送ります', raw_text='見本を送ります')
# 食い違うだけの発言は伏せ字にせず、不確かの印を付けて渡す。食い違った数字だけ伏せる
assert disputed(seg) and safe_segment(seg)['text'] == '日本を送ります' and safe_segment(seg)['evidence_status'] == 'uncertain'
seg = dict(seg, text='納期は19日で、見本を送ります', raw_text='納期は12日で、見本を送ります')
out = safe_segment(seg); assert '19日' not in out['text'] and '12日' not in out['text'] and '見本を送ります' in out['text'], out
# 録音の途切れ・声の重なりは今までどおり伏せる
seg = dict(seg, capture_review={'status': 'unresolved'})
assert safe_segment(seg)['evidence_status'] == 'unconfirmed' and '見本' not in safe_segment(seg)['text']
print('ok')
