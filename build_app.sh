#!/bin/bash
# Builds the native Deployer.app into dist/.
#   ./build_app.sh            — build into dist/
#   ./build_app.sh --install  — build and install into /Applications (replacing the old version)
#   ./build_app.sh --release  — build a universal (Apple Silicon + Intel) zip for GitHub Releases
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

echo "→ Compiling (arm64 + x86_64)…"
BIN="$(mktemp -d)"
for arch in arm64 x86_64; do
  swiftc -O -swift-version 5 -target "$arch-apple-macos12" macos/main.swift -o "$BIN/$arch" \
    -framework Cocoa -framework WebKit 2>&1 | grep -v "warning:" | grep -vE "^\s*(\||[0-9]+ \|)" || true
done
if [ -x "$BIN/arm64" ] && [ -x "$BIN/x86_64" ]; then
  lipo -create "$BIN/arm64" "$BIN/x86_64" -output "$APP/Contents/MacOS/Deployer"
elif [ -x "$BIN/arm64" ] || [ -x "$BIN/x86_64" ]; then
  cp "$BIN/$( [ -x "$BIN/arm64" ] && echo arm64 || echo x86_64 )" "$APP/Contents/MacOS/Deployer"
  echo "⚠️  Built for one architecture only"
fi
rm -rf "$BIN"
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

if [ "${1:-}" = "--release" ]; then
  ZIP="dist/Deployer-${VERSION}-macOS.zip"
  rm -f "$ZIP"
  ditto -c -k --norsrc --noextattr --keepParent "$APP" "$ZIP"
  echo "✅ Release archive: $(pwd)/$ZIP"
  shasum -a 256 "$ZIP"
fi

if [ "${1:-}" = "--install" ]; then
  osascript -e 'tell application id "local.deployer" to quit' >/dev/null 2>&1 || true
  sleep 1
  rm -rf /Applications/Deployer.app
  cp -R "$APP" /Applications/
  rm -rf dist
  echo "✅ Installed: /Applications/Deployer.app"
fi
