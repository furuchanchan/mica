# 変更の記録

## 0.12.1（2026-11-19）

全体を点検して直した版です。

- 会議後の分析で、Claude Code に道具を持たせない形にした。文字起こしと画面の静止画は Mica が渡す。0.12.0 までは、会議の発言の中身しだいで Claude が録音のフォルダの外を読み書きできた
- 設定 `delivery.knowledge_base.search_command`（分析の間にコマンドで過去の文脈を引く）を無くした。書いてあると、起動時に消すよう言って止まる
- つないだ先の転送（リダイレクト）に従わないようにした。この Mac の中の宛先へは、プロキシを通さない
- `MICA_NO_CLOUD=1` で、カレンダー・Google ドライブ・Slack への通信も止めるようにした
- `./mica connect` は、試しの呼び出しが通ってから設定を保存するようにした。`./mica doctor` は、入っているサービスが古い時に知らせる
- 文字起こしの2重確認も、設定 `ai.providers.openai` の URL と鍵に従うようにした
- 録画から文字に起こし直す時に、入れてある音声認識をオフラインで使うようにした（0.12.0 までは `uvx` で取り直すことがあった）
- 分析の完了の記録が無い録画は、Google ドライブへ上げた後も Mac から消さないようにした
- `install.sh` は、常駐に置き場とポートの設定を引き継ぎ、アプリのサービスが起きるまで待って確かめる
- 設定ファイルの型の誤り（文字列の所に真偽値など）を、起動時に知らせるようにした
- 自動の試験（GitHub Actions）と、不具合の報告の型を足した

## 0.12.0（2026-11-19）

- つなぎ先を選べるようにした。設定 `ai.advice`・`ai.analysis` に `名前/モデル` を書く。先は OpenAI・Anthropic・Gemini・OpenRouter・Groq・Ollama・LM Studio・OpenAI 互換のサーバー・Claude Code
- `./mica status`・`connect`・`disconnect`・`doctor` を足した
- 会議後の分析を、API とこの Mac の LLM でも動くようにした
- 送るのは設定 `ai` に書いた先だけにした。鍵が Mac にあるだけでは送らない
- 設定ファイルの書き換えを、常駐が起こし直さずに読み直すようにした

上げる時に変わる所

- 設定 `analysis.engine` は `ai.analysis` に替わった。`{"analysis": {"engine": "claude_cli"}}` は `{"ai": {"analysis": "claude_cli"}}` と書く
- OpenAI の鍵があるだけでは助言が動かなくなった。`./mica connect openai` でつなぐ
- `git pull` の後は `./install.sh` を動かし直す

## 0.11.3（2026-11-19）

- 最初の公開
