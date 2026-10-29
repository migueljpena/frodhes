// The reed model (frodhes tools/reed_model.py, the "bell" pickup) as an AudioWorklet: play it live.
// Per voice: two modes driven by the hammer's half-sine force, the pickup's bell; after the mix: the input
// high-pass, the preamp's tanh, the vibrato, the line or the 200A's speaker (reed_model.speaker), the key-return
// knocks and the volume. With a set's "keys" (every key's
// own regulation), "damp2" (the two-stage damper) and "thump" (the knock), as reed_model.py; without, as bell3_v1.

const TAU = 2 * Math.PI;
const MAXV = 16;          // voices before the oldest is stolen

function interpLog(a, b, midi, mid) {
  if (mid === undefined || mid === null) {
    const t = Math.min(Math.max((midi - 33) / 63, 0), 1);
    return a * Math.pow(b / a, t);
  }
  if (midi <= 64) return interpLog(a, mid, 33 + (midi - 33) * 63 / 31);
  return interpLog(mid, b, 33 + (midi - 64) * 63 / 32);
}

// this key's value of a "keys" quantity (reed_model.key_of)
function keyOf(P, midi, what, def) {
  return P.keys && midi >= 33 && midi <= 96 ? P.keys[what][midi - 33] : def;
}
function f0Of(P, midi) { return 440 * Math.pow(2, (midi - 69) / 12 + (keyOf(P, midi, 'cents', 0) + (P.tune || 0)) / 1200); }

// the VOICE knobs (reed_model.KNOBS / apply_knobs / reg_keys): 0.0-10.0, 5.0 the set as fitted; REG 10 the measured
// regulation, 0 every key even; TUNE in cents. Read at the next strike
const BELL_MAX = 0.3;     // reed_model.BELL_MAX
const KNOBS = { tone: 5, bark: 5, bell: 5, hammer: 5, decay: 5, release: 5, noise: 5, tune: 0, reg: 10 };
function regKeys(P, r) {
  const R = P.reg, a = Math.min(Math.max(r / 10, 0), 1), amts = R.amt;
  let i = 0;
  while (i < amts.length - 2 && a > amts[i + 1]) i++;
  const t = (a - amts[i]) / (amts[i + 1] - amts[i]);
  const A = R.tables[i], B = R.tables[i + 1], out = {};
  for (const q of Object.keys(A)) {
    const lg = q === 'd' || q === 'g' || q === 't60';
    out[q] = A[q].map((x, j) => lg ? Math.exp((1 - t) * Math.log(x) + t * Math.log(B[q][j])) : (1 - t) * x + t * B[q][j]);
  }
  return out;
}
function applyKnobs(P, K0) {
  const K = { ...KNOBS, ...K0 };
  const x = (k) => (K[k] - 5) / 5;
  const Q = { ...P };
  Q.bell_p = (Q.bell_p || 1) * Math.pow(2, x('tone'));
  for (const n of ['u_lo', 'u_mid', 'u_hi']) if (Q[n] !== undefined && Q[n] !== null) Q[n] *= Math.pow(2, 0.75 * x('bark'));
  if (K.bell <= 5) Q.w2 = Q.w2 * Math.pow(K.bell / 5, 2); else Q.bell_x = BELL_MAX * Math.pow(x('bell'), 2);
  Q.hammer_x = Math.pow(2, -x('hammer'));
  for (const n of ['t60_lo', 't60_mid', 't60_hi']) if (Q[n] !== undefined) Q[n] *= Math.pow(2, x('decay'));
  if (Q.damp2) Q.damp2 = { ...Q.damp2, fast: Q.damp2.fast * Math.pow(3, x('release')), slow: Q.damp2.slow * Math.pow(3, x('release')) };
  Q.t60_rel = (Q.t60_rel || 0.12) * Math.pow(3, x('release'));
  Q.tune = K.tune;
  if (Q.reg && K.reg !== 10) Q.keys = regKeys(Q, K.reg);
  const n = Math.pow(K.noise / 5, 1.5);
  if (Q.keys && Q.keys.sn) Q.keys = { ...Q.keys, sn: Q.keys.sn.map((s) => s * n) };
  if (Q.thump) { if (n > 0) Q.thump = { ...Q.thump, db: Q.thump.db + 20 * Math.log10(n) }; else delete Q.thump; }
  return Q;
}

class Mode {
  constructor(f, t60, w, fs) {
    const a = 6.907755 / (t60 * fs);       // per-sample decay of e^{-a n}
    const om = TAU * f / fs;
    const r = Math.exp(-a);
    this.c1 = 2 * r * Math.cos(om);
    this.c2 = -r * r;
    this.b = w * r * Math.sin(om);
    this.y1 = 0; this.y2 = 0; this.x1 = 0;
  }
  step(x, damp) {
    const y = this.c1 * this.y1 + this.c2 * this.y2 + this.b * this.x1;
    this.x1 = x; this.y2 = this.y1 * damp; this.y1 = y * damp;
    return y;
  }
}

// a half-sine pulse t s long (2 samples at least), its area 1 (reed_model.half_sine)
function halfSine(t, fs) {
  const n = Math.max(Math.round(t * fs), 2);
  const f = new Float32Array(n); let s = 0;
  for (let i = 0; i < n; i++) { f[i] = Math.sin(Math.PI * (i + 0.5) / n); s += f[i]; }
  for (let i = 0; i < n; i++) f[i] /= s;
  return f;
}

// reed_model.attack_w: w_lo at key 33 to w_mid at key 64 (log), faded linearly to 0 at key top
function attackW(P, midi) {
  if (!P.attack) return 0;
  const A = P.attack;
  if (midi <= 64) return interpLog(A.w_lo, A.w_mid, 33 + (midi - 33) * 63 / 31);
  return A.w_mid * Math.min(Math.max((A.top - midi) / (A.top - 64), 0), 1);
}

class Voice {
  constructor(midi, vel, P, fs, gainCache) {
    this.midi = midi; this.P = P; this.fs = fs;
    const f0 = f0Of(P, midi);
    const t60 = (P.t60_lo !== undefined ? interpLog(P.t60_lo, P.t60_hi, midi, P.t60_mid) : 6.0)
                * keyOf(P, midi, 't60', 1);
    this.modes = [new Mode(f0, t60, 1, fs)];
    if (f0 * P.r2 < fs * 0.45) this.modes.push(new Mode(f0 * P.r2, t60 / P.q2, P.w2, fs));
    // reed_model "early": a share of the first mode with its own short T60 (left out of peak50's normalisation)
    if (P.early) this.modes.push(new Mode(f0, P.early.t60, interpLog(P.early.w_lo, P.early.w_hi, midi), fs));
    // reed_model "attack": the strike's knock, a share of the first mode gone in a few ms (keys 33 to attack.top)
    const wa = attackW(P, midi);
    if (wa > 0) this.modes.push(new Mode(f0, P.attack.t60, wa, fs));
    this.pulse = Voice.hammer(vel, P, fs, midi);
    this.pi = 0;
    // reed_model.reed's BELL ping: the second mode once more, struck by its own short pulse
    if (P.bell_x > 0 && f0 * P.r2 < fs * 0.45) {
      this.bmode = new Mode(f0 * P.r2, t60 / P.q2, P.bell_x, fs);
      this.bpulse = halfSine(Math.min(2.5e-4, 0.5 / (f0 * P.r2)), fs); this.bpi = 0;
    }
    this.gamma = P.gamma_hi !== undefined ? interpLog(P.gamma, P.gamma_hi, midi) : P.gamma;
    this.t60 = t60; this.f0 = f0;
    // the level: the strike's own 50 ms peak set to (vel/127)^gamma times the key's full-strike gain
    const g = P.gamma_hi !== undefined ? interpLog(P.gamma, P.gamma_hi, midi) : P.gamma;
    const own = Voice.peak50(f0, t60, P, fs, Voice.refPulse(vel, P, fs, midi, this.pulse));
    if (!(midi in gainCache)) gainCache[midi] = Voice.hardGain(f0, P, fs, midi);
    // reed_model.g_key: the key's own output gain over the table's (REG leaves it alone)
    this.keyGain = keyOf(P, midi, 'g', 1) * (P.g_key && midi >= 33 && midi <= 96 ? P.g_key[midi - 33] : 1);
    this.scale = Math.pow(vel / 127, g) / Math.max(own, 1e-12) * gainCache[midi];
    this.swing = interpLog(P.u_lo, P.u_hi, midi, P.u_mid);
    // reed_model.swing_of: u_boost (the middle's swing raised, faded out by key off), u_key (a factor per key)
    if (P.u_boost) { const B = P.u_boost; this.swing *= Math.pow(B.x, Math.min(Math.max((B.off - midi) / (B.off - B.full), 0), 1)); }
    if (P.u_key && midi >= 33 && midi <= 96) this.swing *= P.u_key[midi - 33];
    this.d = (P.d_mid !== undefined ? interpLog(P.d, P.d_hi, midi, P.d_mid) : P.d) * keyOf(P, midi, 'd', 1);
    // the bell (1 + z^2)^-p, p = bell_p (reed_model.pickup); slope 1 at rest
    this.p = P.bell_p || 1;
    this.b0 = Math.pow(1 + this.d * this.d, -this.p);
    this.norm = Math.pow(1 + this.d * this.d, this.p + 1) / (2 * this.p * this.d);
    this.dampStep = 1; this.released = false; this.age = 0; this.quiet = 0;
  }
  // reed_model.tc_of / hammer: with tc_top the top two octaves' hammers stay on longer; hammer_x (HAMMER) times
  // the contact, but not the reference hammer's (ref), which sets the level
  static hammer(vel, P, fs, midi, ref = false) {
    let t = interpLog(P.tc_soft, P.tc_hard, 33 + 63 * (vel - 1) / 126);
    if (P.tc_top !== undefined && midi > 72) t *= Math.pow(P.tc_top, (Math.min(midi, 96) - 72) / 24);
    if (!ref) t *= P.hammer_x || 1;
    if (P.tc_per !== undefined) t = Math.min(t, P.tc_per / f0Of(P, midi));   // off within tc_per periods
    return halfSine(t, fs);
  }
  static refPulse(vel, P, fs, midi, pulse) {
    return P.hammer_x !== undefined && P.hammer_x !== 1 ? Voice.hammer(vel, P, fs, midi, true) : pulse;
  }
  static hardGain(f0, P, fs, midi) {
    // reed_model._hard_gain: 1 / the 50 ms peak of the undamped modes struck at vel 127. The modes run as
    // resonators with no decay (the same numbers as the direct sum of sines, a hundred times quicker)
    return 1 / Math.max(Voice.peak50(f0, 1e12, P, fs, Voice.hammer(127, P, fs, midi, true)), 1e-12);
  }
  static peak50(f0, t60, P, fs, pulse) {
    const ms = [new Mode(f0, t60, 1, fs)];
    if (f0 * P.r2 < fs * 0.45) ms.push(new Mode(f0 * P.r2, t60 / P.q2, P.w2, fs));
    let pk = 0;
    const n = Math.round(0.05 * fs);
    for (let i = 0; i < n; i++) {
      const x = i < pulse.length ? pulse[i] : 0;
      let y = 0; for (const m of ms) y += m.step(x, 1);
      pk = Math.max(pk, Math.abs(y));
    }
    return pk;
  }
  // the key struck again while its reed still sounds (reed_model.render_notes): the new strike's force joins the
  // same modes (scaled to this voice's units), the damper lifts off; the pickup reads the sum
  restrike(vel, gainCache) {
    const pulse = Voice.hammer(vel, this.P, this.fs, this.midi);
    const own = Voice.peak50(this.f0, this.t60, this.P, this.fs, Voice.refPulse(vel, this.P, this.fs, this.midi, pulse));
    const k = Math.pow(vel / 127, this.gamma) / Math.max(own, 1e-12) * gainCache[this.midi] / this.scale;
    const join = (old, at, add) => {
      const rest = Math.max(old.length - at, 0), n = Math.max(rest, add.length), f = new Float32Array(n);
      for (let i = 0; i < n; i++) f[i] = (i < rest ? old[at + i] : 0) + (i < add.length ? add[i] * k : 0);
      return f;
    };
    this.pulse = join(this.pulse, this.pi, pulse); this.pi = 0;
    if (this.bmode) {
      this.bpulse = join(this.bpulse, this.bpi, halfSine(Math.min(2.5e-4, 0.5 / (this.f0 * this.P.r2)), this.fs)); this.bpi = 0;
    }
    this.released = false; this.quiet = 0;
  }
  steal(fs) {
    this.fading = true; this.fade = 1; this.fadeStep = 1 / Math.round(0.005 * fs);
  }
  release() {
    if (this.released) return;
    this.released = true;
    const D = this.P.damp2;
    if (D) {
      // reed_model.damper: fast down to the knee (after kneeN samples), then the slow tail
      this.dampStep = Math.exp(-6.907755 / (D.fast * this.fs));
      this.slowStep = Math.exp(-6.907755 / (D.slow * this.fs));
      this.kneeN = Math.round(D.fast * D.knee_db / -60 * this.fs);
    } else {
      this.dampStep = Math.exp(-6.907755 / ((this.P.t60_rel || 0.12) * this.fs));
      this.kneeN = Infinity;
    }
    this.relN = 0;
  }
  next() {
    const x = this.pi < this.pulse.length ? this.pulse[this.pi++] : 0;
    let damp = 1;
    if (this.released) { damp = this.relN < this.kneeN ? this.dampStep : this.slowStep; this.relN++; }
    let disp = 0;
    for (const m of this.modes) disp += m.step(x, damp);
    if (this.bmode) disp += this.bmode.step(this.bpi < this.bpulse.length ? this.bpulse[this.bpi++] : 0, damp);
    const u = disp * this.scale * this.swing;
    const z = u + this.d;
    const bz = this.p === 1 ? 1 / (1 + z * z) : Math.pow(1 + z * z, -this.p);
    let v = (this.b0 - bz) * this.norm * this.keyGain;
    this.age++;
    if (Math.abs(u) < 1e-5 && this.pi >= this.pulse.length) this.quiet++; else this.quiet = 0;
    if (this.fading) { v *= this.fade; this.fade = Math.max(this.fade - this.fadeStep, 0); }
    return v;
  }
  done() { return this.quiet > 2048 || (this.fading && this.fade <= 0); }
}

// the key's return knock (reed_model.thump): LCG noise, two band-passes, a 5 ms rise and a T60 decay, 0.5 s long
class Knock {
  constructor(midi, P, fs) {
    const T = P.thump;
    this.s = Math.imul(midi, 2654435761 | 0) >>> 0;
    const w = TAU * T.hz / fs, al = Math.sin(w) / (2 * T.q), a0 = 1 + al;
    this.b0 = al / a0; this.a1 = -2 * Math.cos(w) / a0; this.a2 = (1 - al) / a0;
    this.f = [[0, 0, 0, 0], [0, 0, 0, 0]];          // x1 x2 y1 y2 of each band-pass
    this.gain = T.norm * keyOf(P, midi, 'ref', 0) * Math.pow(10, T.db / 20);
    this.dec = Math.exp(-6.907755 / (T.t60 * fs)); this.env = 1;
    this.rise = 0.005 * fs; this.i = 0; this.n = Math.round(0.5 * fs);
  }
  next() {
    this.s = (Math.imul(1664525, this.s) + 1013904223) >>> 0;
    let x = (this.s >>> 8) / 8388608 - 1;
    for (const f of this.f) {
      const y = this.b0 * x - this.b0 * f[1] - this.a1 * f[2] - this.a2 * f[3];
      f[1] = f[0]; f[0] = x; f[3] = f[2]; f[2] = y; x = y;
    }
    const e = Math.min(this.i / this.rise, 1) * this.env;
    this.env *= this.dec; this.i++;
    return x * e * this.gain;
  }
  done() { return this.i >= this.n; }
}

// the strike's action noise (reed_model.snoise): LCG noise (its own seed), two band-passes, a rise and a T60
// decay, 0.1 s long; its gain the key's "sn" times its share at this velocity. In the line (it is picked up)
class SNoise {
  constructor(midi, vel, P, fs) {
    const S = P.snoise;
    this.s = (Math.imul(midi, 2654435761 | 0) ^ 0x5BD1E995) >>> 0;
    const w = TAU * S.hz / fs, al = Math.sin(w) / (2 * S.q), a0 = 1 + al;
    this.b0 = al / a0; this.a1 = -2 * Math.cos(w) / a0; this.a2 = (1 - al) / a0;
    this.f = [[0, 0, 0, 0], [0, 0, 0, 0]];
    this.gain = keyOf(P, midi, 'sn', 0) * SNoise.share(vel, S);
    this.dec = Math.exp(-6.907755 / (S.t60 * fs)); this.env = 1;
    this.rise = S.rise * fs; this.i = 0; this.n = Math.round(0.1 * fs);
  }
  static share(vel, S) { return Math.min(Math.max((vel - S.vel0) / (S.vel1 - S.vel0), 0), 1); }
  next() {
    this.s = (Math.imul(1664525, this.s) + 1013904223) >>> 0;
    let x = (this.s >>> 8) / 8388608 - 1;
    for (const f of this.f) {
      const y = this.b0 * x - this.b0 * f[1] - this.a1 * f[2] - this.a2 * f[3];
      f[1] = f[0]; f[0] = x; f[3] = f[2]; f[2] = y; x = y;
    }
    const e = Math.min(this.i / this.rise, 1) * this.env;
    this.env *= this.dec; this.i++;
    return x * e * this.gain;
  }
  done() { return this.i >= this.n; }
}

// the 200A's vibrato (reed_model.trem_gain): in dB a narrow trough and a broad top, TREM_DB at full depth; h2 0
// is the plain sine of the custom-shop instruments (the Instagram reels of 2026-10-27)
const TREM_DB = 20;
// the loud phase is the darker one (reed_model.trem_stage): a one-pole low-pass mixed in by 1 - 1 / (gain over the
// cycle's trough), so no vibrato leaves the sound as it is
const TREM_LP_HZ = 1000;
function tremGain(ph, depth, h2) {
  const x = TAU * ph;
  const dip = (1 + Math.cos(x) + h2 * (Math.cos(2 * x) - 1)) / 2;
  return Math.pow(10, -depth * TREM_DB * dip / 20);
}

// the speaker as reed_model.speaker_biquads: RBJ high-pass, three peaks, low-pass, then the gain
function rbj(kind, f, q, db, fs) {
  const w = TAU * f / fs, c = Math.cos(w), al = Math.sin(w) / (2 * q), A = Math.pow(10, (db || 0) / 40);
  let b, a;
  if (kind === 'hp') { b = [(1 + c) / 2, -(1 + c), (1 + c) / 2]; a = [1 + al, -2 * c, 1 - al]; }
  else if (kind === 'lp') { b = [(1 - c) / 2, 1 - c, (1 - c) / 2]; a = [1 + al, -2 * c, 1 - al]; }
  else { b = [1 + al * A, -2 * c, 1 - al * A]; a = [1 + al / A, -2 * c, 1 - al / A]; }
  return { b0: b[0] / a[0], b1: b[1] / a[0], b2: b[2] / a[0], a1: a[1] / a[0], a2: a[2] / a[0], x1: 0, x2: 0, y1: 0, y2: 0 };
}
class Speaker {
  constructor(S, fs) {
    this.f = [rbj('hp', S.hp_hz, S.hp_q, 0, fs), rbj('pk', S.lo_hz, S.lo_q, S.lo_db, fs),
              rbj('pk', S.mid_hz, S.mid_q, S.mid_db, fs), rbj('pk', S.pr_hz, S.pr_q, S.pr_db, fs),
              rbj('lp', S.lp_hz, S.lp_q, 0, fs)];
    this.g = Math.pow(10, S.gain_db / 20);
  }
  step(x) {
    for (const q of this.f) {
      const y = q.b0 * x + q.b1 * q.x1 + q.b2 * q.x2 - q.a1 * q.y1 - q.a2 * q.y2;
      q.x2 = q.x1; q.x1 = x; q.y2 = q.y1; q.y1 = y; x = y;
    }
    return x * this.g;
  }
}

class ReedProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.voices = [];
    this.knocks = [];
    this.snoises = [];
    this.ldrS = 0;        // the LDR's lagged vibrato gain (dB), with a set's trem_ldr
    this.P = null; this.P0 = null; this.K = { ...KNOBS };
    this.gainCache = {};
    this.hp = { x1: 0, y1: 0 };
    this.vol = 0.5; this.tremDepth = 0; this.tremRate = 5.4; this.tremPh = 0; this.tremH2 = 0.22;
    this.tremZ = 0;
    // the output: 0 the line, 1 the speaker; spkMix glides to it in 20 ms (a switch mid-note does not click)
    this.speaker = null; this.out = 0; this.spkMix = 0;
    this.sustain = false; this.held = new Set();
    this.port.onmessage = (e) => this.onMsg(e.data);
  }
  onMsg(m) {
    if (m.type === 'params') {
      this.P0 = m.P; this.rebuild();
    } else if (m.type === 'knob') {
      // only BELL, HAMMER, TUNE and REG move the full-strike gains (w2, the contact time, f0: REG scales each key's cents)
      this.K[m.key] = m.value; if (this.P0) this.rebuild(['bell', 'hammer', 'tune', 'reg'].includes(m.key));
    } else if (m.type === 'on' && this.P) {
      const vel = Math.max(1, Math.min(127, m.vel));
      if (this.P.snoise && SNoise.share(vel, this.P.snoise) > 0) this.snoises.push(new SNoise(m.midi, vel, this.P, sampleRate));
      // the same reed still sounding (the pedal down, or the key struck again): strike it again, one voice per key
      const same = this.voices.find((v) => v.midi === m.midi && !v.fading && v.P === this.P);
      if (same) same.restrike(vel, this.gainCache);
      else {
        for (const v of this.voices) if (v.midi === m.midi) v.release();
        // past the polyphony the oldest voice fades out in 5 ms (cut at once it clicks)
        const live = this.voices.filter((v) => !v.fading);
        if (live.length >= MAXV) live[0].steal(sampleRate);
        this.voices.push(new Voice(m.midi, vel, this.P, sampleRate, this.gainCache));
      }
      this.held.add(m.midi);
    } else if (m.type === 'off') {
      // the key comes back up (with the pedal down too): its knock
      if (this.held.has(m.midi) && this.P && this.P.thump) this.knocks.push(new Knock(m.midi, this.P, sampleRate));
      this.held.delete(m.midi);
      if (!this.sustain) for (const v of this.voices) if (v.midi === m.midi) v.release();
    } else if (m.type === 'sustain') {
      this.sustain = m.on;
      if (!m.on) for (const v of this.voices) if (!this.held.has(v.midi)) v.release();
    } else if (m.type === 'speaker') { this.speaker = new Speaker(m.S, sampleRate);
    } else if (m.type === 'set') { this[m.key] = m.value; }
  }
  // the set with the knobs (sounding voices keep the P they started with), and every key's full-strike gain now, not
  // at its first note-on (44 ms there would drop audio blocks)
  rebuild(gains = true) {
    const P = applyKnobs(this.P0, this.K);
    if (gains) {
      const gc = {};
      for (let k = 33; k <= 96; k++) gc[k] = Voice.hardGain(f0Of(P, k), P, sampleRate, k);
      this.gainCache = gc;
    }
    this.P = P;
  }
  process(inputs, outputs) {
    const out = outputs[0];
    const L = out[0], R = out[1] || out[0];
    const P = this.P;
    if (!P) return true;
    const a = Math.exp(-TAU * P.fc / sampleRate);
    const k = (1 + a) / 2;
    const d = P.drive;
    const dt = this.tremRate / sampleRate;
    const gl = 1 / (0.02 * sampleRate);
    const og = P.out_gain || 1;         // the set's fixed output level (reed_model.render_notes)
    const la = Math.exp(-TAU * TREM_LP_HZ / sampleRate), gmin = Math.pow(10, -this.tremDepth * TREM_DB / 20);
    // a set's trem_ldr (reed_model.trem_stage): the LDR's lag on a drive compensated for the factory rate
    const L4 = P.trem_ldr;
    let kon = 0, koff = 0, lc = 1, lh2 = this.tremH2;
    if (L4) {
      kon = 1 - Math.exp(-1 / (L4.on * sampleRate)); koff = 1 - Math.exp(-1 / (L4.off * sampleRate));
      const c = L4.comp[String(this.tremH2)];
      if (c) { lc = c[0]; lh2 = c[1]; }
    }
    for (let i = 0; i < L.length; i++) {
      let s = 0;
      for (const v of this.voices) s += v.next();
      // the input high-pass, then the preamp
      const y = k * (s - this.hp.x1) + a * this.hp.y1;
      this.hp.x1 = s; this.hp.y1 = y;
      // the preamp, scaled so its ceiling (tanh -> 1) is full scale: no volume setting can clip the output (the
      // knocks, after it as reed_model.render_notes, in the same scale: x drive; held to full scale)
      this.tremPh += dt; if (this.tremPh >= 1) this.tremPh -= 1;
      let g;
      if (L4) {
        const x = TAU * this.tremPh;
        const tgt = -this.tremDepth * TREM_DB * lc * (1 + Math.cos(x) + lh2 * (Math.cos(2 * x) - 1)) / 2;
        this.ldrS += (tgt - this.ldrS) * (tgt > this.ldrS ? kon : koff);
        g = Math.pow(10, this.ldrS / 20);
      } else g = tremGain(this.tremPh, this.tremDepth, this.tremH2);
      let o = Math.tanh(d * y);
      // the action noises, picked up: after the preamp, before the vibrato (reed_model.render_notes)
      for (const q of this.snoises) o += d * q.next();
      this.tremZ = (1 - la) * o + la * this.tremZ;
      o = (o + (this.tremZ - o) * Math.max(1 - gmin / g, 0)) * g;
      if (this.speaker) {
        const sp = this.speaker.step(o);
        this.spkMix += Math.max(-gl, Math.min(gl, this.out - this.spkMix));
        o += (sp - o) * this.spkMix;
      }
      // the knocks are heard in the room, not picked up: through the speaker only
      let kn = 0;
      for (const q of this.knocks) kn += q.next();
      o = Math.max(-1, Math.min(1, (o + d * kn * this.spkMix) * og));
      L[i] = o * this.vol; R[i] = o * this.vol;
    }
    this.voices = this.voices.filter((v) => !v.done());
    this.knocks = this.knocks.filter((q) => !q.done());
    this.snoises = this.snoises.filter((q) => !q.done());
    return true;
  }
}

registerProcessor('reed', ReedProcessor);
