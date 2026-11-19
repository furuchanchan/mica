#!/bin/zsh
# Mica の常駐とアプリを外す。録音・文字起こし（既定 ~/Movies/Mica）と設定（~/.config/mica）には触らない。
#   ./uninstall.sh            常駐・Mica.app・録画アプリ・サービスを外す
#   ./uninstall.sh --runtimes 音声認識と話者分けの実行環境（約2GB）も消す
set -euo pipefail
RUNTIMES=0
for arg in "$@"; do
  case "$arg" in
    --runtimes) RUNTIMES=1 ;;
    *) echo "知らない引数です: $arg"; exit 1 ;;
  esac
done
SUPPORT="${MICA_APP_SUPPORT:-$HOME/Library/Application Support/Mica}"
APP="${MICA_APP_DIR:-$HOME/Applications}/Mica.app"
if pgrep -x mica-recorder >/dev/null; then echo "録画中です。外していません"; exit 1; fi
for name in watch asr; do
  launchctl bootout "gui/$(id -u)/app.mica.$name" 2>/dev/null || true
  rm -f "$HOME/Library/LaunchAgents/app.mica.$name.plist"
done
if [ -d "$APP" ] && [ "$(/usr/libexec/PlistBuddy -c "Print :CFBundleIdentifier" "$APP/Contents/Info.plist")" = app.mica.assistant ]; then
  osascript -e 'tell application id "app.mica.assistant" to quit' 2>/dev/null || true
  rm -rf "$APP"
fi
rm -rf "$SUPPORT/MicaRecorder.app" "$SUPPORT/service"
if [ "$RUNTIMES" = 1 ]; then rm -rf "$SUPPORT/asr-runtime" "$SUPPORT/speaker-runtime"; fi
echo "外しました。録音は残っています。"
