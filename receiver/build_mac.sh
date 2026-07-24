#!/usr/bin/env bash
# build_mac.sh — builds "ANUSA Scanner.app" from wedge_app.py
# Run from this directory: bash build_mac.sh

set -e
cd "$(dirname "$0")"

APP_NAME="ANUSA Scanner"

echo "=== ${APP_NAME} macOS build ==="
echo ""

# ── 1. Install build deps ─────────────────────────────────────────────────────
echo "→ Installing dependencies…"
python3 -m pip install --quiet paho-mqtt cryptography pynput pyinstaller pillow qrcode pywebview \
    google-api-python-client google-auth-oauthlib google-auth-httplib2

# ── 2. App icon ───────────────────────────────────────────────────────────────
echo "→ Generating app icon…"
python3 make_icon.py

# ── 3. PyInstaller ────────────────────────────────────────────────────────────
# We do NOT embed any credential in the .app. Distribution is service-account only: the
# service_account.json is shipped LOOSE in dist/ (below) and Install.command copies it into
# ~/Library/Application Support/ANUSA Scanner on the recipient's Mac. credentials.json (the
# OAuth Desktop client) is deliberately NOT bundled — OAuth is only for the developer, who
# keeps their own credentials.json in Application Support.
CREDS_FLAG=()
if [ ! -f service_account.json ]; then
    echo "⚠  no service_account.json — Union Pantry needs it (see SHEETS_SETUP.md);"
    echo "   Keystroke + Textbook Library modes work without it."
fi

echo "→ Building .app bundle…"
pyinstaller \
    --name "${APP_NAME}" \
    --windowed \
    --noconfirm \
    --clean \
    --icon "appicon.icns" \
    --osx-bundle-identifier "au.org.anusa.scanner" \
    --collect-all googleapiclient \
    --collect-all google_auth_oauthlib \
    --collect-submodules google.auth \
    --collect-submodules google.oauth2 \
    --collect-submodules qrcode \
    --collect-all webview \
    --hidden-import google_auth_httplib2 \
    --hidden-import Quartz \
    --add-data "ui.html:." \
    "${CREDS_FLAG[@]}" \
    wedge_app.py

# ── 4. Make it as launch-clean as possible on THIS Mac ────────────────────────
# The app is unsigned (ad-hoc). Gatekeeper still marks unsigned apps with a
# prohibitory badge on first launch — clearing attributes and re-signing ad-hoc
# keeps it as clean as possible. To remove the badge entirely you need an Apple
# Developer ID + notarization. Recipients: right-click → Open the first time.
echo "→ Clearing attributes + ad-hoc signing…"
xattr -cr "dist/${APP_NAME}.app" 2>/dev/null || true
codesign --force --deep --sign - "dist/${APP_NAME}.app" 2>/dev/null || true

# Ship the first-run installer next to the app so recipients don't have to right-click → Open.
if [ -f Install.command ]; then
    cp Install.command "dist/Install.command"
    chmod +x "dist/Install.command"
    echo "→ Added dist/Install.command (clears quarantine on the recipient's Mac)"
fi

# Ship the service-account key as a loose file next to the app too — Install.command copies it
# into the recipient's Application Support so Sheets access works with no "Load key…" step.
if [ -f service_account.json ]; then
    cp service_account.json "dist/service_account.json"
    chmod 600 "dist/service_account.json"
    echo "→ Added dist/service_account.json (Install.command installs it to Application Support)"
fi

# Remove PyInstaller's redundant "onedir" folder (dist/<name>/ with _internal/). The .app bundle
# is fully self-contained (it has its own Frameworks/ + Resources/), so this loose copy is never
# distributed — deleting it leaves dist/ holding ONLY the files you actually send.
rm -rf "dist/${APP_NAME}" 2>/dev/null || true
rm -f  "dist/.DS_Store" 2>/dev/null || true

# ── 5. Done ───────────────────────────────────────────────────────────────────
echo ""
echo "✓  dist/${APP_NAME}.app is ready"
echo ""
echo "To distribute: zip the WHOLE dist/ folder — ${APP_NAME}.app + Install.command"
echo "   (+ service_account.json if present) — and send that."
echo ""
echo "⚠  Recipients: double-click  Install.command  once (right-click → Open if macOS blocks"
echo "   it). It clears the quarantine flag, installs to /Applications, and launches — after"
echo "   that the app opens with a normal double-click, no prompts."
echo "   Keystroke mode also needs: System Settings → Privacy & Security → Accessibility →"
echo "   enable '${APP_NAME}'. (Union Pantry + Textbook Library don't need it.)"
echo ""
