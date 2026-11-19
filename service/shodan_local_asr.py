#!/usr/bin/env python3
"""Persistent local Whisper client (stdlib only) and isolated JSONL worker."""
from __future__ import annotations

import argparse
import array
import atexit
from contextlib import contextmanager
import fcntl
import importlib
import json
import math
import os
from pathlib import Path
import selectors
import socket
import stat
import subprocess
import sys
import threading
import time
import wave

import mica_config
RUNTIME = mica_config.app_support() / 'asr-runtime'
MODEL_REVISION = 'a4aaeec0636e6fef84abdcbe3544cb2bf7e9f6fb'
MODEL = Path.home() / '.cache/huggingface/hub/models--mlx-community--whisper-large-v3-turbo/snapshots' / MODEL_REVISION
MAX_PROTOCOL_BYTES = 8 * 1024 * 1024
MAX_SERVICE_REQUEST_BYTES = 16 * 1024
SERVICE_SOCKET_NAME = 'service.sock'
CAPTURE_LOCK_NAME = 'capture.lock'
SERVICE_TIMEOUT = 190
# 無音や雑音のところで whisper が作りがちな決まり文句（録画からの文字起こしと共通）
HALLUCINATIONS = ('ご視聴ありがとうございました', 'チャンネル登録', 'おやすみなさい', '字幕')


class LocalASRError(RuntimeError):
    pass


class ASRUnavailableError(LocalASRError):
    pass


class CaptureGateTimeout(LocalASRError):
    pass


def _terms(values):
    if not isinstance(values, (list, tuple)):
        raise LocalASRError('local_asr_invalid_terms')
    from shodan_evidence import clean_terms
    return clean_terms(list(values))


def _audio(path):
    try:
        with wave.open(str(path), 'rb') as source:
            if (source.getnchannels(), source.getsampwidth(), source.getframerate(), source.getcomptype()) != (1, 2, 16000, 'NONE'):
                raise LocalASRError('local_asr_expected_mono_pcm16_16khz')
            samples = array.array('h', source.readframes(source.getnframes()))
            if sys.byteorder != 'little':
                samples.byteswap()
    except (OSError, wave.Error, EOFError) as error:
        raise LocalASRError('local_asr_audio_unreadable') from error
    # Match shodan_transcribe._silent: retain every sample with amplitude >=16.
    first = next((i for i, v in enumerate(samples) if abs(v) >= 16), None)
    if first is None:
        return samples, 0, 0
    last = next(i for i in range(len(samples) - 1, -1, -1) if abs(samples[i]) >= 16)
    return samples, max(0, first - 1600), min(len(samples), last + 1601)


class LocalASR:
    def __init__(self, runtime_python=None, model_path=None, timeout=180):
        self.runtime_python = Path(runtime_python or RUNTIME / '.venv/bin/python')
        self.model_path = Path(model_path or MODEL)
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError('timeout must be positive')
        self.timeout = timeout
        self._lock = threading.Lock()
        self._process = None
        self._buffer = b''
        self._request = 0
        self._closed = False
        atexit.register(self.close)

    def _stop(self):
        process, self._process = self._process, None
        self._buffer = b''
        if process is None:
            return
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        for stream in (process.stdin, process.stdout):
            if stream:
                stream.close()

    def close(self):
        with self._lock:
            self._closed = True
            self._stop()

    def _receive(self, deadline):
        while b'\n' not in self._buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise LocalASRError('local_asr_timeout')
            with selectors.DefaultSelector() as selector:
                selector.register(self._process.stdout, selectors.EVENT_READ)
                if not selector.select(remaining):
                    raise LocalASRError('local_asr_timeout')
            chunk = os.read(self._process.stdout.fileno(), 65536)
            if not chunk:
                raise LocalASRError('local_asr_worker_exited')
            self._buffer += chunk
            if len(self._buffer) > MAX_PROTOCOL_BYTES:
                raise LocalASRError('local_asr_protocol_oversized')
        line, self._buffer = self._buffer.split(b'\n', 1)
        try:
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError()
            return value
        except (ValueError, UnicodeError) as error:
            raise LocalASRError('local_asr_invalid_protocol') from error

    def _start(self, deadline, capture_gate_fd=None):
        if not self.runtime_python.is_file():
            raise LocalASRError('local_asr_runtime_missing')
        if not (self.model_path / 'weights.safetensors').is_file():
            raise LocalASRError('local_asr_model_missing')
        env = os.environ.copy()
        env.update(HF_HUB_OFFLINE='1', HF_HUB_DISABLE_IMPLICIT_TOKEN='1', PYTHONDONTWRITEBYTECODE='1')
        # The worker cannot read inherited cloud credentials.
        for name in list(env):
            if any(part in name.upper() for part in ('API_KEY', 'TOKEN', 'SECRET')) and name != 'HF_HUB_DISABLE_IMPLICIT_TOKEN':
                env.pop(name)
        args = [str(self.runtime_python), str(Path(__file__).resolve()), '--worker', '--model', str(self.model_path)]
        inherited = ()
        if capture_gate_fd is not None:
            args.extend(['--capture-gate-fd', str(capture_gate_fd)])
            inherited = (capture_gate_fd,)
        self._process = subprocess.Popen(
            args,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env=env, bufsize=0, start_new_session=True, pass_fds=inherited,
        )
        response = self._receive(deadline)
        if response != {'ready': True, 'protocol': 1}:
            raise LocalASRError(response.get('error', 'local_asr_start_failed'))

    def worker_pid(self):
        process = self._process
        return process.pid if not self._closed and process is not None and process.poll() is None else None

    def _exchange(self, deadline, **fields):
        self._request += 1
        request = {'id': self._request, **fields}
        self._process.stdin.write((json.dumps(request, ensure_ascii=False) + '\n').encode())
        self._process.stdin.flush()
        response = self._receive(deadline)
        if response.get('id') != self._request:
            raise LocalASRError('local_asr_response_mismatch')
        if response.get('ok') is not True:
            raise LocalASRError(response.get('error', 'local_asr_inference_failed'))
        return response

    def warm(self, timeout, capture_gate_fd):
        deadline = time.monotonic() + timeout
        if not self._lock.acquire(timeout=timeout):
            raise LocalASRError('local_asr_timeout')
        try:
            if self._closed:
                raise LocalASRError('local_asr_closed')
            if self.worker_pid() is None:
                self._stop()
                self._start(deadline, capture_gate_fd)
            self._exchange(deadline, warm=True)
        except (LocalASRError, OSError, ValueError):
            self._stop()
            raise
        finally:
            self._lock.release()

    def transcribe(self, path, terms, *, allow_start=True):
        terms = _terms(terms)
        path = Path(path).resolve()
        _, start, end = _audio(path)
        deadline = time.monotonic() + self.timeout
        if not self._lock.acquire(timeout=self.timeout):
            raise LocalASRError('local_asr_timeout')
        try:
            if self._closed:
                raise LocalASRError('local_asr_closed')
            if end == start:
                return []
            if self._process is None:
                if not allow_start:
                    raise ASRUnavailableError('local_asr_unavailable')
                self._start(deadline)
            elif self._process.poll() is not None:
                raise LocalASRError('local_asr_worker_exited')
            response = self._exchange(deadline, path=str(path), terms=terms)
            words = response.get('words')
            if not isinstance(words, list):
                raise LocalASRError('local_asr_word_timestamps_missing')
            return words
        except (LocalASRError, OSError, ValueError) as error:
            self._stop()
            if isinstance(error, LocalASRError):
                raise
            raise LocalASRError('local_asr_worker_io_failed') from error
        finally:
            self._lock.release()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def private_runtime():
    RUNTIME.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(RUNTIME, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if info.st_uid != os.getuid():
            raise PermissionError('local ASR runtime directory owner differs')
        if stat.S_IMODE(info.st_mode) != 0o700:
            os.fchmod(fd, 0o700)
    finally:
        os.close(fd)
    return RUNTIME


@contextmanager
def runtime_lock(name):
    path = private_runtime() / name
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise PermissionError('invalid local ASR lock file')
        yield fd
    finally:
        os.close(fd)


def read_service_message(connection, maximum, deadline):
    data = bytearray()
    while b'\n' not in data:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('local ASR message timeout')
        connection.settimeout(remaining)
        chunk = connection.recv(min(65536, maximum + 1 - len(data)))
        if not chunk or len(data) + len(chunk) > maximum:
            raise ValueError('incomplete or oversized local ASR message')
        data.extend(chunk)
    if not data.endswith(b'\n') or data.count(b'\n') != 1:
        raise ValueError('expected one local ASR message')
    value = json.loads(data)
    if not isinstance(value, dict):
        raise ValueError('expected local ASR object')
    return value


def _ask(request, timeout):
    payload = (json.dumps(request) + '\n').encode()
    if len(payload) > MAX_SERVICE_REQUEST_BYTES:
        raise LocalASRError('local_asr_invalid_request')
    path = RUNTIME / SERVICE_SOCKET_NAME
    deadline = time.monotonic() + timeout
    try:
        info = path.lstat()
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise PermissionError('invalid local ASR socket')
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(min(timeout, 2))
            connection.connect(str(path))
            connection.sendall(payload)
            return read_service_message(connection, MAX_PROTOCOL_BYTES, deadline)
    except OSError as error:
        raise ASRUnavailableError('local_asr_unavailable') from error
    except (ValueError, RecursionError) as error:
        raise LocalASRError('local_asr_invalid_protocol') from error


def service_health(timeout=2):
    response = _ask({'op': 'health'}, timeout)
    ready, service_pid, worker_pid, warm_count = (response.get(key) for key in ('ready', 'service_pid', 'worker_pid', 'warm_count'))
    if (response.get('ok') is not True or type(ready) is not bool
            or type(service_pid) is not int or service_pid <= 0
            or worker_pid is not None and (type(worker_pid) is not int or worker_pid <= 0)
            or ready and worker_pid is None or type(warm_count) is not int or warm_count < 0):
        raise LocalASRError('local_asr_invalid_protocol')
    return response


def ensure_ready(timeout=20):
    deadline = time.monotonic() + max(0, timeout)
    while True:
        remaining = deadline - time.monotonic()
        try:
            if service_health(min(.25, max(.01, remaining)))['ready']:
                return True
        except LocalASRError:
            pass
        if remaining <= 0:
            return False
        time.sleep(min(.1, max(0, deadline - time.monotonic())))


@contextmanager
def capture_ready_guard(timeout=20):
    deadline = time.monotonic() + max(0, timeout)
    with runtime_lock(CAPTURE_LOCK_NAME) as fd:
        while True:
            ensure_ready(max(0, deadline - time.monotonic()))
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CaptureGateTimeout('local_asr_capture_gate_timeout')
                time.sleep(min(.1, remaining))
                continue
            ready = ensure_ready(0)
            if ready or time.monotonic() >= deadline:
                yield ready
                return
            fcntl.flock(fd, fcntl.LOCK_UN)


def transcribe(path, terms):
    response = _ask({'op': 'transcribe', 'path': str(Path(path).resolve()), 'terms': _terms(terms)}, SERVICE_TIMEOUT)
    if response == {'ok': False, 'error': 'local_asr_unavailable'}:
        raise ASRUnavailableError('local_asr_unavailable')
    if response.get('ok') is False and isinstance(response.get('error'), str):
        raise LocalASRError(response['error'])
    words = response.get('words')
    if response.get('ok') is not True or not isinstance(words, list):
        raise LocalASRError('local_asr_invalid_protocol')
    return words


def unreliable(segment):
    """whisper が温度を上げて言い直しても自分の基準を満たせなかった区間と、決まり文句。
    雑音を「スープを作る」「このように、私は」の繰り返しや「ご視聴ありがとうございました」にしてしまう（実録音での観測）。"""
    text = segment.get('text', '').strip()
    return (not text or segment.get('no_speech_prob', 0) > .6 or segment.get('avg_logprob', 0) < -1.0
            or segment.get('compression_ratio', 0) > 2.4 or any(phrase in text for phrase in HALLUCINATIONS))


def _word_result(result, offset, duration):
    """話し声が無ければ空を返す（雑音だけの区間は失敗ではない）。"""
    words = []
    for segment in result.get('segments', []):
        if unreliable(segment):
            continue
        if not segment.get('words'):
            raise LocalASRError('local_asr_word_timestamps_missing')
        for item in segment.get('words', []):
            text = item.get('word')
            start, end = item.get('start'), item.get('end')
            if (not isinstance(text, str) or not text.strip()
                    or not isinstance(start, (int, float)) or not isinstance(end, (int, float))
                    or not math.isfinite(start) or not math.isfinite(end)
                    or start < 0 or end < start or end + offset > duration + .1):
                raise LocalASRError('local_asr_invalid_word_timestamps')
            words.append({'start': min(duration, start + offset), 'end': min(duration, end + offset), 'text': text})
    return words


def _worker(model_path, capture_gate_fd):
    # Reserve an unredirected descriptor solely for JSONL. Silence library/native chatter.
    protocol = os.fdopen(os.dup(sys.stdout.fileno()), 'w', buffering=1)
    with open(os.devnull, 'w') as quiet:
        os.dup2(quiet.fileno(), 1)
        os.dup2(quiet.fileno(), 2)
        def send(value):
            protocol.write(json.dumps(value, ensure_ascii=False) + '\n')
            protocol.flush()
        try:
            import mlx.core as mx
            import mlx_whisper
            import numpy as np
            module = importlib.import_module('mlx_whisper.transcribe')
            module.ModelHolder.get_model(str(model_path), mx.float16)
        except Exception:
            send({'ready': False, 'error': 'local_asr_model_load_failed'})
            return 1
        send({'ready': True, 'protocol': 1})
        while True:
            line = sys.stdin.buffer.readline(MAX_PROTOCOL_BYTES + 1)
            if not line:
                return 0
            request_id = None
            try:
                if len(line) > MAX_PROTOCOL_BYTES or not line.endswith(b'\n'):
                    raise LocalASRError('local_asr_invalid_request')
                request = json.loads(line)
                request_id = request['id']
                warming = request.get('warm') is True
                if warming:
                    # A short deterministic, non-speech input exercises the same
                    # first inference without saving audio or using meeting data.
                    time_axis = np.arange(16000, dtype=np.float32) / 16000
                    audio = np.sin(2 * np.pi * 233 * time_axis) * np.sin(np.pi * time_axis) ** 2 * .03
                    terms = []
                else:
                    terms = _terms(request['terms'])
                    samples, start, end = _audio(request['path'])
                    if start == end:
                        send({'id': request_id, 'ok': True, 'words': []})
                        continue
                    audio = np.asarray(samples[start:end], dtype=np.float32) / 32768
                # 温度は既定（0.0→1.0 の言い直し）にする。0.0 固定だと言い直しが効かず、空の結果や同じ語の繰り返しがそのまま出る。
                # 言い直しても基準を満たせない区間は unreliable() で捨てる。hallucination_silence_threshold は
                # 20秒の区切りでは話し始めを落とし、雑音で40秒以上かかったので使わない（録音20区間で比較した）。
                result = mlx_whisper.transcribe(
                    audio, path_or_hf_repo=str(model_path), language='ja',
                    condition_on_previous_text=False, initial_prompt='、'.join(terms) or None,
                    word_timestamps=True, verbose=None,
                )
                if warming and capture_gate_fd is not None:
                    # The child holds the capture gate through preparation even
                    # if its supervising service exits unexpectedly.
                    os.close(capture_gate_fd)
                    capture_gate_fd = None
                words = [] if warming else _word_result(result, start / 16000, len(samples) / 16000)
                send({'id': request_id, 'ok': True, 'words': words})
            except LocalASRError as error:
                send({'id': request_id, 'ok': False, 'error': str(error)})
            except Exception:
                send({'id': request_id, 'ok': False, 'error': 'local_asr_inference_failed'})
            finally:
                # MLX は使い終えたバッファを上限（既定は実メモリ相当）まで手元に残す。音声の長さが毎回違うと
                # 使い回せずに溜まり、常駐2日で 12GB になった。1回ごとに空ける。モデルの重みは残る。
                mx.clear_cache()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--worker', action='store_true', required=True)
    parser.add_argument('--model', type=Path, default=MODEL)
    parser.add_argument('--capture-gate-fd', type=int)
    args = parser.parse_args()
    raise SystemExit(_worker(args.model, args.capture_gate_fd))
