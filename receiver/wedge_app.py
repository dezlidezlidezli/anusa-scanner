#!/usr/bin/env python3
"""
ANUSA Scanner — macOS receiver app.

Enter the room code from the phone app and Connect. Then pick what each scan does:

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
import json
import os
import queue
import ssl
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog
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

# pynput (keystrokes) and the Google libraries (via sheets.py) are imported lazily
# so the app still opens if a mode's deps aren't ready — you see an in-app message.
import sheets

# ── constants ─────────────────────────────────────────────────────────────────

VERSION        = "14.42"   # shared version across the Mac app + web app
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
        self._kb         = None    # lazy: (Controller, Key) from pynput
        self._hist_rows  = []      # [{ts, id, mode, seq, dev}]
        self._app_focused = False
        self._sheet       = None   # sheets.SheetSession once signed in
        self._sheet_ready = False  # columns configured → ready to check in

        root.title("ANUSA Scanner")
        root.configure(bg=C["bg"])
        root.resizable(False, True)
        root.geometry("460x620")

        # Dock-icon click restores the window
        try:
            root.createcommand("tk::mac::ReopenApplication", root.deiconify)
        except Exception:
            pass

        self._build_ui()
        self._recompute_geometry()
        self._poll_queue()
        self._poll_focus()

        # Reflect an existing Google sign-in without opening a browser.
        if sheets.token_available():
            self._signin_lbl.configure(text="signing in…", fg=C["amber"])
            threading.Thread(target=self._silent_sign_in, daemon=True).start()

    # ── helpers ───────────────────────────────────────────────────────────────

    def _sep(self, parent=None):
        tk.Frame(parent or self.root, height=1, bg=C["line"]).pack(fill="x")

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

        # ── Setup (collapsible: connection + on-scan target) ─────────────────
        self._setup_hdr = tk.Frame(r, bg=C["bg"])
        self._setup_hdr.pack(fill="x", padx=18, pady=(8, 2))
        self._setup_toggle = tk.Label(
            self._setup_hdr, text="▾ SETUP", font=("Menlo", 9, "bold"),
            bg=C["bg"], fg=C["muted"], cursor="hand2")
        self._setup_toggle.pack(side="left")
        self._setup_toggle.bind("<Button-1>", lambda _: self._toggle_setup())
        self._setup_summary = tk.Label(self._setup_hdr, text="", font=("Menlo", 9),
                                       bg=C["bg"], fg=C["line"])
        self._setup_summary.pack(side="right")

        self._setup_body = tk.Frame(r, bg=C["bg"])
        self._setup_body.pack(fill="x")
        self._build_connection(self._setup_body)
        self._sep(self._setup_body)
        self._build_mode_section(self._setup_body)
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

        # ── Test-mode banner (keystroke mode only; invisible otherwise) ───────
        self._banner = tk.Label(
            r, text="  ", font=("Menlo", 10, "bold"),
            bg=C["bg"], fg=C["bg"], pady=7,
        )
        self._banner.pack(fill="x")
        self._sep()

        # ── Big number display ────────────────────────────────────────────────
        disp = tk.Frame(r, bg=C["bg"])
        disp.pack(fill="x", padx=18, pady=(18, 18))

        self._id_lbl = tk.Label(
            disp, text="·······", font=("Menlo", 46, "bold"),
            bg=C["bg"], fg=C["line"],
        )
        self._id_lbl.pack()

        self._id_sub = tk.Label(
            disp, text="waiting for first scan", font=("Menlo", 11),
            bg=C["bg"], fg=C["muted"],
        )
        self._id_sub.pack()
        self._sep()

        # ── Manual entry (type an ID here when a card won't scan) ─────────────
        man = tk.Frame(r, bg=C["bg"])
        man.pack(fill="x", padx=18, pady=(8, 6))
        tk.Label(man, text="MANUAL", font=("Menlo", 9),
                 bg=C["bg"], fg=C["muted"]).pack(side="left", padx=(0, 8))
        self._manual_var = tk.StringVar()
        me = tk.Entry(man, textvariable=self._manual_var, font=("Menlo", 14),
                      bg=C["bg"], fg=C["text"], insertbackground=C["text"], relief="flat",
                      highlightthickness=1, highlightbackground=C["line"],
                      highlightcolor=C["orange"])
        me.pack(side="left", fill="x", expand=True, ipady=4)
        me.bind("<Return>", lambda _: self._manual_submit())
        tk.Button(man, text="Enter", font=("Menlo", 12, "bold"), bg=C["orange"],
                  fg="#1a1005", activebackground="#e06910", relief="flat", cursor="hand2",
                  padx=14, pady=4, command=self._manual_submit).pack(side="left", padx=(8, 0))
        self._sep()

        # ── History ───────────────────────────────────────────────────────────
        hist_hdr = tk.Frame(r, bg=C["bg"])
        hist_hdr.pack(fill="x", padx=18, pady=(8, 4))
        tk.Label(hist_hdr, text="RECENT SCANS", font=("Menlo", 9),
                 bg=C["bg"], fg=C["muted"]).pack(side="left")
        tk.Button(
            hist_hdr, text="download CSV",
            font=("Menlo", 9), bg=C["bg"], fg=C["muted"],
            activebackground=C["deck"], relief="flat", cursor="hand2",
            command=self._download_csv,
        ).pack(side="right")

        self._hist_box = tk.Listbox(
            r, font=("Menlo", 12),
            bg=C["deck"], fg=C["text"], selectbackground=C["line"],
            activestyle="none", relief="flat", borderwidth=0,
            highlightthickness=0, height=5,
        )
        self._hist_box.pack(fill="x", padx=18, pady=(0, 16))

    def _build_connection(self, parent):
        conn_bg = tk.Frame(parent, bg=C["deck"])
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
            room_row, textvariable=self._room_var, font=("Menlo", 20), width=8,
            bg=C["bg"], fg=C["text"], insertbackground=C["text"], relief="flat",
            highlightthickness=1, highlightbackground=C["line"], highlightcolor=C["orange"])
        room_entry.pack(side="left", ipady=6, padx=(0, 10))
        room_entry.bind("<Return>", lambda _: self._toggle_connect())
        self._conn_btn = tk.Button(
            room_row, text="Connect", font=("Menlo", 13, "bold"), bg=C["orange"],
            fg="#1a1005", activebackground="#e06910", activeforeground="#1a1005",
            relief="flat", cursor="hand2", padx=16, pady=6, command=self._toggle_connect)
        self._conn_btn.pack(side="left")

        tk.Label(conn, text="BROKER", font=("Menlo", 9),
                 bg=C["deck"], fg=C["muted"]).pack(anchor="w")
        self._broker_var = tk.StringVar(value=DEFAULT_BROKER)
        tk.Entry(
            conn, textvariable=self._broker_var, font=("Menlo", 10), bg=C["bg"],
            fg=C["muted"], insertbackground=C["text"], relief="flat", highlightthickness=1,
            highlightbackground=C["line"], highlightcolor=C["orange"]).pack(
                fill="x", ipady=3, pady=(4, 0))

    def _build_mode_section(self, r):
        mode_bg = tk.Frame(r, bg=C["deck"])
        mode_bg.pack(fill="x")
        mode = tk.Frame(mode_bg, bg=C["deck"])
        mode.pack(fill="x", padx=18, pady=(10, 4))
        tk.Label(mode, text="ON SCAN", font=("Menlo", 9),
                 bg=C["deck"], fg=C["muted"]).pack(anchor="w")

        self._mode_var = tk.StringVar(value="keys")
        row = tk.Frame(mode, bg=C["deck"])
        row.pack(fill="x", pady=(4, 0))
        for val, lbl in (("keys", "Type keystrokes"), ("sheet", "Google Sheet")):
            tk.Radiobutton(
                row, text=lbl, value=val, variable=self._mode_var,
                command=self._on_mode_change, font=("Menlo", 11),
                bg=C["deck"], fg=C["text"], selectcolor=C["bg"],
                activebackground=C["deck"], activeforeground=C["text"],
                highlightthickness=0, bd=0,
            ).pack(side="left", padx=(0, 16))

        # Google Sheet config panel — shown only in sheet mode.
        self._sheet_wrap = tk.Frame(mode_bg, bg=C["deck"])
        sp = tk.Frame(self._sheet_wrap, bg=C["deck"])
        sp.pack(fill="x", padx=18, pady=(2, 12))

        signin = tk.Frame(sp, bg=C["deck"])
        signin.pack(fill="x", pady=(2, 6))
        self._signin_btn = tk.Button(
            signin, text="Sign in with Google", font=("Menlo", 11),
            bg=C["line"], fg=C["text"], activebackground=C["deck"],
            relief="flat", cursor="hand2", padx=10, pady=4, command=self._sign_in,
        )
        self._signin_btn.pack(side="left")
        self._signin_lbl = tk.Label(signin, text="not signed in", font=("Menlo", 10),
                                    bg=C["deck"], fg=C["muted"])
        self._signin_lbl.pack(side="left", padx=(10, 0))

        url_row = tk.Frame(sp, bg=C["deck"])
        url_row.pack(fill="x", pady=(2, 6))
        self._url_var = tk.StringVar()
        tk.Entry(url_row, textvariable=self._url_var, font=("Menlo", 10),
                 bg=C["bg"], fg=C["text"], insertbackground=C["text"], relief="flat",
                 highlightthickness=1, highlightbackground=C["line"],
                 highlightcolor=C["orange"]).pack(side="left", fill="x", expand=True, ipady=3)
        self._load_btn = tk.Button(
            url_row, text="Load", font=("Menlo", 11, "bold"), bg=C["orange"],
            fg="#1a1005", activebackground="#e06910", relief="flat",
            cursor="hand2", padx=12, pady=4, command=self._load_sheet,
        )
        self._load_btn.pack(side="left", padx=(8, 0))

        self._col_vars = {"id": tk.StringVar(), "tick": tk.StringVar(),
                          "name": tk.StringVar()}
        self._col_menus = {}
        for key, lbl in (("id", "ID column"), ("tick", "Tick column"),
                         ("name", "Name column")):
            crow = tk.Frame(sp, bg=C["deck"])
            crow.pack(fill="x", pady=1)
            tk.Label(crow, text=lbl, font=("Menlo", 9), bg=C["deck"], fg=C["muted"],
                     width=11, anchor="w").pack(side="left")
            om = tk.OptionMenu(crow, self._col_vars[key], "")
            om.configure(font=("Menlo", 10), bg=C["bg"], fg=C["text"],
                         activebackground=C["line"], highlightthickness=0,
                         relief="flat", anchor="w")
            om["menu"].configure(bg=C["deck"], fg=C["text"])
            om.pack(side="left", fill="x", expand=True)
            self._col_menus[key] = om
        for v in self._col_vars.values():
            v.trace_add("write", lambda *_: self._apply_columns())

        status_row = tk.Frame(sp, bg=C["deck"])
        status_row.pack(fill="x", pady=(6, 0))
        self._sheet_status = tk.Label(status_row, text="sign in, then load a sheet",
                                      font=("Menlo", 10), bg=C["deck"], fg=C["muted"],
                                      anchor="w", justify="left")
        self._sheet_status.pack(side="left", fill="x", expand=True)
        self._sync_btn = tk.Button(
            status_row, text="↻ Re-sync", font=("Menlo", 10), bg=C["line"], fg=C["text"],
            activebackground=C["deck"], relief="flat", cursor="hand2", padx=8, pady=2,
            command=self._sync)
        self._sync_btn.pack(side="right")

    # ── Focus polling (test-mode banner is keystroke-mode only) ───────────────

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
        if focused and self._mode_var.get() == "keys":
            self._banner.configure(
                text="⚠   TEST MODE  —  window focused: scans display here, not typed   ⚠",
                bg=C["amber"], fg="#1a1005",
            )
        else:
            self._banner.configure(text="  ", bg=C["bg"], fg=C["bg"])

    def _on_mode_change(self):
        if self._mode_var.get() == "sheet":
            self._sheet_wrap.pack(fill="x")
        else:
            self._sheet_wrap.pack_forget()
        self._recompute_geometry()
        self._update_banner(self._app_focused)

    def _toggle_setup(self):
        if self._setup_body.winfo_ismapped():
            self._setup_body.pack_forget()
            self._setup_toggle.configure(text="▸ SETUP")
            self._update_setup_summary()
        else:
            self._setup_body.pack(fill="x", after=self._setup_hdr)
            self._setup_toggle.configure(text="▾ SETUP")
            self._setup_summary.configure(text="")
        self._recompute_geometry()

    def _update_setup_summary(self):
        room = self._room_var.get().strip() or "—"
        mode = "Sheet" if self._mode_var.get() == "sheet" else "Keys"
        conn = "●" if self._connected else "○"
        self._setup_summary.configure(text=f"{conn} {room} · {mode}")

    def _recompute_geometry(self):
        if not self._setup_body.winfo_ismapped():
            h = 490
        elif self._mode_var.get() == "sheet":
            h = 915
        else:
            h = 695
        self.root.geometry(f"460x{h}")

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
        if not self._setup_body.winfo_ismapped():
            self._update_setup_summary()

    def _set_status(self, kind: str, text: str):
        col = {"ok": C["green"], "wait": C["amber"],
               "err": C["red"],  "off": C["muted"]}.get(kind, C["muted"])
        self._dot.configure(fg=col)
        self._status_lbl.configure(text=text, fg=col)

    # ── Google Sheet setup ────────────────────────────────────────────────────

    def _sign_in(self):
        self._signin_lbl.configure(text="opening browser…", fg=C["amber"])
        self._signin_btn.configure(state="disabled")
        threading.Thread(target=self._do_sign_in, args=(True,), daemon=True).start()

    def _silent_sign_in(self):
        self._do_sign_in(interactive=False)

    def _do_sign_in(self, interactive):
        try:
            svc = sheets.build_service(interactive=interactive)
            self.q.put(("sheet_auth", svc, None))
        except Exception as e:
            self.q.put(("sheet_auth", None, str(e)))

    def _load_sheet(self):
        if not self._sheet:
            self._sheet_status.configure(text="sign in first", fg=C["red"])
            return
        url = self._url_var.get().strip()
        if not url:
            self._sheet_status.configure(text="paste a sheet URL", fg=C["amber"])
            return
        self._sheet_status.configure(text="loading…", fg=C["amber"])
        self._load_btn.configure(state="disabled")
        threading.Thread(target=self._do_load, args=(url,), daemon=True).start()

    def _do_load(self, url):
        try:
            info = self._sheet.open(url)
            guess = self._sheet.guess_columns()
            self.q.put(("sheet_loaded", info, guess))
        except Exception as e:
            self.q.put(("sheet_error", str(e)))

    def _sync(self):
        """Re-read the sheet so ticks set elsewhere (other stations / manual edits)
        are reflected, without losing the loaded columns."""
        if not self._sheet or self._sheet.sid is None:
            self._sheet_status.configure(text="load a sheet first", fg=C["amber"])
            return
        self._sheet_status.configure(text="re-syncing…", fg=C["amber"])
        self._sync_btn.configure(state="disabled")
        threading.Thread(target=self._do_sync, daemon=True).start()

    def _do_sync(self):
        try:
            self._sheet.refresh()
            self.q.put(("sheet_synced", self._sheet.tab, max(0, len(self._sheet.values) - 1)))
        except Exception as e:
            self.q.put(("sheet_error", str(e)))

    def _populate_columns(self, headers, guess):
        id_g, tick_g, name_g = guess
        self._fill_menu("id", headers, id_g or (headers[0] if headers else ""))
        self._fill_menu("tick", headers, tick_g or "")
        self._fill_menu("name", ["(none)"] + list(headers), name_g or "(none)")

    def _fill_menu(self, key, options, selected):
        om = self._col_menus[key]
        var = self._col_vars[key]
        menu = om["menu"]
        menu.delete(0, "end")
        for opt in options:
            menu.add_command(label=opt, command=lambda v=opt, var=var: var.set(v))
        var.set(selected)   # fires _apply_columns via trace

    def _apply_columns(self):
        if not self._sheet or not self._sheet.headers:
            return
        idc = self._col_vars["id"].get()
        tickc = self._col_vars["tick"].get()
        namec = self._col_vars["name"].get()
        if not idc or not tickc:
            self._sheet_ready = False
            return
        try:
            self._sheet.set_columns(idc, tickc,
                                    None if namec in ("", "(none)") else namec)
            self._sheet_ready = True
            self._sheet_status.configure(
                text=f"ready · {self._sheet.tab} · check in on {idc} → {tickc}",
                fg=C["green"])
        except Exception as e:
            self._sheet_ready = False
            self._sheet_status.configure(text=f"column error: {e}", fg=C["red"])

    # ── Scan handling ─────────────────────────────────────────────────────────

    def _manual_submit(self):
        """Process a hand-typed ID exactly like a scan (check-in or keystroke)."""
        sid = "".join(ch for ch in self._manual_var.get() if ch.isdigit())
        if not sid:
            return
        self._manual_var.set("")
        self._handle_scan({"seq": None, "dev": "manual"}, sid)

    def _handle_scan(self, data: dict, sid: str):
        ts = datetime.now().strftime("%H:%M:%S")
        self._id_lbl.configure(text="u" + sid, fg=C["text"])

        if self._mode_var.get() == "sheet":
            if not self._sheet_ready:
                self._id_sub.configure(text="set up the Google Sheet first", fg=C["amber"])
                return
            self._id_sub.configure(text="checking in…", fg=C["muted"])
            threading.Thread(target=self._do_checkin, args=(data, sid, ts),
                             daemon=True).start()
            return

        # keystroke mode
        if self._is_app_focused():
            self._id_sub.configure(text=f"test mode — not typed   {ts}", fg=C["amber"])
            mode = "test"
        else:
            mode = self._type_id(sid, ts)
        self._record(ts, sid, mode, data)
        self.bridge.ack(data.get("seq"), data.get("dev"))

    def _do_checkin(self, data, sid, ts):
        try:
            res = self._sheet.check_in(sid)
        except Exception as e:
            res = {"status": "error", "name": "", "err": str(e)}
        self.q.put(("checkin", data, sid, ts, res))

    def _checkin_result(self, data, sid, ts, res):
        status = res.get("status", "error")
        name = res.get("name", "")
        colour = {"checked-in": C["green"], "already": C["amber"],
                  "not-registered": C["red"], "error": C["red"]}.get(status, C["muted"])
        label = {"checked-in": "checked in ✓", "already": "already checked in",
                 "not-registered": "not registered", "error": "sheet error — stopped"}.get(
                     status, status)
        sub = label + (f"  ·  {name}" if name and status != "error" else "")
        self._id_sub.configure(text=f"{sub}   {ts}", fg=colour)
        self._record(ts, sid, status, data)
        self.bridge.send_status(data.get("seq"), data.get("dev"), status, name, sid)

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

    def _record(self, ts: str, sid: str, label: str, data: dict):
        icon = {"typed": "✓", "test": "◉", "checked-in": "✓", "already": "•",
                "not-registered": "✗", "error": "✗"}.get(label, "?")
        self._hist_box.insert(0, f"  {ts}    u{sid}    {icon} {label}")
        if self._hist_box.size() > 50:
            self._hist_box.delete(50)
        self._hist_rows.insert(0, {
            "ts": ts, "id": sid, "mode": label,
            "seq": data.get("seq"), "dev": data.get("dev"),
        })
        self._log_csv(ts, sid, label, data)

    def _log_csv(self, ts: str, sid: str, mode: str, data: dict):
        try:
            needs_header = not LOG_PATH.exists()
            with open(LOG_PATH, "a", newline="") as f:
                w = csv.writer(f)
                if needs_header:
                    w.writerow(["timestamp", "id", "result", "device", "seq"])
                w.writerow([ts, sid, mode, data.get("dev", ""), data.get("seq", "")])
        except Exception:
            pass

    def _download_csv(self):
        if not self._hist_rows:
            self._id_sub.configure(text="no scans to download yet", fg=C["amber"])
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".csv",
            initialfile=f"anusa_scans_{datetime.now().strftime('%Y%m%d_%H%M')}.csv",
            filetypes=[("CSV files", "*.csv")], title="Download scans CSV")
        if not path:
            return
        rows = ["timestamp,id,result,device,seq"]
        for h in reversed(self._hist_rows):
            rows.append(
                f"{h['ts']},{h['id']},{h['mode']},{h.get('dev','')},{h.get('seq','')}")
        try:
            with open(path, "w", newline="") as f:
                f.write("\n".join(rows) + "\n")
            self._id_sub.configure(
                text=f"saved CSV · {len(self._hist_rows)} rows", fg=C["green"])
        except Exception as e:
            self._id_sub.configure(text=f"CSV save failed: {e}", fg=C["red"])

    # ── Main loop bridge ──────────────────────────────────────────────────────

    def _poll_queue(self):
        try:
            while True:
                ev = self.q.get_nowait()
                kind = ev[0]
                if kind == "status":
                    self._set_status(ev[1], ev[2])
                elif kind == "scan":
                    self._handle_scan(ev[1], ev[2])
                elif kind == "checkin":
                    self._checkin_result(ev[1], ev[2], ev[3], ev[4])
                elif kind == "sheet_auth":
                    self._on_sheet_auth(ev[1], ev[2])
                elif kind == "sheet_loaded":
                    self._load_btn.configure(state="normal")
                    self._populate_columns(ev[1]["headers"], ev[2])
                elif kind == "sheet_synced":
                    self._sync_btn.configure(state="normal")
                    self._sheet_status.configure(
                        text=f"re-synced · {ev[1]} · {ev[2]} rows", fg=C["green"])
                elif kind == "sheet_error":
                    self._load_btn.configure(state="normal")
                    self._sync_btn.configure(state="normal")
                    self._sheet_status.configure(text=f"failed: {ev[1][:44]}",
                                                 fg=C["red"])
        except queue.Empty:
            pass
        self.root.after(80, self._poll_queue)

    def _on_sheet_auth(self, svc, err):
        self._signin_btn.configure(state="normal")
        if svc is not None:
            self._sheet = sheets.SheetSession(svc)
            self._signin_lbl.configure(text="signed in ✓", fg=C["green"])
        else:
            self._signin_lbl.configure(text="not signed in", fg=C["muted"])
            if err and "not signed in" not in err:
                self._sheet_status.configure(text=f"sign-in failed: {err[:44]}", fg=C["red"])


# ── entry point ───────────────────────────────────────────────────────────────

def _selftest():
    """Headless check that the Google stack is importable + buildable in this
    (possibly frozen) build. Exits 0 on success. Needs a cached token."""
    try:
        svc = sheets.build_service(interactive=False)
        svc.spreadsheets()   # touch the discovery-built resource
        print("SELFTEST OK: google libs import + sheets service built")
        return 0
    except Exception as e:
        print(f"SELFTEST FAIL: {type(e).__name__}: {e}")
        return 1


def main():
    if "--selftest" in sys.argv:
        sys.exit(_selftest())

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
