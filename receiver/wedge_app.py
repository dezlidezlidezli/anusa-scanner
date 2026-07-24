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
import re
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

VERSION        = "14.80"   # shared version across the Mac app + web app
DEFAULT_BROKER = "wss://broker.emqx.io:8084/mqtt"
PWA_URL        = "https://dezlidezlidezli.github.io/anusa-scanner/"  # for pairing QR
LOG_PATH       = Path.home() / "Documents" / "ANUSAScanner_scans.csv"
TB_LOG_PATH    = Path.home() / "Documents" / "ANUSAScanner_textbooks.csv"


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

    def send_roster(self, rows):
        """Push the roster + tick state to the paired phones so they can show results
        (name / registered / already) instantly, with no round-trip. `rows` = [[uid,name,ticked]]."""
        self._publish({"t": "roster", "r": rows})

    def send_mode(self, mode):
        """Tell the phones which mode the receiver is in, so they run the right scan flow
        (e.g. the two-stage student→textbook flow in Textbook Library mode)."""
        self._publish({"t": "mode", "mode": mode})

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
            if t not in ("scan", "tbpair"):
                return
            pair = (data.get("dev"), data.get("seq"))
            if pair in self._seen_s:
                return
            if len(self._seen) == self._seen.maxlen:
                self._seen_s.discard(self._seen.popleft())
            self._seen.append(pair)
            self._seen_s.add(pair)
            if t == "tbpair":                      # Textbook Library: a student↔textbook pairing
                self._q.put(("tbpair", data))
                return
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
        self._tblog = []             # Textbook Library: [{ts,student,code}]

    # called once the window + GUI loop are up
    def attach(self, window):
        self.window = window
        threading.Thread(target=self._drain, daemon=True).start()
        self._emit("version", {"v": VERSION})
        self._emit_auth()      # tell the UI the saved auth mode + whether it's ready on open
        if sheets.auth_ready():
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

    def _emit_auth(self):
        """Single source of truth for the Settings auth panel: which mode is selected, whether
        it's ready to use without interaction, and (service account) the email to share with."""
        mode = sheets.get_auth_mode()
        self._emit("auth", {
            "mode": mode,
            "ready": sheets.auth_ready(mode),
            "sa_present": sheets.has_service_account(),
            "sa_email": sheets.service_account_email() or "",
        })

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
        elif kind == "tbpair":
            self._tbpair(ev[1])
        elif kind == "paired":
            self._emit("paired", {"dev": ev[1]})
            self.bridge.send_mode(self.mode)   # tell the (re)joined phone the current mode
            self._push_roster()   # a phone (re)joined → give it the roster for instant results

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
            # This window has focus, so typing would land in the receiver itself, not the
            # target app — so we deliberately DON'T type. Tell the phone too, or its row
            # would sit on "sent" while this screen says "not typed" (a UI/backend mismatch).
            self._record(ts, sid, "test")
            self._emit("result", {"status": "test", "id": sid, "name": "", "ts": ts})
            self.bridge.send_status(data.get("seq"), data.get("dev"), "test", "", sid)
            return
        ok = self._type_id(sid)
        self._record(ts, sid, "typed" if ok else "error")
        self._emit("result", {"status": "typed" if ok else "error",
                              "id": sid, "name": "", "ts": ts})
        self.bridge.ack(data.get("seq"), data.get("dev"))

    @staticmethod
    def _is_permission_error(e):
        """A Google 'no access' error — in service-account mode this almost always means the
        sheet hasn't been shared with the service account's email."""
        status = getattr(getattr(e, "resp", None), "status", None)
        if status == 403:
            return True
        s = str(e).lower()
        return "permission" in s or "does not have permission" in s or "permission_denied" in s

    def _sa_permission(self, e):
        """True + surface the 'share the sheet' prompt when a service-account op is blocked."""
        if self._is_permission_error(e) and sheets.get_auth_mode() == "service_account":
            self._emit("sa_share_needed", {"email": sheets.service_account_email() or ""})
            return True
        return False

    def _do_checkin(self, data, sid, ts):
        # Optimistic: decide the outcome from the loaded sheet (fast) and show it NOW, then
        # write the tick to Google Sheets in the background — the slow API write no longer
        # sits between the scan and the result. Only a write FAILURE corrects the UI.
        try:
            plan = self.sheet.plan_checkin(sid)
        except Exception as e:
            self.q.put(("checkin", data, sid, ts, {"status": "error", "name": str(e)}))
            return
        self.q.put(("checkin", data, sid, ts, plan))
        if plan.get("status") == "checked-in":
            try:
                self.sheet.commit_checkin(plan)
            except Exception as e:
                msg = ("share the sheet as Editor with the service account"
                       if self._sa_permission(e) else "sheet write failed — rescan")
                self.q.put(("checkin", data, sid, ts,
                            {"status": "error", "name": msg, "id": sid}))

    def _checkin_result(self, data, sid, ts, res):
        status = res.get("status", "error")
        name = res.get("name", "")
        if status == "fuzzy":
            # Near-miss (one digit off a roster UID). Don't record/tick yet — ask the
            # operator to confirm by name (cures form-typos in the sheet).
            self._emit("fuzzy", {"scanned": sid, "id": res.get("id", ""),
                                 "name": name, "ts": ts})
            self.bridge.send_status(data.get("seq"), data.get("dev"), "fuzzy", name, sid)
            return
        self._record(ts, sid, status)
        self._emit("result", {"status": status, "id": sid,
                              "name": name if status != "error" else "", "ts": ts})
        self.bridge.send_status(data.get("seq"), data.get("dev"), status, name, sid)
        if status == "checked-in":
            self._push_attendance()

    def confirm_fuzzy(self, cand_id):
        """Operator confirmed a fuzzy match — check in the candidate UID (exact match)."""
        cand_id = "".join(ch for ch in str(cand_id or "") if ch.isdigit())
        if cand_id:
            self._handle_scan({"seq": None, "dev": "manual"}, cand_id)

    def _push_sheet_data(self):
        try:
            self._emit("roster", {"list": self.sheet.roster()})
        except Exception:
            pass
        self._push_attendance()
        self._push_roster()

    def _push_attendance(self):
        try:
            self._emit("attendance", self.sheet.attendance())
        except Exception:
            pass

    def _push_roster(self):
        """Send the roster + tick state to the paired phones (MQTT) so they can show scan
        results instantly. No-op until the sheet is set up."""
        if not (self.sheet and self.sheet_ready):
            return
        try:
            self.bridge.send_roster(self.sheet.roster_state())
        except Exception:
            pass

    # Virtual keycodes for the digit keys + Return (US layout, layout-independent for digits).
    _MAC_KEYCODES = {"0": 29, "1": 18, "2": 19, "3": 20, "4": 21,
                     "5": 23, "6": 22, "7": 26, "8": 28, "9": 25}
    _MAC_RETURN = 36

    def _type_id(self, sid):
        """Type the number + Enter into the frontmost app.

        macOS: post CGEvents with explicit digit keycodes via Quartz. We must NOT use
        pynput here — pynput looks the character up through HIToolbox/TSM, which macOS only
        permits on the main thread, and this runs on the MQTT drain thread. Doing it
        in-process aborted the whole app (SIGTRAP, see crash reports). CGEventPost is
        thread-safe and needs only the Accessibility permission we already require — no new
        Automation prompt, and no TSM lookup (we type digits by keycode). Other platforms
        fall back to pynput."""
        try:
            if sys.platform == "darwin":
                import Quartz
                def tap(code):
                    for down in (True, False):
                        ev = Quartz.CGEventCreateKeyboardEvent(None, code, down)
                        Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
                for ch in str(sid):
                    code = self._MAC_KEYCODES.get(ch)
                    if code is not None:
                        tap(code)
                tap(self._MAC_RETURN)
                return True
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

    # ── Textbook Library ─────────────────────────────────────────────────────
    def _tbpair(self, data):
        """A phone paired a student number with a textbook code. Log the pairing and show it."""
        ts = datetime.now().strftime("%H:%M:%S")
        student = re.sub(r"\D", "", str(data.get("student", "")))
        code = str(data.get("code", "")).strip().upper()
        if not (student and code):
            return
        self._tblog.insert(0, {"ts": ts, "student": student, "code": code})
        self._log_pair_csv(ts, student, code)
        self._emit("tbpair", {"student": student, "code": code, "ts": ts})

    def _log_pair_csv(self, ts, student, code):
        try:
            needs_header = not TB_LOG_PATH.exists()
            with open(TB_LOG_PATH, "a", newline="") as f:
                w = csv.writer(f)
                if needs_header:
                    w.writerow(["timestamp", "student", "textbook"])
                w.writerow([ts, student, code])
        except Exception:
            pass

    # ── JS-callable methods ──────────────────────────────────────────────────
    def get_state(self):
        self._emit("version", {"v": VERSION})
        self._emit("mode", {"mode": self.mode})   # UI adopts the backend's real mode
        self._emit_auth()
        self._emit("user_info", {"initials": sheets.get_user_initials()})
        self.push_recent_sheets()
        return {"version": VERSION, "mode": self.mode}

    def set_user_initials(self, v):
        return sheets.set_user_initials(v)

    def remember_sheet(self, url, title):
        """Add the just-loaded sheet to the current mode's recently-used list and push it."""
        try:
            sheets.remember_recent_sheet(self.mode, sheets.spreadsheet_id(url), title, url)
            self._emit("recent_sheets",
                       {"mode": self.mode, "list": sheets.recent_sheets(self.mode)})
        except Exception:
            pass

    def push_recent_sheets(self):
        for m in ("sheet", "textbook"):
            self._emit("recent_sheets", {"mode": m, "list": sheets.recent_sheets(m)})

    def set_focus(self, f):
        self.focused = bool(f)

    def set_mode(self, m):
        self.mode = m if m in ("keys", "sheet", "textbook") else "keys"
        self.bridge.send_mode(self.mode)   # tell the phones which scan flow to run
        # Sheet mode → give phones the roster for instant results; other modes → clear it so a
        # stale roster can't make phones show sheet-style results.
        if self.mode == "sheet":
            self._push_roster()
        else:
            try:
                self.bridge.send_roster([])
            except Exception:
                pass

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

    def set_auth_mode(self, mode):
        """Switch between OAuth and service-account auth (persisted). Rebuilds the session
        non-interactively if the newly-selected mode is already set up; otherwise the UI
        prompts for set-up. Never opens a browser — that's the explicit 'Sign in' button."""
        mode = sheets.set_auth_mode(mode)
        self.sheet = None
        if sheets.auth_ready(mode):
            self._do_sign_in(False)
        else:
            self._emit_auth()
        return mode

    def sign_in(self):
        sheets.set_auth_mode("oauth")   # the browser sign-in button implies OAuth mode
        threading.Thread(target=lambda: self._do_sign_in(True), daemon=True).start()

    def use_service_account(self):
        """Pick a service-account JSON key → switch Union Pantry to service-account auth (no
        user sign-in, no token expiry). The key is copied into the app's support dir; roster
        sheets just need to be shared with the key's client_email."""
        try:
            sel = self.window.create_file_dialog(
                webview.OPEN_DIALOG, allow_multiple=False, file_types=("JSON key (*.json)",))
        except Exception:
            sel = None
        if not sel:
            return
        path = sel[0] if isinstance(sel, (list, tuple)) else sel
        try:
            with open(path) as f:
                data = json.load(f)
            if data.get("type") != "service_account" or not data.get("client_email"):
                self._emit("sheet_status", {"text": "not a service-account JSON key", "kind": "bad"})
                return
            import shutil
            shutil.copyfile(path, sheets.service_account_dest())
        except Exception as e:
            self._emit("sheet_status", {"text": f"couldn't load key: {str(e)[:40]}", "kind": "bad"})
            return
        sheets.set_auth_mode("service_account")   # loading a key selects service-account mode
        self._emit("sa_email", {"email": data.get("client_email", "")})
        self._do_sign_in(False)   # picks up the new key → builds the service, signed_in{sa:true}

    def clear_service_account(self):
        """Remove a UI-loaded service-account key."""
        try:
            os.remove(sheets.service_account_dest())
        except FileNotFoundError:
            pass
        if sheets.get_auth_mode() == "service_account":
            self.sheet = None
        self._emit("signed_in", {"ok": False, "sa": False})
        self._emit_auth()

    def _do_sign_in(self, interactive):
        try:
            svc = sheets.build_service(interactive=interactive)
            self.sheet = sheets.SheetSession(svc)
            self._emit("signed_in", {"ok": True, "sa": sheets.get_auth_mode() == "service_account"})
        except Exception as e:
            if interactive:
                self._emit("sheet_status", {"text": f"sign-in failed: {str(e)[:40]}", "kind": "bad"})
        finally:
            self._emit_auth()   # keep the Settings panel's ready/not-ready state in sync

    def load_sheet(self, url):
        if not self.sheet:
            self._emit("sheet_status", {"text": "sign in first", "kind": "bad"})
            return
        threading.Thread(target=self._do_load, args=(url,), daemon=True).start()

    def _do_load(self, url):
        try:
            info = self.sheet.open(url)
            guess = self.sheet.guess_columns()
            self.remember_sheet(url, info.get("title", ""))   # recently-used list (per mode)
            self._emit("sheet_loaded", {"tab": info["tab"], "rows": info["rows"],
                                       "headers": info["headers"], "guess": list(guess)})
        except Exception as e:
            if self._sa_permission(e):
                # Not shared with the service account — block set-up; the UI shows the email.
                self.sheet_ready = False
                self._emit("sheet_status",
                           {"text": "sheet not shared with the service account", "kind": "bad"})
            else:
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
            self._push_sheet_data()   # roster (for autofill) + attendance %
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
            if self.sheet_ready:
                self._push_sheet_data()
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
    """Headless check that every backend dep imports in this (possibly frozen) build.
    Exits 0 on success. Auth state is NOT required — the cached token expires every 7 days
    in Testing mode, and that must not fail a bundle check."""
    try:
        from google.auth.transport.requests import Request  # noqa: F401
        from google_auth_oauthlib.flow import InstalledAppFlow  # noqa: F401
        from googleapiclient.discovery import build  # noqa: F401
        import qrcode  # noqa: F401
        from qrcode.image.styledpil import StyledPilImage  # noqa: F401
        from qrcode.image.styles.moduledrawers import RoundedModuleDrawer  # noqa: F401
        import webview as _wv  # noqa: F401
        # Exercise the service build too, but a missing/expired token is fine here.
        try:
            sheets.build_service(interactive=False).spreadsheets()
            auth = "signed in"
        except Exception as e:
            auth = f"no valid session ({type(e).__name__}) — ok"
        print(f"SELFTEST OK: google + sheets + qrcode + pywebview [{auth}]")
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
