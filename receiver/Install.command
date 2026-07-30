#!/bin/bash
# ANUSA Scanner — run once, first time only.
#
# ANUSA Scanner is an unsigned internal tool, so macOS would normally make you right-click →
# Open it the first time. This helper clears the "quarantine" flag Apple puts on downloaded
# apps, installs it to /Applications, and launches it — after this it opens with a normal
# double-click, no prompts.
#
# Double-click this file. (If macOS blocks it the first time, right-click it → Open once.)

APP="ANUSA Scanner.app"
HERE="$(cd "$(dirname "$0")" && pwd)"

echo "────────────────────────────────"
echo "   ANUSA Scanner — first-run setup"
echo "────────────────────────────────"
echo ""

TARGET=""
if [ -d "$HERE/$APP" ]; then
    echo "→ Installing to /Applications…"
    # Quit any running copy first, or macOS refuses to replace the running bundle and `open` below
    # just re-focuses the OLD version (the "I updated but it's still the old build" trap).
    pkill -f "/Applications/$APP/Contents/MacOS/" 2>/dev/null && sleep 1 || true
    rm -rf "/Applications/$APP" 2>/dev/null
    # ditto (not cp -R) preserves the code signature + bundle metadata intact.
    if ditto "$HERE/$APP" "/Applications/$APP" 2>/dev/null; then
        TARGET="/Applications/$APP"
    else
        echo "  (couldn't write to /Applications — running it from here instead)"
        TARGET="$HERE/$APP"
    fi
elif [ -d "/Applications/$APP" ]; then
    TARGET="/Applications/$APP"
else
    echo "✗ Couldn't find \"$APP\"."
    echo "  Keep this Install.command in the SAME folder as the app, then run it again."
    echo ""
    read -n 1 -r -s -p "Press any key to close…"; echo
    exit 1
fi

xattr -dr com.apple.quarantine "$TARGET" 2>/dev/null
echo "✓ Cleared the quarantine flag — it'll open with a normal double-click now."

# Install the service-account key (if it was zipped alongside) into the app's per-user support
# dir, so the recipient is signed in to Google Sheets automatically — no "Load key…" step.
KEY="service_account.json"
SUPPORT="$HOME/Library/Application Support/ANUSA Scanner"
if [ -f "$HERE/$KEY" ]; then
    mkdir -p "$SUPPORT"
    if cp "$HERE/$KEY" "$SUPPORT/$KEY" 2>/dev/null; then
        chmod 600 "$SUPPORT/$KEY" 2>/dev/null
        echo "✓ Installed the Google service-account key — Sheets access is ready."
    else
        echo "  (couldn't install the service-account key — you can load it in Settings instead)"
    fi
fi

open "$TARGET"
echo "✓ Launched:  $TARGET"
echo ""
echo "All set. Open it any time from Applications. You can delete the downloaded folder."
echo ""
read -n 1 -r -s -p "Press any key to close…"; echo
