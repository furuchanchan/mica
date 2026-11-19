#!/usr/bin/env python3
"""Verified Drive storage and immediate cleanup of completed recordings.

設定 delivery.drive がオンの時だけ使う（呼ぶ側で分岐する）。オフの間、録画ファイルは消さない。
"""
from __future__ import annotations
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
import urllib.parse
import urllib.request

import mica_config

ROOT = mica_config.data_root()
CRED_DIR = mica_config.google_credentials_dir()
# 保存先は設定 delivery.drive から読む。推測しない。
POLICY_VERSION = 'verified-immediate-v1'


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''): h.update(chunk)
    return h.hexdigest()


def identity(path):
    s = Path(path).stat()
    return s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns


def atomic_json(path, value):
    path = Path(path)
    tmp = path.with_name(path.name + f'.tmp-{os.getpid()}')
    try:
        with tmp.open('w') as f:
            json.dump(value, f, ensure_ascii=False, indent=2)
            f.flush(); os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def atomic_text(path, text):
    path = Path(path)
    tmp = path.with_name(path.name + f'.tmp-{os.getpid()}')
    try:
        with tmp.open('w') as f:
            f.write(text); f.flush(); os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


@contextlib.contextmanager
def folder_lock(folder):
    with (Path(folder) / '.processing.lock').open('a') as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try: yield
        finally: fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def analysis_kind(folder):
    p = Path(folder) / 'analysis.md'
    if not p.is_file() or p.is_symlink(): return None
    lines = p.read_text().splitlines()
    if not lines: return None
    m = re.fullmatch(r'\s*`?種別:\s*(商談|仕事|私的)`?\s*', lines[0])
    return m.group(1) if m and len(p.read_text().strip()) > 20 else None


def analysis_hashes(folder):
    return {name: sha256(Path(folder) / name) for name in ('analysis.md', 'transcript.md')}


def mark_analysis_complete(folder):
    folder = Path(folder)
    if not analysis_kind(folder) or not (folder / 'transcript.md').stat().st_size:
        raise ValueError('analysis incomplete')
    atomic_json(folder / 'analysis_complete.json', {'hashes': analysis_hashes(folder), 'completed_at': time.time()})


def analysis_complete(folder):
    folder = Path(folder)
    if not analysis_kind(folder): return False
    for name in ('analysis.md', 'transcript.md', 'drive_url.txt'):
        p = folder / name
        if p.is_symlink() or not p.is_file() or not p.stat().st_size: return False
    marker = folder / 'analysis_complete.json'
    if marker.exists():
        return json.loads(marker.read_text()).get('hashes') == analysis_hashes(folder)
    # Existing pipeline writes drive_url only AFTER the analyzer succeeds.
    uploaded = (folder / 'drive_url.txt').stat().st_mtime_ns
    return all((folder / n).stat().st_mtime_ns <= uploaded for n in ('analysis.md', 'transcript.md'))


def _pids_busy(folder, names):
    for name in names:
        p = Path(folder) / name
        if p.exists():
            try: pid = int(p.read_text().strip())
            except ValueError: return True
            if pid <= 1: return True
            try: os.kill(pid, 0)
            except ProcessLookupError: continue
            except PermissionError: return True
            else: return True
    return False


def capture_busy(folder):
    return _pids_busy(folder, ('video.mov.pid',))


def recording_busy(folder):
    return _pids_busy(folder, ('video.mov.pid', 'live-worker.pid'))


def files_in_use(folder):
    # Detect recorder/analyzer handles as well as a process cwd in this folder.
    p = subprocess.run(['/usr/sbin/lsof', '-n', '-P', '+D', str(folder), '-Fp'],
                       capture_output=True, text=True, timeout=30)
    if p.returncode not in (0, 1) or p.stderr.strip():
        raise RuntimeError('open-file check failed')
    return any(line.startswith('p') and line[1:] != str(os.getpid()) for line in p.stdout.splitlines())


def access_token(kind):
    account, _ = mica_config.drive_destination(kind)
    c = json.loads((CRED_DIR / (account + '.json')).read_text())
    body = urllib.parse.urlencode({k: c[k] for k in ('client_id', 'client_secret', 'refresh_token')} |
                                  {'grant_type': 'refresh_token'}).encode()
    with urllib.request.urlopen(urllib.request.Request(c['token_uri'], data=body), timeout=30) as r:
        return json.load(r)['access_token']


def api(token, url, data=None, headers=None, method=None, timeout=60):
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={'Authorization': 'Bearer ' + token, **(headers or {})})
    return urllib.request.urlopen(req, timeout=timeout)


def file_id(url):
    parsed = urllib.parse.urlparse(url.strip())
    m = re.fullmatch(r'/file/d/([A-Za-z0-9_-]+)/view/?', parsed.path)
    if parsed.scheme != 'https' or parsed.hostname != 'drive.google.com' or not m:
        raise ValueError('invalid Drive URL')
    return m.group(1)


def remote_metadata(url, kind):
    fid = file_id(url)
    endpoint = f'https://www.googleapis.com/drive/v3/files/{fid}?fields=id,size,md5Checksum,trashed'
    with api(access_token(kind), endpoint) as r: return json.load(r)


def verify_remote(video, url, kind):
    video = Path(video)
    before = identity(video)
    h = hashlib.md5()
    with video.open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''): h.update(chunk)
    remote = remote_metadata(url, kind)
    if remote.get('trashed') is not False or remote.get('id') != file_id(url):
        raise ValueError('Drive file missing or trashed')
    if int(remote.get('size', -1)) != before[2] or remote.get('md5Checksum') != h.hexdigest():
        raise ValueError('Drive content mismatch')
    if identity(video) != before: raise ValueError('recording changed during verification')
    return {'file_id': remote['id'], 'size': before[2], 'md5': h.hexdigest(), 'verified_at': time.time()}


def ensure_uploaded(folder, name, kind):
    if kind not in ('商談', '仕事', '私的'): raise ValueError('destination is not classified')
    folder = Path(folder); video = folder / 'video.mov'; marker = folder / 'drive_url.txt'
    if capture_busy(folder): raise ValueError('recording is active')
    before = identity(video)
    if marker.is_file() and marker.read_text().strip():
        url = marker.read_text().strip()
    else:
        token = access_token(kind)
        _, dest = mica_config.drive_destination(kind)
        query = urllib.parse.urlencode({'q': f"name='{dest}' and mimeType='application/vnd.google-apps.folder' and 'root' in parents and trashed=false", 'fields': 'files(id)'})
        with api(token, 'https://www.googleapis.com/drive/v3/files?' + query) as r: found = json.load(r)['files']
        if found: parent = found[0]['id']
        else:
            with api(token, 'https://www.googleapis.com/drive/v3/files', json.dumps({'name': dest, 'mimeType': 'application/vnd.google-apps.folder'}).encode(), {'Content-Type': 'application/json'}, 'POST') as r: parent = json.load(r)['id']
        with api(token, 'https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable',
                 json.dumps({'name': name + '.mov', 'parents': [parent]}).encode(),
                 {'Content-Type': 'application/json', 'X-Upload-Content-Type': 'video/quicktime', 'X-Upload-Content-Length': str(before[2])}, 'POST') as r:
            location = r.headers['Location']
        # Stream the file; no whole-recording allocation in RAM.
        with video.open('rb') as f, api(token, location, f, {'Content-Length': str(before[2])}, 'PUT', 600) as r:
            uploaded = json.load(r)
        url = f"https://drive.google.com/file/d/{uploaded['id']}/view"
        # Persist only a real successful upload, never an empty success marker.
        atomic_text(marker, url + '\n')
    verified = verify_remote(video, url, kind)
    if identity(video) != before: raise ValueError('recording changed during upload')
    atomic_json(folder / 'drive_verified.json', verified)
    return url


def cleanup_recording(folder, apply=False):
    folder = Path(folder)
    if folder.is_symlink() or folder.resolve().parent != ROOT.resolve():
        return {'status': 'preserved', 'reason': 'outside recording root'}
    video = folder / 'video.mov'
    if not video.exists(): return {'status': 'absent'}
    if video.is_symlink() or not video.is_file(): return {'status': 'preserved', 'reason': 'not a regular recording'}
    try:
        with folder_lock(folder):
            if recording_busy(folder) or files_in_use(folder): return {'status': 'preserved', 'reason': 'in use'}
            if not analysis_complete(folder): return {'status': 'preserved', 'reason': 'analysis incomplete'}
            before = identity(video); hashes = analysis_hashes(folder)
            kind = analysis_kind(folder); url = (folder / 'drive_url.txt').read_text().strip()
            verified = verify_remote(video, url, kind)
            if identity(video) != before or analysis_hashes(folder) != hashes:
                raise ValueError('local files changed')
            if recording_busy(folder) or files_in_use(folder): return {'status': 'preserved', 'reason': 'in use'}
            if identity(video) != before: raise ValueError('recording changed before cleanup')
            if not apply: return {'status': 'eligible', 'bytes': before[2]}
            receipt = {'policy': POLICY_VERSION, 'drive': verified, 'analysis_hashes': hashes, 'deletion_requested_at': time.time()}
            # Verification and processing lock precede the only destructive action.
            atomic_json(folder / 'local_video_removed.json', receipt)
            video.unlink()
            return {'status': 'deleted', 'bytes': before[2]}
    except Exception as e:
        return {'status': 'preserved', 'reason': type(e).__name__}


def sweep(apply=False):
    rows = []
    for folder in sorted(ROOT.iterdir()):
        if folder.is_dir() and (folder / 'video.mov').exists():
            rows.append({'recording': hashlib.sha256(folder.name.encode()).hexdigest()[:12], **cleanup_recording(folder, apply)})
    return rows


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    if not mica_config.drive_enabled():
        raise SystemExit('設定 delivery.drive.enabled が false です。Drive へ退避していない録画は消せないため、何もしません')
    print(json.dumps(sweep(args.apply), ensure_ascii=False, indent=2))
