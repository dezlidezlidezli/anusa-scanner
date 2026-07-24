#!/usr/bin/env python3
"""
sheets.py — Google Sheets check-in for the ANUSA Scanner receiver.

Reusable core shared by the receiver app and the auth spike:
  * OAuth sign-in (desktop flow, token cached locally)
  * column resolution (letter / header text / 'today' date auto-detect)
  * student-number normalization ('u8221537' and '8221537' both match)
  * SheetSession: load a tab, then flip a FALSE→TRUE tick per scan

Google libraries are imported lazily inside build_service(), so `import sheets`
never fails just because the API client isn't installed — it only matters once
you actually sign in.

    pip install google-api-python-client google-auth-oauthlib google-auth-httplib2
"""

import datetime
import json
import os
import re
import sys
import threading
from pathlib import Path

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
HERE = os.path.dirname(os.path.abspath(__file__))


def _app_dir():
    """Writable per-user dir. In the packaged .app the bundle is read-only, so the
    token (and any user-supplied credentials.json) live in Application Support."""
    if getattr(sys, "frozen", False):
        d = Path.home() / "Library" / "Application Support" / "ANUSA Scanner"
    else:
        d = Path(HERE)
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return d


def creds_file():
    """credentials.json: a user-placed one wins, else the copy bundled in the app
    (or alongside the source)."""
    user = _app_dir() / "credentials.json"
    if user.exists():
        return str(user)
    bundle = getattr(sys, "_MEIPASS", HERE)   # PyInstaller unpack dir when frozen
    return os.path.join(bundle, "credentials.json")


def token_file():
    return str(_app_dir() / "token.json")


def service_account_file():
    """A service-account key, if present — the app then authenticates AS the service account
    (no user sign-in, no browser, no token, no expiry; the roster sheet is just shared with the
    service account's email). A user-placed key wins, else the copy bundled in the app. Returns
    None when there isn't one, in which case sign-in falls back to the OAuth flow."""
    user = _app_dir() / "service_account.json"
    if user.exists():
        return str(user)
    bundle = getattr(sys, "_MEIPASS", HERE)
    p = os.path.join(bundle, "service_account.json")
    return p if os.path.exists(p) else None


def has_service_account():
    return service_account_file() is not None


def service_account_dest():
    """Where a UI-loaded service-account key is written (the writable per-user support dir)."""
    return str(_app_dir() / "service_account.json")


def service_account_email():
    """The client_email of the loaded service-account key (what to share sheets with), or None."""
    p = service_account_file()
    if not p:
        return None
    try:
        with open(p) as f:
            return json.load(f).get("client_email")
    except Exception:
        return None


# ── auth-mode preference (persisted) ──────────────────────────────────────────
# The app can authenticate to Google Sheets two ways; the operator picks one in
# Settings and the choice is remembered across launches. New installs default to
# the service account (no sign-in, no 7-day Testing-mode expiry).
DEFAULT_AUTH_MODE = "service_account"
AUTH_MODES = ("service_account", "oauth")


def prefs_file():
    return str(_app_dir() / "prefs.json")


def _read_prefs():
    try:
        with open(prefs_file()) as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _write_prefs(d):
    # Atomic write: serialise to a temp file, fsync, then os.replace() over the real file.
    # A crash or kill mid-write can't leave a truncated prefs.json that would read back empty
    # and silently wipe the operator's settings — the old file stays intact until the rename.
    try:
        p = prefs_file()
        tmp = p + ".tmp"
        with open(tmp, "w") as f:
            json.dump(d, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
    except Exception:
        pass


def get_auth_mode():
    m = _read_prefs().get("auth_mode")
    return m if m in AUTH_MODES else DEFAULT_AUTH_MODE


def set_auth_mode(mode):
    mode = mode if mode in AUTH_MODES else DEFAULT_AUTH_MODE
    d = _read_prefs()
    d["auth_mode"] = mode
    _write_prefs(d)
    return mode


def recent_sheets(mode):
    """Recently-used spreadsheets for a mode, newest first. Each:
    {id, title, url, used, tab, cols:{<tab>:[id_i,tick_i,name_i]}}."""
    return (_read_prefs().get("recent_sheets") or {}).get(mode) or []


def recent_sheet_record(mode, sid):
    for r in recent_sheets(mode):
        if r.get("id") == sid:
            return r
    return None


def _save_recent(mode, lst):
    d = _read_prefs()
    rec = d.get("recent_sheets") or {}
    rec[mode] = lst[:8]
    d["recent_sheets"] = rec
    _write_prefs(d)


def remember_recent_sheet(mode, sid, title, url, tab=None):
    """Record (or bump to the top) a spreadsheet in a mode's recently-used list, keyed by the
    spreadsheet id, labelled by its title, stamped with today's date. Preserves any cached
    per-tab column choices, and records the last-used tab. Capped at 8."""
    if not sid:
        return
    prev = recent_sheet_record(mode, sid) or {}
    lst = [x for x in recent_sheets(mode) if x.get("id") != sid]
    lst.insert(0, {
        "id": sid,
        "title": (title or "").strip() or prev.get("title") or "(untitled sheet)",
        "url": url or prev.get("url"),
        "used": datetime.date.today().strftime("%Y-%m-%d"),
        "tab": tab if tab is not None else prev.get("tab"),
        "cols": prev.get("cols") or {},
    })
    _save_recent(mode, lst)


def remember_sheet_cols(mode, sid, tab, cols):
    """Cache the chosen [id_i, tick_i, name_i] column indices for a specific sheet + tab, so a
    later reload restores them instead of re-guessing. Also records `tab` as the last used."""
    if not sid or tab is None:
        return
    rec = recent_sheet_record(mode, sid)
    if rec is None:
        return
    lst = [x for x in recent_sheets(mode) if x.get("id") != sid]
    rec = dict(rec)
    colmap = dict(rec.get("cols") or {})
    colmap[str(tab)] = list(cols)
    rec["cols"] = colmap
    rec["tab"] = tab
    rec["used"] = datetime.date.today().strftime("%Y-%m-%d")
    lst.insert(0, rec)
    _save_recent(mode, lst)


def recent_sheet_cols(mode, sid, tab):
    rec = recent_sheet_record(mode, sid)
    if not rec:
        return None
    return (rec.get("cols") or {}).get(str(tab))


def get_user_initials():
    v = _read_prefs().get("user_initials", "")
    return str(v or "")


def set_user_initials(v):
    v = re.sub(r"[^A-Za-z]", "", str(v or "")).upper()[:5]
    d = _read_prefs()
    d["user_initials"] = v
    _write_prefs(d)
    return v


def auth_ready(mode=None):
    """True if the selected auth mode can build a Sheets service WITHOUT any interaction —
    a loaded service-account key, or a cached OAuth token that's valid/refreshable."""
    mode = mode or get_auth_mode()
    if mode == "service_account":
        return has_service_account()
    try:
        creds = _load_cached()
        return bool(creds and (creds.valid or (creds.expired and creds.refresh_token)))
    except Exception:
        return False

TRUE_SET = {"TRUE", "1", "YES", "Y", "X", "✓", "TICK", "PRESENT"}


# ── pure helpers (no Google deps — unit-tested) ───────────────────────────────
def normalize(v):
    """u8221537 / U8221537 / ' 8221537 ' / 8221537(int) → '8221537'."""
    return re.sub(r"\D", "", str(v if v is not None else ""))


def col_letter(idx):            # 0-based index → A, B, … Z, AA
    s = ""
    idx += 1
    while idx:
        idx, r = divmod(idx - 1, 26)
        s = chr(65 + r) + s
    return s


def letter_index(letter):       # 'A' → 0
    n = 0
    for ch in letter.upper():
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def spreadsheet_id(url_or_id):
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", url_or_id or "")
    return m.group(1) if m else (url_or_id or "").strip()


def gid_from_url(url):
    m = re.search(r"[#&?]gid=(\d+)", url or "")
    return int(m.group(1)) if m else None


def today_headers():
    d = datetime.date.today()
    return {
        d.strftime("%Y-%m-%d"), d.strftime("%d/%m/%Y"), d.strftime("%m/%d/%Y"),
        f"{d.day}/{d.month}", f"{d.day}/{d.month}/{d.year}",
        d.strftime("%d %b"), d.strftime("%a %d"), d.strftime("%b %d"),
    }


def resolve_col(headers, spec):
    """Column letter (A/B…), exact/contains header match, or 'today'. → 0-based index."""
    s = (spec or "").strip()
    if not s:
        raise ValueError("empty column")
    low = s.lower()
    # An exact header-name match wins first, so a header literally named "ID" (which also
    # looks like a spreadsheet column letter) resolves to that column, not to column "ID".
    for i, h in enumerate(headers):
        if str(h).strip().lower() == low:
            return i
    if re.fullmatch(r"[A-Za-z]{1,2}", s):
        return letter_index(s)
    if low == "today":
        wants = {t.lower() for t in today_headers()}
        for i, h in enumerate(headers):
            if str(h).strip().lower() in wants:
                return i
        raise ValueError("no header matches today's date")
    for i, h in enumerate(headers):
        if low in str(h).strip().lower():
            return i
    raise ValueError(f"column {spec!r} not found in headers")


# ── OAuth (Google deps imported lazily) ───────────────────────────────────────
def _load_cached():
    from google.oauth2.credentials import Credentials
    tok = token_file()
    if os.path.exists(tok):
        return Credentials.from_authorized_user_file(tok, SCOPES)
    return None


def token_available():
    """True if a Sheets service can be built without a browser for the SELECTED auth mode."""
    return auth_ready()


def _interactive_signin():
    """Open the browser sign-in flow and cache the new token."""
    from google_auth_oauthlib.flow import InstalledAppFlow
    cf = creds_file()
    if not os.path.exists(cf):
        raise FileNotFoundError(
            "No credentials.json — add your OAuth Desktop client (see SHEETS_SETUP.md).")
    flow = InstalledAppFlow.from_client_secrets_file(cf, SCOPES)
    creds = flow.run_local_server(port=0)
    _save(creds)
    return creds


def build_service(interactive=False):
    """Return a Sheets service for the SELECTED auth mode. In service-account mode the loaded
    key is used (no sign-in) or it raises if none is loaded. In OAuth mode, interactive=True may
    open a browser to sign in, and interactive=False uses the cached token (refreshing if
    needed) or raises. The mode is authoritative — service-account mode never falls back to
    OAuth and vice-versa, so the operator's choice in Settings is always honoured."""
    from googleapiclient.discovery import build

    if get_auth_mode() == "service_account":
        sa = service_account_file()
        if not sa:
            raise RuntimeError("no service-account key loaded")
        from google.oauth2 import service_account
        creds = service_account.Credentials.from_service_account_file(sa, scopes=SCOPES)
        return build("sheets", "v4", credentials=creds, cache_discovery=False)

    from google.auth.transport.requests import Request
    creds = _load_cached()
    if creds and creds.valid:
        pass
    elif creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            _save(creds)
        except Exception:
            # The refresh token itself is dead — almost always the 7-day expiry that Google
            # applies to apps in "Testing" publishing status. Drop the stale token and either
            # re-authenticate (interactive) or report signed-out — never get stuck retrying a
            # dead token, which is what blocked the Sign in button.
            sign_out()
            if interactive:
                creds = _interactive_signin()
            else:
                raise RuntimeError("not signed in")
    elif interactive:
        creds = _interactive_signin()
    else:
        raise RuntimeError("not signed in")
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def _save(creds):
    with open(token_file(), "w") as f:
        f.write(creds.to_json())


def sign_out():
    try:
        os.remove(token_file())
    except FileNotFoundError:
        pass


# ── a loaded sheet/tab you check students into ────────────────────────────────
class SheetSession:
    def __init__(self, service):
        self.svc = service
        self._lock = threading.Lock()
        self.sid = None
        self.tab = None
        self.headers = []
        self.values = []
        self.header_i = 0     # which row of self.values holds the column names (auto-detected)
        self.id_i = self.tick_i = self.name_i = None
        # Textbook Library borrow-log columns (a different sheet shape — see append_borrow)
        self.tb_status_i = self.tb_date_i = self.tb_init_i = self.tb_uid_i = self.tb_code_i = None
        self.tb_retby_i = self.tb_realret_i = None   # return columns (mark a book returned)

    def open(self, url, tab=None):
        """Load a spreadsheet tab. Returns {title, tab, tabs, headers, rows}."""
        with self._lock:
            self.sid = spreadsheet_id(url)
            meta = self.svc.spreadsheets().get(spreadsheetId=self.sid).execute()
            tabs = [s["properties"] for s in meta.get("sheets", [])]
            if tab:
                self.tab = tab
            else:
                gid = gid_from_url(url)
                self.tab = next((t["title"] for t in tabs if t["sheetId"] == gid),
                                None) or (tabs[0]["title"] if tabs else None)
            self._reload_locked()
            return {
                "title": meta.get("properties", {}).get("title", ""),
                "tab": self.tab,
                "tabs": [t["title"] for t in tabs],
                "headers": list(self.headers),
                "rows": max(0, len(self.values) - (self.header_i + 1)),
            }

    def guess_columns(self):
        """Best-guess (id_i, tick_i, name_i) column INDICES for the dropdowns (None if no
        guess). Indices, not header text — a sheet can have two columns with the same name."""
        def find(pats):
            for i, h in enumerate(self.headers):
                hl = str(h).strip().lower()
                if any(p in hl for p in pats):
                    return i
            return None
        def find_exact(names):     # exact match — safe for short tokens like "id"
            for i, h in enumerate(self.headers):
                if str(h).strip().lower() in names:
                    return i
            return None
        id_g = find(["uid", "student", "number"])
        if id_g is None:
            id_g = find_exact({"id", "sid"})
        if id_g is None and self.headers:
            id_g = 0
        tick_g = None
        wants = {t.lower() for t in today_headers()}
        for i, h in enumerate(self.headers):
            if str(h).strip().lower() in wants:
                tick_g = i
                break
        if tick_g is None:
            tick_g = find(["attend", "present", "check", "tick", "here"])
        name_g = find(["name"])
        return id_g, tick_g, name_g

    def set_columns(self, id_col, tick_col, name_col=None):
        """Set the ID / tick / name columns. Accepts an explicit 0-based INDEX (what the
        dropdowns send — so duplicate header names pick the exact column) or a text spec
        (header name / letter / 'today')."""
        self.id_i = self._to_index(id_col)
        self.tick_i = self._to_index(tick_col)
        self.name_i = (None if name_col in (None, "", "(none)", -1, "-1")
                       else self._to_index(name_col))

    def _to_index(self, spec):
        if isinstance(spec, bool):
            raise ValueError("bad column")
        if isinstance(spec, int):
            if 0 <= spec < len(self.headers):
                return spec
            raise ValueError(f"column index {spec} out of range")
        s = str(spec).strip()
        if re.fullmatch(r"\d+", s):            # dropdown sends the index as a string
            i = int(s)
            if 0 <= i < len(self.headers):
                return i
        return resolve_col(self.headers, s)    # header name / letter / 'today'

    # ── Textbook Library: a borrow-log sheet you APPEND rows to ───────────────
    def _col_texts(self):
        """Per column, the header cell + the banner row above it (rows 1 & 2), lowercased — so
        fuzzy matching can use BOTH rows (these sheets often describe a column across two rows)."""
        hi = self.header_i
        banner = self.values[hi - 1] if hi >= 1 else []
        hdr = self.values[hi] if hi < len(self.values) else []
        n = max(len(banner), len(hdr))
        out = []
        for i in range(n):
            b = str(banner[i]).strip().lower() if i < len(banner) else ""
            h = str(hdr[i]).strip().lower() if i < len(hdr) else ""
            out.append((b + " " + h).strip())
        return out

    def guess_textbook_columns(self):
        """Best-guess column INDICES for the borrow log:
        [status, date, init, uid, code, return_by, real_return]. Fuzzy — matches across the header
        row and the banner row above it."""
        texts = self._col_texts()

        def find(pats, exclude=()):
            for i, t in enumerate(texts):
                if any(p in t for p in pats) and not any(x in t for x in exclude):
                    return i
            return None

        status = find(["status"])
        date_i = find(["date of hire", "date student collected", "date of collection", "hire date"])
        init_i = find(["hired out by", "issued by"], exclude=["return"])
        if init_i is None:
            init_i = find(["operated the scanner", "lent by"])
        uid_i = find(["uid", "student number", "student id"])
        code_i = find(["assigned code", "asset code", "assigned title", "title code", "code"])
        retby_i = find(["return received by", "returned by", "received by"])
        realret_i = find(["real return", "date and init", "date returned"])
        return [status, date_i, init_i, uid_i, code_i, retby_i, realret_i]

    def set_textbook_columns(self, status, date, init, uid, code, return_by=None, real_return=None):
        def idx(v):
            return None if v in (None, "", "(none)", -1, "-1") else self._to_index(v)
        self.tb_status_i = self._to_index(status)
        self.tb_date_i = self._to_index(date)
        self.tb_init_i = self._to_index(init)
        self.tb_uid_i = self._to_index(uid)
        self.tb_code_i = self._to_index(code)
        self.tb_retby_i = idx(return_by)
        self.tb_realret_i = idx(real_return)

    def append_borrow(self, uid, code, initials, date):
        """Append a borrow row: writes On Hire / date / initials / uXXXXXXX / code into the first
        empty data row (or a new row at the end). Returns the 1-based row written."""
        with self._lock:
            if self.tb_uid_i is None or self.tb_status_i is None:
                raise RuntimeError("textbook columns not set")
            target = None
            for r in range(self.header_i + 1, len(self.values)):
                u = normalize(self._cell(r, self.tb_uid_i))
                s = str(self._cell(r, self.tb_status_i)).strip()
                if not u and not s:
                    target = r
                    break
            if target is None:
                target = len(self.values)
                self.values.append([])
            data = []

            def put(ci, val):
                if ci is None:
                    return
                self._set_cell(target, ci, val)
                data.append({"range": f"'{self.tab}'!{col_letter(ci)}{target + 1}",
                             "values": [[val]]})

            put(self.tb_status_i, "On Hire")
            put(self.tb_date_i, date)                 # DD/MM/YYYY
            put(self.tb_init_i, initials)             # e.g. MK
            put(self.tb_uid_i, "u" + normalize(uid))  # uXXXXXXX
            put(self.tb_code_i, code)                 # e.g. PAL/001
            self.svc.spreadsheets().values().batchUpdate(
                spreadsheetId=self.sid,
                body={"valueInputOption": "USER_ENTERED", "data": data}).execute()
            return target + 1

    def _is_open_row(self, r):
        """A borrow row is OPEN (book still out) if it has a UID, its return hasn't been logged,
        and its status isn't 'Returned'."""
        if not normalize(self._cell(r, self.tb_uid_i)):
            return False
        if self.tb_realret_i is not None and str(self._cell(r, self.tb_realret_i)).strip():
            return False
        status = str(self._cell(r, self.tb_status_i)).strip().lower() if self.tb_status_i is not None else ""
        return status != "returned"

    def find_open_borrow(self, uid):
        """The first OPEN borrow row for this student → (1-based row, code) or (None, None).
        Powers the 'one book each' guard."""
        with self._lock:
            if self.tb_uid_i is None:
                return None, None
            target = normalize(uid)
            for r in range(self.header_i + 1, len(self.values)):
                if normalize(self._cell(r, self.tb_uid_i)) == target and self._is_open_row(r):
                    code = self._cell(r, self.tb_code_i) if self.tb_code_i is not None else ""
                    return r + 1, str(code)
            return None, None

    def log_return(self, row, initials, date):
        """Mark a borrow returned: Return received by = initials, Real Return = 'date init',
        Status = Returned. `row` is 1-based."""
        with self._lock:
            r = row - 1
            data = []

            def put(ci, val):
                if ci is None:
                    return
                self._set_cell(r, ci, val)
                data.append({"range": f"'{self.tab}'!{col_letter(ci)}{row}", "values": [[val]]})

            put(self.tb_retby_i, initials)     # Return received by (initials)
            put(self.tb_realret_i, date)       # Real Return date (DD/MM/YYYY) — date only
            put(self.tb_status_i, "Returned")
            if data:
                self.svc.spreadsheets().values().batchUpdate(
                    spreadsheetId=self.sid,
                    body={"valueInputOption": "USER_ENTERED", "data": data}).execute()
            return row

    def tb_register(self):
        """Compact active-borrow register for the phones: [[uid, code], ...] for every OPEN row —
        so a phone can flag 'already has a book' the instant a student card is scanned."""
        with self._lock:
            if self.tb_uid_i is None:
                return []
            out, seen = [], set()
            for r in range(self.header_i + 1, len(self.values)):
                if not self._is_open_row(r):
                    continue
                uid = normalize(self._cell(r, self.tb_uid_i))
                if uid in seen:
                    continue
                seen.add(uid)
                code = self._cell(r, self.tb_code_i) if self.tb_code_i is not None else ""
                out.append([uid, str(code)])
            return out

    def plan_checkin(self, student):
        """Decide the check-in outcome from the loaded sheet WITHOUT the (slow) API write, so
        the caller can show the result immediately. A student is ONE person even if they
        appear in several rows, so all matching rows are ticked together. For 'checked-in' the
        rows are optimistically marked TRUE in the local cache and returned under '_to_tick'
        for commit_checkin() to write. Returns a status dict; raises only on a bad setup."""
        with self._lock:
            if self.id_i is None or self.tick_i is None:
                raise RuntimeError("columns not set")
            target = normalize(student)
            if not target:
                return {"status": "not-registered", "name": "", "row": None, "rows": []}
            rows = self._find_all(target)
            if not rows:
                # The roster is fixed for a scan session, so a miss means "not on the list" —
                # answer straight from the cache, no API round-trip (the Sync button reloads
                # the sheet if it's edited mid-session).
                # If exactly one roster UID is a single digit off, it's likely a form-typo in
                # the sheet — offer it for the operator to confirm by name.
                fuzz = self._find_fuzzy(target)
                if len(fuzz) == 1:
                    return {"status": "fuzzy", "name": fuzz[0]["name"],
                            "id": fuzz[0]["id"], "row": fuzz[0]["row"], "rows": []}
                return {"status": "not-registered", "name": "", "row": None, "rows": []}

            name = ""
            if self.name_i is not None:
                for r in rows:
                    n = self._cell(r, self.name_i)
                    if n:
                        name = n
                        break

            sheet_rows = [r + 1 for r in rows]
            to_tick = [r for r in rows
                       if str(self._cell(r, self.tick_i)).strip().upper() not in TRUE_SET]
            if not to_tick:
                return {"status": "already", "name": name,
                        "row": sheet_rows[0], "rows": sheet_rows}

            for r in to_tick:                  # optimistic local tick; commit writes it remotely
                self._set_cell(r, self.tick_i, "TRUE")
            return {"status": "checked-in", "name": name, "row": (to_tick[0] + 1),
                    "rows": sheet_rows, "_to_tick": to_tick}

    def commit_checkin(self, plan):
        """Write the ticks planned by plan_checkin() in ONE batched API call. On failure the
        optimistic local update is reverted and the error re-raised so the caller can correct
        the UI."""
        to_tick = plan.get("_to_tick") or []
        if not to_tick:
            return
        col = col_letter(self.tick_i)
        with self._lock:
            try:
                self.svc.spreadsheets().values().batchUpdate(
                    spreadsheetId=self.sid,
                    body={"valueInputOption": "USER_ENTERED",
                          "data": [{"range": f"'{self.tab}'!{col}{r + 1}", "values": [["TRUE"]]}
                                   for r in to_tick]}).execute()
            except Exception:
                for r in to_tick:              # revert so a retry works + attendance stays honest
                    self._set_cell(r, self.tick_i, "FALSE")
                raise

    def check_in(self, student):
        """Plan + commit synchronously (write confirmed before returning)."""
        plan = self.plan_checkin(student)
        if plan.get("status") == "checked-in":
            self.commit_checkin(plan)
        return plan

    def roster(self):
        """[{id, name}] for every row that has a UID — powers manual-entry autofill."""
        with self._lock:
            out = []
            if self.id_i is None:
                return out
            seen = set()
            for r in range(self.header_i + 1, len(self.values)):
                uid = normalize(self._cell(r, self.id_i))
                if not uid or uid in seen:
                    continue
                seen.add(uid)
                name = self._cell(r, self.name_i) if self.name_i is not None else ""
                out.append({"id": uid, "name": str(name)})
            return out

    def roster_state(self):
        """Compact roster for the phones so they can show results instantly without a
        round-trip: [[uid, name, ticked], ...]. ticked=1 iff the student is fully checked in
        (all their rows TRUE) — i.e. a scan would be 'already'; 0 means a scan would check in."""
        with self._lock:
            if self.id_i is None:
                return []
            order, info = [], {}
            for r in range(self.header_i + 1, len(self.values)):
                uid = normalize(self._cell(r, self.id_i))
                if not uid:
                    continue
                is_true = (self.tick_i is not None and
                           str(self._cell(r, self.tick_i)).strip().upper() in TRUE_SET)
                if uid not in info:
                    nm = self._cell(r, self.name_i) if self.name_i is not None else ""
                    info[uid] = [str(nm), is_true]
                    order.append(uid)
                else:
                    info[uid][1] = info[uid][1] and is_true
            return [[u, info[u][0], 1 if info[u][1] else 0] for u in order]

    def attendance(self):
        """Expected attendance: how many rows have a TRUE/FALSE tick box, and how many
        are TRUE."""
        with self._lock:
            if self.tick_i is None:
                return {"present": 0, "total": 0}
            present = total = 0
            for r in range(self.header_i + 1, len(self.values)):
                val = str(self._cell(r, self.tick_i)).strip().upper()
                if val in ("TRUE", "FALSE"):
                    total += 1
                    if val == "TRUE":
                        present += 1
            return {"present": present, "total": total}

    def _find_fuzzy(self, target):
        """Roster UIDs that differ from `target` by exactly one digit (same length)."""
        out = []
        if not target or self.id_i is None:
            return out
        seen = set()
        for r in range(self.header_i + 1, len(self.values)):
            uid = normalize(self._cell(r, self.id_i))
            if len(uid) != len(target) or uid == target or uid in seen:
                continue
            if sum(1 for a, b in zip(uid, target) if a != b) == 1:
                seen.add(uid)
                name = self._cell(r, self.name_i) if self.name_i is not None else ""
                out.append({"id": uid, "name": str(name), "row": r + 1})
        return out

    def refresh(self):
        with self._lock:
            self._reload_locked()

    # ── internals (call with _lock held) ─────────────────────────────────────
    def _reload_locked(self):
        self.values = self.svc.spreadsheets().values().get(
            spreadsheetId=self.sid, range=f"'{self.tab}'").execute().get("values", [])
        self.header_i = self._detect_header_row()
        self.headers = self.values[self.header_i] if self.values else []

    def _detect_header_row(self):
        """The column names aren't always in row 1 — some sheets have a banner row above
        them (e.g. a merged 'DO NOT EDIT'). Scan the first few rows and use the first that
        looks like a header (has a name-ish or id-ish column); default to the first row."""
        for i in range(min(len(self.values), 6)):
            if self._looks_like_header(self.values[i]):
                return i
        return 0

    @staticmethod
    def _looks_like_header(cells):
        # Match the id signal on a real HEADER cell, not the word "student" buried in a banner
        # sentence (e.g. "Date student collected" must NOT read as an id column).
        low = [str(c).strip().lower() for c in (cells or [])]
        has_id = any(
            "uid" in c or c.startswith("student") or "student number" in c or "student id" in c
            or c in ("id", "sid", "number", "student no", "student number", "student id")
            for c in low)
        has_name = any(c == "name" or c.startswith("name") or c.endswith("name")
                       or "full name" in c or "student name" in c for c in low)
        return has_id or has_name

    def _find_all(self, target):
        return [r for r in range(self.header_i + 1, len(self.values))
                if normalize(self._cell(r, self.id_i)) == target]

    @staticmethod
    def _cell_row(row, i):
        return row[i] if (i is not None and i < len(row)) else ""

    def _cell(self, r, i):
        return self._cell_row(self.values[r], i) if r < len(self.values) else ""

    def _set_cell(self, r, i, val):
        while len(self.values[r]) <= i:
            self.values[r].append("")
        self.values[r][i] = val
