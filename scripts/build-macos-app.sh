#!/usr/bin/env bash
# Build a macOS .app bundle (and, when possible, a .dmg) for BedrockOnLinux.
#
# Like the Linux artifacts, the heavy components — the macOS Wine backend
# (Game Porting Toolkit / CrossOver / Wine) and the game itself — are detected
# or fetched at first run, so the bundle stays tiny: it ships only the launcher
# and the bol/ package.
#
# Run this ON macOS for a proper icon (.icns via sips/iconutil) and a .dmg
# (hdiutil); both steps degrade gracefully if those tools are absent. The
# resulting app needs a Python 3 with Tk — macOS's system python3 has it.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VER="$(grep -m1 '^VERSION = ' "$SRC/bol/config.py" | cut -d'"' -f2)"
OUT="$SRC/dist"
APP="$OUT/BedrockOnLinux.app"
C="$APP/Contents"
rm -rf "$APP"
mkdir -p "$C/MacOS" "$C/Resources" "$OUT"

[[ -f "$SRC/data/icon.png" ]] || { echo "data/icon.png missing" >&2; exit 1; }

# ---- implementation: entry wrapper + bol/ package + icon ----
install -m755 "$SRC/bedrock-on-linux" "$C/Resources/bedrock-on-linux"
cp -R "$SRC/bol" "$C/Resources/bol"
find "$C/Resources/bol" -name __pycache__ -type d -exec rm -rf {} +
mkdir -p "$C/Resources/data"
cp "$SRC/data/icon.png" "$C/Resources/data/icon.png"

# ---- launcher: find a Python with Tk and widen PATH so brew / GPTK / wine
#      resolve (Finder gives GUI apps only a minimal PATH) ----
cat > "$C/MacOS/BedrockOnLinux" <<'EOF'
#!/bin/bash
# Finder-launched apps inherit a minimal PATH; add the usual Homebrew and
# CrossOver locations so brew, gameportingtoolkit and wine are found at run time.
export PATH="/opt/homebrew/bin:/usr/local/bin:/Applications/CrossOver.app/Contents/SharedSupport/CrossOver/bin:$PATH"
HERE="$(cd "$(dirname "$0")/../Resources" && pwd)"
# Drop the legacy -psn_* process-serial arg Finder may inject; default to the GUI.
args=()
for a in "$@"; do case "$a" in -psn_*) ;; *) args+=("$a");; esac; done
[ "${#args[@]}" -eq 0 ] && args=(gui)
for PY in /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3 python3; do
  if command -v "$PY" >/dev/null 2>&1; then exec "$PY" "$HERE/bedrock-on-linux" "${args[@]}"; fi
done
/usr/bin/osascript -e 'display alert "BedrockOnLinux" message "Python 3 was not found. Install it from python.org, or: brew install python-tk"' >/dev/null 2>&1 || true
exit 1
EOF
chmod +x "$C/MacOS/BedrockOnLinux"

# ---- icon (best-effort: sips + iconutil are macOS-only) ----
ICON="AppIcon"
if command -v sips >/dev/null 2>&1 && command -v iconutil >/dev/null 2>&1; then
  ICONSET="$OUT/AppIcon.iconset"; rm -rf "$ICONSET"; mkdir -p "$ICONSET"
  for s in 16 32 64 128 256 512; do
    sips -z "$s" "$s" "$SRC/data/icon.png" \
         --out "$ICONSET/icon_${s}x${s}.png" >/dev/null
    d=$((s * 2))
    sips -z "$d" "$d" "$SRC/data/icon.png" \
         --out "$ICONSET/icon_${s}x${s}@2x.png" >/dev/null
  done
  iconutil -c icns "$ICONSET" -o "$C/Resources/AppIcon.icns"
  rm -rf "$ICONSET"
else
  cp "$SRC/data/icon.png" "$C/Resources/AppIcon.png"; ICON="AppIcon.png"
  echo "note: sips/iconutil not found — bundling icon.png as-is" \
       "(run on macOS for a proper .icns)" >&2
fi

# ---- Info.plist ----
cat > "$C/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>BedrockOnLinux</string>
  <key>CFBundleDisplayName</key><string>BedrockOnLinux</string>
  <key>CFBundleIdentifier</key><string>io.github.wyze3306.BedrockOnLinux</string>
  <key>CFBundleVersion</key><string>${VER}</string>
  <key>CFBundleShortVersionString</key><string>${VER}</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>BedrockOnLinux</string>
  <key>CFBundleIconFile</key><string>${ICON}</string>
  <key>LSMinimumSystemVersion</key><string>12.0</string>
  <key>LSApplicationCategoryType</key><string>public.app-category.games</string>
  <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
EOF

echo "OK -> $APP"

# ---- optional .dmg (macOS only, best-effort) ----
if command -v hdiutil >/dev/null 2>&1; then
  DMG="$OUT/BedrockOnLinux-${VER}.dmg"
  STAGE="$OUT/dmg"; rm -f "$DMG"; rm -rf "$STAGE"; mkdir -p "$STAGE"
  cp -R "$APP" "$STAGE/"
  ln -s /Applications "$STAGE/Applications"
  if hdiutil create -volname "BedrockOnLinux" -srcfolder "$STAGE" \
       -ov -format UDZO "$DMG" >/dev/null; then
    echo "OK -> $DMG"
  fi
  rm -rf "$STAGE"
else
  echo "note: hdiutil not found — skipped .dmg (run on macOS to produce one)" >&2
fi
