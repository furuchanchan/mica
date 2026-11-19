# Mica

Mac の会議アシスタントです。会議が始まると自動で録り、Mac の中で文字に起こし、終わると記録を残します。
鍵もアカウントも無しで動きます。この Mac の LLM（Ollama など）や、OpenAI・Claude・Gemini などの鍵をつなぐと、つないだ分だけ機能が増えます。

![鍵なしで会議が終わった後の画面](docs/after-meeting-no-key.png)

## できること

| 段 | つなぐ物 | 増える機能 |
|---|---|---|
| 1 | なし | 録音と録画、Mac の中の文字起こし、自分と相手の区別、記録の保存 |
| 2 | 会議中の AI（設定 `ai.advice`） | 会議中の助言と質問、会議前の目的の整理 |
| 3 | 会議後の AI（設定 `ai.analysis`） | 会議後の分析（結論・決まったこと・次やること。発言の引用つき） |

- 段2と段3は別々につなげます。片方だけでも、別々の会社でも構いません。
- つなぐ先はこの Mac の LLM（Ollama・LM Studio）、各社の API の鍵（OpenAI・Anthropic・Gemini・OpenRouter・Groq）、Claude Code、OpenAI 互換のサーバーから選べます。
- Zoom・Google Meet・Teams・Slack ハドルなど、アプリがマイクを使い始めると録り始めます。相手が主催の会議も録れます。
- 対面の会議は Mica の［録音する］で録ります。
- 段1では音声も文字も Mac の外へ出ません。この Mac の LLM だけをつないだ時も同じです（下のカレンダー・外部への保存や配信を別にオンにした場合を除く）。

## 要る物

- Apple シリコンの Mac、macOS 26 以上
- Xcode のコマンドラインツール（`xcode-select --install`）
- [uv](https://docs.astral.sh/uv/) と ffmpeg（`brew install uv ffmpeg`）
- 空き容量 約3GB（音声認識のモデル 約1.6GB を含む）

## 入れ方

```bash
git clone https://github.com/furuchanchan/mica.git
cd mica
./install.sh
```

`install.sh` は次の4つを入れ、常駐を2つ起こします。何度動かしても構いません。

1. 音声認識と話者分けの実行環境（`~/Library/Application Support/Mica` の下。モデルは配布元から取ります）
2. 録画アプリ `MicaRecorder.app`
3. サービス（見張り役と音声認識）
4. `~/Applications/Mica.app`

最初の会議で、macOS が `MicaRecorder` に「画面とシステムオーディオ録音」と「マイク」の許可を求めます。許可すると次から録れます。
手元でビルドするので、アプリを作り直すと許可が外れることがあります。外れたら システム設定 > プライバシーとセキュリティ で入れ直してください。

入った物を確かめるには、合成した会話で通しの試験を動かします（外へは何も送りません）。

```bash
python3 tests/selftest.py
```

つないだ AI で会議後の分析まで確かめる時は `--connected` を付けます。合成の会話がつないだ先へ送られます。

外す時は `./uninstall.sh`。録音と設定は残ります。

## AI をつなぐ

```bash
./mica connect
```

聞かれた順に、つなぐ先・モデル・使い道（会議中の助言、会議後の分析、両方）を答えます。鍵は Keychain に入り、設定ファイルには入りません。
API・この Mac の LLM は、試しの呼び出しが通ってから設定を保存します。Claude Code と文字起こしの2重確認は、つなぐ時には呼び出しを試しません。開いている Mica は次の呼び出しから新しいつなぎ先を使います。

| 名前 | つなぐ物 | 鍵の環境変数 |
|---|---|---|
| `ollama` | この Mac の [Ollama](https://ollama.com)。Mac の外へ送らない | なし |
| `lmstudio` | この Mac の [LM Studio](https://lmstudio.ai)。Mac の外へ送らない | なし |
| `openai` | OpenAI の API | `OPENAI_API_KEY` |
| `anthropic` | Anthropic（Claude）の API | `ANTHROPIC_API_KEY` |
| `gemini` | Google Gemini の API | `GEMINI_API_KEY` |
| `openrouter` | OpenRouter | `OPENROUTER_API_KEY` |
| `groq` | Groq | `GROQ_API_KEY` |
| `claude_cli` | [Claude Code](https://claude.com/claude-code)（Claude の契約か API の鍵）。会議後の分析だけ | なし（下の「Claude Code の認証」） |
| 自分で付ける名前 | OpenAI 互換のサーバー（vLLM、LiteLLM のプロキシ、社内のサーバーなど） | 自分で決める |

鍵は表の環境変数、Keychain の `<名前>-api-key` の順に探します。

聞かれずにつなぐ時は、引数で渡します。

```bash
./mica connect ollama --model gemma3:4b --advice --analysis    # この Mac の中だけで全部動かす
./mica connect openai --model gpt-6-luna --advice              # 会議中の助言を OpenAI に
./mica connect claude_cli                                      # 会議後の分析を Claude Code に
./mica connect myserver --base-url http://192.168.1.10:8000/v1 --model my-model --analysis
```

今の状態を見る時と、外す時は次のとおりです。

```bash
./mica status            # 今どの段まで使えるか、Mac の外のどこへ送るか
./mica doctor            # 入っている物を確かめ、つないだ先を実際に1回ずつ試す
./mica disconnect all    # advice・analysis・check のどれかだけも外せる
```

### 送る先の決まり

- AI に送るのは、設定 `ai` に書いた先だけです。鍵が Mac にあるだけでは何も送りません。カレンダー・外部への保存や配信は、それぞれの設定をオンにした時だけ使います。
- つながらない時に、別の会社やクラウドへ切り替えません。失敗は画面に理由を出します。
- 環境変数 `MICA_NO_CLOUD=1` を付けると、設定に書いてあっても、この Mac の中のサーバーにだけ送ります。カレンダー・外部への保存や配信も止めます。
- つないだ先が別の URL へ転送（リダイレクト）しても従いません。設定には転送の無い URL を書いてください。
- 段2では会議の文字起こしが、段3では文字起こしが、つないだ先へ送られます。`claude_cli` の分析では画面の静止画も送られます。

### 選び方の目安

- Mac の外へ出したくない時は `ollama` か `lmstudio` を選びます。小さいモデル（4B 前後）では、分析で選ばれる発言が少なく、提案も薄くなります。
- 分析の質を取りたい時は `claude_cli`・`anthropic`・`openai` を選びます。分析の間に画面の静止画を読むのは `claude_cli` だけです。
- 分析は、AI が発言の番号を選び、Mica が録音の原文をそのまま引用して組み立てます。AI が無い番号を返した時は、分析を出さずに止めます。
- 文字起こしの誤りを減らしたい時は、設定 `ai.transcription_check` を `openai` にします（`./mica connect openai --transcription-check`）。会議中の音声が OpenAI へ送られ、Mac の中の認識と比べます。`ai.providers.openai` を書いた時は、その URL と鍵の設定を使います。音声の API に対応した先が必要です。

### Claude Code の認証

`claude_cli` の分析は `claude -p` で動きます。認証は次の順に探します。

1. Keychain の `claude-code-oauth-token`（`claude setup-token` で作る長期トークン。Claude の契約で動かす時）
2. Keychain の `anthropic-api-key`（API の鍵で動かす時）
3. Claude Code の通常のログイン

会議の発言は他人の言葉なので、分析の間、Claude に道具（ファイル・コマンド・MCP）を持たせません。Mica が文字起こしと画面の静止画を渡し、返った JSON を確かめて保存します。Claude Code の個人とプロジェクトの設定やフックも読み込みません。

## 設定

設定ファイルは `~/.config/mica/config.json` です（環境変数 `MICA_CONFIG` で場所を替えられます）。無ければ全部既定で動きます。
書いた項目だけが既定を上書きします。知らない項目や型の違う値があると、原因の分かる文で止まります。見本は `config.example.json`。

| 項目 | 既定 | 内容 |
|---|---|---|
| `data_root` | `~/Movies/Mica` | 録画・文字起こし・分析の置き場 |
| `ffmpeg` | PATH から探す | ffmpeg の場所 |
| `user_name` | `自分` | 分析の文で自分を指す呼び名 |
| `ignore_mic_apps` | `[]` | マイクを使っても会議と見なさないアプリの bundle id（前方一致）。音声入力アプリなど |
| `ai.advice` | null | 会議中の助言と質問に使う先。`名前/モデル`（例 `openai/gpt-6-luna`、`ollama/gemma3:4b`） |
| `ai.analysis` | null | 会議後の分析に使う先。`claude_cli`（`claude_cli/モデル名` も可）か `名前/モデル`。null は文字起こしの保存まで |
| `ai.transcription_check` | null | `openai` で、会議中の音声を OpenAI でも文字に起こし、Mac の中の認識と比べる |
| `ai.providers` | `{}` | 自分で足す OpenAI 互換のサーバー。`{"名前": {"base_url": "http://…/v1", "api_key_env": "環境変数の名前"}}`。組み込みの名前で書くと、その URL を上書きする |
| `calendar.enabled` | false | Google カレンダーを読む（会議の題、目的の準備、予定の時刻での対面の録音） |
| `calendar.account` / `.calendars` / `.personal_calendar` | null / `["primary"]` / null | 読むアカウント、読む暦、対面でも自動で録らない暦 |
| `google_credentials_dir` | `~/.google_workspace_mcp/credentials` | `<アカウント>.json`（client_id・client_secret・refresh_token）の置き場 |
| `delivery.drive.*` | オフ | 動画を Google ドライブへ上げ、内容の一致を確かめてから Mac の動画を消す |
| `delivery.slack.*` | オフ | 分析と失敗を Slack へ届ける。`command` は argv の配列で、本文を標準入力に受け取る |
| `delivery.knowledge_base.*` | オフ | 分析ページを自分の知識ベースへ入れる（`ingest_command`） |
| `context.*` | オフ | 過去の Claude Code の会話から、会議の背景を探す |
| `negotiation_notes` | null | 商談の技の台帳（Markdown）。あると、商談の分析で進め方を採点する |

`ai` の4項目は `./mica connect` が書きます。手で書いても構いません。
`delivery.*` と `negotiation_notes` は、`ai.analysis` を書いた時だけ使えます。

## 録音について

会議を録る前に、相手に録音することを伝えてください。録った物は `data_root` の下に会議ごとのフォルダで残り、Mica は自分では消しません（`delivery.drive` をオンにした時の動画を除く）。

## 仕組み

- `recorder/` 録画アプリ（Swift）。画面、相手の声（Mac から出る音）、自分の声（マイク）を別々に録り、5秒ごとの音声を文字起こしへ渡す
- `service/shodan_watch.py` 見張り役。マイクを使うアプリを見て録画を始め、終わったら会議後の処理を起こす
- `service/shodan_asr_service.py` 音声認識の常駐（mlx-whisper の whisper-large-v3-turbo）
- `service/shodan_live.py` 会議中の文字起こしと、画面が読むサーバー
- `service/shodan_analyze.py` 会議後の文字起こしの保存と分析
- `service/mica_llm.py` 外の AI と、この Mac の LLM を呼ぶ所（OpenAI の Responses API、Anthropic の Messages API、OpenAI 互換の Chat Completions）
- `mica` つなぎ先を見る・つなぐ・確かめる道具
- `app/` Mica.app（SwiftUI）
- 常駐のログは `~/Library/Application Support/Mica/logs/` に出ます

## 開発

入っている Mica と別にもう1つ動かす時は、置き場とポートを環境変数で分けます。

```bash
export MICA_APP_SUPPORT=~/mica-dev/support MICA_APP_DIR=~/mica-dev/apps MICA_CONFIG=~/mica-dev/config.json
export MICA_APP_PORT=18870 MICA_LIVE_PORT=18868
./install.sh --no-agents
/usr/bin/python3 "$MICA_APP_SUPPORT/service/shodan_asr_service.py" &   # 音声認識の常駐を手で起こす
python3 tests/selftest.py
```

試験は `service/test_*.py`（`python3 service/test_llm.py` など）と `tests/selftest.py`。`service/test_*.py` は通信せずに確かめます。

## 既知の制約

- 文字起こしは日本語だけです。
- Mac の中の認識は、API の認識より誤りが多くなります。数字や固有名詞は録音で確かめてください。
- 話者分けは推定です。相手が複数いると取り違えることがあります。
- つなぎ先のうち、実物で確かめたのは Ollama・OpenAI・Claude Code です。Anthropic の API・Gemini・OpenRouter・Groq・LM Studio は、各社が公開している仕様に合わせて書いてあり、実際の接続はまだ確かめていません。動かない時は Issue で知らせてください。
- 0.11 の設定 `analysis.engine` は `ai.analysis` に替わりました。0.11 では OpenAI の鍵があるだけで助言が動きましたが、0.12 からは `./mica connect openai` でつないだ時だけ動きます。
- 無保証です。macOS の更新で動かなくなることがあります。

## ライセンス

MIT（`LICENSE`）。使っている部品とモデルのライセンスは `THIRD_PARTY_NOTICES.md` にあります。
