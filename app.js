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
    const ctx = state.audio;
    const t0 = ctx.currentTime;

    // Audible confirmation tone
    const osc = ctx.createOscillator(), g = ctx.createGain();
    osc.type = 'square';
    osc.frequency.setValueAtTime(880, t0);
    osc.frequency.setValueAtTime(1320, t0 + 0.07);
    g.gain.setValueAtTime(0.12, t0);
    g.gain.exponentialRampToValueAtTime(0.001, t0 + 0.16);
    osc.connect(g); g.connect(ctx.destination);
    osc.start(t0); osc.stop(t0 + 0.17);

    // Physical thump via speaker — iOS Taptic Engine is inaccessible from web,
    // but a 40 Hz burst makes the speaker cone move enough to feel through the hand.
    const thump = ctx.createOscillator(), tg = ctx.createGain();
    thump.type = 'sine';
    thump.frequency.value = 40;
    tg.gain.setValueAtTime(0.9, t0);
    tg.gain.exponentialRampToValueAtTime(0.001, t0 + 0.12);
    thump.connect(tg); tg.connect(ctx.destination);
    thump.start(t0); thump.stop(t0 + 0.12);
  } catch (e) {}
  if (navigator.vibrate) navigator.vibrate([80, 60, 80]); // Android fallback
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

function flashGreen() {
  const el = $('#successFlash');
  el.style.opacity = '1';
  el.style.display = 'block';
  requestAnimationFrame(() => requestAnimationFrame(() => {
    el.style.transition = 'opacity 0.5s ease-out';
    el.style.opacity = '0';
    setTimeout(() => { el.style.display = 'none'; el.style.transition = ''; }, 550);
  }));
}

async function initOCR() {
  if (state.workerReady) return;
  if (typeof Tesseract === 'undefined') throw new Error('OCR engine failed to load — check your connection and reload.');
  setReadout('', 'loading OCR engine…', '');
  const worker = await Tesseract.createWorker('eng');
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
  greyscaleStretch(c);

  // Rotate when the DISPLAY area is portrait — the video stream dimensions are unreliable
  // on iOS (sensor reports landscape 1920×1080 even when phone is held portrait).
  // stage = video.getBoundingClientRect(), computed above; its aspect ratio matches the screen.
  if (stage.height <= stage.width) return c;

  const rot = document.createElement('canvas');
  rot.width  = c.height;
  rot.height = c.width;
  const rctx = rot.getContext('2d');
  // 90° CCW: card held portrait (90° CW from landscape) + 90° CCW canvas = digits upright.
  // CW would double the rotation → 180° → upside-down digits → garbled OCR.
  rctx.translate(0, rot.height);
  rctx.rotate(-Math.PI / 2);
  rctx.drawImage(c, 0, 0);
  // No zone crop: PSM 6 + digits-only whitelist + 7-digit filter handle false-positive
  // exclusion; zone crop breaks tilted cards where the number ends up in the wrong quadrant.
  return greyscaleStretch(rot);
}

// Physical layout of the ANU ID-1 card (85.6 × 54 mm, landscape orientation).
// Tune BC_H_MM / OFF_* if the extracted patch drifts; OUT_H trades speed for clarity.
const KS = {
  BC_H_MM:    38,   // barcode physical height — used for ppm from eigenvalue spread
  OFF_X_MM:   18,   // barcode-centre → student-number-centre, card +x (rightward)
  OFF_Y_MM:   27,   // barcode-centre → student-number-centre, card +y (downward)
  PATCH_W_MM: 42,   // extraction patch width  (generous)
  PATCH_H_MM: 16,   // extraction patch height
  OUT_H:      200,  // output canvas height in px
  DARK_RATIO: 0.60, // threshold = mean * DARK_RATIO (relative to scene brightness)
  BD_THRESH:  0.55, // high-density block threshold = maxDensity * BD_THRESH (relative)
};

// Locate the barcode region by edge-transition density (no decoding required —
// works even on worn/blurry barcodes). Returns {snx,sny,angle,ppm} in video pixels.
function densityKeystone(video) {
  const VW = video.videoWidth, VH = video.videoHeight;
  if (!VW || !VH) return null;

  // Downsample to ~192px wide for fast analysis
  const S   = Math.max(1, Math.round(VW / 192));
  const AW  = Math.ceil(VW/S), AH = Math.ceil(VH/S);
  const tmp = document.createElement('canvas');
  tmp.width = AW; tmp.height = AH;
  const tctx = tmp.getContext('2d', {willReadFrequently: true});
  tctx.drawImage(video, 0, 0, AW, AH);
  const raw = tctx.getImageData(0, 0, AW, AH).data;

  // Grayscale (fixed-point integer — avoids repeated float ops)
  const gray = new Uint8Array(AW * AH);
  let gsum = 0;
  for (let i=0,p=0; i<gray.length; i++,p+=4) {
    gray[i] = (raw[p]*77 + raw[p+1]*150 + raw[p+2]*29) >> 8;
    gsum += gray[i];
  }
  // Relative dark threshold: fraction of scene mean — adapts to lighting
  const darkT = (gsum / gray.length) * KS.DARK_RATIO;

  // Per-pixel 4-neighbour transition count: high = barcode-like texture
  const trans = new Uint8Array(AW * AH);
  for (let y=1; y<AH-1; y++) {
    for (let x=1; x<AW-1; x++) {
      const c = gray[y*AW+x] < darkT;
      trans[y*AW+x] =
        ((gray[y*AW+x+1]  < darkT) !== c ? 1 : 0) +
        ((gray[y*AW+x-1]  < darkT) !== c ? 1 : 0) +
        ((gray[(y+1)*AW+x]< darkT) !== c ? 1 : 0) +
        ((gray[(y-1)*AW+x]< darkT) !== c ? 1 : 0);
    }
  }

  // Block density map (6×6 analysis-px blocks)
  const BLK = 6;
  const NBX = Math.floor(AW/BLK), NBY = Math.floor(AH/BLK);
  const bd = new Float32Array(NBX * NBY);
  let maxBD = 0;
  for (let by=0; by<NBY; by++)
    for (let bx=0; bx<NBX; bx++) {
      let s=0;
      for (let y=by*BLK; y<(by+1)*BLK && y<AH; y++)
        for (let x=bx*BLK; x<(bx+1)*BLK && x<AW; x++)
          s += trans[y*AW+x];
      bd[by*NBX+bx] = s;
      if (s > maxBD) maxBD = s;
    }
  if (maxBD < 8) return null;

  // Weighted centroid + second moments of blocks above relative density threshold
  const bdT = maxBD * KS.BD_THRESH;
  let wcx=0, wcy=0, wsum=0;
  for (let by=0; by<NBY; by++)
    for (let bx=0; bx<NBX; bx++) {
      const d=bd[by*NBX+bx]; if (d<bdT) continue;
      wcx += (bx+.5)*BLK*d; wcy += (by+.5)*BLK*d; wsum += d;
    }
  if (!wsum) return null;
  const cx=wcx/wsum, cy=wcy/wsum;

  let m20=0, m02=0, m11=0;
  for (let by=0; by<NBY; by++)
    for (let bx=0; bx<NBX; bx++) {
      const d=bd[by*NBX+bx]; if (d<bdT) continue;
      const dx=(bx+.5)*BLK-cx, dy=(by+.5)*BLK-cy;
      m20+=dx*dx*d; m02+=dy*dy*d; m11+=dx*dy*d;
    }
  m20/=wsum; m02/=wsum; m11/=wsum;

  // Card orientation angle from principal axis of barcode density blob
  const angle = 0.5 * Math.atan2(2*m11, m20-m02);

  // ppm from eigenvalue spread (uniform distribution: σ = L/(2√3))
  const ev1 = (m20+m02)/2 + Math.sqrt(((m20-m02)/2)**2 + m11**2);
  const ppm  = Math.sqrt(ev1) * S * 2*Math.sqrt(3) / KS.BC_H_MM;

  // Project barcode centre → student number centre in video pixels
  const vcx=cx*S, vcy=cy*S;
  const cos=Math.cos(angle), sin=Math.sin(angle);
  const snx = vcx + (KS.OFF_X_MM*cos - KS.OFF_Y_MM*sin)*ppm;
  const sny = vcy + (KS.OFF_X_MM*sin + KS.OFF_Y_MM*cos)*ppm;

  if (snx<0||snx>VW||sny<0||sny>VH) return null;
  return {snx, sny, angle, ppm};
}

function keystoneGrab() {
  const video = $('#video');
  const ks = densityKeystone(video);
  if (!ks) return null;
  const {snx, sny, angle, ppm} = ks;
  const outH = KS.OUT_H;
  const outW = Math.round(KS.PATCH_W_MM / KS.PATCH_H_MM * outH);
  const out  = document.createElement('canvas');
  out.width  = outW; out.height = outH;
  const ctx  = out.getContext('2d');
  const sf   = outH / (KS.PATCH_H_MM * ppm);
  ctx.translate(outW/2, outH/2);
  ctx.rotate(-angle);
  ctx.scale(sf, sf);
  ctx.drawImage(video, -snx, -sny);
  return greyscaleStretch(out);
}

function extractId(text, nDigits, prefix) {
  const ok = (r) => r.length === nDigits && (!prefix || r.startsWith(prefix));
  const rev = (r) => r.split('').reverse().join('');
  const runs = (text.match(/\d+/g) || []);
  for (const r of runs) if (ok(r)) return r;
  const joined = runs.join('');
  if (ok(joined)) return joined;
  for (const r of runs) { const rv = rev(r); if (ok(rv)) return rv; }
  if (ok(rev(joined))) return rev(joined);
  return null;
}

/* ────────────────────────── scan loop ───────────────────────── */

let dbgVisible = false;

const _dbgLog = [];
function dbgRecord(mode, rawText, candidate) {
  if (!dbgVisible) return;
  const willConfirm = candidate && candidate === state.lastRead;
  const entry = `[${mode}] "${rawText.replace(/\s+/g,' ').trim().slice(0,48)}" → ${candidate||'null'} ${willConfirm?'✓CONFIRM':candidate?'(1st)':''}`;
  _dbgLog.unshift(entry);
  if (_dbgLog.length > 8) _dbgLog.pop();
  const el = $('#dbgText');
  if (el) el.textContent = _dbgLog.join('\n');
  // Publish plaintext debug to MQTT so any subscriber can see it
  if (state.client) {
    try { state.client.publish(topicBase() + '/dbg', JSON.stringify({t:'dbg',mode,raw:rawText.trim().slice(0,60),id:candidate,ts:Date.now()}), {qos:0}); }
    catch(e) {}
  }
}

const _frameLog = [];

function captureFrame(mode, frame, rawText, candidate) {
  if (!dbgVisible || !frame) return;
  _frameLog.unshift({ mode, dataUrl: frame.toDataURL('image/jpeg', 0.85),
    w: frame.width, h: frame.height,
    raw: (rawText || '').trim().replace(/\s+/g,' ').slice(0, 36),
    id: candidate, ts: Date.now() });
  if (_frameLog.length > 10) _frameLog.pop();
}

async function saveFrames() {
  if (!_frameLog.length) { alert('No frames yet — enable debug (tap hint text) and scan first.'); return; }
  const n = _frameLog.length;
  const COLS = Math.min(3, n), ROWS = Math.ceil(n / COLS);
  const FW = _frameLog[0].w, FH = _frameLog[0].h, LH = 16, PAD = 3;
  const sheet = document.createElement('canvas');
  sheet.width  = COLS * (FW + PAD);
  sheet.height = ROWS * (FH + LH + PAD);
  const ctx = sheet.getContext('2d');
  ctx.fillStyle = '#111';
  ctx.fillRect(0, 0, sheet.width, sheet.height);
  ctx.font = '11px monospace';
  await Promise.all(_frameLog.map((f, i) => new Promise(res => {
    const img = new Image();
    img.onload = () => {
      const col = i % COLS, row = Math.floor(i / COLS);
      const x = col * (FW + PAD), y = row * (FH + LH + PAD);
      ctx.drawImage(img, x, y, FW, FH);
      ctx.fillStyle = f.id ? '#a8ff78' : '#ff6b6b';
      ctx.fillText(`[${f.mode}]${f.id ? ' '+f.id : ' null'} "${f.raw}"`, x + 2, y + FH + 13);
      res();
    };
    img.onerror = res;
    img.src = f.dataUrl;
  })));
  const sheetUrl = sheet.toDataURL('image/jpeg', 0.88);
  try {
    const blob = await (await fetch(sheetUrl)).blob();
    const file = new File([blob], `idwedge_frames_${Date.now()}.jpg`, { type: 'image/jpeg' });
    if (navigator.canShare?.({ files: [file] })) { await navigator.share({ files: [file], title: 'ID Wedge debug frames' }); return; }
  } catch(e) {}
  window.open(sheetUrl); // fallback: open in tab → long-press to save
}

async function scanTick() {
  if (!state.scanning || state.busy || !state.workerReady) return;
  state.busy = true;
  try {
    let mode = '--', frame = keystoneGrab();
    if (frame) { mode = 'KS'; } else { frame = grabReticle(); if (frame) mode = 'RT'; }
    if (frame) {
      if (dbgVisible) {
        const dbg = $('#dbgCanvas');
        dbg.width = frame.width; dbg.height = frame.height;
        const dctx = dbg.getContext('2d');
        dctx.drawImage(frame, 0, 0);
        // Grid overlay — 10% lines with labels so screenshots reveal exact position
        const W = frame.width, H = frame.height;
        dctx.font = `bold ${Math.max(9, Math.round(W * 0.06))}px monospace`;
        dctx.textBaseline = 'top';
        for (let pct = 10; pct < 100; pct += 10) {
          const x = W * pct / 100, y = H * pct / 100;
          dctx.strokeStyle = pct === 50 ? 'rgba(255,122,26,0.8)' : 'rgba(255,122,26,0.35)';
          dctx.lineWidth = pct === 50 ? 1.5 : 0.8;
          dctx.beginPath(); dctx.moveTo(x,0); dctx.lineTo(x,H); dctx.stroke();
          dctx.beginPath(); dctx.moveTo(0,y); dctx.lineTo(W,y); dctx.stroke();
          dctx.fillStyle = 'rgba(0,0,0,0.6)';
          dctx.fillText(pct+'%', x+2, 0);
          dctx.fillStyle = 'rgba(255,122,26,1)';
          dctx.fillText(pct+'%', x+2, 0);
          dctx.fillStyle = 'rgba(0,0,0,0.6)';
          dctx.fillText(pct+'%', 2, y+1);
          dctx.fillStyle = 'rgba(255,122,26,1)';
          dctx.fillText(pct+'%', 2, y+1);
        }
        // Diagnostic: vw×vh and whether rotation fired
        const vid = $('video'), st = vid.getBoundingClientRect();
        const rotFired = st.height > st.width;
        const label = `${vid.videoWidth}×${vid.videoHeight} rot:${rotFired?'YES':'NO'} out:${W}×${H}`;
        dctx.font = `bold ${Math.max(10,Math.round(W*0.06))}px monospace`;
        dctx.fillStyle = 'rgba(0,0,0,0.7)';
        dctx.fillText(label, 3, H-Math.round(W*0.08)-1);
        dctx.fillStyle = '#ff7a1a';
        dctx.fillText(label, 2, H-Math.round(W*0.08));
      }
      const { data } = await state.worker.recognize(frame);
      const id = extractId(data.text || '', state.settings.digits, state.settings.prefix);
      captureFrame(mode, frame, data.text || '', id);
      dbgRecord(mode, data.text || '', id);
      handleRead(id);
    }
  } catch (e) { /* transient recognize errors: skip frame */ }
  finally { state.busy = false; }
}

function handleRead(id) {
  if (!id) { return; }  // no valid N-digit read — don't reset the consecutive streak
  if (id !== state.lastRead) { state.lastRead = id; return; }
  state.lastRead = null;

  // Block re-scan of any ID already in history — show Re-scan button instead
  if (state.history.some(h => h.id === id)) {
    const btn = $('#rescanBtn');
    btn.dataset.rid = id;
    btn.style.display = 'block';
    return;
  }
  state.lastAccepted = { id, t: Date.now() };

  flashGreen(); beep(); flashReticle();
  $('#rescanBtn').style.display = 'none';
  setReadout(id, '', '');
  sendScan(id, 'ocr');
}

let loopTimer = null;
function startScanning() {
  state.scanning = true;
  $('#pauseBtn').style.display = 'block';
  if (!loopTimer) loopTimer = setInterval(scanTick, 350);
  // Show video stream dimensions — useful for diagnosing iOS rotation issues
  const v = $('#video');
  setReadout('', `ready (${v.videoWidth}×${v.videoHeight})`, '');
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

  $('#hint').addEventListener('click', () => {
    dbgVisible = !dbgVisible;
    $('#dbgCanvas').style.display = dbgVisible ? 'block' : 'none';
    $('#dbgText').style.display = dbgVisible ? 'block' : 'none';
    $('#saveFramesBtn').style.display = dbgVisible ? 'block' : 'none';
    if (!dbgVisible) { const d = $('#dbgCanvas'); d.width = 0; $('#dbgText').textContent = ''; _frameLog.length = 0; }
  });

  $('#rescanBtn').addEventListener('click', () => {
    const id = $('#rescanBtn').dataset.rid;
    if (!id) return;
    // Remove all history entries for this ID so it can be re-sent cleanly
    state.history = state.history.filter(h => h.id !== id);
    persistHistory(); renderHistory();
    state.lastAccepted = { id, t: Date.now() };
    flashGreen(); beep(); flashReticle();
    $('#rescanBtn').style.display = 'none';
    setReadout(id, '', '');
    sendScan(id, 'ocr');
  });

  $('#saveFramesBtn').addEventListener('click', saveFrames);
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
