"""スピーカーの回り込みで、相手の発言が自分側にも出るのを除く。
python3 test_echo.py"""
from shodan_text import without_echo

def seg(i, channel, start, end, text):
    return {'id': f'{i:012d}-{channel}-x', 'source_channel': channel, 'who': '自分' if channel == 'self' else '相手1', 'start': start, 'end': end, 'text': text}

LONG = 'また、メールでのやり取りの中で、先ほど事例を含めた資料をまたいただければなと思いますので、そういったところもまた引き続きお願いできればと思います。'
segs = [
    seg(1, 'remote', 3435, 3445, LONG),
    seg(2, 'self', 3435.5, 3445.5, 'また、あの、メールでのやり取りの中で、先ほど事例を含めた資料をまたいただければなと思いますので、伝えたところもまた引き続きお願いできればと思います。'),  # 回り込み
    seg(3, 'remote', 3446, 3447.5, 'よろしくお願いします。'),
    seg(4, 'self', 3448, 3449, 'よろしくお願いします。'),  # 相手が言い終えた後の返事は残す
    seg(5, 'self', 3449, 3453, 'はい、わかりました。こちらは今日中にちょっとまとめて送らせていただきます。'),  # 自分の発言
    seg(6, 'remote', 3460, 3462, 'ありがとうございました。'),
    seg(7, 'self', 3460.2, 3462.1, 'ありがとうございました。'),  # 同じ時刻の同じ短い文は回り込み
]
segs += [seg(8, 'remote', 3470, 3476, 'では、すみません、本日はこれで終えたいと思います。'),
         seg(9, 'self', 3470.1, 3471.2, 'では、すいません。'),  # 回り込みの頭だけが残った物
         seg(10, 'self', 3473, 3473.5, 'はい。')]  # 相手が話している間の相づちは残す
kept = [s['id'][:12].lstrip('0') for s in without_echo(segs)]
assert kept == ['1', '3', '4', '5', '6', '8', '10'], kept
# 対面（相手側の音が無い）は何も消さない
mic = [seg(1, 'self', 0, 5, LONG), seg(2, 'self', 6, 11, LONG)]
assert without_echo(mic) == mic
# 速報は確定済みの相手の発言と照らす
assert without_echo([seg(9, 'self', 3436, 3445, LONG)], reference=segs[:1]) == []
print('ok')
