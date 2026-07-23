# ANUSA Scanner

A phone-camera student-card scanner for fast event check-in. An iPhone home-screen
web app reads the 7-digit ANU student number off a card with an on-device ML OCR model,
and sends it — over an encrypted relay — to a paired Mac that either **types it** into
whatever field has focus, or **checks the student in on a Google Sheet**.

```
 iPhone PWA                        encrypted relay                 Mac receiver app
 camera → PaddleOCR (ONNX)  ──▶  MQTT over WSS (public broker,  ──▶  types the number,
 → 7-digit number,               AES-256-GCM, key derived            OR checks the student
 two-read confirmed              from the room code)                 in on a Google Sheet
```

The scanner is tuned for the number printed above **STUDENT U** on an ANU card
(e.g. `u8221537`). Digit count, an optional leading-digit filter, room code, and broker
are all adjustable in Settings.

## OCR: on-device, rotation-proof

Recognition runs entirely in the browser via **PaddleOCR** (DBNet text detection +
PP-OCRv5 recognition) on [onnxruntime-web](https://onnxruntime.ai/docs/tutorials/web/).
The detector finds the number at *any* angle, so cards held tilted or skewed still read —
no need to line the card up square. A number is only accepted after **two consecutive
identical reads**, so a lone misread is never sent.

The models (~10 MB) live in [`models/`](models/) and, with the ONNX runtime (~11 MB),
are downloaded once on first use (with a progress bar) and then cached by the service
worker in a stable cache that survives app updates.

## Two receiver modes

- **Type keystrokes** — the number + Enter is typed into whatever window has focus on the
  Mac. The same workflow as a handheld "keyboard-wedge" barcode scanner, done with a phone.
- **Union Pantry (Google Sheet)** — the Mac signs in with your Google account and flips the
  scanned student's tick `FALSE → TRUE` on a sheet you pick, showing **checked-in / already
  in / not registered** on both the Mac and the phone. Includes manual entry with name
  autofill, a live attendance %, and a fuzzy "did you mean…?" prompt for one-digit typos in
  the sheet.

## Why there's a receiver (the Bluetooth constraint)

A web page can't emulate a Bluetooth keyboard — Web Bluetooth only lets a browser *connect
to* peripherals, never *be* one, and iOS Safari doesn't support it at all. So the "types
into the focused field" behaviour is delivered by the Mac receiver listening on the
encrypted relay instead — functionally identical at the desk, no pairing required.

## The phone app (PWA)

**Deploy (GitHub Pages).** Push everything in this folder to the root of `main`, then
Settings → Pages → *Deploy from a branch* → `main` / `(root)`. Open
`https://<user>.github.io/<repo>/` — HTTPS (required for the camera) is automatic. This
repo's live copy: <https://dezlidezlidezli.github.io/anusa-scanner/>.

**Install on the iPhone.** Open the URL in Safari → Share → **Add to Home Screen**. Launch
from the icon and pair (below). First launch downloads the OCR engine + models (~20 MB,
shown with a progress bar); it's cached after that.

## The Mac receiver

Build the native app (macOS):

```
cd receiver
bash build_mac.sh          # installs deps, generates the icon, produces dist/ANUSA Scanner.app
```

The app is unsigned, so on first launch **right-click → Open** (once) to get past Gatekeeper.
For keystroke mode, also enable it under **System Settings → Privacy & Security →
Accessibility** so it can type into other apps (Union Pantry mode doesn't need this).

To run from source instead of building: `pip install -r requirements.txt` then
`python wedge_app.py`.

## Pairing

Launch the Mac app — it opens on a **pairing QR**. Point the phone's camera at it (the app
scans the code) and the two share a room automatically. Or tap **Skip → set up manually**
and type the room code shown in the phone's header into the Mac (and vice-versa). One room
can hold several phones; the receiver de-duplicates.

## Union Pantry (Google Sheets) setup

Sheet mode needs a Google OAuth "Desktop app" client — see
[`receiver/SHEETS_SETUP.md`](receiver/SHEETS_SETUP.md). Drop the `credentials.json` next to
the app (or in `~/Library/Application Support/ANUSA Scanner/`), click **Sign in with
Google**, paste your sheet's URL, and confirm the **UID / attendance / name** columns (the
app guesses them). For demos and screen-shares, import a roster of obviously-fake data
(e.g. `u7878787` / John Smith / `TRUE`) rather than a real one, so no student data is exposed.

## How fast the result appears

The check-in feedback is effectively instant: the Mac pushes the roster (+ who's already
ticked) to the phone on pairing, so the phone shows **name / not-registered / already /
checking-in the moment it reads the card** — no round-trip. The Mac still writes the sheet
in the background (optimistically, so it never blocks the result) and its confirmation
reconciles quietly. The only thing left on the wire is that background write via the MQTT
broker; pointing both sides at a local/near broker makes even that feel instant.

## Scanning technique

Hold the card so the number fills a good part of the frame, in decent light (a torch button
appears if the phone supports it). On a confirmed read the brackets pulse **grey** and it
chimes — grey, not green, because a *scan* isn't yet a check-in; the full-screen **green /
orange / red** result is the receiver's verdict once it's checked the sheet. The same card
is ignored briefly so you can't double-send. Damaged card → **Type manually** on the Mac.

The exactly-*N*-digits filter does most of the accuracy work: every other number on a card
(dates, chip UID, valid-to stamp) fails it. If all your IDs share a first digit, set
**Number starts with** to reject the rare one-digit misread.

## Settings (phone)

- **Room code** — pairs phone and receiver; regenerate per event.
- **Digits** — length of the target number (default 7).
- **Number starts with** / **First digit is one of** — optional misread filters.
- **Output** — *Bridge* (sends to the paired Mac) or *Clipboard only* (copies on the phone;
  no receiver needed).
- **Relay broker** — any MQTT-over-WSS endpoint. Default is the public `broker.emqx.io`.
  For anything beyond casual event check-in, point both sides at your own broker (a free
  EMQX Cloud instance, or Mosquitto on a laptop on the venue Wi-Fi) — lower, steadier latency.

## Privacy

Payloads (scans, results, and the roster push) are AES-256-GCM encrypted with a key derived
from the room code, which never crosses the network — the public broker sees only ciphertext
on a random topic. Anyone with the room code can receive scans, so treat it like a password
and rotate it per event. Student data is personal information: use the demo roster for
demos/screen-shares, and handle the real sheet and the on-phone/on-Mac scan history
accordingly.

## Troubleshooting

- **Camera denied** — iOS Settings → Apps → ANUSA Scanner → allow Camera; relaunch.
- **Stuck on "loading OCR engine"** — first run downloads ~20 MB; the progress bar shows it
  moving. A dropped connection aborts with an error — reconnect and retry.
- **Shows "test — not typed"** — keystroke mode with the Mac window focused (it won't type
  into itself). Click your target app, or switch to Union Pantry.
- **Nothing types (macOS)** — grant Accessibility permission (above); relaunch.
- **"bridge offline"** — public brokers hiccup; wait a few seconds or switch broker.
- **Slow/failed reads** — more light or use the torch; fill more of the frame with the card;
  clean the lens. Glossy-laminate glare is the usual culprit — tilt the card slightly.
