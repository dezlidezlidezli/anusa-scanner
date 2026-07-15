#!/usr/bin/env python3
"""
ANUSA Scanner receiver — run this on the computer that should receive the keystrokes.

It joins the same room as the phone app, decrypts each scan, and types the ID
number into whatever window/field currently has focus (i.e. it behaves like a
keyboard wedge scanner). Every scan is also appended to a CSV log.

Usage:
    python receiver.py --room 7KQ2FX
    python receiver.py --room 7KQ2FX --suffix tab --csv scans.csv
    python receiver.py --room 7KQ2FX --no-type          # log only, don't type

Requires:  pip install -r requirements.txt
macOS:     grant your terminal app Accessibility permission
           (System Settings -> Privacy & Security -> Accessibility),
           or keystrokes will silently not appear.
"""

import argparse
import base64
import csv
import hashlib
import json
import signal
import ssl
import sys
import time
from collections import deque
from datetime import datetime
from urllib.parse import urlparse

try:
    import paho.mqtt.client as mqtt
except ImportError:
    sys.exit("Missing dependency: pip install paho-mqtt")
try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
except ImportError:
    sys.exit("Missing dependency: pip install cryptography")

DEFAULT_BROKER = "wss://broker.emqx.io:8084/mqtt"


def derive_key(room: str) -> bytes:
    return hashlib.sha256(f"idwedge|v1|{room.strip().upper()}".encode()).digest()


def decrypt(key: bytes, payload_b64: str) -> dict:
    raw = base64.b64decode(payload_b64)
    pt = AESGCM(key).decrypt(raw[:12], raw[12:], None)
    return json.loads(pt)


def encrypt(key: bytes, obj: dict) -> str:
    import os
    iv = os.urandom(12)
    ct = AESGCM(key).encrypt(iv, json.dumps(obj).encode(), None)
    return base64.b64encode(iv + ct).decode()


class Typer:
    """Lazy pynput wrapper so --no-type works without the dependency."""

    def __init__(self, suffix: str, char_delay: float):
        from pynput.keyboard import Controller, Key  # imported here on purpose
        self.kb = Controller()
        self.Key = Key
        self.suffix = suffix
        self.char_delay = char_delay

    def type_id(self, text: str):
        if self.char_delay > 0:
            for ch in text:
                self.kb.type(ch)
                time.sleep(self.char_delay)
        else:
            self.kb.type(text)
        if self.suffix == "enter":
            self.kb.press(self.Key.enter); self.kb.release(self.Key.enter)
        elif self.suffix == "tab":
            self.kb.press(self.Key.tab); self.kb.release(self.Key.tab)


def main():
    ap = argparse.ArgumentParser(description="ANUSA Scanner receiver — types scans from the phone app")
    ap.add_argument("--room", required=True, help="room code shown in the phone app")
    ap.add_argument("--broker", default=DEFAULT_BROKER, help=f"MQTT-over-websockets URL (default {DEFAULT_BROKER})")
    ap.add_argument("--suffix", choices=["enter", "tab", "none"], default="enter",
                    help="key pressed after each ID (default: enter)")
    ap.add_argument("--char-delay", type=float, default=0.0,
                    help="seconds between characters, for apps that drop fast input (e.g. 0.02)")
    ap.add_argument("--csv", default="scans.csv", help="CSV log file (default scans.csv)")
    ap.add_argument("--no-type", action="store_true", help="log scans only, don't inject keystrokes")
    args = ap.parse_args()

    key = derive_key(args.room)
    room = args.room.strip().upper()
    base = f"idwedge/{room}"

    typer = None
    if not args.no_type:
        try:
            typer = Typer(args.suffix, args.char_delay)
        except Exception as e:
            sys.exit(f"Could not initialise keyboard control ({e}). "
                     f"Install pynput, or run with --no-type to log only.")

    u = urlparse(args.broker)
    use_tls = u.scheme == "wss"
    host = u.hostname or "broker.emqx.io"
    port = u.port or (443 if use_tls else 80)
    path = u.path or "/mqtt"

    seen = deque(maxlen=500)
    seen_set = set()
    csv_path = args.csv

    def log_csv(row):
        new = False
        try:
            with open(csv_path, "x", newline="") as f:
                csv.writer(f).writerow(["timestamp", "device", "seq", "source", "id"])
                new = True
        except FileExistsError:
            pass
        with open(csv_path, "a", newline="") as f:
            csv.writer(f).writerow(row)
        return new

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                         client_id=f"wedge_rx_{int(time.time())}",
                         transport="websockets")
    client.ws_set_options(path=path)
    if use_tls:
        client.tls_set(cert_reqs=ssl.CERT_REQUIRED)

    def on_connect(c, userdata, flags, reason_code, properties=None):
        if reason_code == 0:
            c.subscribe(f"{base}/scan", qos=1)
            print(f"[{datetime.now():%H:%M:%S}] connected — room {room}, waiting for scans "
                  f"({'logging only' if args.no_type else 'typing into the focused window'})")
        else:
            print(f"connect failed: {reason_code}")

    def on_disconnect(c, userdata, flags, reason_code, properties=None):
        print(f"[{datetime.now():%H:%M:%S}] disconnected — retrying…")

    def on_message(c, userdata, msg):
        try:
            data = decrypt(key, msg.payload.decode())
        except Exception:
            return  # different room or malformed — ignore
        if data.get("t") != "scan":
            return
        dedupe = (data.get("dev"), data.get("seq"))
        if dedupe in seen_set:
            return
        if len(seen) == seen.maxlen:
            seen_set.discard(seen.popleft())
        seen.append(dedupe)
        seen_set.add(dedupe)

        sid = str(data.get("id", "")).strip()
        if not sid.isdigit():
            return
        stamp = datetime.now().strftime("%H:%M:%S")
        if typer:
            typer.type_id(sid)
        log_csv([datetime.now().isoformat(timespec="seconds"),
                 data.get("dev", ""), data.get("seq", ""), data.get("src", ""), sid])
        print(f"[{stamp}] {sid}  ({'typed' if typer else 'logged'})")
        try:
            c.publish(f"{base}/ack", encrypt(key, {"t": "ack", "seq": data.get("seq"),
                                                   "dev": data.get("dev")}), qos=1)
        except Exception:
            pass

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message

    signal.signal(signal.SIGINT, lambda *_: (print("\nbye"), sys.exit(0)))

    print(f"connecting to {host}:{port}{path} …")
    client.connect(host, port, keepalive=30)
    client.loop_forever(retry_first_connection=True)


if __name__ == "__main__":
    main()
