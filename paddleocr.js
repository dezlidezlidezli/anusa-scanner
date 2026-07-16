/* paddleocr.js — PP-OCR (ONNX Runtime Web) digit reader, self-contained.
   Pipeline: DBNet detection → oriented-box extraction → upright crop → PP-OCRv5 rec (CTC).
   The detector finds text at ANY angle, so skew/rotation is handled without a rotation search.

   Usage:
     await PaddleOCR.init({ ort, detUrl, recUrl, dictUrl });
     const r = await PaddleOCR.read(canvasOrImage, { validate });  // validate(str)->str|null
     // r = { id, lines:[{text,digits,box,score}], ms }
*/
const PaddleOCR = (() => {
  let ort, det, rec, charset = null, ready = false;

  // detection tunables
  const DET_MAX = 640;          // longest side fed to the detector (speed vs reach)
  const THRESH = 0.3;           // probability-map binarisation
  const BOX_THRESH = 0.5;       // min mean-probability inside a box to keep it
  const UNCLIP = 1.7;           // grow shrunk DB boxes back out
  const MIN_SIDE = 4;           // drop tiny specks (in det px)
  const REC_H = 48;             // PP-OCRv5 rec input height (v5 uses 48, not 32)

  async function init(opts) {
    ort = opts.ort;
    const dictText = await (await fetch(opts.dictUrl)).text();
    const dict = dictText.split(/\r?\n/);
    if (dict[dict.length - 1] === '') dict.pop();
    // CTC classes = [blank] + dict (+ maybe a trailing space); reconciled to the model's
    // real class count at decode time.
    charset = ['<blank>'].concat(dict);
    const so = { executionProviders: ['wasm'], graphOptimizationLevel: 'all' };
    // det/rec may be a URL string OR pre-fetched bytes (Uint8Array) — ORT accepts both.
    det = await ort.InferenceSession.create(opts.det || opts.detUrl, so);
    rec = await ort.InferenceSession.create(opts.rec || opts.recUrl, so);
    ready = true;
    return { detIn: det.inputNames, detOut: det.outputNames,
             recIn: rec.inputNames, recOut: rec.outputNames, classes: charset.length };
  }

  // ── helpers ───────────────────────────────────────────────────────────────
  function toCanvas(src) {
    if (src instanceof HTMLCanvasElement) return src;
    const c = document.createElement('canvas');
    c.width = src.naturalWidth || src.videoWidth || src.width;
    c.height = src.naturalHeight || src.videoHeight || src.height;
    c.getContext('2d').drawImage(src, 0, 0, c.width, c.height);
    return c;
  }

  // resize so the longest side ≤ DET_MAX and both sides are multiples of 32 (DBNet needs that)
  function resizeForDet(src) {
    let r = Math.min(1, DET_MAX / Math.max(src.width, src.height));
    let w = Math.max(32, Math.round(src.width * r / 32) * 32);
    let h = Math.max(32, Math.round(src.height * r / 32) * 32);
    const c = document.createElement('canvas'); c.width = w; c.height = h;
    c.getContext('2d').drawImage(src, 0, 0, w, h);
    return { canvas: c, scaleX: src.width / w, scaleY: src.height / h };
  }

  // det input tensor: NCHW float32, normalised with ImageNet mean/std
  const DMEAN = [0.485, 0.456, 0.406], DSTD = [0.229, 0.224, 0.225];
  function detTensor(canvas) {
    const W = canvas.width, H = canvas.height;
    const d = canvas.getContext('2d', { willReadFrequently: true }).getImageData(0, 0, W, H).data;
    const out = new Float32Array(3 * W * H), plane = W * H;
    for (let i = 0, p = 0; i < plane; i++, p += 4) {
      out[i]             = ((d[p]     / 255) - DMEAN[0]) / DSTD[0];
      out[i + plane]     = ((d[p + 1] / 255) - DMEAN[1]) / DSTD[1];
      out[i + 2 * plane] = ((d[p + 2] / 255) - DMEAN[2]) / DSTD[2];
    }
    return new ort.Tensor('float32', out, [1, 3, H, W]);
  }

  // ── DB post-process: probability map → oriented boxes (in det-canvas px) ─────
  function dbBoxes(prob, W, H) {
    const bin = new Uint8Array(W * H);
    for (let i = 0; i < W * H; i++) bin[i] = prob[i] > THRESH ? 1 : 0;
    const label = new Int32Array(W * H).fill(0);
    const boxes = [];
    const stack = [];
    let cur = 0;
    for (let s = 0; s < W * H; s++) {
      if (!bin[s] || label[s]) continue;
      cur++;
      const xs = [], ys = [];
      stack.push(s); label[s] = cur;
      while (stack.length) {
        const p = stack.pop();
        const x = p % W, y = (p / W) | 0;
        xs.push(x); ys.push(y);
        if (x > 0     && bin[p - 1] && !label[p - 1]) { label[p - 1] = cur; stack.push(p - 1); }
        if (x < W - 1 && bin[p + 1] && !label[p + 1]) { label[p + 1] = cur; stack.push(p + 1); }
        if (y > 0     && bin[p - W] && !label[p - W]) { label[p - W] = cur; stack.push(p - W); }
        if (y < H - 1 && bin[p + W] && !label[p + W]) { label[p + W] = cur; stack.push(p + W); }
      }
      if (xs.length < MIN_SIDE * MIN_SIDE) continue;
      const box = orientedRect(xs, ys);
      if (Math.min(box.w, box.h) < MIN_SIDE) continue;
      // score = mean prob over the component's pixels
      let sc = 0; for (let k = 0; k < xs.length; k++) sc += prob[ys[k] * W + xs[k]];
      sc /= xs.length;
      if (sc < BOX_THRESH) continue;
      unclip(box, UNCLIP);
      box.score = sc;
      boxes.push(box);
    }
    return boxes;
  }

  // PCA-based oriented bounding rectangle for a set of points
  function orientedRect(xs, ys) {
    const n = xs.length;
    let mx = 0, my = 0;
    for (let i = 0; i < n; i++) { mx += xs[i]; my += ys[i]; }
    mx /= n; my /= n;
    let cxx = 0, cxy = 0, cyy = 0;
    for (let i = 0; i < n; i++) {
      const dx = xs[i] - mx, dy = ys[i] - my;
      cxx += dx * dx; cxy += dx * dy; cyy += dy * dy;
    }
    cxx /= n; cxy /= n; cyy /= n;
    const angle = 0.5 * Math.atan2(2 * cxy, cxx - cyy);   // principal axis
    const ca = Math.cos(-angle), sa = Math.sin(-angle);
    let minu = 1e9, maxu = -1e9, minv = 1e9, maxv = -1e9;
    for (let i = 0; i < n; i++) {
      const dx = xs[i] - mx, dy = ys[i] - my;
      const u = dx * ca - dy * sa, v = dx * sa + dy * ca;
      if (u < minu) minu = u; if (u > maxu) maxu = u;
      if (v < minv) minv = v; if (v > maxv) maxv = v;
    }
    // recompute centre from the extents (more stable than the raw centroid)
    const ucx = (minu + maxu) / 2, vcy = (minv + maxv) / 2;
    const cx = mx + ucx * ca + vcy * sa;
    const cy = my - ucx * sa + vcy * ca;
    return { cx, cy, w: maxu - minu, h: maxv - minv, angle };
  }

  function unclip(box, ratio) {
    const area = box.w * box.h, peri = 2 * (box.w + box.h);
    const d = peri ? area * ratio / peri : 0;
    box.w += 2 * d; box.h += 2 * d;
  }

  // extract an oriented box from `src` into an upright crop (long side horizontal)
  function cropBox(src, box, scaleX, scaleY, flip180) {
    let { cx, cy, w, h, angle } = box;
    cx *= scaleX; cy *= scaleY; w *= scaleX; h *= scaleY;   // det-px → source-px
    // text lines are wider than tall — orient the long side horizontal
    let a = angle;
    if (h > w) { const t = w; w = h; h = t; a += Math.PI / 2; }
    if (flip180) a += Math.PI;
    const outH = REC_H, outW = Math.max(REC_H, Math.round(REC_H * w / h));
    const c = document.createElement('canvas'); c.width = outW; c.height = outH;
    const ctx = c.getContext('2d', { willReadFrequently: true });
    ctx.translate(outW / 2, outH / 2);
    ctx.scale(outW / w, outH / h);
    ctx.rotate(-a);
    ctx.translate(-cx, -cy);
    ctx.drawImage(src, 0, 0);
    return c;
  }

  // ── recognition: crop → CTC text ────────────────────────────────────────────
  async function recognize(crop) {
    const W = crop.width, H = crop.height;
    const d = crop.getContext('2d', { willReadFrequently: true }).getImageData(0, 0, W, H).data;
    const plane = W * H, out = new Float32Array(3 * plane);
    for (let i = 0, p = 0; i < plane; i++, p += 4) {
      out[i]             = (d[p]     / 255 - 0.5) / 0.5;
      out[i + plane]     = (d[p + 1] / 255 - 0.5) / 0.5;
      out[i + 2 * plane] = (d[p + 2] / 255 - 0.5) / 0.5;
    }
    const t = new ort.Tensor('float32', out, [1, 3, H, W]);
    const res = await rec.run({ [rec.inputNames[0]]: t });
    const o = res[rec.outputNames[0]];
    const [, T, C] = o.dims;             // [1, timesteps, classes]
    const cs = charset.slice();
    while (cs.length < C) cs.push(' ');  // reconcile class count (blank + dict + maybe space)
    let text = '', prev = -1;
    for (let ti = 0; ti < T; ti++) {
      let best = 0, bv = -1e9;
      for (let c = 0; c < C; c++) { const v = o.data[ti * C + c]; if (v > bv) { bv = v; best = c; } }
      if (best !== 0 && best !== prev) text += (cs[best] || '');
      prev = best;
    }
    return text;
  }

  // ── public: read digits from an image/canvas ────────────────────────────────
  async function read(src, opts = {}) {
    if (!ready) throw new Error('PaddleOCR not initialised');
    const validate = opts.validate || ((s) => s);
    const t0 = performance.now();
    const canvas = toCanvas(src);
    const { canvas: detC, scaleX, scaleY } = resizeForDet(canvas);
    const res = await det.run({ [det.inputNames[0]]: detTensor(detC) });
    const prob = res[det.outputNames[0]];
    const boxes = dbBoxes(prob.data, prob.dims[3], prob.dims[2]).sort((a, b) => b.score - a.score);

    const lines = [];
    let id = null;
    for (const box of boxes.slice(0, 8)) {          // read the strongest few regions
      let text = await recognize(cropBox(canvas, box, scaleX, scaleY, false));
      let digits = (text.match(/\d/g) || []).join('');
      let v = validate(digits);
      if (!v) {   // maybe upside-down — try the 180° crop
        const text2 = await recognize(cropBox(canvas, box, scaleX, scaleY, true));
        const dig2 = (text2.match(/\d/g) || []).join('');
        const v2 = validate(dig2);
        if (v2) { text = text2; digits = dig2; v = v2; }
      }
      lines.push({ text, digits, box, score: box.score });
      if (v && !id) id = v;
    }
    return { id, lines, ms: Math.round(performance.now() - t0), nboxes: boxes.length };
  }

  return { init, read, _internal: { dbBoxes, cropBox, recognize } };
})();
if (typeof window !== 'undefined') window.PaddleOCR = PaddleOCR;
