#!/bin/sh
# Builds Claudemon.app next to this script.
set -e
cd "$(dirname "$0")"

APP=Claudemon.app
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS"
swiftc -O claudemon.swift -o "$APP/Contents/MacOS/claudemon" -framework AppKit

cat > "$APP/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>Claudemon</string>
  <key>CFBundleIdentifier</key><string>local.claudemon</string>
  <key>CFBundleExecutable</key><string>claudemon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>LSUIElement</key><true/>
  <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
EOF

codesign --force --sign - "$APP" >/dev/null 2>&1 || true
echo "built $(pwd)/$APP — run: open $APP"
