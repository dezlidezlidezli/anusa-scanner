#!/usr/bin/env python3
"""
ANUSA Scanner — macOS receiver app.

Drag to Applications, grant Accessibility permission when prompted,
enter the room code from the phone app, and click Connect.

When this window is focused: scans display here (test mode — nothing is typed).
When another window is focused: each confirmed scan is typed + Enter into it.
"""

import base64
import csv
import hashlib
import json
import os
import queue
import ssl
import sys
import threading
import time
import tkinter as tk
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

# pynput is imported lazily so the app still opens if accessibility isn't
# granted yet — the user sees a clear in-app error instead of a crash.

# ── constants ─────────────────────────────────────────────────────────────────

VERSION        = "1.0"
DEFAULT_BROKER = "wss://broker.emqx.io:8084/mqtt"
LOG_PATH       = Path.home() / "Documents" / "ANUSAScanner_scans.csv"

# PWA colour palette so the app feels like the same product
C = dict(
    bg     = "#101418",
    deck   = "#1a2027",
    line   = "#2a333d",
    text   = "#e8edf2",
    muted  = "#8a97a5",
    orange = "#ff7a1a",
    green  = "#3ecf8e",
    amber  = "#ffb020",
    red    = "#ff5d5d",
)

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

    def ack(self, seq, dev):
        if not (self._client and self._key):
            return
        try:
            payload = _encrypt(self._key, {"t": "ack", "seq": seq, "dev": dev})
            self._client.publish(f"{self._base}/ack", payload, qos=1)
        except Exception:
            pass

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
            if data.get("t") != "scan":
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

# ── App ───────────────────────────────────────────────────────────────────────

class App:
    def __init__(self, root: tk.Tk):
        self.root        = root
        self.q           = queue.Queue()
        self.bridge      = Bridge(self.q)
        self._connected  = False
        self._kb         = None   # lazy: (Controller, Key) from pynput
        self._hist_rows  = []     # [{ts, id, mode, seq, dev}]
        self._app_focused = False

        root.title("ANUSA Scanner")
        root.configure(bg=C["bg"])
        root.resizable(False, False)
        root.geometry("440x570")

        # Dock-icon click restores the window
        try:
            root.createcommand("tk::mac::ReopenApplication", root.deiconify)
        except Exception:
            pass

        self._build_ui()
        self._poll_queue()
        self._poll_focus()

    # ── helpers ───────────────────────────────────────────────────────────────

    def _sep(self):
        tk.Frame(self.root, height=1, bg=C["line"]).pack(fill="x")

    def _label(self, parent, text, font, fg, bg=None, **kw):
        return tk.Label(parent, text=text, font=font, fg=fg,
                        bg=bg or C["bg"], **kw)

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self):
        r = self.root

        # ── Header ────────────────────────────────────────────────────────────
        hdr = tk.Frame(r, bg=C["bg"])
        hdr.pack(fill="x", padx=18, pady=(16, 12))
        tk.Label(hdr, text="ANUSA", font=("Menlo", 15, "bold"),
                 bg=C["bg"], fg=C["text"]).pack(side="left")
        tk.Label(hdr, text=" SCANNER  RECEIVER", font=("Menlo", 15),
                 bg=C["bg"], fg=C["muted"]).pack(side="left")
        tk.Label(hdr, text=f"v{VERSION}", font=("Menlo", 10),
                 bg=C["bg"], fg=C["line"]).pack(side="right")
        self._sep()

        # ── Connection section ────────────────────────────────────────────────
        conn_bg = tk.Frame(r, bg=C["deck"])
        conn_bg.pack(fill="x")
        conn = tk.Frame(conn_bg, bg=C["deck"])
        conn.pack(fill="x", padx=18, pady=14)

        tk.Label(conn, text="ROOM CODE", font=("Menlo", 9),
                 bg=C["deck"], fg=C["muted"]).pack(anchor="w")

        room_row = tk.Frame(conn, bg=C["deck"])
        room_row.pack(fill="x", pady=(4, 10))

        self._room_var = tk.StringVar()
        self._room_var.trace_add("write", self._auto_upper)
        room_entry = tk.Entry(
            room_row,
            textvariable=self._room_var,
            font=("Menlo", 20), width=8,
            bg=C["bg"], fg=C["text"],
            insertbackground=C["text"],
            relief="flat",
            highlightthickness=1,
            highlightbackground=C["line"],
            highlightcolor=C["orange"],
        )
        room_entry.pack(side="left", ipady=6, padx=(0, 10))
        room_entry.bind("<Return>", lambda _: self._toggle_connect())

        self._conn_btn = tk.Button(
            room_row,
            text="Connect",
            font=("Menlo", 13, "bold"),
            bg=C["orange"], fg="#1a1005",
            activebackground="#e06910", activeforeground="#1a1005",
            relief="flat", cursor="hand2",
            padx=16, pady=6,
            command=self._toggle_connect,
        )
        self._conn_btn.pack(side="left")

        tk.Label(conn, text="BROKER", font=("Menlo", 9),
                 bg=C["deck"], fg=C["muted"]).pack(anchor="w")
        self._broker_var = tk.StringVar(value=DEFAULT_BROKER)
        tk.Entry(
            conn,
            textvariable=self._broker_var,
            font=("Menlo", 10), bg=C["bg"], fg=C["muted"],
            insertbackground=C["text"], relief="flat",
            highlightthickness=1, highlightbackground=C["line"],
            highlightcolor=C["orange"],
        ).pack(fill="x", ipady=3, pady=(4, 0))

        self._sep()

        # ── Status row ────────────────────────────────────────────────────────
        sr = tk.Frame(r, bg=C["bg"])
        sr.pack(fill="x", padx=18, pady=9)
        self._dot = tk.Label(sr, text="●", font=("Menlo", 11),
                             bg=C["bg"], fg=C["muted"])
        self._dot.pack(side="left")
        self._status_lbl = tk.Label(sr, text="not connected", font=("Menlo", 11),
                                    bg=C["bg"], fg=C["muted"])
        self._status_lbl.pack(side="left", padx=(7, 0))
        self._sep()

        # ── Test-mode banner (always present; invisible when not focused) ─────
        # Uses a fixed-height label that changes colour rather than packing /
        # unpacking so the layout beneath it doesn't jump.
        self._banner = tk.Label(
            r,
            text="  ",          # non-empty so height is stable
            font=("Menlo", 10, "bold"),
            bg=C["bg"], fg=C["bg"],
            pady=7,
        )
        self._banner.pack(fill="x")
        self._sep()

        # ── Big number display ────────────────────────────────────────────────
        disp = tk.Frame(r, bg=C["bg"])
        disp.pack(fill="x", padx=18, pady=(18, 18))

        self._id_lbl = tk.Label(
            disp,
            text="·······",
            font=("Menlo", 46, "bold"),
            bg=C["bg"], fg=C["line"],
        )
        self._id_lbl.pack()

        self._id_sub = tk.Label(
            disp,
            text="waiting for first scan",
            font=("Menlo", 11),
            bg=C["bg"], fg=C["muted"],
        )
        self._id_sub.pack()
        self._sep()

        # ── History ───────────────────────────────────────────────────────────
        hist_hdr = tk.Frame(r, bg=C["bg"])
        hist_hdr.pack(fill="x", padx=18, pady=(8, 4))
        tk.Label(hist_hdr, text="RECENT SCANS", font=("Menlo", 9),
                 bg=C["bg"], fg=C["muted"]).pack(side="left")
        tk.Button(
            hist_hdr, text="copy CSV",
            font=("Menlo", 9), bg=C["bg"], fg=C["muted"],
            activebackground=C["deck"], relief="flat", cursor="hand2",
            command=self._copy_csv,
        ).pack(side="right")

        self._hist_box = tk.Listbox(
            r,
            font=("Menlo", 12),
            bg=C["deck"], fg=C["text"],
            selectbackground=C["line"],
            activestyle="none",
            relief="flat", borderwidth=0,
            highlightthickness=0,
            height=5,
        )
        self._hist_box.pack(fill="x", padx=18, pady=(0, 16))

    # ── Focus polling (determines test-mode banner and typing behaviour) ───────

    def _is_app_focused(self) -> bool:
        try:
            return self.root.focus_displayof() is not None
        except Exception:
            return False

    def _poll_focus(self):
        focused = self._is_app_focused()
        if focused != self._app_focused:
            self._app_focused = focused
            self._update_banner(focused)
        self.root.after(200, self._poll_focus)

    def _update_banner(self, focused: bool):
        if focused:
            self._banner.configure(
                text="⚠   TEST MODE  —  window focused: scans display here, not typed   ⚠",
                bg=C["amber"], fg="#1a1005",
            )
        else:
            self._banner.configure(text="  ", bg=C["bg"], fg=C["bg"])

    # ── Connection ────────────────────────────────────────────────────────────

    def _auto_upper(self, *_):
        v = self._room_var.get()
        up = v.upper()
        if v != up:
            self._room_var.set(up)

    def _toggle_connect(self):
        if not self._connected:
            room = self._room_var.get().strip().upper()
            if not room:
                self._set_status("err", "enter a room code first")
                return
            self._connected = True
            self._conn_btn.configure(text="Disconnect")
            self._set_status("wait", "connecting…")
            broker = self._broker_var.get().strip() or DEFAULT_BROKER
            self.bridge.connect(room, broker)
        else:
            self._connected = False
            self.bridge.stop()
            self._conn_btn.configure(text="Connect")
            self._set_status("off", "not connected")

    def _set_status(self, kind: str, text: str):
        col = {"ok": C["green"], "wait": C["amber"],
               "err": C["red"],  "off": C["muted"]}.get(kind, C["muted"])
        self._dot.configure(fg=col)
        self._status_lbl.configure(text=text, fg=col)

    # ── Scan handling ─────────────────────────────────────────────────────────

    def _handle_scan(self, data: dict, sid: str):
        ts = datetime.now().strftime("%H:%M:%S")
        self._id_lbl.configure(text=sid, fg=C["text"])

        if self._is_app_focused():
            # Test mode — show the ID but don't type it anywhere
            self._id_sub.configure(
                text=f"test mode — not typed   {ts}",
                fg=C["amber"],
            )
            mode = "test"
        else:
            mode = self._type_id(sid, ts)

        icon = {"typed": "✓", "test": "◉", "error": "✗"}.get(mode, "?")
        row_text = f"  {ts}    {sid}    {icon} {mode}"
        self._hist_box.insert(0, row_text)
        if self._hist_box.size() > 50:
            self._hist_box.delete(50)

        self._hist_rows.insert(0, {
            "ts": ts, "id": sid, "mode": mode,
            "seq": data.get("seq"), "dev": data.get("dev"),
        })
        self._log_csv(ts, sid, mode, data)
        self.bridge.ack(data.get("seq"), data.get("dev"))

    def _type_id(self, sid: str, ts: str) -> str:
        try:
            if self._kb is None:
                from pynput.keyboard import Controller, Key
                self._kb = (Controller(), Key)
            ctrl, Key = self._kb
            ctrl.type(sid)
            ctrl.press(Key.enter)
            ctrl.release(Key.enter)
            self._id_sub.configure(text=f"typed ✓   {ts}", fg=C["green"])
            return "typed"
        except ImportError:
            self._id_sub.configure(
                text="pynput not installed — pip install pynput", fg=C["red"])
            return "error"
        except Exception as e:
            msg = str(e)
            if "accessibility" in msg.lower() or "permission" in msg.lower():
                self._id_sub.configure(
                    text="⚠ grant Accessibility in System Settings → Privacy → Accessibility",
                    fg=C["red"])
            else:
                self._id_sub.configure(text=f"error: {msg}   {ts}", fg=C["red"])
            return "error"

    # ── Persistence ───────────────────────────────────────────────────────────

    def _log_csv(self, ts: str, sid: str, mode: str, data: dict):
        try:
            needs_header = not LOG_PATH.exists()
            with open(LOG_PATH, "a", newline="") as f:
                w = csv.writer(f)
                if needs_header:
                    w.writerow(["timestamp", "id", "mode", "device", "seq"])
                w.writerow([ts, sid, mode, data.get("dev", ""), data.get("seq", "")])
        except Exception:
            pass

    def _copy_csv(self):
        rows = ["timestamp,id,mode,device,seq"]
        for h in reversed(self._hist_rows):
            rows.append(
                f"{h['ts']},{h['id']},{h['mode']},"
                f"{h.get('dev','')},{h.get('seq','')}"
            )
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append("\n".join(rows))
        except Exception:
            pass

    # ── Main loop bridge ──────────────────────────────────────────────────────

    def _poll_queue(self):
        try:
            while True:
                ev = self.q.get_nowait()
                if ev[0] == "status":
                    _, kind, text = ev
                    self._set_status(kind, text)
                elif ev[0] == "scan":
                    _, data, sid = ev
                    self._handle_scan(data, sid)
        except queue.Empty:
            pass
        self.root.after(80, self._poll_queue)


# ── entry point ───────────────────────────────────────────────────────────────

def main():
    root = tk.Tk()
    try:
        # Suppress the default Tk console window on macOS
        root.tk.call("::tk::unsupported::MacWindowStyle", "style",
                     root._w, "document", "closeBox miniaturizeBox")
    except Exception:
        pass

    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
