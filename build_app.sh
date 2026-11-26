#!/bin/bash
# Builds the native Deployer.app into dist/.
#   ./build_app.sh            — build into dist/
#   ./build_app.sh --install  — build and install into /Applications (replacing the old version)
# Requires the Xcode Command Line Tools (swiftc, python3):  xcode-select --install
set -euo pipefail
cd "$(dirname "$0")"
APP="dist/Deployer.app"
VERSION="$(cat VERSION 2>/dev/null || echo 1.0.0)"

command -v swiftc >/dev/null || { echo "swiftc not found. Install the Command Line Tools: xcode-select --install"; exit 1; }

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp -R src/app.py src/askpass.py src/index.html src/remote_deploy.sh src/locales "$APP/Contents/Resources/"
chmod +x "$APP/Contents/Resources/askpass.py"

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Deployer</string>
  <key>CFBundleDisplayName</key><string>Deployer</string>
  <key>CFBundleIdentifier</key><string>local.deployer</string>
  <key>CFBundleVersion</key><string>${VERSION}</string>
  <key>CFBundleShortVersionString</key><string>${VERSION}</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>Deployer</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>LSMinimumSystemVersion</key><string>12.0</string>
  <key>NSPrincipalClass</key><string>NSApplication</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSAppTransportSecurity</key><dict><key>NSAllowsLocalNetworking</key><true/></dict>
</dict></plist>
PLIST

echo "→ Compiling…"
swiftc -O -swift-version 5 macos/main.swift -o "$APP/Contents/MacOS/Deployer" \
  -framework Cocoa -framework WebKit 2>&1 | grep -v "warning:" | grep -vE "^\s*(\||[0-9]+ \|)" || true
[ -x "$APP/Contents/MacOS/Deployer" ] || { echo "❌ Compilation failed"; exit 1; }

# App icon (drawn with AppKit)
TMP=$(mktemp -d)
mkdir -p "$TMP/AppIcon.iconset"
if swift macos/make_icon.swift "$TMP/AppIcon.iconset" >/dev/null 2>&1 \
   && iconutil -c icns "$TMP/AppIcon.iconset" -o "$APP/Contents/Resources/AppIcon.icns" 2>/dev/null; then
  echo "→ Icon created"
fi
rm -rf "$TMP"

codesign --force --deep -s - "$APP" >/dev/null 2>&1 || true
echo "✅ Built: $(pwd)/$APP"

if [ "${1:-}" = "--install" ]; then
  osascript -e 'tell application id "local.deployer" to quit' >/dev/null 2>&1 || true
  sleep 1
  rm -rf /Applications/Deployer.app
  cp -R "$APP" /Applications/
  rm -rf dist
  echo "✅ Installed: /Applications/Deployer.app"
fi
