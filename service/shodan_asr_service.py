#!/usr/bin/env python3
"""Keep one warmed Whisper worker outside the capture lifecycle."""

import fcntl
import json
import os
from pathlib import Path
import signal
import socketserver
import stat
import subprocess
import threading
import time

from shodan_local_asr import (
    ASRUnavailableError, CAPTURE_LOCK_NAME, LocalASR, LocalASRError,
    MAX_PROTOCOL_BYTES, MAX_SERVICE_REQUEST_BYTES, SERVICE_SOCKET_NAME,
    private_runtime, read_service_message, runtime_lock,
)


INSTANCE_LOCK_NAME = 'service.lock'
WARM_TIMEOUT = 12
UNAVAILABLE = {'ok': False, 'error': 'local_asr_unavailable'}
INVALID = {'ok': False, 'error': 'local_asr_invalid_request'}
WORKER_UNAVAILABLE = {
    'local_asr_timeout', 'local_asr_worker_exited', 'local_asr_worker_io_failed',
    'local_asr_response_mismatch', 'local_asr_invalid_protocol', 'local_asr_protocol_oversized',
}


def recording_active():
    try:
        result = subprocess.run(['/usr/bin/pgrep', '-x', 'mica-recorder'], timeout=1,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return True
    # A failed process inventory is not evidence that the recorder is absent.
    return result.returncode != 1


class RequestHandler(socketserver.BaseRequestHandler):
    def handle(self):
        try:
            request = read_service_message(self.request, MAX_SERVICE_REQUEST_BYTES, time.monotonic() + 2)
        except (OSError, ValueError, RecursionError):
            response = INVALID
        else:
            response = self.server.respond(request)
        payload = (json.dumps(response, ensure_ascii=False) + '\n').encode()
        if len(payload) > MAX_PROTOCOL_BYTES:
            payload = b'{"ok":false,"error":"local_asr_protocol_oversized"}\n'
        try:
            self.request.settimeout(3)
            self.request.sendall(payload)
        except OSError:
            pass


class ASRService(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    timeout = .25

    def __init__(self, path, worker):
        self.worker = worker
        self.warmed = False
        self.warm_count = 0
        super().__init__(str(path), RequestHandler)

    def is_ready(self):
        return self.warmed and self.worker.worker_pid() is not None

    def maintain(self, stop):
        while not stop.is_set():
            failed = not self.is_ready() and self.prepare_if_idle(stop) is False
            stop.wait(15 if failed else 2)

    def prepare_if_idle(self, stop):
        with runtime_lock(CAPTURE_LOCK_NAME) as fd:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            if stop.is_set() or recording_active():
                return
            self.warmed = False
            self.warm_count += 1
            try:
                self.worker.warm(WARM_TIMEOUT, fd)
            except (LocalASRError, OSError, ValueError) as error:
                print(f'Local ASR preparation failed ({type(error).__name__}: {error})', flush=True)
                return False
            else:
                self.warmed = True

    def respond(self, request):
        if request == {'op': 'health'}:
            worker_pid = self.worker.worker_pid()
            return {'ok': True, 'ready': self.warmed and worker_pid is not None,
                    'service_pid': os.getpid(), 'worker_pid': worker_pid, 'warm_count': self.warm_count}
        path, terms = request.get('path'), request.get('terms')
        if (request.get('op') != 'transcribe' or set(request) != {'op', 'path', 'terms'}
                or not isinstance(path, str) or not Path(path).is_absolute() or '\0' in path
                or not isinstance(terms, list) or not all(isinstance(term, str) for term in terms)):
            return INVALID
        if not self.is_ready():
            return UNAVAILABLE
        try:
            words = self.worker.transcribe(path, terms, allow_start=False)
        except LocalASRError as error:
            if isinstance(error, ASRUnavailableError) or str(error) in WORKER_UNAVAILABLE:
                return UNAVAILABLE
            return {'ok': False, 'error': str(error)}
        return {'ok': True, 'words': words}


def unlink_socket(path, expected=None):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
        raise PermissionError('refusing to unlink unexpected local ASR socket path')
    if expected is None or (info.st_dev, info.st_ino) == (expected.st_dev, expected.st_ino):
        path.unlink()


def serve(stop, worker_factory=LocalASR):
    path = private_runtime() / SERVICE_SOCKET_NAME
    with runtime_lock(INSTANCE_LOCK_NAME) as fd:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        unlink_socket(path)
        with worker_factory() as worker, ASRService(path, worker) as server:
            bound = path.lstat()
            path.chmod(0o600)
            maintenance = threading.Thread(target=server.maintain, args=(stop,), name='asr-warmup')
            maintenance.start()
            try:
                while not stop.is_set():
                    server.handle_request()
            finally:
                stop.set()
                maintenance.join()
                unlink_socket(path, bound)
        return True


if __name__ == '__main__':
    stopped = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stopped.set())
    serve(stopped)
