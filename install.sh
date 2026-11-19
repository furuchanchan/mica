#!/bin/zsh
# Mica を入れる。何度動かしてもよい（入っている物は飛ばし、アプリと常駐は入れ替える）。
#   ./install.sh              全部入れて、常駐（見張り役・音声認識）を起こす
#   ./install.sh --no-agents  常駐を登録しない（入っている Mica と別にもう1つ試す時。README の「開発」）
# 置き場は環境変数で替えられる：MICA_APP_SUPPORT（既定 ~/Library/Application Support/Mica）、MICA_APP_DIR（既定 ~/Applications）
set -euo pipefail
cd "$(dirname "$0")"
AGENTS=1
for arg in "$@"; do
  case "$arg" in
    --no-agents) AGENTS=0 ;;
    *) echo "知らない引数です: $arg"; exit 1 ;;
  esac
done
SUPPORT="${MICA_APP_SUPPORT:-$HOME/Library/Application Support/Mica}"
APP="${MICA_APP_DIR:-$HOME/Applications}/Mica.app"

[ "$(uname -sm)" = "Darwin arm64" ] || { echo "Apple シリコンの Mac が要ります"; exit 1; }
[ "$(sw_vers -productVersion | cut -d. -f1)" -ge 26 ] || { echo "macOS 26 以上が要ります"; exit 1; }
xcrun --find swiftc >/dev/null 2>&1 || { echo "Xcode のコマンドラインツールが使えません。xcode-select --install で入れてから、もう一度実行してください"; exit 1; }
if pgrep -x mica-recorder >/dev/null; then
  echo "録音中です。入れ替えていません。録音が終わってから、もう一度実行してください"; exit 1
fi
for tool in swiftc uv ffmpeg; do
  command -v $tool >/dev/null || { echo "$tool が見つかりません。README の「要る物」を入れてください"; exit 1; }
done
# 同じ名前の別のアプリを上書きしない
if [ -d "$APP" ]; then
  found=$(/usr/libexec/PlistBuddy -c "Print :CFBundleIdentifier" "$APP/Contents/Info.plist")
  [ "$found" = app.mica.assistant ] || { echo "$APP は別のアプリ（$found）です。上書きしません。MICA_APP_DIR で入れ先を替えてください"; exit 1; }
fi

echo "1/4 音声認識と話者分けの実行環境"
/usr/bin/python3 setup/install_runtimes.py
echo "2/4 録画アプリ"
recorder/build.sh "$SUPPORT"
echo "3/4 サービス"
mkdir -p "$SUPPORT/service" "$SUPPORT/logs"
cp service/*.py service/live.html "$SUPPORT/service/"
echo "4/4 Mica.app"
app/install.sh

if [ $AGENTS = 1 ]; then
  mkdir -p "$HOME/Library/LaunchAgents"
  for agent in watch:shodan_watch.py:60 asr:shodan_asr_service.py:10; do
    name=${agent%%:*}; rest=${agent#*:}; script=${rest%%:*}; throttle=${rest#*:}
    plist="$HOME/Library/LaunchAgents/app.mica.$name.plist"
    /usr/bin/python3 - "$plist" "$name" "$SUPPORT" "$script" "$throttle" <<'PY'
import os
from pathlib import Path
import plistlib
import sys

output, name, support, script, throttle = sys.argv[1:]
env = {
    'PYTHONDONTWRITEBYTECODE': '1',
    'PATH': str(Path.home() / '.local/bin') + ':/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin',
    'MICA_APP_SUPPORT': support,
}
for key in ('MICA_CONFIG', 'MICA_APP_DIR', 'MICA_APP_PORT', 'MICA_LIVE_PORT', 'MICA_NO_CLOUD'):
    if key in os.environ:
        env[key] = os.environ[key]
value = {
    'Label': 'app.mica.' + name,
    'ProgramArguments': ['/usr/bin/python3', str(Path(support) / 'service' / script)],
    'EnvironmentVariables': env, 'RunAtLoad': True, 'KeepAlive': True,
    'ThrottleInterval': int(throttle),
    'StandardOutPath': str(Path(support) / 'logs' / (name + '.log')),
    'StandardErrorPath': str(Path(support) / 'logs' / (name + '.log')),
}
with open(output, 'wb') as destination:
    plistlib.dump(value, destination)
PY
    launchctl bootout "gui/$(id -u)/app.mica.$name" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$plist"
  done
  echo "入りました。最初の会議で、MicaRecorder に「画面とシステムオーディオ録音」と「マイク」の許可を求める窓が出ます。"
else
  echo "入りました（常駐は登録していません）。"
fi
echo "今の状態は ./mica status、AI をつなぐ時は ./mica connect です（つながなくても、録音と文字起こしは動きます）。"
