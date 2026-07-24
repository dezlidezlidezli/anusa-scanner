'use strict';
/* ANUSA Scanner — camera → OCR → encrypted relay → keystrokes on the paired computer.
   Runs entirely as a static page (GitHub Pages friendly). */

const $ = (s) => document.querySelector(s);

/* ────────────────────────── settings ────────────────────────── */

const DEFAULTS = {
  room: '',
  digits: 7,
  prefix: '',                                      // optional exact leading digits, e.g. '8'
  startDigits: '5678',                             // accepted first digits; blank = any
  mode: 'bridge',                                  // 'bridge' | 'clip'
  broker: 'wss://broker.emqx.io:8084/mqtt',
};

function randomRoom() {
  const abc = 'ABCDEFGHJKMNPQRSTUVWXYZ23456789';   // no 0/O/1/I/L
  let out = '';
  const rnd = crypto.getRandomValues(new Uint8Array(6));
  for (const b of rnd) out += abc[b % abc.length];
  return out;
}

function loadSettings() {
  let s = {};
  try { s = JSON.parse(localStorage.getItem('wedge.settings') || '{}'); } catch (e) {}
  const merged = Object.assign({}, DEFAULTS, s);
  if (!merged.room) { merged.room = randomRoom(); saveSettings(merged); }
  return merged;
}
function saveSettings(s) { localStorage.setItem('wedge.settings', JSON.stringify(s)); }

const state = {
  settings: loadSettings(),
  deviceId: localStorage.getItem('wedge.dev') ||
    (() => { const d = Math.random().toString(36).slice(2, 8); localStorage.setItem('wedge.dev', d); return d; })(),
  seq: Number(localStorage.getItem('wedge.seq') || 0),
  key: null,               // CryptoKey derived from room
  client: null,            // mqtt client
  connected: false,
  paddleReady: false,      // PaddleOCR (ONNX) engine loaded
  stream: null,
  track: null,
  scanning: false,
  busy: false,
  lastAccepted: { id: null, t: 0 },
  suppressId: null,   // an id the user just deleted — ignored until the card leaves the frame

  history: (() => { try { return JSON.parse(localStorage.getItem('wedge.hist') || '[]'); } catch (e) { return []; } })(),
  audio: null,
  wakeLock: null,
  roster: null,        // uid → {name, ticked}, pushed by the Mac in sheet mode; null = not synced
  _shown: {},          // seq → status shown locally, so the Mac's echo doesn't double-flash
  rxMode: null,        // receiver mode: null/'keys'/'sheet' = normal scan; 'textbook' = two-stage flow
  tbStage: null,       // Textbook Library flow: 'student' | 'await' | 'textbook' | 'done'
  sessionActive: false,// a scan session is live (survives backgrounding) → auto-reboot on resume
  _hiddenAt: 0,        // when the tab was last backgrounded (ms) — gauges how stale things are
  _resuming: false,
  rxSynced: false,     // received the receiver's mode/roster since connecting (setup handshake)
  _syncResolve: null,
  _silentEl: null,     // silent looping <audio> that holds the iOS media channel open
};

/* ────────────────────────── crypto ──────────────────────────── */

async function deriveKey(room) {
  const raw = await crypto.subtle.digest('SHA-256',
    new TextEncoder().encode('idwedge|v1|' + room.trim().toUpperCase()));
  return crypto.subtle.importKey('raw', raw, 'AES-GCM', false, ['encrypt', 'decrypt']);
}

function b64(bytes) { let s = ''; bytes.forEach(b => s += String.fromCharCode(b)); return btoa(s); }
function unb64(str) { const bin = atob(str); const out = new Uint8Array(bin.length); for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i); return out; }

async function encryptJSON(obj) {
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const pt = new TextEncoder().encode(JSON.stringify(obj));
  const ct = new Uint8Array(await crypto.subtle.encrypt({ name: 'AES-GCM', iv }, state.key, pt));
  const buf = new Uint8Array(12 + ct.length); buf.set(iv, 0); buf.set(ct, 12);
  return b64(buf);
}
async function decryptJSON(payload) {
  const buf = unb64(payload);
  const pt = await crypto.subtle.decrypt({ name: 'AES-GCM', iv: buf.slice(0, 12) }, state.key, buf.slice(12));
  return JSON.parse(new TextDecoder().decode(pt));
}

/* ────────────────────────── bridge (MQTT) ───────────────────── */

function topicBase() { return 'idwedge/' + state.settings.room.trim().toUpperCase(); }

function setStatus(kind, txt) {
  const dot = $('#dot');
  dot.className = 'dot' + (kind ? ' ' + kind : '');
  $('#statusTxt').textContent = txt;
}

async function connectBridge() {
  if (state.client) { try { state.client.end(true); } catch (e) {} state.client = null; }
  state.connected = false;
  state.rxSynced = false;   // re-handshake with the receiver on every (re)connect
  state.key = await deriveKey(state.settings.room);

  if (state.settings.mode === 'clip') { setStatus('', 'clipboard mode'); return; }
  if (typeof mqtt === 'undefined') { setStatus('err', 'relay lib failed to load'); return; }

  setStatus('wait', 'connecting…');
  let client;
  try {
    client = mqtt.connect(state.settings.broker, {
      clean: true, keepalive: 30, connectTimeout: 8000, reconnectPeriod: 4000,
      clientId: 'wedge_' + state.deviceId + '_' + Math.random().toString(16).slice(2, 8),
    });
  } catch (e) { setStatus('err', 'bad broker URL'); return; }
  state.client = client;

  client.on('connect', () => {
    state.connected = true;
    setStatus('ok', 'bridge connected');
    client.subscribe(topicBase() + '/ack', { qos: 1 });
    sendHello();   // tell the Mac a phone has paired to this room
  });
  client.on('reconnect', () => { state.connected = false; setStatus('wait', 'reconnecting…'); });
  client.on('close', () => { state.connected = false; if (state.settings.mode === 'bridge') setStatus('err', 'bridge offline'); });
  client.on('error', () => { /* handled by close/reconnect */ });
  client.on('message', async (topic, payload) => {
    if (topic !== topicBase() + '/ack') return;
    try {
      const msg = await decryptJSON(new TextDecoder().decode(payload));
      const own = msg.dev === state.deviceId;
      const manual = msg.dev === 'manual';        // typed on the Mac, not scanned here
      if (msg.t === 'ack') {
        if (own) markHistory(msg.seq, 'typed', 'ok');
        return;
      }
      if (msg.t === 'mode') { markRxSynced(); applyRxMode(msg.mode); return; }
      if (msg.t === 'roster') { markRxSynced(); applyRoster(msg.r); return; }
      if (msg.t === 'checkin') {
        // Keep the local roster's tick state in sync with EVERY check-in (this phone AND any
        // other phone in the room), so a repeat scan correctly reads as "already".
        if (msg.id && state.roster) {
          const rr = state.roster[normId(msg.id)];
          if (rr) {
            if (msg.status === 'checked-in' || msg.status === 'already') rr.ticked = true;
            else if (msg.status === 'error') rr.ticked = false;   // write failed → allow retry
          }
        }
        if (!(own || manual)) return;   // another phone's scan: sync only, don't display
        // The receiver's AUTHORITATIVE result. If it just confirms what we already showed
        // locally (same seq + status), don't replay the sound / re-flash — only correct it.
        const [label, cls] = resultLabel(msg.status, msg.name);
        const confirmsLocal = own && state._shown[msg.seq] === msg.status;
        if (!confirmsLocal) { unlockAudio(); resultSound(msg.status); }
        if (own) {
          const h = state.history.find(x => x.seq === msg.seq);
          markHistory(msg.seq, label, cls);
          if (h && state.lastAccepted.id === h.id) setReadout(h.id, label, cls);
          if (!confirmsLocal) showResult(msg.status, h ? h.id : msg.id, msg.name);
          delete state._shown[msg.seq];   // reconciled — drop the local-shown marker
        } else {
          setReadout(msg.id, label, cls);   // manual entry on the Mac
          showResult(msg.status, msg.id, msg.name);
        }
      }
    } catch (e) { /* wrong room / stray traffic */ }
  });
}

async function sendHello() {
  if (!state.client) return;
  try {
    const payload = await encryptJSON({ t: 'hello', dev: state.deviceId });
    state.client.publish(topicBase() + '/scan', payload, { qos: 1 });
  } catch (e) { /* not fatal */ }
}

// Setup handshake: the receiver answers a phone's hello with its current mode + roster. We
// treat the first such reply as "synced", and hold the scan UI on a brief "syncing…" state
// until it arrives — so the mode chip / hint / reticle paint correctly the FIRST time instead
// of flashing from a default and then flipping when the mode message lands a moment later.
function markRxSynced() {
  state.rxSynced = true;
  if (state._syncResolve) state._syncResolve();   // resolve the in-flight wait (idempotent)
}
function waitForRxSync(timeoutMs) {
  if (state.settings.mode !== 'bridge' || state.rxSynced) return Promise.resolve();
  setReadout('', 'syncing with receiver…', 'warn');
  return new Promise((res) => {
    // `finish` closes over THIS promise's resolver, so a stale fallback timer from an earlier
    // wait can't resolve a later one — it just no-ops on its own already-done flag.
    let done = false;
    const finish = () => {
      if (done) return; done = true;
      if (state._syncResolve === finish) state._syncResolve = null;
      res();
    };
    state._syncResolve = finish;
    setTimeout(finish, timeoutMs);   // fallback so an older/quiet receiver never leaves us stuck
  });
}

// ── result labels + phone-side roster cache ─────────────────────────────────────
const normId = (v) => String(v == null ? '' : v).replace(/\D/g, '');
const RESULT_MAP = {
  'checked-in':     ['checked in ✓',            'ok'],
  'already':        ['already in',              'warn'],
  'not-registered': ['not registered',          'bad'],
  'error':          ['sheet error',             'bad'],
  'fuzzy':          ['confirm on receiver',     'warn'],
  'test':           ['not typed (Mac focused)', 'warn'],
};
function resultLabel(status, name) {
  const [txt, cls] = RESULT_MAP[status] || [status, ''];
  return [txt + (name ? '  ·  ' + name : ''), cls];
}
// The Mac pushes the roster (+tick state) in sheet mode; we cache it to answer scans locally.
function applyRoster(rows) {
  if (!rows || !rows.length) { state.roster = null; return; }   // cleared → disable local eval
  const m = {};
  for (const row of rows) {
    if (row && row[0] != null) m[String(row[0])] = { name: row[1] || '', ticked: !!row[2] };
  }
  state.roster = m;
}
// Instant result straight from the cached roster — no round-trip. The Mac still writes the
// sheet and its confirmation reconciles this shortly after. Returns true if it showed a result.
function showLocalResult(id, seq) {
  if (!state.roster) return false;   // roster not synced (e.g. keystroke mode) → let the Mac answer
  const r = state.roster[normId(id)];
  let status, name;
  if (!r)              { status = 'not-registered'; name = ''; }
  else if (r.ticked)   { status = 'already';        name = r.name; }
  else                 { status = 'checked-in';     name = r.name; r.ticked = true; /* optimistic */ }
  const [label, cls] = resultLabel(status, name);
  unlockAudio(); resultSound(status);
  markHistory(seq, label, cls);
  setReadout(id, label, cls);
  showResult(status, id, name);
  state._shown[seq] = status;
  return true;
}

async function sendScan(id, source) {
  state.seq += 1; localStorage.setItem('wedge.seq', String(state.seq));
  const item = { id, ts: Date.now(), seq: state.seq, status: '…', cls: '' };
  state.history.unshift(item); state.history = state.history.slice(0, 80);
  persistHistory(); renderHistory();

  if (state.settings.mode === 'clip') {
    try {
      await navigator.clipboard.writeText(id);
      markHistory(item.seq, 'copied', 'ok');
      setReadout(id, 'copied', 'ok');
    } catch (e) {
      markHistory(item.seq, 'tap row to copy', 'warn');
      setReadout(id, 'tap history to copy', 'warn');
    }
    return;
  }

  // Show the result instantly from the cached roster (if synced); the Mac still records it.
  const shownLocal = showLocalResult(id, item.seq);

  const payload = await encryptJSON({ t: 'scan', id, ts: item.ts, seq: item.seq, dev: state.deviceId, src: source });
  if (!shownLocal) {
    markHistory(item.seq, state.connected ? 'sending' : 'queued', '');
    setReadout(id, state.connected ? 'sending…' : 'queued', state.connected ? '' : 'warn');
  }
  if (!state.client) { markHistory(item.seq, 'no bridge', 'warn'); return; }
  state.client.publish(topicBase() + '/scan', payload, { qos: 1 }, (err) => {
    if (err) { markHistory(item.seq, 'failed', 'warn'); setReadout(id, 'send failed', 'warn'); }
    else if (!shownLocal) {
      // relay accepted; upgrade to "typed"/result when the receiver responds
      const h = state.history.find(x => x.seq === item.seq);
      if (h && h.status !== 'typed') { markHistory(item.seq, 'sent', ''); setReadout(id, 'sent', 'ok'); }
    }
  });
}

/* ────────────────────────── history UI ──────────────────────── */

function persistHistory() { localStorage.setItem('wedge.hist', JSON.stringify(state.history.slice(0, 80))); }

function markHistory(seq, status, cls) {
  const h = state.history.find(x => x.seq === seq);
  if (!h) return;
  h.status = status; h.cls = cls || '';
  persistHistory(); renderHistory();
  if (state.lastAccepted.id === h.id && status === 'typed') setReadout(h.id, 'typed ✓', 'ok');
}

function renderHistory() {
  const ul = $('#histList');
  if (!ul) return;   // history UI lives on the Mac receiver, not the phone
  ul.innerHTML = '';
  for (const h of state.history.slice(0, 40)) {
    const li = document.createElement('li');
    const d = new Date(h.ts);
    const hh = String(d.getHours()).padStart(2, '0'), mm = String(d.getMinutes()).padStart(2, '0'), ss = String(d.getSeconds()).padStart(2, '0');
    li.innerHTML = '<span class="id"></span><span class="t"></span><span class="s"></span>';
    li.querySelector('.id').textContent = h.id;
    li.querySelector('.t').textContent = hh + ':' + mm + ':' + ss;
    const s = li.querySelector('.s'); s.textContent = h.status; s.className = 's ' + (h.cls || '');
    li.addEventListener('click', async () => {
      try { await navigator.clipboard.writeText(h.id); toast('Copied ' + h.id); } catch (e) { toast('Copy failed'); }
    });
    ul.appendChild(li);
  }
}

function historyCSV() {
  const rows = ['timestamp,id,status'];
  for (const h of [...state.history].reverse()) rows.push(new Date(h.ts).toISOString() + ',' + h.id + ',' + h.status);
  return rows.join('\n');
}

/* ────────────────────────── readout / feedback ──────────────── */

// Student numbers are shown with a leading 'u' (how they're used day-to-day) even
// though we OCR / send / match on the bare digits.
function uDisp(id) { return id ? 'u' + id : ''; }

function setReadout(id, stateTxt, cls) {
  const n = $('#numOut');
  n.textContent = id ? uDisp(id) : '·······';
  n.classList.toggle('empty', !id);
  const st = $('#numState');
  st.textContent = stateTxt || '';
  st.className = 'state ' + (cls || '');
}

function toast(msg) {
  const t = $('#toast');
  t.textContent = msg; t.style.display = 'block';
  clearTimeout(t._h); t._h = setTimeout(() => { t.style.display = 'none'; }, 1800);
}

// A short silent WAV, generated once — played on a loop through an <audio> element to hold the
// iOS *media* audio channel open (see primeMediaChannel).
const SILENT_WAV = (() => {
  try {
    const sr = 8000, n = 2400, hdr = 44, buf = new Uint8Array(hdr + n), dv = new DataView(buf.buffer);
    const wr = (o, s) => { for (let i = 0; i < s.length; i++) dv.setUint8(o + i, s.charCodeAt(i)); };
    wr(0, 'RIFF'); dv.setUint32(4, 36 + n, true); wr(8, 'WAVE'); wr(12, 'fmt ');
    dv.setUint32(16, 16, true); dv.setUint16(20, 1, true); dv.setUint16(22, 1, true);
    dv.setUint32(24, sr, true); dv.setUint32(28, sr, true); dv.setUint16(32, 1, true); dv.setUint16(34, 8, true);
    wr(36, 'data'); dv.setUint32(40, n, true);
    for (let i = 0; i < n; i++) buf[hdr + i] = 128;      // 8-bit unsigned silence
    let bin = ''; for (let i = 0; i < buf.length; i++) bin += String.fromCharCode(buf[i]);
    return 'data:audio/wav;base64,' + btoa(bin);
  } catch (e) { return ''; }
})();

// iOS routes Web Audio through the RINGER channel by default, so result chimes are silenced by
// the mute switch / low ring volume even with media volume up (a well-documented WebKit quirk —
// "auto" session type is "ambient"). Fix it on the first user gesture two ways: (1) the
// AudioSession API — declare this page 'playback' audio, which uses the media channel and
// ignores the mute switch (iOS 16.4+); (2) a silent looping <audio> element as a fallback on
// older iOS, which holds the media channel open so Web Audio follows it.
function primeMediaChannel() {
  try { if (navigator.audioSession) navigator.audioSession.type = 'playback'; } catch (e) {}
  if (state._silentEl) { state._silentEl.play().catch(() => {}); return; }
  if (!SILENT_WAV) return;
  try {
    const el = document.createElement('audio');
    el.loop = true; el.preload = 'auto'; el.setAttribute('playsinline', '');
    el.src = SILENT_WAV; el.volume = 0.02;
    el.play().catch(() => {});
    state._silentEl = el;
  } catch (e) {}
}

function unlockAudio() {
  primeMediaChannel();
  if (state.audio) {
    if (state.audio.state === 'suspended') state.audio.resume().catch(() => {});   // e.g. after resume
    return;
  }
  try {
    const Ctx = window.AudioContext || window.webkitAudioContext;
    state.audio = new Ctx();
    const b = state.audio.createBuffer(1, 1, 22050);
    const src = state.audio.createBufferSource(); src.buffer = b; src.connect(state.audio.destination); src.start(0);
  } catch (e) {}
}

// Play a short sequence of notes. (No sub-bass thump — removed.)
function tone(notes, type = 'triangle', gain = 0.2, dur = 0.19) {
  if (!state.audio) return;
  try {
    const ctx = state.audio, t0 = ctx.currentTime;
    for (const [freq, dt] of notes) {
      const osc = ctx.createOscillator(), g = ctx.createGain();
      osc.type = type; osc.frequency.value = freq;
      const s = t0 + dt;
      g.gain.setValueAtTime(0.0001, s);
      g.gain.exponentialRampToValueAtTime(gain, s + 0.012);
      g.gain.exponentialRampToValueAtTime(0.0001, s + dur);
      osc.connect(g); g.connect(ctx.destination);
      osc.start(s); osc.stop(s + dur + 0.02);
    }
  } catch (e) {}
}

// Distinct, logical cues per check-in result.
function chimeOk()   { tone([[784, 0], [1175, 0.085]], 'triangle', 0.2, 0.19);        // bright ascending
                       if (navigator.vibrate) navigator.vibrate(60); }
function chimeWarn() { tone([[588, 0], [588, 0.13]], 'triangle', 0.18, 0.15);         // two flat mid notes — "heads up"
                       if (navigator.vibrate) navigator.vibrate([40, 60, 40]); }
function chimeFail() { tone([[247, 0], [165, 0.14]], 'sawtooth', 0.17, 0.26);         // low descending buzz — "no"
                       if (navigator.vibrate) navigator.vibrate([140, 70, 140]); }

// Sound for a check-in result (sheet mode only — driven by the receiver's status).
function resultSound(status) {
  if (status === 'checked-in') chimeOk();
  else if (status === 'already' || status === 'fuzzy' || status === 'test') chimeWarn();
  else chimeFail();                       // not-registered / error
}

function flashReticle() {
  const r = $('#reticle');
  r.classList.remove('hit'); void r.offsetWidth; r.classList.add('hit');
  setTimeout(() => r.classList.remove('hit'), 600);
}

/* ────────────────────────── camera ──────────────────────────── */

async function startCamera() {
  const video = $('#video');
  const stream = await navigator.mediaDevices.getUserMedia({
    audio: false,
    video: { facingMode: { ideal: 'environment' }, width: { ideal: 1920 }, height: { ideal: 1080 }, frameRate: { ideal: 30 } },
  });
  state.stream = stream;
  state.track = stream.getVideoTracks()[0];
  video.srcObject = stream;
  await video.play().catch(() => {});
  setupCamTools();
  requestWakeLock();
}

function stopCamera() {
  if (state.stream) state.stream.getTracks().forEach(t => t.stop());
  state.stream = null; state.track = null;
  $('#video').srcObject = null;
  releaseWakeLock();
}

function setupCamTools() {
  const caps = state.track && state.track.getCapabilities ? state.track.getCapabilities() : {};

  // Keep the card sharp while it (and the phone) move — continuous autofocus reduces the
  // out-of-focus frames the sharpness gate would otherwise skip.
  if (caps.focusMode && caps.focusMode.includes('continuous')) {
    state.track.applyConstraints({ advanced: [{ focusMode: 'continuous' }] }).catch(() => {});
  }
  // (Torch + zoom controls removed — the camera runs with autofocus only.)
}

async function requestWakeLock() {
  try { state.wakeLock = await navigator.wakeLock.request('screen'); } catch (e) {}
}
function releaseWakeLock() { try { state.wakeLock && state.wakeLock.release(); } catch (e) {} state.wakeLock = null; }

/* ── resume after the tab is backgrounded / frozen ──────────────────────────────
   iOS suspends a backgrounded PWA: it mutes/ends the camera track and usually drops the
   MQTT socket. Coming back, the old code left a frozen video + a possibly-dead relay, so
   scanning silently stopped and people had to reload + re-pair. Instead, whenever we return
   to a live scan session we auto-reboot exactly the parts that died — camera, relay, OCR —
   and drop straight back into scanning. Silent by design (a brief "reconnecting…" only). */
function cameraAlive() {
  const v = $('#video');
  return !!(state.track && state.track.readyState === 'live' && !state.track.muted
            && v && v.videoWidth > 0 && !v.paused);
}

async function resumeSession() {
  if (!state.sessionActive || state._resuming) return;
  state._resuming = true;
  const away = Date.now() - (state._hiddenAt || 0);
  try {
    // 1) Camera — rebuild the stream if the track died while backgrounded; else just un-pause.
    if (!cameraAlive()) {
      setReadout('', 'reconnecting…', 'warn');
      try { stopCamera(); } catch (e) {}
      await startCamera();
    } else {
      try { await $('#video').play(); } catch (e) {}
      requestWakeLock();
    }
    // 2) Relay — after a real backgrounding (>8s) the WebSocket is almost always dead even if
    //    the client still reads "connected", so force a clean reconnect (which re-sends hello,
    //    re-pairing on the receiver). Quick app-switches keep the existing connection.
    if (state.settings.mode === 'bridge' && (!state.connected || away > 8000)) {
      connectBridge();
    }
    // 3) OCR normally survives in memory; reload only if it somehow didn't.
    if (!state.paddleReady) { try { await loadPaddle(); } catch (e) {} }
    // 4) Back to scanning.
    if (!state.scanning) startScanning();
  } catch (e) {
    // Couldn't silently recover (e.g. camera permission lost) — show a tap-to-resume gate.
    setReadout('', 'tap to resume', 'warn');
    $('#goBtn').textContent = 'Resume scanning';
    $('#gate').style.display = 'flex';
  } finally {
    state._resuming = false;
  }
}

document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'hidden') { state._hiddenAt = Date.now(); return; }
  if (state.sessionActive) resumeSession();
  else if (state.scanning) requestWakeLock();
});
// pageshow fires when the page is restored from the bfcache (a common iOS "came back" path
// that doesn't always fire visibilitychange).
window.addEventListener('pageshow', () => { if (state.sessionActive) resumeSession(); });

/* ────────────────────────── OCR ─────────────────────────────── */

function greyscaleStretch(canvas) {
  const ctx = canvas.getContext('2d', {willReadFrequently: true});
  const W = canvas.width, H = canvas.height, n = W * H;
  const img = ctx.getImageData(0, 0, W, H);
  const d = img.data;
  const lum = new Uint8Array(n), histo = new Uint32Array(256);
  for (let i = 0, p = 0; i < n; i++, p += 4) {
    const v = (d[p] * 0.299 + d[p+1] * 0.587 + d[p+2] * 0.114) | 0;
    lum[i] = v; histo[v]++;
  }
  let lo = 0, hi = 255, acc = 0;
  const loCut = n * 0.05, hiCut = n * 0.95;
  for (let v = 0; v < 256; v++) { acc += histo[v]; if (acc >= loCut) { lo = v; break; } }
  acc = 0;
  for (let v = 0; v < 256; v++) { acc += histo[v]; if (acc >= hiCut) { hi = v; break; } }
  const range = Math.max(1, hi - lo);
  for (let i = 0, p = 0; i < n; i++, p += 4) {
    let v = ((lum[i] - lo) * 255 / range) | 0;
    if (v < 0) v = 0; else if (v > 255) v = 255;
    d[p] = d[p+1] = d[p+2] = v;
  }
  ctx.putImageData(img, 0, 0);
  return canvas;
}

// Full-screen, colour-coded check-in result: green ✓ / orange ↺ / red ✕.
// Non-blocking (pointer-events:none) so scanning keeps running underneath.
const RESULT_MS = 2600;   // hold the full-screen check-in result on screen this long
function showResult(status, id, name) {
  const map = {
    'checked-in':     ['ok',   '✓', 'CHECKED IN'],
    'already':        ['warn', '↺', 'ALREADY IN'],
    'not-registered': ['bad',  '✕', 'NOT REGISTERED'],
    'error':          ['bad',  '⚠', 'SHEET ERROR'],
    'fuzzy':          ['warn', '?', 'CONFIRM ON MAC'],
    'test':           ['warn', '⊘', 'NOT TYPED'],
  };
  const [cls, glyph, word] = map[status] || ['', '', status];
  const el = $('#result');
  el.className = cls;
  el.querySelector('.glyph').textContent = glyph;
  el.querySelector('.word').textContent  = word;
  el.querySelector('.num').textContent   = uDisp(id);
  el.querySelector('.who').textContent   = name || '';
  el.style.display = 'flex';
  requestAnimationFrame(() => el.classList.add('show'));
  clearTimeout(showResult._t);
  showResult._t = setTimeout(() => {
    el.classList.remove('show');
    setTimeout(() => { el.style.display = 'none'; }, 200);
  }, RESULT_MS);
}

// Neutral grey flash to acknowledge a raw scan/read. NOT green — the check-in hasn't been
// verified against the sheet yet; the green success is showResult() on the receiver's result.
function flashScan() {
  const el = $('#scanFlash');
  el.style.opacity = '1';
  el.style.display = 'block';
  requestAnimationFrame(() => requestAnimationFrame(() => {
    el.style.transition = 'opacity 0.5s ease-out';
    el.style.opacity = '0';
    setTimeout(() => { el.style.display = 'none'; el.style.transition = ''; }, 550);
  }));
}

// ── PaddleOCR (ONNX) engine ─────────────────────────────────────────────────────
// PP-OCR (DBNet detection + PP-OCRv5 recognition) via onnxruntime-web. The detector
// finds text at any angle, so there is no rotation search. ONNX Runtime comes from the
// CDN; the ~10MB models are served from ./models and cached by the service worker.
const ORT_SRC = 'https://cdn.jsdelivr.net/npm/onnxruntime-web@1.20.1/dist/';
function loadScript(src) {
  return new Promise((res, rej) => {
    if (document.querySelector(`script[src="${src}"]`)) return res();
    const s = document.createElement('script');
    s.src = src; s.onload = res; s.onerror = () => rej(new Error('load failed: ' + src));
    document.head.appendChild(s);
  });
}

// Fetch with a byte-progress callback + a stall guard (abort if no data for stallMs), so a
// dropped connection surfaces an error instead of hanging on "loading" forever.
async function fetchProgress(url, onByte, stallMs = 45000) {
  const ctrl = new AbortController();
  let stall = setTimeout(() => ctrl.abort(), stallMs);
  let res;
  try { res = await fetch(url, { signal: ctrl.signal }); }
  catch (e) { clearTimeout(stall); throw new Error('download stalled — check your connection'); }
  if (!res.ok) { clearTimeout(stall); throw new Error(`download failed (${res.status})`); }
  const total = +(res.headers.get('content-length') || 0);
  if (!res.body) { clearTimeout(stall); return new Uint8Array(await res.arrayBuffer()); }
  const reader = res.body.getReader();
  const chunks = []; let received = 0;
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      chunks.push(value); received += value.length;
      clearTimeout(stall); stall = setTimeout(() => ctrl.abort(), stallMs);
      onByte(received, total);
    }
  } catch (e) { throw new Error('download stalled — check your connection'); }
  finally { clearTimeout(stall); }
  const out = new Uint8Array(received); let o = 0;
  for (const c of chunks) { out.set(c, o); o += c.length; }
  return out;
}

// Download progress bar in the deck. pct 0-100 = determinate; null = indeterminate (moving
// stripe, used while ORT fetches its WASM which we can't byte-track); false = hide.
function dlBar(pct) {
  const bar = $('#dlBar'), fill = $('#dlFill');
  if (!bar || !fill) return;
  if (pct === false) { bar.style.display = 'none'; fill.classList.remove('indet'); fill.style.width = '0'; return; }
  bar.style.display = 'block';
  if (pct === null) { fill.style.width = ''; fill.classList.add('indet'); }
  else { fill.classList.remove('indet'); fill.style.width = Math.max(2, Math.min(100, pct)) + '%'; }
}

let _paddleLoading = null;
async function loadPaddle() {
  if (state.paddleReady) return;
  if (_paddleLoading) return _paddleLoading;
  _paddleLoading = (async () => {
    try {
      // First run pulls the models (~10MB) + the ONNX runtime WASM (~11MB); everything is
      // cached by the service worker afterwards, so this only happens once. Show a real
      // progress bar so it's obviously downloading, not frozen.
      const urls = ['./models/det.onnx', './models/rec.onnx'];
      const sizes = [2429873, 7830888], total = sizes[0] + sizes[1];
      const bytes = [];
      let base = 0;
      for (let i = 0; i < urls.length; i++) {
        bytes[i] = await fetchProgress(urls[i], (rcv) => {
          const pct = Math.min(99, Math.round((base + rcv) / total * 100));
          dlBar(pct);
          setReadout('', `downloading OCR model… ${pct}%`, 'warn');
        });
        base += sizes[i];
      }
      // Load the WASM-ONLY ORT build (small loader, no WebGPU — WebGPU init is flaky/slow on
      // iOS Safari and was the likely hang). It fetches an ~11MB WASM once, then SW-cached.
      dlBar(null);   // indeterminate — ORT fetches its WASM internally (no byte progress)
      setReadout('', 'starting OCR engine (~11 MB)…', 'warn');
      await loadScript(ORT_SRC + 'ort.wasm.min.js');
      window.ort.env.wasm.numThreads = 1;             // no cross-origin isolation → single-thread
      window.ort.env.wasm.wasmPaths = ORT_SRC;
      await PaddleOCR.init({ ort: window.ort, det: bytes[0], rec: bytes[1],
                             dictUrl: './models/en_dict.txt' });
      state.paddleReady = true;
      dlBar(100);
      setReadout('', 'ready — frame the number', '');
      setTimeout(() => dlBar(false), 500);
    } catch (e) {
      dlBar(false);
      throw e;
    }
  })();
  try { await _paddleLoading; } finally { _paddleLoading = null; }
}

// Reuse the app's ID rule (length + prefix + first-digit whitelist) on PaddleOCR's reads.
// Called as validate(text, digits) — keep using the digit-only string, so student-number
// reading behaves exactly as before.
function validateId(text, digits) {
  const d = (digits != null) ? digits : text;
  return extractId(d, state.settings.digits, state.settings.prefix, state.settings.startDigits);
}

// ── Textbook Library: read a textbook code from the label ────────────────────────
// The label reads ANUSA/H/TB/XYZ/123. ANUSA/H/TB is a fixed prefix (no value) — fuzzy-match
// it to confirm we're actually reading a textbook label, then return ONLY the valuable part,
// which is exactly 3 letters + "/" + 3 digits (e.g. PSY/101). Returns null otherwise.
// Called as validate(text, digits) — uses the full text (letters + slashes), not just digits.
const TB_PREFIX = ['ANUSA', 'H', 'TB'];
const TB_TOL = [2, 1, 1];   // per-token fuzzy tolerance (edit distance) for the fixed prefix
function extractTextbookCode(text) {
  const up = String(text || '').toUpperCase();
  // split on slash (OCR may render / as \ or |), keep alphanumerics within each token
  const parts = up.split(/[\/\\|]+/).map(s => s.replace(/[^A-Z0-9]/g, '')).filter(Boolean);
  if (parts.length < TB_PREFIX.length + 1) return null;
  for (let i = 0; i < TB_PREFIX.length; i++)
    if (_lev(parts[i], TB_PREFIX[i]) > TB_TOL[i]) return null;   // not a textbook label
  const rest = parts.slice(TB_PREFIX.length);
  // valuable part = 3 letters then 3 digits, whether OCR kept the slash ("XYZ/123") or not.
  if (rest.length >= 2 && /^[A-Z]{3}$/.test(rest[0]) && /^[0-9]{3}$/.test(rest[1]))
    return rest[0] + '/' + rest[1];
  const m = rest[0] && rest[0].match(/^([A-Z]{3})([0-9]{3})$/);
  return m ? (m[1] + '/' + m[2]) : null;
}
// Levenshtein edit distance (small strings only).
function _lev(a, b) {
  a = a || ''; b = b || '';
  const m = a.length, n = b.length;
  if (!m) return n; if (!n) return m;
  let prev = Array.from({ length: n + 1 }, (_, j) => j);
  for (let i = 1; i <= m; i++) {
    const cur = [i];
    for (let j = 1; j <= n; j++)
      cur[j] = Math.min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (a[i - 1] === b[j - 1] ? 0 : 1));
    prev = cur;
  }
  return prev[n];
}

// ── Textbook Library two-stage flow ─────────────────────────────────────────────
// The receiver tells us its mode. In 'textbook' mode a scan is a two-step flow: read the
// student number, prompt "now scan textbook code", then read the code and show "complete".
let _tbStudent = null;
function applyRxMode(mode) {
  const prev = state.rxMode;
  state.rxMode = mode;
  if (mode === 'textbook' && prev !== 'textbook') {
    state.tbStage = 'student'; _tbStudent = null; resetPaddleConfirm(); hideTbOverlay();
  } else if (mode !== 'textbook' && prev === 'textbook') {
    state.tbStage = null; _tbStudent = null; resetPaddleConfirm(); hideTbOverlay();
  }
  updateHint();
  updateModeChip();
}
function resetPaddleConfirm() { _pCandId = null; _pSentId = null; }

// What to read + what to do with it right now. Normal modes are unchanged (student number →
// check-in/keystroke). Textbook mode swaps in the textbook reader + the two-stage handlers.
function currentScanTask() {
  if (state.rxMode === 'textbook') {
    if (state.tbStage === 'student')  return { validate: validateId, accept: onStudentScanned };
    if (state.tbStage === 'textbook') return { validate: (t) => extractTextbookCode(t), accept: onTextbookScanned };
    return null;   // 'await' / 'done' → waiting for a button tap, don't scan
  }
  return { validate: validateId, accept: handleAccept };
}
function onStudentScanned(studentId) {
  _tbStudent = studentId;
  flashScan(); flashReticle(); unlockAudio(); chimeWarn();   // captured — one more to go
  state.tbStage = 'await'; resetPaddleConfirm();
  showTbOverlay('await', studentId, null);
  updateHint();
}
function onTextbookScanned(code) {
  flashScan(); flashReticle(); unlockAudio(); chimeOk();     // complete
  state.tbStage = 'done'; resetPaddleConfirm();
  sendTextbookPair(_tbStudent, code);
  showTbOverlay('done', _tbStudent, code);
  updateHint();
}
async function sendTextbookPair(student, code) {
  if (!state.client) return;
  state.seq += 1; localStorage.setItem('wedge.seq', String(state.seq));
  try {
    const payload = await encryptJSON({ t: 'tbpair', student, code, ts: Date.now(),
                                        seq: state.seq, dev: state.deviceId });
    state.client.publish(topicBase() + '/scan', payload, { qos: 1 });
  } catch (e) { /* not fatal — the pairing still showed on the phone */ }
}
function showTbOverlay(stage, student, code) {
  const ov = $('#tbOverlay'); if (!ov) return;
  if (stage === 'await') {
    $('#tbTitle').textContent = 'Student captured';
    $('#tbSub').textContent = 'u' + student;
    $('#tbBtn').textContent = 'Okay — scan textbook code';
  } else {
    $('#tbTitle').textContent = 'Complete ✓';
    $('#tbSub').textContent = 'u' + student + '  →  ' + code;
    $('#tbBtn').textContent = 'Next student';
  }
  ov.style.display = 'flex';
}
function hideTbOverlay() { const ov = $('#tbOverlay'); if (ov) ov.style.display = 'none'; }

// Scan tick — no rotation search (the detector handles orientation). Two-in-a-row confirm:
// a value must read identically on two consecutive frames before it's accepted, so a lone
// misread is never sent. _pSentId then blocks re-sends of a lingering card until it leaves.
let _pCandId = null;   // pending candidate
let _pSentId = null;   // already accepted this presentation — cleared when the card leaves
async function scanTick() {
  if (!state.scanning || state.busy || !state.paddleReady) return;
  const task = currentScanTask();
  if (!task) return;   // gated stage (e.g. textbook prompt showing) — don't scan
  state.busy = true;
  try {
    const frame = grabFrame();
    if (!frame) return;
    // Skip motion-blurred frames (cheap) so OCR only spends time on sharp ones.
    const fs = focusScore(frame);
    _focusPeak = Math.max(fs, _focusPeak * 0.92);
    if (fs < _focusPeak * SHARP_FRAC && _blurSkips < MAX_BLUR_SKIP) {
      _blurSkips++;
      return;
    }
    _blurSkips = 0;
    const res = await PaddleOCR.read(frame, { validate: task.validate });
    const val = res.id;   // the validated value (student number OR textbook code)
    // Two-in-a-row confirm, then run the task's accept action.
    if (val && val === _pCandId) {
      if (_pSentId !== val) { task.accept(val); _pSentId = val; }
    } else if (val) {
      _pCandId = val;
    } else {
      _pCandId = null; _pSentId = null;
    }
  } catch (e) { /* transient error: skip frame */ }
  finally { state.busy = false; }
}

/* Grab the full video frame (portrait-corrected). The detector is orientation-agnostic,
   so there is no rotation search — one grab per tick. */
const GRAB_SCALE = 0.75;   // capture size as a fraction of native video (speed vs detail)
function grabFrame() {
  const video = $('#video');
  const stage = video.getBoundingClientRect();
  const vw = video.videoWidth, vh = video.videoHeight;
  if (!vw || !vh) return null;
  const portrait = stage.height > stage.width;
  const rad = -(portrait ? 1 : 0) * Math.PI / 2;   // upright the common portrait hold (cosmetic)
  const srcW = Math.round(vw * GRAB_SCALE), srcH = Math.round(vh * GRAB_SCALE);
  const swap = portrait;
  const outW = swap ? srcH : srcW, outH = swap ? srcW : srcH;
  const out = document.createElement('canvas');
  out.width = outW; out.height = outH;
  const ctx = out.getContext('2d', { willReadFrequently: true });
  ctx.translate(outW / 2, outH / 2);
  ctx.rotate(rad);
  ctx.drawImage(video, -srcW / 2, -srcH / 2, srcW, srcH);
  return greyscaleStretch(out);
}

// Reset per-session scan state (called on start / after a reset). No rotation lock any more —
// just the two-in-a-row confirm + sharpness baseline.
function resetRtState() {
  _pCandId = null; _pSentId = null;
  _focusPeak = 0; _blurSkips = 0;
  state.suppressId = null;
}

function extractId(text, nDigits, prefix, startSet) {
  const ok = (r) => r.length === nDigits
    && (!prefix || r.startsWith(prefix))
    && (!startSet || startSet.indexOf(r[0]) >= 0);   // first digit must be allowed
  const runs = (text.match(/\d+/g) || []);
  for (const r of runs) if (ok(r)) return r;         // a clean N-digit run
  const joined = runs.join('');
  if (ok(joined)) return joined;                     // number split by spaces/glyphs
  return null;
}

/* ────────────────────────── scan loop ───────────────────────── */

// ── sharpness gate ──────────────────────────────────────────────
// Handheld scanning produces lots of motion-blurred frames. OCR-ing a blurry frame
// wastes ~300–500ms AND tends to misread (which breaks the two-in-a-row confirm),
// so both add up to a slow, flaky scan. We score each grabbed frame's high-frequency
// content (sharp = high, blurred = low) and skip OCR on the blurry ones — but never
// starve: after MAX_BLUR_SKIP skips we OCR anyway, and the peak decays so it re-adapts
// to lighting. Net effect: OCR fires on the sharp moments between shakes → faster,
// cleaner reads. Skipping is cheap (no OCR), and the loop reschedules immediately.
let _focusPeak = 0;
let _blurSkips = 0;
const SHARP_FRAC = 0.6;      // OCR only frames within this fraction of the recent peak
const MAX_BLUR_SKIP = 4;     // …but force a read after this many consecutive skips

function focusScore(canvas) {
  const w = canvas.width, h = canvas.height;
  if (!w || !h) return 0;
  // Sample the central band (where the card/number usually sits) at a stride for speed.
  const x0 = Math.floor(w * 0.15), y0 = Math.floor(h * 0.20);
  const sw = Math.max(1, Math.floor(w * 0.70)), sh = Math.max(1, Math.floor(h * 0.60));
  const d = canvas.getContext('2d', { willReadFrequently: true }).getImageData(x0, y0, sw, sh).data;
  const step = 2;
  let sum = 0, n = 0;
  for (let y = 0; y < sh; y += step) {
    const row = y * sw * 4;
    for (let x = 0; x < sw - step; x += step) {
      const i = row + x * 4;
      const dv = d[i] - d[i + step * 4];   // horizontal gradient (grayscale → use R)
      sum += dv * dv; n++;
    }
  }
  return n ? sum / n : 0;
}

const DUP_WINDOW_MS = 1200;   // min gap before the SAME id can be re-sent
// A card that stays in view is sent ONCE: _pSentId (see scanTick) blocks every repeat read
// until the card leaves the frame. This DUP_WINDOW_MS check is a backstop for a shaky card
// that briefly drops and re-acquires — it stops the same id firing twice within the window.
function handleAccept(id) {
  const now = Date.now();
  // A scan the user explicitly deleted stays ignored until the card leaves the frame.
  if (state.suppressId === id) return;
  state.suppressId = null;
  // Backstop against a momentary lock drop + relock of the same card.
  if (state.lastAccepted.id === id && (now - state.lastAccepted.t) < DUP_WINDOW_MS) return;
  state.lastAccepted = { id, t: now };

  flashScan(); flashReticle();   // grey — a scan, not a confirmed check-in; sound plays on the result
  setReadout(id, '', '');
  sendScan(id, 'ocr');
}

// A clean de-dupe reset that does NOT disturb the rotation lock or interrupt scanning:
// forget the last-accepted id, drop any delete-suppression, and mark whatever card is
// currently in view as already handled for this lock — so a reset can never instantly
// re-send it. The card rescans only after it leaves the frame and is presented again.
function resetDedupe() {
  state.lastAccepted = { id: null, t: 0 };
  state.suppressId = null;
  _pSentId = _pCandId;   // mark the in-view card as handled so a reset can't instantly re-send it
}

// Self-rescheduling loop: the next OCR fires as soon as the previous one FINISHES,
// instead of on a fixed 350ms grid. A fixed interval quantised every read up to the
// next tick boundary (a ~400ms recognise() effectively cost ~700ms because the
// mid-flight timer fired while busy and was skipped). Chaining removes that dead time.
let loopTimer = null;
const LOOP_GAP_MS = 30;   // brief yield so the UI/GC breathe between reads
function scanLoop() {
  loopTimer = null;
  if (!state.scanning) return;
  scanTick().finally(() => {
    if (state.scanning) loopTimer = setTimeout(scanLoop, LOOP_GAP_MS);
  });
}
function startScanning() {
  // Belt-and-suspenders: ensure the QR pairing loop isn't still running against the camera.
  if (state._pairTimer) { clearTimeout(state._pairTimer); state._pairTimer = null; }
  state._pairResolve = null;
  state.scanning = true;
  resetRtState();  // re-detect card orientation + clear pending candidate each session
  $('#pauseBtn').style.display = 'block';
  if (!loopTimer) scanLoop();
  // Show video stream dimensions — useful for diagnosing iOS rotation issues
  const v = $('#video');
  setReadout('', `ready (${v.videoWidth}×${v.videoHeight})`, '');
}
function stopScanning() {
  state.scanning = false;
  if (loopTimer) { clearTimeout(loopTimer); loopTimer = null; }
  $('#pauseBtn').style.display = 'none';
}

/* ────────────────────────── UI wiring ───────────────────────── */

function refreshChrome() {
  updateHint();
  updateModeChip();
}

// The hint line under the reticle: empty in normal scanning; in Textbook Library mode it
// says which code we're scanning for right now.
function updateHint() {
  const h = $('#hint'); if (!h) return;
  let txt = '';
  if (state.rxMode === 'textbook')
    txt = (state.tbStage === 'textbook') ? 'Scanning for textbook' : 'Scanning for student card';
  h.textContent = txt;
}

// Header chip showing the receiver's mode; and the deck's dot placeholder is hidden in
// modes where it never fills in (Textbook Library uses its own overlay for feedback).
function updateModeChip() {
  const el = $('#modeChip');
  if (el) {
    const label = { keys: 'KEYSTROKE', sheet: 'PANTRY', textbook: 'TEXTBOOK' }[state.rxMode];
    if (label) { el.textContent = label; el.style.display = ''; }
    else { el.style.display = 'none'; }
  }
  const ro = document.querySelector('.readout');
  if (ro) ro.style.visibility = (state.rxMode === 'textbook') ? 'hidden' : 'visible';
}

// Extract a room code from a scanned string — either a …/?room=XXXX URL or a bare code.
function parseRoom(text) {
  const s = String(text || '');
  const m = s.match(/[?&]room=([A-Za-z0-9]+)/i) || s.match(/^\s*([A-Za-z0-9]{5,12})\s*$/);
  return m ? m[1].toUpperCase() : null;
}

function setGateMode(mode) {
  const desc = $('#gateDesc'), btn = $('#goBtn');
  if (mode === 'pair') {
    desc.textContent = 'Point your camera at the pairing QR shown on the ANUSA Scanner receiver.';
    btn.textContent = 'Scan pairing code';
  } else {
    desc.textContent = 'Frame the number on a student ID; each confirmed read is checked in on the receiver.';
    btn.textContent = 'Start scanning';
  }
}

// Read the receiver's pairing QR (a …/?room=XXXX URL) from the camera. Resolves the room.
function scanPairingQR() {
  const video = $('#video');
  const cv = document.createElement('canvas');
  const ctx = cv.getContext('2d', { willReadFrequently: true });
  setReadout('', 'point at the receiver QR…', '');
  $('#pairManual').style.display = 'block';
  return new Promise((resolve) => {
    state._pairResolve = resolve;
    (function tick() {
      if (!state._pairResolve) return;
      const vw = video.videoWidth, vh = video.videoHeight;
      if (vw && vh && typeof jsQR === 'function') {
        const w = Math.min(480, vw), h = Math.round(vh * w / vw);
        cv.width = w; cv.height = h;
        ctx.drawImage(video, 0, 0, w, h);
        try {
          const img = ctx.getImageData(0, 0, w, h);
          const code = jsQR(img.data, w, h, { inversionAttempts: 'dontInvert' });
          const room = code && parseRoom(code.data);
          if (room) { finishPair(room); return; }
        } catch (e) { /* transient */ }
      }
      state._pairTimer = setTimeout(tick, 120);   // ~8 fps — gentle on memory vs 60fps rAF
    })();
  });
}
function finishPair(room) {
  const r = state._pairResolve;
  state._pairResolve = null;
  if (state._pairTimer) { clearTimeout(state._pairTimer); state._pairTimer = null; }
  $('#pairManual').style.display = 'none';
  if (r) r(room);
}

// Hard recovery when the PWA is stuck/glitched: drop the service worker + all caches
// (so nothing stale is served) and reload fresh from the network. Keeps saved settings
// (room, etc.) since those live in localStorage, which is left untouched.
async function forceReload() {
  if (!confirm('Force reload the app?\n\nClears the cache to recover from a glitch. Your room stays paired.')) return;
  try {
    if ('serviceWorker' in navigator) {
      const regs = await navigator.serviceWorker.getRegistrations();
      await Promise.all(regs.map(r => r.unregister()));
    }
    if (window.caches) {
      const ks = await caches.keys();
      await Promise.all(ks.map(k => caches.delete(k)));
    }
  } catch (e) { /* best effort */ }
  location.reload();
}

async function onStart() {
  const err = $('#gateErr');
  err.style.display = 'none';
  unlockAudio();
  $('#goBtn').disabled = true;
  try {
    await startCamera();
    $('#gate').style.display = 'none';
    if (state.needPairing) {
      const room = await scanPairingQR();      // scan the receiver's QR first
      if (room) {
        state.settings.room = room; saveSettings(state.settings);
        state.needPairing = false; refreshChrome();
        toast('Paired · room ' + room);
        connectBridge();                       // join the room + say hello
      }
    }
    if (!state.connected) connectBridge();
    await loadPaddle();
    await waitForRxSync(1500);   // let the receiver confirm its mode so scan UI paints correctly
    startScanning();
    state.sessionActive = true;   // resume auto-reboots camera/relay/OCR after backgrounding
  } catch (e) {
    stopCamera();
    $('#gate').style.display = 'flex';   // re-show the gate so the error (e.g. OCR download) is visible
    err.textContent = (e && e.name === 'NotAllowedError')
      ? 'Camera access was denied. Allow camera for this app in iOS Settings → Apps, then try again.'
      : 'Could not start: ' + (e.message || e);
    err.style.display = 'block';
  } finally {
    $('#goBtn').disabled = false;
  }
}

function onPause() {
  state.sessionActive = false;   // deliberate pause — don't auto-reboot on the next foreground
  stopScanning();
  stopCamera();            // stops the camera track + releases the wake lock
  setReadout('', 'paused — camera off', 'warn');
  // A distinct paused gate so it's clearly a pause (session/room kept), not an end.
  const desc = $('#gateDesc');
  if (desc) desc.textContent = 'Paused — camera off to save battery. The room stays paired; tap Resume to keep scanning.';
  $('#goBtn').textContent = 'Resume scanning';
  $('#gate').style.display = 'flex';
}

function openSheet() {
  const s = state.settings;
  $('#setRoom').value = s.room;
  $('#setDigits').value = s.digits;
  $('#setPrefix').value = s.prefix || '';
  $('#setStart').value = s.startDigits || '';
  $('#setMode').value = s.mode;
  $('#setBroker').value = s.broker;
  $('#sheetBack').classList.add('open');
  $('#sheet').classList.add('open');
}
function closeSheet() {
  $('#sheetBack').classList.remove('open');
  $('#sheet').classList.remove('open');
}
function onSave() {
  const s = state.settings;
  s.room = ($('#setRoom').value.trim().toUpperCase() || randomRoom());
  s.digits = Math.max(4, Math.min(12, Number($('#setDigits').value) || DEFAULTS.digits));
  s.prefix = $('#setPrefix').value.replace(/\D/g, '').slice(0, Math.max(0, s.digits - 1));
  s.startDigits = [...new Set($('#setStart').value.replace(/\D/g, ''))].join(''); // unique digits, blank = any
  s.mode = $('#setMode').value === 'clip' ? 'clip' : 'bridge';
  s.broker = $('#setBroker').value.trim() || DEFAULTS.broker;
  saveSettings(s);
  refreshChrome();
  closeSheet();
  connectBridge();
}

function wireUI() {
  $('#goBtn').addEventListener('click', onStart);
  $('#pauseBtn').addEventListener('click', onPause);
  $('#reloadBtn').addEventListener('click', forceReload);
  $('#tbBtn').addEventListener('click', () => {
    if (state.tbStage === 'await') { state.tbStage = 'textbook'; resetPaddleConfirm(); hideTbOverlay(); }
    else if (state.tbStage === 'done') { state.tbStage = 'student'; _tbStudent = null; resetPaddleConfirm(); hideTbOverlay(); }
    updateHint();
  });
  $('#pairManual').addEventListener('click', () => {
    const v = prompt('Enter the room code shown on the receiver:');
    if (v == null) return;
    const room = parseRoom(v) || v.replace(/[^A-Za-z0-9]/g, '').toUpperCase().slice(0, 12);
    if (room) finishPair(room);
  });
  $('#gearBtn').addEventListener('click', openSheet);
  $('#sheetBack').addEventListener('click', closeSheet);
  $('#saveBtn').addEventListener('click', onSave);
  $('#newRoom').addEventListener('click', () => { $('#setRoom').value = randomRoom(); });
}

/* ────────────────────────── boot ────────────────────────────── */

// Deep-link pairing: the Mac shows a QR of  …/?room=XXXXXX  — opening it here sets
// the room so there's nothing to type. The URL is then cleaned so a later reload
// doesn't re-pin a stale room.
function applyRoomFromURL() {
  try {
    const p = new URLSearchParams(location.search).get('room');
    if (!p) return false;
    const room = p.replace(/[^A-Za-z0-9]/g, '').toUpperCase().slice(0, 12);
    if (!room) return false;
    state.settings.room = room;
    saveSettings(state.settings);
    history.replaceState(null, '', location.pathname);
    return true;
  } catch (e) { return false; }
}

window.addEventListener('load', () => {
  wireUI();
  const paired = applyRoomFromURL();
  state.needPairing = !paired;   // no ?room deep-link → scan the receiver's QR to pair
  refreshChrome();
  renderHistory();
  setGateMode(paired ? 'scan' : 'pair');
  if (paired) {
    // Deep-link (or native-camera scan of the receiver QR): join the relay right away so
    // the Mac's pairing screen clears the moment this phone opens.
    toast('Paired · room ' + state.settings.room);
    connectBridge();
  }
  if ('serviceWorker' in navigator) navigator.serviceWorker.register('./sw.js').catch(() => {});
});
