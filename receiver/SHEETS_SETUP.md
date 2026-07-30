# Google Sheets — setup (one-time)

**Union Pantry** and **Textbook Library** both write to a Google Sheet. **Keystroke mode needs
none of this** — it doesn't touch Google.

Two ways to authenticate; the receiver's **Settings** picks between them (new installs default to
the service account):

- **Service account (default, recommended for sharing the app)** — no one ever signs in. You
  create one key; each sheet is just *shared* with the service account's email. No sign-in, no
  7-day token expiry, no "unverified app" warning — ever.
- **Google sign-in / OAuth** — each operator signs in with their own Google account. Fine for
  personal use, but in a personal (non-Workspace) project it's stuck in **Testing** mode: logins
  re-prompt every 7 days behind an "unverified app" screen. See §OAuth below.

At launch the receiver requires **operator initials + working auth** before it will pair a phone.

## Service account (recommended, ~5 min once)

1. <https://console.cloud.google.com/> → your project → **APIs & Services → Library → Google
   Sheets API → Enable** (if it isn't already).
2. **IAM & Admin → Service Accounts → Create service account.** Name it (e.g.
   `anusa-scanner`). **Skip** the optional roles/access steps → **Done**.
3. Open the new service account → **Keys → Add key → Create new key → JSON → Create.** A JSON
   file downloads.
4. Rename it to **`service_account.json`** and put it in `receiver/` (next to `build_mac.sh`).
   It's git-ignored — never commit it.
5. Copy the service account's **email** — like `anusa-scanner@your-project.iam.gserviceaccount.com`
   (the receiver's Settings also shows + copies it).
6. **For every sheet you'll use** — Union Pantry rosters **and** Textbook Library borrow logs —
   open it → **Share** → paste that email → set **Editor** → Send. (Uncheck "Notify people".)
7. Build: `bash build_mac.sh`. It copies the key **loose into `dist/`** (not embedded in the
   `.app`), and `Install.command` installs it to the recipient's
   `~/Library/Application Support/ANUSA Scanner/` on first run. Launch → Settings shows
   **key loaded — ready**; no sign-in.

**Distribution:** zip the whole `dist/` folder (`.app` + `Install.command` + `service_account.json`)
and send it. The recipient double-clicks `Install.command` once.

**Security / rotation:** the key is a real credential. It ships **loose in the zip** and lands in
the recipient's Application Support (`chmod 600`) — it is *not* embedded in the distributed `.app`.
Its reach is only sheets you've shared with it, so use a **dedicated** service account and share
only event sheets. If a key leaks: **IAM → Service Accounts → Keys → delete** the old key, create a
new one, rebuild, and re-distribute (old zips keep working until the key is deleted in IAM).
Everything sensitive is git-ignored (`service_account.json`, `credentials.json`, `token.json`,
`dist/`, `*.zip`) — never commit the key or the build.

---

## OAuth (alternative — per-user Google sign-in)

Only needed if you choose **Google sign-in** in Settings instead of a service account. This is a
**developer-only** path — the OAuth client is not bundled into the distributed app.

1. <https://console.cloud.google.com/> → **create a project** (e.g. `anusa-scanner`).
2. **APIs & Services → Library → Google Sheets API → Enable**.
3. **APIs & Services → OAuth consent screen:** User type **External** → Create; app name
   `ANUSA Scanner`, your email as support + developer contact; **Scopes:** add
   `.../auth/spreadsheets`; **Test users:** add every Google account that will sign in. *(Test mode
   re-prompts every 7 days. "Publish app" makes logins persistent but shows a one-time "Google
   hasn't verified this app → Advanced → Go to app" screen.)*
4. **Credentials → Create credentials → OAuth client ID → Desktop app → Create → Download JSON.**
   Rename to **`credentials.json`** and put it in `~/Library/Application Support/ANUSA Scanner/`
   (or `receiver/` when running from source). Git-ignored — never commit it.
5. In the receiver: **Settings → Google sign-in → Sign in with Google.** A browser opens once;
   the token is cached in Application Support (and refreshed until the 7-day Testing expiry).

Then set up a sheet exactly as in the service-account flow (pick the sheet + tab, confirm the
columns). The `token.json` the app writes is git-ignored too.
