# Mica が使う他者の部品とモデル

このリポジトリには、下の部品もモデルも入っていません。`setup/install_runtimes.py` が、使う人の Mac へ配布元から直接取ります。
ライセンスは 2026-11-19 に配布元で確かめました。

| 物 | 使う所 | ライセンス | 配布元 |
|---|---|---|---|
| mlx-whisper 0.4.3（Apple MLX examples） | Mac の中の文字起こし | MIT | https://github.com/ml-explore/mlx-examples |
| whisper-large-v3-turbo（OpenAI。MLX 版は mlx-community、リビジョン a4aaeec0） | 同上のモデル | MIT | https://huggingface.co/mlx-community/whisper-large-v3-turbo |
| sherpa-onnx 2.0.1（k2-fsa） | 話者分けの実行 | Apache-2.0 | https://github.com/k2-fsa/sherpa-onnx |
| WeSpeaker ResNet34-LM（wenet-e2e。VoxCeleb で学習） | 話者の声の特徴 | CC BY 4.0 | https://github.com/wenet-e2e/wespeaker/blob/master/docs/pretrained.md |
| pyannote/segmentation-3.0（Copyright (c) 2022 CNRS。ONNX 版は k2-fsa） | 声の重なりの検出 | MIT | https://huggingface.co/pyannote/segmentation-3.0 |
| NumPy 2.5.4 | 数値の計算 | BSD-3-Clause | https://github.com/numpy/numpy |

mlx-whisper が連れてくる部品（PyTorch、SciPy、Numba、tiktoken、huggingface-hub など）の版は `setup/asr-requirements.lock` にあります。
それぞれのライセンスは、入れた後の `asr-runtime/.venv` の中の各パッケージに付いています。

WeSpeaker のモデルは CC BY 4.0 です。表示：WeSpeaker（wenet-e2e）、VoxCeleb ResNet34-LM。ONNX 版の配布は k2-fsa / sherpa-onnx。変更はしていません。
