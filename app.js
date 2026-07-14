'use strict';
/* ID Wedge — camera → OCR → encrypted relay → keystrokes on the paired computer.
   Runs entirely as a static page (GitHub Pages friendly). */

const $ = (s) => document.querySelector(s);

/* ────────────────────────── settings ────────────────────────── */

const DEFAULTS = {
  room: '',
  digits: 7,
  prefix: '',                                      // optional leading digits, e.g. '8'
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
  worker: null,            // tesseract
  workerReady: false,
  stream: null,
  track: null,
  scanning: false,
  busy: false,
  lastRead: null,
  lastAccepted: { id: null, t: 0 },
  history: (() => { try { return JSON.parse(localStorage.getItem('wedge.hist') || '[]'); } catch (e) { return []; } })(),
  audio: null,
  wakeLock: null,
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
  });
  client.on('reconnect', () => { state.connected = false; setStatus('wait', 'reconnecting…'); });
  client.on('close', () => { state.connected = false; if (state.settings.mode === 'bridge') setStatus('err', 'bridge offline'); });
  client.on('error', () => { /* handled by close/reconnect */ });
  client.on('message', async (topic, payload) => {
    if (topic !== topicBase() + '/ack') return;
    try {
      const msg = await decryptJSON(new TextDecoder().decode(payload));
      if (msg.t === 'ack' && msg.dev === state.deviceId) markHistory(msg.seq, 'typed', 'ok');
    } catch (e) { /* wrong room / stray traffic */ }
  });
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

  const payload = await encryptJSON({ t: 'scan', id, ts: item.ts, seq: item.seq, dev: state.deviceId, src: source });
  markHistory(item.seq, state.connected ? 'sending' : 'queued', '');
  setReadout(id, state.connected ? 'sending…' : 'queued', state.connected ? '' : 'warn');
  if (!state.client) { markHistory(item.seq, 'no bridge', 'warn'); return; }
  state.client.publish(topicBase() + '/scan', payload, { qos: 1 }, (err) => {
    if (err) { markHistory(item.seq, 'failed', 'warn'); setReadout(id, 'send failed', 'warn'); }
    else {
      // relay accepted; upgrade to "typed" when the receiver acks
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

function setReadout(id, stateTxt, cls) {
  const n = $('#numOut');
  n.textContent = id || '·······';
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

function unlockAudio() {
  if (state.audio) return;
  try {
    const Ctx = window.AudioContext || window.webkitAudioContext;
    state.audio = new Ctx();
    const b = state.audio.createBuffer(1, 1, 22050);
    const src = state.audio.createBufferSource(); src.buffer = b; src.connect(state.audio.destination); src.start(0);
  } catch (e) {}
}

function beep() {
  if (!state.audio) return;
  try {
    const t0 = state.audio.currentTime;
    const osc = state.audio.createOscillator(), g = state.audio.createGain();
    osc.type = 'square'; osc.frequency.setValueAtTime(880, t0); osc.frequency.setValueAtTime(1320, t0 + 0.07);
    g.gain.setValueAtTime(0.12, t0); g.gain.exponentialRampToValueAtTime(0.001, t0 + 0.16);
    osc.connect(g); g.connect(state.audio.destination);
    osc.start(t0); osc.stop(t0 + 0.17);
  } catch (e) {}
  if (navigator.vibrate) navigator.vibrate(60);
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
    video: { facingMode: { ideal: 'environment' }, width: { ideal: 1920 }, height: { ideal: 1080 } },
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
  const torchBtn = $('#torchBtn');
  if (caps.torch) {
    torchBtn.style.display = 'block';
    torchBtn.onclick = async () => {
      const on = !torchBtn.classList.contains('on');
      try { await state.track.applyConstraints({ advanced: [{ torch: on }] }); torchBtn.classList.toggle('on', on); } catch (e) {}
    };
  } else torchBtn.style.display = 'none';

  const zw = $('#zoomWrap'), z = $('#zoom');
  if (caps.zoom && caps.zoom.max > caps.zoom.min) {
    zw.style.display = 'flex';
    z.min = caps.zoom.min; z.max = Math.min(caps.zoom.max, caps.zoom.min + 6); z.step = caps.zoom.step || 0.1;
    z.value = state.track.getSettings().zoom || caps.zoom.min;
    z.oninput = () => { state.track.applyConstraints({ advanced: [{ zoom: Number(z.value) }] }).catch(() => {}); };
  } else zw.style.display = 'none';
}

async function requestWakeLock() {
  try { state.wakeLock = await navigator.wakeLock.request('screen'); } catch (e) {}
}
function releaseWakeLock() { try { state.wakeLock && state.wakeLock.release(); } catch (e) {} state.wakeLock = null; }
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible' && state.scanning) requestWakeLock();
});

/* ────────────────────────── OCR ─────────────────────────────── */

async function initOCR() {
  if (state.workerReady) return;
  if (typeof Tesseract === 'undefined') throw new Error('OCR engine failed to load — check your connection and reload.');
  setReadout('', 'loading OCR engine…', '');
  const worker = await Tesseract.createWorker('eng');
  // PSM 6 (uniform block) tested markedly more tolerant of loose framing than
  // PSM 7 (single line) against a real card photo — survives the label line
  // below the number entering the frame.
  const psm = (Tesseract.PSM && Tesseract.PSM.SINGLE_BLOCK) ? Tesseract.PSM.SINGLE_BLOCK : '6';
  await worker.setParameters({
    tessedit_char_whitelist: '0123456789',
    tessedit_pageseg_mode: psm,
    user_defined_dpi: '300',
  });
  state.worker = worker;
  state.workerReady = true;
  setReadout('', 'ready — frame the number', '');
}

/* map the on-screen reticle to source-video pixels (object-fit: cover) */
function grabReticle() {
  const video = $('#video');
  const stage = video.getBoundingClientRect();
  const ret = $('#reticle').getBoundingClientRect();
  const vw = video.videoWidth, vh = video.videoHeight;
  if (!vw || !vh) return null;

  const scale = Math.max(stage.width / vw, stage.height / vh);
  const dispW = vw * scale, dispH = vh * scale;
  const offX = (dispW - stage.width) / 2, offY = (dispH - stage.height) / 2;

  let sx = (ret.left - stage.left + offX) / scale;
  let sy = (ret.top  - stage.top  + offY) / scale;
  let sw = ret.width  / scale;
  let sh = ret.height / scale;
  sx = Math.max(0, Math.min(vw - 2, sx)); sy = Math.max(0, Math.min(vh - 2, sy));
  sw = Math.min(sw, vw - sx); sh = Math.min(sh, vh - sy);
  if (sw < 8 || sh < 8) return null;

  const targetH = 340;   // full portrait card — 7-digit filter handles false-read rejection
  const targetW = Math.round(sw * (targetH / sh));
  const c = document.createElement('canvas');
  c.width = targetW; c.height = targetH;
  const ctx = c.getContext('2d', { willReadFrequently: true });
  ctx.imageSmoothingEnabled = true;
  ctx.drawImage(video, sx, sy, sw, sh, 0, 0, targetW, targetH);

  // grayscale + contrast stretch (5th–95th percentile)
  const img = ctx.getImageData(0, 0, targetW, targetH);
  const d = img.data, n = targetW * targetH;
  const lum = new Uint8Array(n), histo = new Uint32Array(256);
  for (let i = 0, p = 0; i < n; i++, p += 4) {
    const v = (d[p] * 0.299 + d[p + 1] * 0.587 + d[p + 2] * 0.114) | 0;
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
    d[p] = d[p + 1] = d[p + 2] = v;
  }
  ctx.putImageData(img, 0, 0);

  // Only rotate when the video stream is portrait (iPhone back camera in portrait mode).
  // Mac/webcam delivers landscape frames where card text already reads horizontally —
  // applying the rotation there would make the text vertical and break OCR.
  if (video.videoHeight <= video.videoWidth) return c;

  const rot = document.createElement('canvas');
  rot.width  = c.height;
  rot.height = c.width;
  const rctx = rot.getContext('2d');
  rctx.translate(rot.width, 0);
  rctx.rotate(Math.PI / 2);
  rctx.drawImage(c, 0, 0);
  return rot;
}

function extractId(text, nDigits, prefix) {
  const ok = (r) => r.length === nDigits && (!prefix || r.startsWith(prefix));
  const rev = (r) => r.split('').reverse().join('');
  const runs = text.match(/\d+/g) || [];
  for (const r of runs) if (ok(r)) return r;         // clean forward run
  const joined = runs.join('');
  if (ok(joined)) return joined;                      // digits split by spaces (forward)
  // Reverse fallback — catches card held 180° flipped or camera-mirrored reads
  for (const r of runs) { const rv = rev(r); if (ok(rv)) return rv; }
  const rj = rev(joined);
  if (ok(rj)) return rj;
  return null;
}

/* ────────────────────────── scan loop ───────────────────────── */

async function scanTick() {
  if (!state.scanning || state.busy || !state.workerReady) return;
  state.busy = true;
  try {
    const frame = grabReticle();
    if (frame) {
      const { data } = await state.worker.recognize(frame);
      const id = extractId(data.text || '', state.settings.digits, state.settings.prefix);
      handleRead(id);
    }
  } catch (e) { /* transient recognize errors: skip frame */ }
  finally { state.busy = false; }
}

function handleRead(id) {
  if (!id) { state.lastRead = null; return; }
  if (id !== state.lastRead) { state.lastRead = id; return; }     // need two matching reads
  state.lastRead = null;

  const now = Date.now();
  if (state.lastAccepted.id === id && now - state.lastAccepted.t < 8000) return;  // same card, debounce
  state.lastAccepted = { id, t: now };

  beep(); flashReticle();
  setReadout(id, '', '');
  sendScan(id, 'ocr');
}

let loopTimer = null;
function startScanning() {
  state.scanning = true;
  $('#pauseBtn').style.display = 'block';
  if (!loopTimer) loopTimer = setInterval(scanTick, 350);
}
function stopScanning() {
  state.scanning = false;
  if (loopTimer) { clearInterval(loopTimer); loopTimer = null; }
  $('#pauseBtn').style.display = 'none';
}

/* ────────────────────────── UI wiring ───────────────────────── */

function refreshChrome() {
  $('#roomTxt').textContent = state.settings.room;
  $('#hint').textContent = 'Align the student card in the frame';
}

async function onStart() {
  const err = $('#gateErr');
  err.style.display = 'none';
  unlockAudio();
  $('#goBtn').disabled = true;
  try {
    await startCamera();
    $('#gate').style.display = 'none';
    connectBridge();          // don't block scanning on the relay
    await initOCR();
    startScanning();
  } catch (e) {
    stopCamera();
    err.textContent = (e && e.name === 'NotAllowedError')
      ? 'Camera access was denied. Allow camera for this app in iOS Settings → Apps, then try again.'
      : 'Could not start: ' + (e.message || e);
    err.style.display = 'block';
  } finally {
    $('#goBtn').disabled = false;
  }
}

function onPause() {
  stopScanning();
  stopCamera();
  setReadout('', '', '');
  $('#gate').style.display = 'flex';
  $('#goBtn').textContent = 'Resume scanning';
}

function openSheet() {
  const s = state.settings;
  $('#setRoom').value = s.room;
  $('#setDigits').value = s.digits;
  $('#setPrefix').value = s.prefix || '';
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
  $('#gearBtn').addEventListener('click', openSheet);
  $('#roomChip').addEventListener('click', openSheet);
  $('#sheetBack').addEventListener('click', closeSheet);
  $('#saveBtn').addEventListener('click', onSave);
  $('#newRoom').addEventListener('click', () => { $('#setRoom').value = randomRoom(); });

  $('#manualBtn').addEventListener('click', () => {
    $('#manualRow').classList.toggle('open');
    if ($('#manualRow').classList.contains('open')) $('#manualIn').focus();
  });
  $('#manualSend').addEventListener('click', () => {
    const v = ($('#manualIn').value.match(/\d+/g) || []).join('');
    if (!v) return;
    $('#manualIn').value = '';
    unlockAudio(); beep();
    setReadout(v, '', '');
    state.lastAccepted = { id: v, t: Date.now() };
    sendScan(v, 'manual');
  });

  $('#histBtn').addEventListener('click', () => $('#hist').classList.toggle('open'));
  $('#copyAllBtn').addEventListener('click', async () => {
    try { await navigator.clipboard.writeText(historyCSV()); toast('CSV copied to clipboard'); }
    catch (e) { toast('Copy failed'); }
  });
}

/* ────────────────────────── boot ────────────────────────────── */

window.addEventListener('load', () => {
  wireUI();
  refreshChrome();
  renderHistory();
  if ('serviceWorker' in navigator) navigator.serviceWorker.register('./sw.js').catch(() => {});
});
