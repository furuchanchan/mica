#!/bin/zsh
# Mica.app を作って入れ替える。作れなかったら、入っているアプリには触らない。
# ビルドが落ちたのに入れ替えを進めてアプリを消さないよう、手で rm と cp を並べず、必ずこれを使う。
# 入れ先は ~/Applications/Mica.app。環境変数 MICA_APP_DIR で入れ先のフォルダを替えられる。
set -euo pipefail
cd "$(dirname "$0")"
APP="${MICA_APP_DIR:-$HOME/Applications}/Mica.app"
if [ -d "$APP" ]; then
  found=$(/usr/libexec/PlistBuddy -c "Print :CFBundleIdentifier" "$APP/Contents/Info.plist")
  [ "$found" = app.mica.assistant ] || { echo "$APP は別のアプリ（$found）です。上書きしません"; exit 1; }
fi
python3 build.py > build.log 2>&1 || { tail -5 build.log; echo "ビルド失敗。入れ替えていません"; exit 1; }
NEW=$(python3 -c "import json;print(json.load(open('build.json'))['app'])")
[ -x "$NEW/Contents/MacOS/Mica" ] || { echo "作ったアプリが見つかりません。入れ替えていません"; exit 1; }
codesign --verify --deep --strict "$NEW"
U=http://127.0.0.1:${MICA_APP_PORT:-18770}
if curl -s -m 5 -H "Origin: $U" $U/api/state | python3 -c "import json,sys;d=json.load(sys.stdin);sys.exit(0 if (d.get('recording') or d.get('capture_active')) else 1)" 2>/dev/null; then
  echo "録音中です。入れ替えていません"; exit 1
fi
OLD=$(mktemp -d)/Mica-old.app
if [ -d "$APP" ]; then
  osascript -e 'tell application id "app.mica.assistant" to quit' 2>/dev/null || true; sleep 2
  pkill -f "$APP/Contents/Resources/mica_service.py" 2>/dev/null || true; sleep 1
  mv "$APP" "$OLD"
fi
mkdir -p "$(dirname "$APP")"
if cp -R "$NEW" "$APP" && codesign --verify --deep --strict "$APP"; then rm -rf "$OLD" "$NEW"; else rm -rf "$APP"; [ -d "$OLD" ] && mv "$OLD" "$APP"; echo "入れ替え失敗。元に戻しました"; exit 1; fi
# 置き場やポートを替えて入れた時は、アプリにも同じ値を渡す（open は今の環境変数を引き継がない）
ENVS=()
for name in MICA_APP_SUPPORT MICA_APP_PORT MICA_LIVE_PORT MICA_CONFIG MICA_NO_CLOUD; do
  [ -n "${(P)name:-}" ] && ENVS+=(--env "$name=${(P)name}")
done
open -g "${ENVS[@]}" "$APP"; sleep 5
curl -s -o /dev/null -m 5 -w "installed・service %{http_code}\n" -H "Origin: $U" $U/api/state
