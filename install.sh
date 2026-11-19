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
    cat > "$plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>app.mica.$name</string>
<key>ProgramArguments</key><array><string>/usr/bin/python3</string><string>$SUPPORT/service/$script</string></array>
<key>EnvironmentVariables</key><dict>
<key>PYTHONDONTWRITEBYTECODE</key><string>1</string>
<key>PATH</key><string>$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
</dict>
<key>RunAtLoad</key><true/>
<key>KeepAlive</key><true/>
<key>ThrottleInterval</key><integer>$throttle</integer>
<key>StandardOutPath</key><string>$SUPPORT/logs/$name.log</string>
<key>StandardErrorPath</key><string>$SUPPORT/logs/$name.log</string>
</dict></plist>
PLIST
    launchctl bootout "gui/$(id -u)/app.mica.$name" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$plist"
  done
  echo "入りました。最初の会議で、MicaRecorder に「画面とシステムオーディオ録音」と「マイク」の許可を求める窓が出ます。"
else
  echo "入りました（常駐は登録していません）。"
fi
