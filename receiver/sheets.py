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
    if re.fullmatch(r"[A-Za-z]{1,2}", s):
        return letter_index(s)
    if s.lower() == "today":
        wants = {t.lower() for t in today_headers()}
        for i, h in enumerate(headers):
            if str(h).strip().lower() in wants:
                return i
        raise ValueError("no header matches today's date")
    low = s.lower()
    for i, h in enumerate(headers):
        if str(h).strip().lower() == low:
            return i
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
    """True if a cached token exists that is valid or refreshable (no browser)."""
    try:
        creds = _load_cached()
        return bool(creds and (creds.valid or (creds.expired and creds.refresh_token)))
    except Exception:
        return False


def build_service(interactive=False):
    """Return a Sheets service. interactive=True may open a browser to sign in;
    interactive=False uses the cached token (refreshing if needed) or raises."""
    from google.auth.transport.requests import Request
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    creds = _load_cached()
    if creds and creds.valid:
        pass
    elif creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        _save(creds)
    elif interactive:
        cf = creds_file()
        if not os.path.exists(cf):
            raise FileNotFoundError(
                "No credentials.json — add your OAuth Desktop client (see SHEETS_SETUP.md).")
        flow = InstalledAppFlow.from_client_secrets_file(cf, SCOPES)
        creds = flow.run_local_server(port=0)
        _save(creds)
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
        self.id_i = self.tick_i = self.name_i = None

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
                "rows": max(0, len(self.values) - 1),
            }

    def guess_columns(self):
        """Best-guess (id_header, tick_header, name_header) for the dropdowns."""
        def find(pats):
            for h in self.headers:
                hl = str(h).strip().lower()
                if any(p in hl for p in pats):
                    return h
            return None
        id_g = find(["uid", "student", "number"]) or (self.headers[0] if self.headers else None)
        tick_g = None
        wants = {t.lower() for t in today_headers()}
        for h in self.headers:
            if str(h).strip().lower() in wants:
                tick_g = h
                break
        if not tick_g:
            tick_g = find(["attend", "present", "check", "tick", "here"])
        name_g = find(["name"])
        return id_g, tick_g, name_g

    def set_columns(self, id_col, tick_col, name_col=None):
        self.id_i = resolve_col(self.headers, id_col)
        self.tick_i = resolve_col(self.headers, tick_col)
        self.name_i = resolve_col(self.headers, name_col) if name_col else None

    def check_in(self, student):
        """Flip this student's tick FALSE→TRUE. A student is ONE person even if they
        appear in several rows (one registration per student), so every matching row is
        ticked together. Returns a status dict. Raises on API error."""
        with self._lock:
            if self.id_i is None or self.tick_i is None:
                raise RuntimeError("columns not set")
            target = normalize(student)
            if not target:
                return {"status": "not-registered", "name": "", "row": None, "rows": []}
            rows = self._find_all(target)
            if not rows:
                self._reload_locked()          # maybe rows were added since load
                rows = self._find_all(target)
            if not rows:
                # No exact match. If exactly one roster UID is a single digit off, it's
                # likely a form-typo in the sheet — offer it for the operator to confirm
                # by name rather than silently rejecting a real student.
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

            for r in to_tick:
                rng = f"'{self.tab}'!{col_letter(self.tick_i)}{r + 1}"
                self.svc.spreadsheets().values().update(
                    spreadsheetId=self.sid, range=rng, valueInputOption="USER_ENTERED",
                    body={"values": [["TRUE"]]}).execute()
                self._set_cell(r, self.tick_i, "TRUE")   # keep local cache in sync
            return {"status": "checked-in", "name": name,
                    "row": (to_tick[0] + 1), "rows": sheet_rows}

    def roster(self):
        """[{id, name}] for every row that has a UID — powers manual-entry autofill."""
        with self._lock:
            out = []
            if self.id_i is None:
                return out
            seen = set()
            for r in range(1, len(self.values)):
                uid = normalize(self._cell(r, self.id_i))
                if not uid or uid in seen:
                    continue
                seen.add(uid)
                name = self._cell(r, self.name_i) if self.name_i is not None else ""
                out.append({"id": uid, "name": str(name)})
            return out

    def attendance(self):
        """Expected attendance: how many rows have a TRUE/FALSE tick box, and how many
        are TRUE."""
        with self._lock:
            if self.tick_i is None:
                return {"present": 0, "total": 0}
            present = total = 0
            for r in range(1, len(self.values)):
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
        for r in range(1, len(self.values)):
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
        self.headers = self.values[0] if self.values else []

    def _find_all(self, target):
        return [r for r in range(1, len(self.values))
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
