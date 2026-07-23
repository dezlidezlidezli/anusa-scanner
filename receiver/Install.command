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
    rm -rf "/Applications/$APP" 2>/dev/null
    if cp -R "$HERE/$APP" /Applications/ 2>/dev/null; then
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

open "$TARGET"
echo "✓ Launched:  $TARGET"
echo ""
echo "All set. Open it any time from Applications. You can delete the downloaded folder."
echo ""
read -n 1 -r -s -p "Press any key to close…"; echo
