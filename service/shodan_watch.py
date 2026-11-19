#!/usr/bin/env python3
"""shodan_watch.py — 会話（通話・会議）の録画を自動で始めて止め、終わったら分析に回す見張り役（launchd で常駐）。

始め方は3つ。
  1. 会話アプリ: 音声入力アプリ以外のアプリがマイクを使い始めた（Zoom・Meet・Teams・Slack ハドル・LINE 通話など。
     予定が無い急な通話も録る）。
  2. 対面の予定（設定 calendar がオンの時だけ）: 題に【面談】【商談】【対面】がある、または招待（参加者2人以上）で場所がある予定。
     会議リンクや「オンライン」の書かれた予定と個人の暦（calendar.personal_calendar）は除く（shodan_calendar.in_person）。
     開始30秒前から終了まで。1つの予定で1回だけ。
  3. Mica の［録音］ボタン（Mica が <data_root>/.record-request.json を置く）。
題: その時間の会議予定（会議URLあり、または参加者2人以上）があればその題、無ければアプリ名（ボタンなら「対面の会議」）。
  見る暦: 設定 calendar.calendars（calendar.account の OAuth で、共有された暦として読む）。予定表がオフならアプリ名の題になる。
止める条件:
  会話アプリ: マイクを使うアプリが2分いなくなる、または3時間たつ。
  対面（予定・ボタン）: 声や物音が10分入らない、予定の終了から30分（ボタンは開始から3時間）。Mica の［止める］はどの録音でも止める。
  1分未満の録画で会話が無い物は捨て、それ以外は shodan_analyze.py を裏で起こす。対面で一度も声が入らなかった録音は分析しない（録音は残す）。
置き場: <data_root>/<日時>_<題>/video.mov と meta.json（設定 delivery.drive がオンなら、Driveの内容一致と分析完了を確認した video.mov は直ちに消す。オフなら消さない）
"""
from __future__ import annotations

import array
import datetime as dt
import json
import math
import os
from pathlib import Path
import re
import signal
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
import mica_config
APP = str(mica_config.app_support() / "MicaRecorder.app")  # 画面・マイクの録画アプリ（recorder/build.sh が作る）
REC_BIN = os.path.join(APP, "Contents/MacOS/mica-recorder")
ANALYZE = os.path.join(HERE, "shodan_analyze.py")
LIVE = os.path.join(HERE, "shodan_live.py")
ROOT = str(mica_config.data_root())
from shodan_calendar import CALENDAR, current_event, epoch, event_key
# マイクを使っても「会話」と見なさないもの（ScreenCaptureKit の代理・音声入力）。前方一致
IGNORE_MIC = ("com.apple.replayd", "com.apple.SpeechRecognitionCore", "com.apple.corespeechd",
              "now.typeless.desktop", "com.anthropic.claudefordesktop", "com.openai.codex",
              *mica_config.ignore_mic_apps())  # 足す時は設定 ignore_mic_apps
APP_NAMES = {"us.zoom.xos": "Zoom", "com.microsoft.teams2": "Teams", "com.tinyspeck.slackmacgap": "Slackハドル",
             "jp.naver.line.mac": "LINE通話", "com.google.Chrome": "Chromeの通話", "com.apple.Safari": "Safariの通話",
             "com.apple.FaceTime": "FaceTime", "com.hnc.Discord": "Discord",
             "company.thebrowser.browser": "Arcの通話", "com.microsoft.edgemac": "Edgeの通話",
             "com.brave.Browser": "Braveの通話", "org.mozilla.firefox": "Firefoxの通話",
             "Cisco-Systems.Spark": "Webex", "com.cisco.webexmeetingsapp": "Webex"}
MIN_SEC = 60
JST = dt.timezone(dt.timedelta(hours=9))
POLL = 15
IDLE_STOP = 120
MAX_SEC = 3 * 3600
CAL_TTL = 300
LIVE_WORKERS = {}
RESUME_WORKERS = {}
# 対面の録音（予定の時刻・Mica の［録音］）
REQUEST = os.path.join(ROOT, ".record-request.json")
REQUEST_TTL = 60          # 見張り役が止まっていた間の古い［録音］で、後から急に録り始めない
STARTED = mica_config.app_support() / "in-person-started.json"
LEAD = 30                 # 対面の予定は開始30秒前から録る
ROOM_POLL = 5             # 対面の録音中は5秒ごとにマイクの音を見る
QUIET_STOP = 10 * 60      # 声や物音が10分入らなければ止める
END_GRACE = 30 * 60       # 予定の対面は終了予定から30分で止める
VOICE_DB = 10             # 5秒の中で音の大きさの幅がこれ以上なら「声や物音あり」
# 実測（3会議の自分側1,422本）：静かな部屋は幅0〜6dB、話し声は12dB以上に分かれる


def log(msg: str) -> None:
    print(f"[{dt.datetime.now(JST):%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def mic_users() -> list[str]:
    """マイクを使っている会話アプリ。Electron・Chrome の補助プロセス（*.helper*）と Teams の通話部品（*.modulehost）は本体の id に直す。"""
    out = subprocess.run([REC_BIN, "mic-users"], capture_output=True, text=True, timeout=10, check=True).stdout
    return [re.sub(r"\.(helper|modulehost).*$", "", b) for b in out.split() if not b.startswith(IGNORE_MIC)]


def take_request() -> str | None:
    """Mica の［録音］［止める］を1回だけ受け取る。"""
    try:
        with open(REQUEST) as source:
            request = json.load(source)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        request = {"error": type(exc).__name__}
    try:
        os.unlink(REQUEST)
    except FileNotFoundError:
        pass
    action, at = request.get("action"), request.get("at")
    if action not in ("start", "stop") or not isinstance(at, (int, float)) or not 0 <= time.time() - at <= REQUEST_TTL:
        log(f"record request ignored: {request}")
        return None
    log(f"record request: {action}")
    return action


def wait_request(seconds: int) -> None:
    """待つ間もボタンにはすぐ応える。"""
    for _ in range(seconds):
        if os.path.exists(REQUEST):
            return
        time.sleep(1)


def chunk_has_voice(path: str) -> bool:
    """録画アプリが5秒ごとに書く自分側（マイク）の音（CAF・16bit・1ch）に、声や物音があるか。

    静かな部屋は100ミリ秒ごとの音の大きさがほぼ一定で、話すと上下に大きく揺れる。下から1割と上から1割の差で見る。
    """
    raw = Path(path).read_bytes()
    if raw[:4] != b"caff":
        raise ValueError("CAF ではありません")
    pos, fmt, pcm = 8, None, None
    while pos + 12 <= len(raw):
        kind, size = raw[pos:pos + 4], struct.unpack(">q", raw[pos + 4:pos + 12])[0]
        end = len(raw) if size < 0 else pos + 12 + size
        if kind == b"desc":
            fmt = struct.unpack(">d4sIIIII", raw[pos + 12:pos + 44])
        elif kind == b"data":
            pcm = raw[pos + 16:end]  # 先頭4バイトは編集回数
        if size < 0:
            break
        pos = end
    if fmt is None or pcm is None or fmt[1] != b"lpcm" or fmt[2] & 1 or fmt[5] != 1 or fmt[6] != 16:
        raise ValueError(f"想定外の音声形式 {fmt}")
    samples = array.array("h", pcm[:len(pcm) // 2 * 2])
    if bool(fmt[2] & 2) != (sys.byteorder == "little"):
        samples.byteswap()
    step = int(fmt[0]) // 10
    levels = sorted(10 * math.log10(sum(v * v for v in samples[i:i + step]) / step + 1)
                    for i in range(0, len(samples) - step + 1, step))
    if len(levels) < 10:
        return False
    return levels[int(len(levels) * .9)] - levels[len(levels) // 10] >= VOICE_DB


class Room:
    """対面の録音。会話アプリのマイク利用では止めず、声と時間で止める。"""

    def __init__(self, folder: str, stop_by: float, last_voice: float):
        self.folder, self.stop_by, self.last_voice = folder, stop_by, last_voice
        self.checked: set[str] = set()
        self.voiced = 0
        self.warned = False

    def listen(self, now: float) -> None:
        audio = os.path.join(self.folder, "live-audio")
        try:
            names = sorted(os.listdir(audio))
        except FileNotFoundError:
            return
        for name in names:
            if not name.endswith("-self.caf") or name in self.checked:
                continue
            self.checked.add(name)
            try:
                heard = chunk_has_voice(os.path.join(audio, name))
            except FileNotFoundError:
                continue  # 文字起こしが処理を終えて消した
            except (OSError, ValueError, struct.error) as exc:
                if not self.warned:
                    log(f"voice check failed ({exc}); keep recording until the time limit")
                    self.warned = True
                heard = True  # 判定できないときは止めない側に倒す
            if heard:
                self.voiced += 1
                self.last_voice = now

    def stop_reason(self, now: float) -> str | None:
        if now - self.last_voice > QUIET_STOP:
            return "no voice for 10 min"
        if now > self.stop_by:
            return "time limit"
        return None


def load_started() -> dict:
    try:
        started = json.loads(STARTED.read_text())
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in started.items() if time.time() - v < 2 * 86400}


def in_person_due(events: list[dict], started: dict, now: float) -> dict | None:
    """対面の会議（shodan_calendar.in_person）で、開始30秒前〜終了の間にあり、まだ録っていない物。"""
    due = [e for e in events if e.get("in_person") is True
           and epoch(e["start"]) - LEAD <= now < epoch(e["end"]) and event_key(e) not in started]
    return min(due, key=lambda e: epoch(e["start"]), default=None)


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def start_live(folder: str) -> None:
    """A live-transcription failure must be visible, but must not stop movie recording."""
    from shodan_live import atomic_json, ACTIVE
    atomic_json(ACTIVE, {"folder": folder})
    atomic_json(os.path.join(folder, "live-state.json"), {"title": "会議", "status": "準備中", "error": None, "segments": []})
    try:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{mica_config.live_port()}/api/health", timeout=2) as response:
                if json.load(response).get("service") != "shodan-live":
                    raise RuntimeError(f"port {mica_config.live_port()} is used by another service")
        except urllib.error.URLError:
            with open(os.path.join(HERE, "live-server.log"), "a") as output:
                subprocess.Popen([sys.executable, LIVE, "serve"], stdout=output, stderr=subprocess.STDOUT,
                                 start_new_session=True)
        with open(os.path.join(folder, "live.log"), "a") as output:
            worker = subprocess.Popen([sys.executable, LIVE, "transcribe", folder],
                                      stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
        LIVE_WORKERS[folder] = worker
    except Exception as exc:
        atomic_json(os.path.join(folder, "live-state.json"), {
            "title": "会議", "status": "エラー", "error": f"ライブ文字起こしを起動できません: {exc}", "segments": []})
        log(f"live start failed: {type(exc).__name__}")


def poll_live_workers() -> None:
    from shodan_live import atomic_json
    for folder, worker in list(LIVE_WORKERS.items()):
        code = worker.poll()
        if code is None:
            continue
        del LIVE_WORKERS[folder]
        path = os.path.join(folder, "live-state.json")
        if not os.path.isdir(folder):
            continue
        try:
            with open(path) as source:
                state = json.load(source)
        except (OSError, ValueError):
            state = {"title": "会議", "segments": []}
        if state.get("status") not in ("録画終了", "エラー", "復旧待ち"):
            state.update(status="エラー", error=f"文字起こし処理が終了しました（終了コード {code}）。録画は別に継続しています。")
            atomic_json(path, state)


def poll_offline_resumes() -> None:
    """Recover one saved meeting at a time, without blocking microphone polling."""
    from shodan_live import offline_resume_ready
    from shodan_session import lock
    from shodan_storage import folder_lock
    for folder, worker in list(RESUME_WORKERS.items()):
        if worker.poll() is not None:
            del RESUME_WORKERS[folder]
    if RESUME_WORKERS:
        return
    for folder in sorted(Path(ROOT).iterdir()):
        if not folder.is_dir() or folder.is_symlink() or str(folder) in LIVE_WORKERS:
            continue
        try:
            if not offline_resume_ready(folder):
                continue
            # A detached child can still own either lock after watcher restart.
            # The child also claims the locks itself before doing any work.
            with folder_lock(folder), lock(folder, 'transcription', blocking=False):
                if not offline_resume_ready(folder):
                    continue
            with (folder / 'analyze.log').open('a') as output:
                RESUME_WORKERS[str(folder)] = subprocess.Popen(
                    [sys.executable, ANALYZE, str(folder), '--resume-live'],
                    start_new_session=True, stdout=output, stderr=subprocess.STDOUT)
            return
        except BlockingIOError:
            continue
        except (OSError, ValueError) as error:
            log(f"offline resume unavailable: {type(error).__name__}")


def open_meeting_assistant(folder: str) -> None:
    """Open this meeting once, after capture succeeds; UI failure must not stop recording."""
    try:
        url = "mica://meeting?" + urllib.parse.urlencode({"session": os.path.basename(folder)})
        subprocess.run(["/usr/bin/open", "-b", "app.mica.assistant", url], check=True,
                       capture_output=True, text=True, timeout=10)
        log("Mica opened for current meeting")
    except (OSError, subprocess.SubprocessError) as exc:
        log(f"Mica could not open ({type(exc).__name__}); recording continues")


def start_recording(ev: dict | None, bundle: str | None, mode: str = "app",
                    stop_by: float | None = None) -> tuple[int | None, str]:
    """録画アプリとして起こす（画面収録の許可がこのアプリに付くため）。録り始められなければ pid は None。

    mode: app＝会話アプリのマイク利用、schedule＝対面の予定、manual＝Mica の［録音］。bundle が None なら対面。
    """
    start = dt.datetime.now(JST)
    # 予定が無いときの題。アプリの識別子（com.… など）は題にしない。題は音声認識の手がかりにも渡るため
    ev = ev or {"id": None, "title": APP_NAMES.get(bundle, "通話") if bundle else "対面の会議",
                "attendees": [], "description": ""}
    safe = re.sub(r'[\\/:*?"<>|\s]+', "_", ev["title"])[:40]
    base = os.path.join(ROOT, f"{start:%Y%m%d-%H%M%S}_{safe}")
    folder = base
    suffix = 0
    while True:
        try:
            os.mkdir(folder)
            break
        except FileExistsError:
            suffix += 1
            folder = base + '-' + str(suffix)
    meta = {**ev, "recorded_at": start.isoformat(), "app": bundle or "対面", "mode": mode}
    if mode != "app":
        meta.update(in_person=True, stop_by=stop_by)
    with open(os.path.join(folder, "meta.json"), "w") as metadata:
        json.dump(meta, metadata, ensure_ascii=False, indent=2)
    video = os.path.join(folder, "video.mov")
    rec_log = os.path.join(folder, "rec.log")
    args = ["open", "-g", "-n", "-a", APP, "--stdout", rec_log, "--stderr", rec_log,
            "--args", "record", "--out", video]
    if bundle and not bundle.startswith("pid:"):
        args += ["--bundle", bundle]
    from shodan_local_asr import capture_ready_guard, CaptureGateTimeout
    startup_error = None
    capture_pid = None
    try:
        # Model initialization and capture startup must not overlap. An already
        # warm service survives between meetings; its absence does not discard audio.
        with capture_ready_guard(timeout=20) as recognition_ready:
            if not recognition_ready:
                log("local recognition unavailable; capture starts with retained audio and previews")
            subprocess.run(args, check=True)
            for _ in range(30):
                time.sleep(0.5)
                if os.path.exists(video + ".pid") and os.path.exists(video + ".ready"):
                    with open(video + ".pid") as pid_file:
                        capture_pid = int(pid_file.read())
                    break
    except CaptureGateTimeout:
        startup_error = "音声認識の準備が長引き、録音を開始できませんでした。"
    if capture_pid:
        start_live(folder)
        open_meeting_assistant(folder)
        return capture_pid, folder
    # A launched process can be waiting for OS permission. Do not leave it recording alone.
    if os.path.exists(video + ".pid"):
        try:
            with open(video + ".pid") as pid_file:
                pending_pid = int(pid_file.read())
            os.kill(pending_pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    from shodan_live import atomic_json, ACTIVE
    atomic_json(ACTIVE, {"folder": folder})
    atomic_json(os.path.join(folder, "live-state.json"), {
        "title": ev["title"], "status": "エラー", "segments": [],
        "error": startup_error or "録音を開始できませんでした。システム設定の画面収録とマイクの許可を確認してください。"})
    open_meeting_assistant(folder)
    return None, folder


def stop_recording(pid: int | None, folder: str, seconds: float, silent_room: bool = False) -> None:
    if pid is not None:
        os.kill(pid, signal.SIGTERM)
        for _ in range(120):
            if not alive(pid):
                break
            time.sleep(0.5)
        else:
            os.kill(pid, signal.SIGKILL)
    log(f"stopped pid={pid} {int(seconds)}s {folder}")
    from shodan_session import atomic_json
    from shodan_live import WAITING_LIVE
    short_recording = pid is not None and seconds < MIN_SEC
    if silent_room and not short_recording:
        # 予定の時刻に Mac だけが部屋に残っていた等。聞き間違いの文字起こしで分析・配信しない。消さずに残す
        atomic_json(os.path.join(folder, "analysis-status.json"), {
            "phase": "complete", "updated_at": time.time(),
            "message": "声が入らなかったため分析していません。録音は残しています。"})
        log(f"no voice; analysis skipped {folder}")
        return
    atomic_json(os.path.join(folder, "analysis-status.json"), {
        "phase": WAITING_LIVE, "message": "会議後の分析を開始します", "updated_at": time.time(),
        "short_recording": short_recording})
    args = [sys.executable, ANALYZE, folder]
    if short_recording:
        # Finalize the last live chunk off the microphone-monitoring loop before
        # deciding whether this was an empty accidental capture.
        args.append("--short-recording")
    subprocess.Popen(args, start_new_session=True,
                     stdout=open(os.path.join(folder, "analyze.log"), "a"), stderr=subprocess.STDOUT)



def cleanup() -> None:
    """Verify Drive content and analysis before deleting; never rely on URL age."""
    # Separate process reads the current policy on every sweep, without loading
    # recording/checksum work into the microphone polling loop.
    global cleanup_worker
    if cleanup_worker is not None and cleanup_worker.poll() is None:
        return
    with open(os.path.join(HERE, "storage-cleanup.log"), "a") as output:
        cleanup_worker = subprocess.Popen(
            [sys.executable, os.path.join(HERE, "shodan_storage.py"), "--apply"],
            stdout=output, stderr=subprocess.STDOUT, start_new_session=True)


cleanup_worker = None


def adopt_recording():
    """Reconnect to an existing detached recorder after a watcher restart."""
    found = []
    with os.scandir(ROOT) as entries:
        for item in entries:
            if not item.is_dir(follow_symlinks=False):
                continue
            marker = os.path.join(item.path, "video.mov.pid")
            if not os.path.isfile(marker) or os.path.isfile(os.path.join(item.path, "live-audio", "capture.done")):
                continue
            try:
                pid = int(Path(marker).read_text())
                if pid <= 1 or not alive(pid):
                    continue
                check = subprocess.run(["/usr/sbin/lsof", "-nP", "-a", "-p", str(pid), "-Ffa", "--",
                                        os.path.join(item.path, "video.mov")],
                                       capture_output=True, text=True, timeout=10)
                if check.returncode not in (0, 1) or check.stderr.strip():
                    raise RuntimeError("既存録画の利用状況を確認できません")
                if check.returncode == 1:
                    continue
                fields = check.stdout.splitlines()
                modes = {field for field in fields if field.startswith("a")}
                if f"p{pid}" not in fields or not modes or modes - {"ar", "aw", "au"}:
                    raise RuntimeError("既存録画の書き込み状態を確認できません")
                if modes == {"ar"}:
                    continue
                meta = json.loads((Path(item.path) / "meta.json").read_text())
                started = dt.datetime.fromisoformat(meta["recorded_at"]).timestamp()
                found.append((pid, item.path, started))
            except (ValueError, OSError, subprocess.TimeoutExpired):
                raise RuntimeError("既存録画の状態を確認できません。重複録画を開始しません")
    if len(found) > 1:
        raise RuntimeError("複数の録画が稼働中です。重複録画を開始しません")
    return found[0] if found else (None, None, 0.0)


def main() -> None:
    os.makedirs(ROOT, exist_ok=True)
    events, fetched = [], 0.0
    pid, folder, started = adopt_recording()
    last_voice = time.time() if folder else 0.0
    room = None  # 対面の録音中だけ Room
    if folder:
        meta = json.loads((Path(folder) / "meta.json").read_text())
        if meta.get("in_person"):
            room = Room(folder, meta.get("stop_by") or started + MAX_SEC, time.time())
            room.voiced = 1  # 再起動前に声が入ったかは分からない。分析を省かない側に倒す
    wait_release = False  # True の間は、マイクが一度空くまで録り始めない
    in_person_started = load_started()
    log("watch start; storage policy=verified-immediate-v1; in-person=v1")
    while True:
        now = time.time()
        poll_live_workers()
        poll_offline_resumes()
        if now - fetched > CAL_TTL:
            # Calendar metadata must not terminate an ongoing local recording.
            fetched = now
            if folder is None and mica_config.drive_enabled():
                cleanup()
        events = CALENDAR.snapshot().get('events', []) if mica_config.calendar_enabled() else []
        from shodan_preparation import schedule_next
        if folder is None:
            schedule_next(ROOT)
        users = mic_users()
        request = take_request()
        if folder is None:
            wait_release = wait_release and bool(users)
            meeting = None if request or users else in_person_due(events, in_person_started, now)
            if request == "start" or meeting:
                mode = "schedule" if meeting else "manual"
                stop_by = epoch(meeting["end"]) + END_GRACE if meeting else now + MAX_SEC
                if meeting:
                    in_person_started[event_key(meeting)] = now
                    STARTED.parent.mkdir(parents=True, exist_ok=True)
                    STARTED.write_text(json.dumps(in_person_started))
                pid, folder = start_recording(meeting or current_event(events), None, mode, stop_by)
                started = last_voice = now
                room = Room(folder, stop_by, now)
                log(f"start {mode} -> {folder}")
            elif users and not wait_release:
                pid, folder = start_recording(current_event(events), users[0])
                started = last_voice = now
                log(f"start app={users[0]} -> {folder}")
            elif request == "stop":
                log("stop request ignored: not recording")
        else:
            if request == "start":
                log("start request ignored: already recording")
            if room is None:
                if users:
                    last_voice = now
                over = now - last_voice > IDLE_STOP or now - started > MAX_SEC
            else:
                room.listen(now)
                reason = room.stop_reason(now)
                over = reason is not None
                if over:
                    log(f"in-person stop: {reason}")
            if request == "stop" or pid is None or not alive(pid) or over:
                # 3時間で切った・録画が起きなかった・手で止めた・対面が終わった後は、同じ通話を録り直さない
                wait_release = (request == "stop" or room is not None or now - started > MAX_SEC
                                or pid is None or not alive(pid))
                seconds = (now if room else last_voice) - started
                stop_recording(pid if pid and alive(pid) else None, folder, seconds,
                               silent_room=room is not None and room.voiced == 0 and pid is not None)
                pid = folder = room = None
        wait_request(ROOM_POLL if room else POLL)


if __name__ == "__main__":
    main()
