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
pip install --quiet paho-mqtt cryptography pynput pyinstaller pillow qrcode \
    google-api-python-client google-auth-oauthlib google-auth-httplib2

# ── 2. App icon ───────────────────────────────────────────────────────────────
echo "→ Generating app icon…"
python3 make_icon.py

# ── 3. PyInstaller ────────────────────────────────────────────────────────────
# Bundle credentials.json into the app if present, so operators just "Sign in with
# Google" (the Desktop client_id/secret is not confidential for installed apps).
# The token is written to ~/Library/Application Support/ANUSA Scanner at runtime.
CREDS_FLAG=()
if [ -f credentials.json ]; then
    CREDS_FLAG=(--add-data "credentials.json:.")
    echo "→ Bundling credentials.json"
else
    echo "⚠  no credentials.json — operators must place one in"
    echo "   ~/Library/Application Support/ANUSA Scanner/ (see SHEETS_SETUP.md)"
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
    --hidden-import google_auth_httplib2 \
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

# ── 5. Done ───────────────────────────────────────────────────────────────────
echo ""
echo "✓  dist/${APP_NAME}.app is ready"
echo ""
echo "To distribute: zip the .app and share it."
echo ""
echo "⚠  Recipients need to do this once on first launch:"
echo "   1. Right-click the app → Open  (bypasses the 'unidentified developer' block)"
echo "      or: System Settings → Privacy & Security → Open Anyway"
echo "   2. System Settings → Privacy & Security → Accessibility"
echo "      → enable '${APP_NAME}'  (required so it can type into other apps)"
echo ""
