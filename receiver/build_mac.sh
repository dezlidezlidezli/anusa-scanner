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
# Bundle credentials.json into the app if present, so operators just "Sign in with
# Google" (the Desktop client_id/secret is not confidential for installed apps).
# The token is written to ~/Library/Application Support/ANUSA Scanner at runtime.
CREDS_FLAG=()
if [ -f service_account.json ]; then
    # Preferred for sharing: authenticate as a service account — recipients never sign in.
    CREDS_FLAG+=(--add-data "service_account.json:.")
    echo "→ Bundling service_account.json (service-account auth — no user sign-in)"
fi
if [ -f credentials.json ]; then
    CREDS_FLAG+=(--add-data "credentials.json:.")
    echo "→ Bundling credentials.json (OAuth fallback)"
fi
if [ ${#CREDS_FLAG[@]} -eq 0 ]; then
    echo "⚠  no service_account.json or credentials.json — Union Pantry mode needs one"
    echo "   (see SHEETS_SETUP.md); Keystroke + Textbook Library modes work without it."
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

# ── 5. Done ───────────────────────────────────────────────────────────────────
echo ""
echo "✓  dist/${APP_NAME}.app is ready"
echo ""
echo "To distribute: zip BOTH  dist/${APP_NAME}.app  and  dist/Install.command  together."
echo ""
echo "⚠  Recipients: double-click  Install.command  once (right-click → Open if macOS blocks"
echo "   it). It clears the quarantine flag, installs to /Applications, and launches — after"
echo "   that the app opens with a normal double-click, no prompts."
echo "   Keystroke mode also needs: System Settings → Privacy & Security → Accessibility →"
echo "   enable '${APP_NAME}'. (Union Pantry + Textbook Library don't need it.)"
echo ""
