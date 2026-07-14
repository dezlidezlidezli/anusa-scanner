# ID Wedge

A camera-to-keystroke scanner. An iPhone home-screen web app reads the printed ID
number on a student card, and the number is typed into whatever field has focus on
a paired computer — the same workflow as a handheld barcode "keyboard wedge"
scanner, done with a phone.

```
 iPhone PWA                    encrypted relay                receiver.py
 camera → OCR → 7-digit ──▶  MQTT over WSS (public  ──▶  decrypts, types the
 number, double-read          broker, AES-GCM with a       number + Enter into
 confirmed                    key from the room code)      the focused window
```

Tuned for cards where the target is the 7-digit number printed above **STUDENT U**
(e.g. `8221537`) — frame that line in the reticle. Digit count, an optional
leading-digit filter, room code, and broker are all adjustable in Settings.

## Why there's a receiver script (the Bluetooth constraint)

A web page cannot emulate a Bluetooth keyboard. Web Bluetooth only lets a browser
*connect to* peripherals, never *be* one, and iOS Safari doesn't support Web
Bluetooth at all — no browser API exposes the BLE HID peripheral role. iOS won't
act as a Bluetooth keyboard even for native apps. So the "types into the focused
field" behaviour is delivered by `receiver.py` listening on an encrypted relay
channel instead — functionally identical at the desk, no pairing required.

If you want a literal Bluetooth keyboard with **zero software on the target
machine** (e.g. typing into a locked-down uni PC), the clean path is a ~$15 ESP32
dev board: the phone app posts each scan to it over Wi-Fi, and it presents itself
to the computer as a standard BLE keyboard. The app's relay layer is easily
pointed at one — ask and the firmware sketch can be added.

## Deploy (GitHub Pages)

1. Create a repo and push everything in this folder to the root of `main`.
2. Repo → Settings → Pages → Source: *Deploy from a branch* → `main` / `(root)`.
3. Open `https://<user>.github.io/<repo>/` — done. HTTPS (required for the
   camera) is automatic.

## Install on the iPhone

Open the URL in Safari → Share → **Add to Home Screen**. Launch from the icon,
tap **Start scanning**, allow camera access. The first load fetches the OCR
engine (~5 MB); it's cached after that. Note the room code shown in the header.

## Run the receiver

On the computer that should receive the keystrokes:

```
cd receiver
pip install -r requirements.txt
python receiver.py --room <CODE-FROM-THE-PHONE>
```

Click into the target field (spreadsheet cell, web form, anything). Each scan is
typed there followed by Enter, and appended to `scans.csv`.

Options: `--suffix enter|tab|none`, `--char-delay 0.02` (for apps that drop fast
input), `--no-type` (log to CSV only), `--broker <url>`, `--csv <path>`.

Platform notes: **macOS** — grant your terminal Accessibility permission (System
Settings → Privacy & Security → Accessibility) or keystrokes silently won't
appear. **Windows** — works as-is. **Linux** — fine under X11; under Wayland,
pynput can't inject globally (use `--no-type` or an X11 session).

## Scanning technique

Hold the card flat, fill the reticle with the number line, decent light (torch
button appears if the phone supports it). A read is only accepted after two
consecutive identical OCR passes, then it beeps and the brackets flash green —
about a second when framed well. The same card is ignored for 8 s so you can't
double-send. Damaged card → **Type manually**. Multiple phones can share one
room; the receiver de-duplicates.

The digit-count filter does most of the accuracy work: on a real card, every
other number (dates, chip UID, valid-to stamp) fails the exactly-7-digits test.
If all your IDs share a first digit, set **Number starts with** (e.g. `8`) to
also reject the rare one-digit misread.

## Settings

- **Room code** — pairs phone and receiver; regenerate per event.
- **Digits** — length of the target number (default 7).
- **Number starts with** — optional misread filter.
- **Output** — *Bridge* (types on the paired computer) or *Clipboard only*
  (copies on the phone; no receiver needed).
- **Relay broker** — any MQTT-over-WSS endpoint. Default is the public
  `broker.emqx.io`; alternates: `wss://broker.hivemq.com:8884/mqtt`,
  `wss://test.mosquitto.org:8081`. For anything beyond casual event check-in,
  point both sides at your own broker (a free EMQX Cloud instance, or Mosquitto
  on a laptop/NAS on the venue Wi-Fi).

## Privacy

Payloads are AES-256-GCM encrypted with a key derived from the room code, which
never crosses the network — the public broker sees only ciphertext on a random
topic. Anyone who has the room code can receive scans, so treat it like a
password and rotate it per event. Scan history lives on the phone
(Settings-clearable) and in `scans.csv`; both are personal information — handle
and delete accordingly.

## Troubleshooting

- **Camera denied** — iOS Settings → Apps → ID Wedge → allow Camera; relaunch.
- **"bridge offline"** — public brokers hiccup; give it a few seconds or switch
  broker in Settings (change it in the receiver's `--broker` too).
- **Scans arrive but nothing types (macOS)** — Accessibility permission, above.
- **Slow/failed reads** — more light or use the torch; fill the reticle with the
  number; clean the lens. Glossy laminate glare is the usual culprit — tilt the
  card slightly.
