#!/usr/bin/env python3
"""
ANUSA Scanner — macOS receiver app.

A native window (pywebview) rendering ui.html for the interface, with this file as
the backend. Enter/scan a room code, then pick what each scan does:

  • Type keystrokes  — types the number + Enter into whatever window has focus
                       (when THIS window is focused it's shown but not typed).
  • Google Sheet     — signs in with your Google account and flips the scanned
                       student's tick FALSE→TRUE on a sheet you choose. The result
                       (checked-in / already / not registered) is shown here AND
                       sent back to the phone over the encrypted MQTT channel.
"""

import base64
import csv
import hashlib
import io
import json
import os
import queue
import random
import ssl
import sys
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

# ── optional dependencies with friendly errors ────────────────────────────────

try:
    import paho.mqtt.client as mqtt
except ImportError:
    sys.exit("Missing: pip install paho-mqtt")
try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
except ImportError:
    sys.exit("Missing: pip install cryptography")
try:
    import webview
except ImportError:
    sys.exit("Missing: pip install pywebview")

# pynput (keystrokes), the Google libraries (via sheets.py), and qrcode are imported
# lazily so the app still opens if a mode's deps aren't ready.
import sheets

# ── constants ─────────────────────────────────────────────────────────────────

VERSION        = "14.46"   # shared version across the Mac app + web app
DEFAULT_BROKER = "wss://broker.emqx.io:8084/mqtt"
PWA_URL        = "https://dezlidezlidezli.github.io/anusa-scanner/"  # for pairing QR
LOG_PATH       = Path.home() / "Documents" / "ANUSAScanner_scans.csv"


def _resource(name):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


# ── crypto ────────────────────────────────────────────────────────────────────

def _derive_key(room: str) -> bytes:
    return hashlib.sha256(f"idwedge|v1|{room.strip().upper()}".encode()).digest()

def _decrypt(key: bytes, payload_b64: str) -> dict:
    raw = base64.b64decode(payload_b64)
    return json.loads(AESGCM(key).decrypt(raw[:12], raw[12:], None))

def _encrypt(key: bytes, obj: dict) -> str:
    iv = os.urandom(12)
    ct = AESGCM(key).encrypt(iv, json.dumps(obj).encode(), None)
    return base64.b64encode(iv + ct).decode()

# ── MQTT bridge (background thread → main thread via queue) ───────────────────

class Bridge:
    def __init__(self, q: queue.Queue):
        self._q       = q
        self._client  = None
        self._key     = None
        self._room    = ""
        self._base    = ""
        self._alive   = False
        self._seen    = deque(maxlen=500)
        self._seen_s  = set()

    def connect(self, room: str, broker_url: str):
        self.stop()
        self._room  = room.strip().upper()
        self._base  = f"idwedge/{self._room}"
        self._key   = _derive_key(self._room)
        self._alive = True
        threading.Thread(target=self._run, args=(broker_url,), daemon=True).start()

    def stop(self):
        self._alive = False
        if self._client:
            try:
                self._client.disconnect()
            except Exception:
                pass
            self._client = None

    def _publish(self, obj):
        if not (self._client and self._key):
            return
        try:
            self._client.publish(f"{self._base}/ack", _encrypt(self._key, obj), qos=1)
        except Exception:
            pass

    def ack(self, seq, dev):
        """Keystroke mode: tell the phone the scan was typed."""
        self._publish({"t": "ack", "seq": seq, "dev": dev})

    def send_status(self, seq, dev, status, name="", sid=""):
        """Sheet mode: report the check-in result back to the phone. `sid` lets the
        phone display + flash results it never scanned itself (e.g. manual entries)."""
        self._publish({"t": "checkin", "seq": seq, "dev": dev,
                       "status": status, "name": name, "id": sid})

    def _run(self, broker_url: str):
        u      = urlparse(broker_url)
        host   = u.hostname or "broker.emqx.io"
        port   = u.port or 8084
        path   = u.path or "/mqtt"
        tls    = u.scheme == "wss"

        try:
            c = mqtt.Client(
                mqtt.CallbackAPIVersion.VERSION2,
                client_id=f"wedge_rx_{os.urandom(4).hex()}",
                transport="websockets",
            )
        except AttributeError:
            # paho-mqtt < 2.0 fallback
            c = mqtt.Client(client_id=f"wedge_rx_{os.urandom(4).hex()}",
                            transport="websockets")

        c.ws_set_options(path=path)
        if tls:
            c.tls_set(cert_reqs=ssl.CERT_REQUIRED)
        self._client = c

        def on_connect(client, userdata, flags, rc, props=None):
            code = getattr(rc, 'value', rc)  # VERSION2 uses ReasonCode object
            if code == 0:
                client.subscribe(f"{self._base}/scan", qos=1)
                self._q.put(("status", "ok", f"connected  ·  {self._room}"))
            else:
                self._q.put(("status", "err", f"connect failed (rc {code})"))

        def on_disconnect(client, userdata, flags, rc, props=None):
            if self._alive:
                self._q.put(("status", "wait", "reconnecting…"))

        def on_message(client, userdata, msg):
            try:
                data = _decrypt(self._key, msg.payload.decode())
            except Exception:
                return
            t = data.get("t")
            if t == "hello":                       # a phone joined this room → paired
                self._q.put(("paired", data.get("dev")))
                return
            if t != "scan":
                return
            pair = (data.get("dev"), data.get("seq"))
            if pair in self._seen_s:
                return
            if len(self._seen) == self._seen.maxlen:
                self._seen_s.discard(self._seen.popleft())
            self._seen.append(pair)
            self._seen_s.add(pair)
            sid = str(data.get("id", "")).strip()
            if not sid.isdigit():
                return
            self._q.put(("scan", data, sid))

        c.on_connect    = on_connect
        c.on_disconnect = on_disconnect
        c.on_message    = on_message

        try:
            c.connect(host, port, keepalive=30)
            c.loop_forever(retry_first_connection=True)
        except Exception as e:
            self._q.put(("status", "err", str(e)))


# ── Web-UI bridge (methods here are called from ui.html's JS) ─────────────────

class Api:
    def __init__(self):
        self.q = queue.Queue()
        self.bridge = Bridge(self.q)
        self.window = None
        self.mode = "keys"
        self.focused = False
        self.connected = False
        self.room = ""
        self.broker = DEFAULT_BROKER
        self.sheet = None            # sheets.SheetSession once signed in
        self.sheet_ready = False
        self._kb = None              # lazy pynput (Controller, Key)
        self._hist = []              # [{ts,id,result,dev,seq}]

    # called once the window + GUI loop are up
    def attach(self, window):
        self.window = window
        threading.Thread(target=self._drain, daemon=True).start()
        self._emit("version", {"v": VERSION})
        if sheets.token_available():
            threading.Thread(target=lambda: self._do_sign_in(False), daemon=True).start()
        self.start_pairing()   # open to the pairing QR; a phone's "hello" reveals the app

    def start_pairing(self):
        """Generate a room, connect, and show the pairing QR until a phone says hello."""
        abc = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"   # no 0/O/1/I/L
        self.room = "".join(random.choice(abc) for _ in range(6))
        self.connected = True
        self.bridge.connect(self.room, self.broker)
        self._emit("show_pairing",
                   {"room": self.room, "img": self._qr_data_url(f"{PWA_URL}?room={self.room}")})

    def new_day(self):
        """Forget the session — disconnect, drop the loaded sheet/columns, clear history,
        reset to keystroke mode — and show a fresh pairing QR. Keeps the Google sign-in."""
        self.bridge.stop()
        self.sheet_ready = False
        if self.sheet is not None:
            self.sheet = sheets.SheetSession(self.sheet.svc)   # fresh session, same auth
        self._hist = []
        self.mode = "keys"
        self._emit("reset", {"signed_in": self.sheet is not None})
        self.start_pairing()

    def _emit(self, kind, payload=None):
        if not self.window:
            return
        try:
            self.window.evaluate_js(
                "window.uiEvent(%s, %s)" % (json.dumps(kind), json.dumps(payload or {})))
        except Exception:
            pass

    # ── queue drain ──────────────────────────────────────────────────────────
    def _drain(self):
        while True:
            ev = self.q.get()
            try:
                self._on_event(ev)
            except Exception:
                pass

    def _on_event(self, ev):
        kind = ev[0]
        if kind == "status":
            _, k, text = ev
            if k == "ok":
                self.connected = True
                self._emit("status", {"kind": "ok", "room": self.bridge._room})
            else:
                self._emit("status", {"kind": k, "text": text})
        elif kind == "scan":
            self._handle_scan(ev[1], ev[2])
        elif kind == "checkin":
            self._checkin_result(ev[1], ev[2], ev[3], ev[4])
        elif kind == "paired":
            self._emit("paired", {"dev": ev[1]})

    # ── scan handling ────────────────────────────────────────────────────────
    def _handle_scan(self, data, sid):
        ts = datetime.now().strftime("%H:%M:%S")
        if self.mode == "sheet":
            if not self.sheet_ready:
                self._emit("result", {"status": "error", "id": sid,
                                      "name": "set up the Google Sheet first", "ts": ts})
                return
            self._emit("working", {"id": sid})
            threading.Thread(target=self._do_checkin, args=(data, sid, ts), daemon=True).start()
            return
        # keystroke mode
        if self.focused:
            self._record(ts, sid, "test")
            self._emit("result", {"status": "test", "id": sid, "name": "", "ts": ts})
            return
        ok = self._type_id(sid)
        self._record(ts, sid, "typed" if ok else "error")
        self._emit("result", {"status": "typed" if ok else "error",
                              "id": sid, "name": "", "ts": ts})
        self.bridge.ack(data.get("seq"), data.get("dev"))

    def _do_checkin(self, data, sid, ts):
        try:
            res = self.sheet.check_in(sid)
        except Exception as e:
            res = {"status": "error", "name": str(e)}
        self.q.put(("checkin", data, sid, ts, res))

    def _checkin_result(self, data, sid, ts, res):
        status = res.get("status", "error")
        name = res.get("name", "")
        self._record(ts, sid, status)
        self._emit("result", {"status": status, "id": sid,
                              "name": name if status != "error" else "", "ts": ts})
        self.bridge.send_status(data.get("seq"), data.get("dev"), status, name, sid)

    def _type_id(self, sid):
        try:
            if self._kb is None:
                from pynput.keyboard import Controller, Key
                self._kb = (Controller(), Key)
            ctrl, Key = self._kb
            ctrl.type(sid)
            ctrl.press(Key.enter)
            ctrl.release(Key.enter)
            return True
        except Exception:
            return False

    def _record(self, ts, sid, label):
        self._hist.insert(0, {"ts": ts, "id": sid, "result": label, "dev": "", "seq": ""})
        self._log_csv(ts, sid, label)

    def _log_csv(self, ts, sid, label):
        try:
            needs_header = not LOG_PATH.exists()
            with open(LOG_PATH, "a", newline="") as f:
                w = csv.writer(f)
                if needs_header:
                    w.writerow(["timestamp", "id", "result"])
                w.writerow([ts, sid, label])
        except Exception:
            pass

    # ── JS-callable methods ──────────────────────────────────────────────────
    def get_state(self):
        self._emit("version", {"v": VERSION})
        return {"version": VERSION}

    def set_focus(self, f):
        self.focused = bool(f)

    def set_mode(self, m):
        self.mode = m if m in ("keys", "sheet") else "keys"

    def connect(self, room, broker):
        room = (room or "").strip().upper()
        if not room:
            self._emit("status", {"kind": "err", "text": "enter a room code"})
            return
        self.room = room
        self.broker = (broker or "").strip() or DEFAULT_BROKER
        self.connected = True
        self.bridge.connect(room, self.broker)

    def disconnect(self):
        self.connected = False
        self.bridge.stop()
        self._emit("status", {"kind": "off"})

    def sign_in(self):
        threading.Thread(target=lambda: self._do_sign_in(True), daemon=True).start()

    def _do_sign_in(self, interactive):
        try:
            svc = sheets.build_service(interactive=interactive)
            self.sheet = sheets.SheetSession(svc)
            self._emit("signed_in", {"ok": True})
        except Exception as e:
            if interactive:
                self._emit("sheet_status", {"text": f"sign-in failed: {str(e)[:40]}", "kind": "bad"})

    def load_sheet(self, url):
        if not self.sheet:
            self._emit("sheet_status", {"text": "sign in first", "kind": "bad"})
            return
        threading.Thread(target=self._do_load, args=(url,), daemon=True).start()

    def _do_load(self, url):
        try:
            info = self.sheet.open(url)
            guess = self.sheet.guess_columns()
            self._emit("sheet_loaded", {"tab": info["tab"], "rows": info["rows"],
                                       "headers": info["headers"], "guess": list(guess)})
        except Exception as e:
            self._emit("sheet_status", {"text": f"load failed: {str(e)[:44]}", "kind": "bad"})

    def set_columns(self, id_col, tick_col, name_col):
        if not self.sheet or not self.sheet.headers:
            return
        try:
            self.sheet.set_columns(id_col, tick_col,
                                   None if name_col in ("", "(none)") else name_col)
            self.sheet_ready = True
            self._emit("sheet_status", {"text": f"ready · {self.sheet.tab} · {id_col} → {tick_col}",
                                       "kind": "ok"})
        except Exception as e:
            self.sheet_ready = False
            self._emit("sheet_status", {"text": f"column error: {e}", "kind": "bad"})

    def sync(self):
        if not self.sheet or self.sheet.sid is None:
            self._emit("sheet_status", {"text": "load a sheet first", "kind": "warn"})
            return
        threading.Thread(target=self._do_sync, daemon=True).start()

    def _do_sync(self):
        try:
            self.sheet.refresh()
            n = max(0, len(self.sheet.values) - 1)
            self._emit("sheet_status", {"text": f"re-synced · {self.sheet.tab} · {n} rows", "kind": "ok"})
        except Exception as e:
            self._emit("sheet_status", {"text": f"sync failed: {str(e)[:40]}", "kind": "bad"})

    def manual(self, sid):
        sid = "".join(ch for ch in str(sid or "") if ch.isdigit())
        if not sid:
            return
        self._handle_scan({"seq": None, "dev": "manual"}, sid)

    def download_csv(self):
        if not self._hist:
            return
        try:
            fn = f"anusa_scans_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
            path = self.window.create_file_dialog(webview.SAVE_DIALOG, save_filename=fn)
            if not path:
                return
            if isinstance(path, (list, tuple)):
                path = path[0]
            rows = ["timestamp,id,result"]
            for h in reversed(self._hist):
                rows.append(f"{h['ts']},{h['id']},{h['result']}")
            with open(path, "w", newline="") as f:
                f.write("\n".join(rows) + "\n")
        except Exception:
            pass

    def pair_qr(self):
        """QR for the CURRENT room (the 'Pair a phone' button in the main UI — lets an
        extra phone join the same session)."""
        if not self.room:
            self.start_pairing()
        return {"room": self.room, "img": self._qr_data_url(f"{PWA_URL}?room={self.room}")}

    def _qr_data_url(self, url):
        import qrcode
        from qrcode.image.styledpil import StyledPilImage
        from qrcode.image.styles.moduledrawers import RoundedModuleDrawer
        from qrcode.image.styles.colormasks import SolidFillColorMask
        qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M,
                           box_size=10, border=2)
        qr.add_data(url)
        qr.make(fit=True)
        img = qr.make_image(
            image_factory=StyledPilImage, module_drawer=RoundedModuleDrawer(),
            color_mask=SolidFillColorMask(front_color=(59, 79, 214), back_color=(255, 255, 255)))
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="PNG")
        return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


# ── entry point ───────────────────────────────────────────────────────────────

def _selftest():
    """Headless check that every backend dep imports + builds in this (possibly
    frozen) build. Exits 0 on success. Needs a cached Google token."""
    try:
        svc = sheets.build_service(interactive=False)
        svc.spreadsheets()
        import qrcode  # noqa: F401
        from qrcode.image.styledpil import StyledPilImage  # noqa: F401
        from qrcode.image.styles.moduledrawers import RoundedModuleDrawer  # noqa: F401
        import webview as _wv  # noqa: F401
        print("SELFTEST OK: google + sheets + qrcode + pywebview")
        return 0
    except Exception as e:
        print(f"SELFTEST FAIL: {type(e).__name__}: {e}")
        return 1


def main():
    if "--selftest" in sys.argv:
        sys.exit(_selftest())

    api = Api()
    html = open(_resource("ui.html"), encoding="utf-8").read()
    window = webview.create_window(
        "ANUSA Scanner", html=html, js_api=api,
        width=940, height=720, min_size=(820, 600), background_color="#faf6ec")
    webview.start(api.attach, window)


if __name__ == "__main__":
    main()
