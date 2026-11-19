#!/usr/bin/env python3
"""通しの確かめ：合成した日本語の会話を、録画アプリが出すのと同じ形（5秒ごとの音声）で置き、
鍵なしで文字起こしが出るか、会議後に記録が保存されるかを見る。録画アプリと macOS の許可は通らない。

  python3 tests/selftest.py              # 入れた先（app_support/service）のコードで確かめる。Mac の外へは何も送らない
  python3 tests/selftest.py --connected  # つないだ AI で会議後の分析まで確かめる（合成の会話が、つないだ先へ送られる）
作る会議は、設定の data_root の下の「<日時>_selftest」。終わっても消さない（中を見られるように）。
--connected を付けない時は MICA_NO_CLOUD=1 で動かすので、この Mac の中の LLM（Ollama など）だけが分析に使われる。
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import wave

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / 'service'))
import mica_config

LINES = (  # (どちらの声か, 話す文)。相手の声が20秒より短いと「会話でない」とみなされ、分析に進まない
    ('self', '本日はお時間をいただきありがとうございます。まず、今の課題を教えてください。'),
    ('remote', '見積もりの作成に毎回三日かかっていて、担当者が二人しかいません。先月は依頼が四十件あり、十件は期限に間に合いませんでした。'),
    ('self', '間に合わなかった十件は、どの工程で止まっていましたか。'),
    ('remote', '過去の似た案件を探す所です。担当者の記憶に頼っていて、探すだけで一日かかります。'),
    ('self', '承知しました。来週の火曜日までに、改善の案を三つお送りします。'),
    ('remote', 'ありがとうございます。予算は年間で五百万円を考えています。決めるのは部長の田中で、来月の会議にかけます。'),
)
VOICES = {'self': 'Kyoko', 'remote': 'Otoya'}
RATE = 48000
CHUNK = 5 * RATE


def speak(text, voice, path):
    with tempfile.TemporaryDirectory() as work:
        raw = Path(work) / 'voice.aiff'
        subprocess.run(['/usr/bin/say', '-v', voice, '-o', str(raw), text], check=True)
        subprocess.run([mica_config.ffmpeg(), '-v', 'error', '-y', '-i', str(raw), '-ac', '1', '-ar', str(RATE),
                        '-c:a', 'pcm_s16le', str(path)], check=True)
    with wave.open(str(path), 'rb') as source:
        return source.readframes(source.getnframes())


def main():
    if '--connected' not in sys.argv[1:]:
        os.environ['MICA_NO_CLOUD'] = '1'
    service = mica_config.app_support() / 'service'
    if not (service / 'shodan_live.py').is_file():
        raise SystemExit(f'{service} にサービスがありません。先に ./install.sh を動かしてください')
    installed = {voice for voice in VOICES.values()
                 if voice in subprocess.run(['/usr/bin/say', '-v', '?'], capture_output=True, text=True, check=True).stdout}
    voices = {channel: (voice if voice in installed else 'Kyoko') for channel, voice in VOICES.items()}
    folder = mica_config.data_root() / (time.strftime('%Y%m%d-%H%M%S') + '_selftest')
    audio = folder / 'live-audio'
    audio.mkdir(parents=True)
    (folder / 'meta.json').write_text(json.dumps({'id': None, 'title': 'selftest', 'attendees': [], 'description': '',
                                                  'recorded_at': time.strftime('%Y-%m-%dT%H:%M:%S+09:00'), 'app': 'selftest', 'mode': 'app', 'test': True}, ensure_ascii=False))
    # 2本の声を、話す順に時間をずらして並べる（相手＝remote、自分＝self）
    tracks = {'self': bytearray(), 'remote': bytearray()}
    with tempfile.TemporaryDirectory() as work:
        for index, (channel, text) in enumerate(LINES):
            pcm = speak(text, voices[channel], Path(work) / f'{index}.wav')
            start = max(len(track) for track in tracks.values())
            for name, track in tracks.items():
                track.extend(b'\0' * (start - len(track)))
                track.extend(pcm if name == channel else b'\0' * len(pcm))
            for track in tracks.values():
                track.extend(b'\0' * RATE)  # 発言の間の0.5秒（16bit なので RATE バイト）
    for channel, track in tracks.items():
        for offset in range(0, len(track) // 2, CHUNK):
            frames = bytes(track[offset * 2:(offset + CHUNK) * 2])
            stem = audio / f'{offset * 1000 // RATE:012d}-{channel}'
            with wave.open(str(stem) + '.wav', 'wb') as out:
                out.setnchannels(1); out.setsampwidth(2); out.setframerate(RATE); out.writeframes(frames)
            Path(str(stem) + '.json').write_text(json.dumps({'schema_version': 1, 'start_offset_seconds': offset / RATE,
                                                             'sample_rate': RATE, 'frame_count': len(frames) // 2}))
    # 録画アプリと同じく、動画には音声を2本入れる（1本目＝相手、2本目＝自分）
    with tempfile.TemporaryDirectory() as work:
        for channel, track in tracks.items():
            with wave.open(f'{work}/{channel}.wav', 'wb') as out:
                out.setnchannels(1); out.setsampwidth(2); out.setframerate(RATE); out.writeframes(bytes(track))
        subprocess.run([mica_config.ffmpeg(), '-v', 'error', '-y', '-f', 'lavfi', '-i', 'color=black:s=320x240:r=1',
                        '-i', f'{work}/remote.wav', '-i', f'{work}/self.wav', '-map', '0:v', '-map', '1:a', '-map', '2:a',
                        '-shortest', '-c:v', 'h264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', str(folder / 'video.mov')], check=True)
    (audio / 'capture.done').write_bytes(b'')
    seconds = max(len(track) for track in tracks.values()) / 2 / RATE
    started = time.time()
    run = subprocess.run(['/usr/bin/python3', str(service / 'shodan_live.py'), 'transcribe', str(folder)],
                         capture_output=True, text=True, timeout=900)
    state = json.loads((folder / 'live-state.json').read_text()) if (folder / 'live-state.json').is_file() else {}
    segments = state.get('segments') or []
    print(f'音声 {seconds:.0f}秒、処理 {time.time() - started:.0f}秒、発言 {len(segments)}件、状態 {state.get("status")}、エラー {state.get("error")}')
    for segment in segments:
        print(f"  {segment.get('who')}: {segment.get('text')}")
    if run.returncode or not segments:
        print(run.stdout[-2000:], run.stderr[-2000:])
        raise SystemExit('文字起こしが出ませんでした')
    # 会議後：設定 ai.analysis が無ければ文字起こしの保存まで、あれば分析まで
    after = subprocess.run(['/usr/bin/python3', str(service / 'shodan_analyze.py'), str(folder), '--local-only'], capture_output=True, text=True, timeout=2400)
    status = json.loads((folder / 'analysis-status.json').read_text()) if (folder / 'analysis-status.json').is_file() else {}
    print(f'会議後: 終了コード {after.returncode}、{status.get("phase")}、{status.get("message")}')
    if after.returncode:
        print(after.stdout[-2000:], after.stderr[-2000:])
        raise SystemExit('会議後の処理が失敗しました')
    print(f'記録: {folder}')


if __name__ == '__main__':
    main()
