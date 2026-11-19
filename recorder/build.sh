#!/bin/zsh
# MicaRecorder.app（画面・相手の声・自分の声を録る）を作る。出す先は第1引数（無ければ置き場のフォルダ）。
# 作り直すと macOS の許可（画面とシステムオーディオ録音・マイク）が外れることがある。
# 外れたら システム設定 > プライバシーとセキュリティ で MicaRecorder を入れ直す。
set -euo pipefail
cd "$(dirname "$0")"
OUT="${1:-${MICA_APP_SUPPORT:-$HOME/Library/Application Support/Mica}}"
APP="$OUT/MicaRecorder.app"
VERSION=0.8.0
if pgrep -x mica-recorder >/dev/null; then echo "録画中です。作り直していません"; exit 1; fi
BUILD=$(mktemp -d)
swiftc -module-cache-path /private/tmp/mica-swift-cache -O -o "$BUILD/mica-recorder" MicaRecorder.swift
rm -rf "$APP"; mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
mv "$BUILD/mica-recorder" "$APP/Contents/MacOS/mica-recorder"
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleIdentifier</key><string>app.mica.recorder</string>
<key>CFBundleName</key><string>MicaRecorder</string>
<key>CFBundleExecutable</key><string>mica-recorder</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>CFBundleShortVersionString</key><string>$VERSION</string>
<key>LSUIElement</key><true/>
<key>NSMicrophoneUsageDescription</key><string>会議の自分の声を録るため</string>
</dict></plist>
PLIST
printf '{"version": "%s", "source_sha256": "%s"}' "$VERSION" "$(shasum -a 256 MicaRecorder.swift | cut -d' ' -f1)" > "$APP/Contents/Resources/capture-build.json"
codesign --force --sign - --identifier app.mica.recorder "$APP"
echo "built $APP"
