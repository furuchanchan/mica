#!/usr/bin/env python3
"""Mac の中で動く音声認識と話者分けの実行環境を入れる。鍵もアカウントも要らない。

入れる物（置き場は service/mica_config.py の app_support()。既定 ~/Library/Application Support/Mica）:
  asr-runtime/      mlx-whisper の venv（版は setup/asr-requirements.lock で固定）
  speaker-runtime/  sherpa-onnx の venv と、話者のモデル2つ（ハッシュで確かめる）
  ~/.cache/huggingface/hub  whisper-large-v3-turbo（約1.6GB。リビジョン固定）
何度動かしてもよい。入っている物は飛ばす。
"""
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / 'service'))
import mica_config
from shodan_local_asr import MODEL, MODEL_REVISION

SHERPA = 'https://github.com/k2-fsa/sherpa-onnx/releases/download/'
SPEAKER_MODELS = (
    # (置く名前, 取る先, 取った物の中の名前（tar の時）, sha256)
    ('wespeaker_en_voxceleb_resnet34_LM.onnx', SHERPA + 'speaker-recongition-models/wespeaker_en_voxceleb_resnet34_LM.onnx',
     None, 'e9848563da86f263117134dfd7ad63c92355b37de492b55e325400c9d9c39012'),
    ('pyannote-segmentation-3.0.onnx', SHERPA + 'speaker-segmentation-models/sherpa-onnx-pyannote-segmentation-3-0.tar.bz2',
     'sherpa-onnx-pyannote-segmentation-3-0/model.onnx', '220ad67ca923bef2fa91f2390c786097bf305bceb5e261d4af67b38e938e1079'),
)


def sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as source:
        for block in iter(lambda: source.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def clean_env(cache):
    env = {name: value for name, value in os.environ.items() if not name.startswith(('UV_', 'PIP_'))}
    env.update(UV_CACHE_DIR=str(cache), HF_HUB_DISABLE_IMPLICIT_TOKEN='1', PYTHONDONTWRITEBYTECODE='1')
    return env


def venv(uv, folder, env):
    python = folder / '.venv/bin/python'
    if not python.is_file():
        subprocess.run([uv, '--no-config', 'venv', '--python', '3.12', str(folder / '.venv')], env=env, check=True)
    return python


def install_asr(uv, root):
    runtime = root / 'asr-runtime'
    runtime.mkdir(parents=True, exist_ok=True)
    os.chmod(runtime, 0o700)
    env = clean_env(runtime / 'uv-cache')
    python = venv(uv, runtime, env)
    shutil.copyfile(HERE / 'asr-requirements.lock', runtime / 'requirements.lock')
    subprocess.run([uv, '--no-config', 'pip', 'install', '--python', str(python), '--only-binary', ':all:', '--no-deps',
                    '--index-url', 'https://pypi.org/simple', '-r', str(runtime / 'requirements.lock')], env=env, check=True)
    if not (MODEL / 'weights.safetensors').is_file() or not (MODEL / 'config.json').is_file():
        print('whisper-large-v3-turbo を取ります（約1.6GB）', flush=True)
        # 公開のモデルをリビジョン固定で取る。Hugging Face の認証は使わない
        script = ("import sys\nfrom huggingface_hub import snapshot_download\n"
                  "snapshot_download('mlx-community/whisper-large-v3-turbo', revision=sys.argv[2], cache_dir=sys.argv[1], "
                  "token=False, allow_patterns=['config.json', 'weights.safetensors', 'README.md'])")
        subprocess.run([str(python), '-c', script, str(MODEL.parents[2]), MODEL_REVISION], env=env, check=True)
    if not (MODEL / 'weights.safetensors').is_file():
        raise SystemExit(f'音声認識のモデルが {MODEL} にありません')
    check = "import importlib.metadata as m,json;print(json.dumps({n:m.version(n) for n in ['mlx-whisper','mlx','numpy','torch']}))"
    versions = json.loads(subprocess.check_output([str(python), '-c', check], env=env, text=True))
    (runtime / 'installed.json').write_text(json.dumps(
        {'runtime_python': str(python), 'model_revision': MODEL_REVISION, 'versions': versions}, indent=2) + '\n')
    return versions


def install_speaker(uv, root):
    runtime = root / 'speaker-runtime'
    runtime.mkdir(parents=True, exist_ok=True)
    env = clean_env(runtime / 'uv-cache')
    python = venv(uv, runtime, env)
    subprocess.run([uv, '--no-config', 'pip', 'install', '--python', str(python), '--index-url', 'https://pypi.org/simple',
                    'sherpa-onnx==2.0.1', 'numpy==2.5.4'], env=env, check=True)
    for name, url, member, expected in SPEAKER_MODELS:
        path = runtime / name
        if path.is_file() and sha256(path) == expected:
            continue
        print(f'{name} を取ります', flush=True)
        with tempfile.TemporaryDirectory(dir=runtime) as work:
            download = Path(work) / 'download'
            with urllib.request.urlopen(url, timeout=120) as response, download.open('wb') as out:
                shutil.copyfileobj(response, out)
            if member:
                with tarfile.open(download) as archive:
                    source = archive.extractfile(member)
                    if source is None:
                        raise SystemExit(f'{url} の中に {member} がありません')
                    extracted = Path(work) / 'model.onnx'
                    with extracted.open('wb') as out:
                        shutil.copyfileobj(source, out)
                download = extracted
            if sha256(download) != expected:
                raise SystemExit(f'{name} のハッシュが一致しません。入れていません')
            download.replace(path)
    shutil.copyfile(HERE.parent / 'THIRD_PARTY_NOTICES.md', runtime / 'THIRD_PARTY_NOTICES.md')


def main():
    if sys.platform != 'darwin' or platform.machine() != 'arm64':
        raise SystemExit('Apple シリコンの Mac が要ります')
    uv = shutil.which('uv')
    if not uv:
        raise SystemExit('uv が見つかりません。入れてください: brew install uv')
    root = mica_config.app_support()
    versions = install_asr(uv, root)
    install_speaker(uv, root)
    print(json.dumps({'app_support': str(root), 'asr': versions, 'speaker': 'ok'}, ensure_ascii=False))


if __name__ == '__main__':
    main()
