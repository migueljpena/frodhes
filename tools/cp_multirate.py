#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""The CP-80 engine's algorithm in float: what the integer engine (firmware/src/strings.c) will compute, checked
against the reference (tools/cp_model.py) before it is written in C.

The reference sums every partial of every string at 44.1 kHz: 50-100 resonators a key in the bass and the middle, 2-3x
what a Rhodes voice may cost on the FM-1. The engine computes the same partials three ways cheaper:

1. Each partial at the lowest rate its frequency allows: 44.1 kHz / 2^b, b = 0..4, the lowest whose band holds it
   (f < FRAC x the rate). The voices add each band's partials into one buffer for the band; the bands are brought
   up to 44.1 kHz together (one polyphase IIR half-band a x2 stage, for all the voices). Each partial starts
   divided by the interpolators' complex response at its frequency (their gain and their delay undone), so the
   bands line up with no latency added: only a partial's first ~1 ms is smoothed (the hammer's contact is 0.4-2 ms).
2. A pair of strings (keys 23-88) as one resonator at the pair's mean frequency, times cos(pi df t), the beating
   (linear over a block): the two strings' sum when their amplitudes are equal (the strike gives them within a few
   %; the exact sum, "quad", came out no closer to the reference).
3. The partials whose energy is more than PRUNE_DB under the key's strongest (through the brightest tone settings)
   left out.

Measured against the reference (tests/py/test_cp_multirate.py): -40 to -48 dB after a note's first 10 ms; the demos'
spectra within 0.3 dB an octave. The cost: 0.5 to 17 full-rate resonators a key (mean 9), against 3 to 90.

Each partial decays in the reference's two stages, (1 - w) e^(-se t) + w e^(-sl t): one resonator whose decay is set
each block to that envelope's slope, sl + (se - sl) rho / (1 + rho), rho = (1 - w) / w e^(-(se - sl) t).

  python3 tools/cp_multirate.py --compare DIR   notes through the reference and the algorithm: the difference,
                                                the cost per key, the two as WAVs
  python3 tools/cp_multirate.py --demo DIR      the reference's demos (keys, progression) through the algorithm
"""
import math
import os
import sys

import numpy as np
from scipy import signal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cp_model as m  # noqa: E402

FS = m.FS
BLOCK = 256
NB = 5                      # bands: 44.1 kHz / 1, 2, 4, 8, 16
FRAC = 0.4                  # a band holds f < FRAC x its rate (the interpolators' pass band)
PRUNE_DB = -50.0
FMAX = 21000.0
BEAT_LERP = True            # the beat's cos linear over a block (the integer engine's), not exact a sample
PAIR = "cos"                # a pair: "cos" one resonator x cos (the engine: as close as "quad" in a test, -40..-48 dB),
                            # "quad" x C(t) (exact), "two" a resonator a string
# the x2 interpolator: a polyphase IIR half-band (two all-pass branches, H = (A0(z^2) + z^-1 A1(z^2)) / 2, the
# coefficients fitted for the images (0.3-0.5 of its output rate) under -69 dB): an output pair a sample in, the even
# one A0's, the odd one A1's (x2, the zeros' halving undone). Its delay, 2-6 output samples, is undone with its gain
# in each partial's start (chain_resp), so only a partial's first ~ms is smoothed (the FIR half-band's 23 samples
# a stage smoothed a band-4 partial's first ~8 ms: the attack changed)
HB_A0 = (0.080913, 0.54886)
HB_A1 = (0.836244, 0.286759)


def allpass(c, x, zi=None):
    """the first-order all-pass sections (c + z^-1) / (1 + c z^-1) in cascade"""
    for k in c:
        x = signal.lfilter([k, 1.0], [1.0, k], x)
    return x


def hb_resp(w):
    """a stage's response at w (radians a sample of its output rate)"""
    z2 = np.exp(-2j * w)
    a0 = np.prod([(k + z2) / (1 + k * z2) for k in HB_A0], axis=0)
    a1 = np.prod([(k + z2) / (1 + k * z2) for k in HB_A1], axis=0)
    return 0.5 * (a0 + np.exp(-1j * w) * a1)


def chain_resp(f, b):
    """the interpolators' complex response at f Hz for a partial of band b (every stage from 2^b down to 1)"""
    h = np.ones(len(np.atleast_1d(f)), complex)
    for k in range(b, 0, -1):
        h *= hb_resp(2 * math.pi * np.atleast_1d(f) / (FS / 2 ** (k - 1)))
    return h


def band_of(f):
    for b in range(NB - 1, -1, -1):
        if f < FRAC * FS / 2 ** b:
            return b
    return 0


_PRE = None


def tone_weight(f):
    """the brightest settings' response (BASS, TREBLE max, BRILLIANCE HIGH): what decides a partial's audibility"""
    global _PRE
    if _PRE is None:
        _PRE = m.Preamp()
    return 10 ** (_PRE.resp_db(f, *_PRE.gains(bass=10, treble=10, bril="HIGH")) / 20)


_KEYS = {}


def key_data(k, p=m.P):
    """the engine's partials of key k: for each, the frequency, the beat (Hz; 0 one string), the two stages' losses
    and the knee's weight, the band, and the pickup's weight (T n pi / L (-1)^n x the voicing); pruned at the
    reference's v127 strike (the brightest: the most partials)"""
    if k in _KEYS:
        return _KEYS[k]
    s = m.key_params(k, p)
    n, f = m.partials(s)
    st, _, _, _, _ = m.strike(s, 127, p)
    ws = s["T"] * n * math.pi / s["L"] * (-1.0) ** n
    a = np.abs(st[0]["A"]) * np.abs(ws) * tone_weight(f)
    e = a / np.sqrt(m.sigma(f, p, "t60_late"))
    keep = (20 * np.log10(e / e.max()) > PRUNE_DB) & (f < FMAX)
    if s["n_str"] == 2 and PAIR == "two":
        fs_ = np.array([x["f"] for x in st])
        n, f, ws, keep = np.tile(n, 2), fs_.ravel(), np.tile(ws, 2), np.tile(keep, 2)
        fc, beat = f.copy(), np.zeros_like(f)
    elif s["n_str"] == 2:
        fs_ = np.array([x["f"] for x in st])                       # each string's partials
        fc, beat = fs_.mean(0), fs_[1] - fs_[0]
    else:
        fc, beat = f.copy(), np.zeros_like(f)
    se, sl = m.sigma(fc, p), m.sigma(fc, p, "t60_late")
    w = np.exp(-(se - sl) * p["decay"]["knee_s"])
    g = ws * 10 ** (m.voicing_db(k, p) / 20)
    if k >= p["pickup"]["top_keys_first"]:
        g = g * 10 ** (p["pickup"]["top_keys_db"] / 20)
    d = dict(idx=np.nonzero(keep)[0] % len(m.partials(s)[0]), idx2=np.nonzero(keep)[0], f=fc[keep], beat=beat[keep], se=se[keep], sl=sl[keep], w=w[keep], g=g[keep],
             band=np.array([band_of(x) for x in fc[keep]], int), n_str=s["n_str"], damped=s["damped"])
    _KEYS[k] = d
    return d


def amps(k, vel, p=m.P):
    """the strike's complex amplitude of each kept partial (the reference's A; a pair: the mean of its strings', so
    the pair is 2 a cos(pi df t + phi) e^(i w t)); and the beat's phase phi"""
    d = key_data(k, p)
    s = m.key_params(k, p)
    st, _, _, _, _ = m.strike(s, vel, p)
    if d["n_str"] == 2 and PAIR == "two":
        A = np.array([x["A"] for x in st]).ravel()[d["idx2"]]
        return A, np.zeros(A.size)
    A = np.array([x["A"] for x in st])[:, d["idx"]]
    if d["n_str"] == 2:
        c = (A[0] - A[1]) / (A[0] + A[1]) if PAIR == "quad" else np.zeros(A.shape[1])
        return A[0] + A[1], c                                     # 2 a, and d / a: C(t) = cos - i (d / a) sin
    return A[0], np.zeros(len(d["idx"]))


class Voice:
    def __init__(self, k, vel, t_on, dur, p=m.P):
        self.d = key_data(k, p)
        A, self.c = amps(k, vel, p)
        self.p, self.k = p, k
        self.n0 = int(round(t_on * FS))
        self.off = self.n0 + int(round(dur * FS))
        d = self.d
        # each partial's phasor at its band's rate, its phase advanced by the band's delay (so it lines up)
        self.T = 2.0 ** d["band"] / FS
        H = np.array([chain_resp(fi, bi)[0] for fi, bi in zip(d["f"], d["band"])])
        dly = -np.angle(H) / (2 * math.pi * d["f"])                # (the beat's phase: its delay at f)
        wd = 2 * math.pi * d["f"]
        self.wd = wd
        self.z = A / H                                             # the interpolators' gain and delay undone (the
                                                                    # decay over their delay, < 0.1 %, left out)
        self.rho0 = (1 - d["w"]) / d["w"]                          # rho at t = 0 (< 0 where the late stage is the
                                                                    # faster: the lowest partials)
        self.damp = 6.91 / p["decay"]["damper_t60_s"] if d["damped"] else 0.0
        self.dly = dly

    def block(self, b0, bufs):
        """the block starting at 44.1 kHz sample b0: each band's samples added into bufs[b]"""
        d = self.d
        t_mid = (b0 + BLOCK / 2 - self.n0) / FS
        ds = d["se"] - d["sl"]
        rho = self.rho0 * np.exp(-ds * t_mid)
        sig = d["sl"] + ds * rho / (1 + rho)
        if self.damp and b0 + BLOCK / 2 >= self.off:
            sig = sig + self.damp
        for b in range(NB):
            sel = d["band"] == b
            if not sel.any():
                continue
            nb = BLOCK >> b
            T = 2.0 ** b / FS
            step = np.exp((-sig[sel] + 1j * self.wd[sel]) * T)
            pw = step[:, None] ** np.arange(nb)[None, :]
            zz = self.z[sel][:, None] * pw
            tt = (b0 - self.n0) / FS + np.arange(nb) * T + self.dly[sel][:, None]
            ph = math.pi * d["beat"][sel][:, None] * tt
            if BEAT_LERP:                                          # the integer engine's: cos at the block's two
                c0, c1 = np.cos(ph[:, :1]), np.cos(ph[:, :1] + math.pi * d["beat"][sel][:, None] * BLOCK / FS)
                C = c0 + (c1 - c0) * np.arange(nb)[None, :] / nb   # ends, linear between them
            else:
                C = np.cos(ph) - 1j * self.c[sel][:, None] * np.sin(ph)    # (1 where no beat: one string)
            bufs[b] += np.sum(d["g"][sel][:, None] * (C * zz).imag, axis=0)
            self.z[sel] = zz[:, -1] * step
        return True


def upsample_chain(bands):
    """the bands (lists of arrays at 44.1 kHz / 2^b) to one 44.1 kHz signal: from the lowest, x2 (zeros, the half-band),
    plus the next band, ... (scipy's lfilter: the integer engine keeps each stage's state across blocks the same way)"""
    y = bands[NB - 1]
    for b in range(NB - 1, 0, -1):
        u = np.empty(2 * len(y))
        u[0::2], u[1::2] = allpass(HB_A0, y), allpass(HB_A1, y)
        y = u + bands[b - 1]
    return y


def render(events, length, p=m.P):
    """events (start s, key, vel, dur s) -> the bus (the reference's units)"""
    n = int(math.ceil(length * FS / BLOCK)) * BLOCK
    bands = [np.zeros(n >> b) for b in range(NB)]
    vs = []
    for t0, k, vel, dur in events:
        vs.append(Voice(k, vel, t0, dur, p))
    for b0 in range(0, n, BLOCK):
        bufs = [np.zeros(BLOCK >> b) for b in range(NB)]
        for v in vs:
            if b0 >= v.n0 and not (v.damp and b0 > v.off + FS):    # (a damped note is gone 1 s after)
                v.block(b0, bufs)
        for b in range(NB):
            bands[b][b0 >> b:(b0 + BLOCK) >> b] += bufs[b]
    return upsample_chain(bands)[:int(length * FS)]


def cost(k):
    """full-rate resonators' worth of key k: each partial 2^-b"""
    d = key_data(k)
    return float(np.sum(2.0 ** -d["band"]))


def compare_note(k, vel, dur=2.0, tail=1.0):
    """one note through the reference and the engine's algorithm: (ref, eng) at the bus"""
    ref = m.note(k, vel, dur, tail)
    eng = render([(0.0, k, vel, dur)], dur + tail)
    return ref, eng


def demo(dirpath, p=m.P):
    """the reference's demos (cp_model.render: the keys, the progression) through the engine's algorithm, the notes
    starting on the block they fall in (as the firmware's do)"""
    os.makedirs(dirpath, exist_ok=True)
    g, pre = m.ref_gain(p), m.Preamp(p)
    ev, t = [], 0.0
    for k in [1, 13, 25, 40, 49, 64, 73, 88]:
        for vel in (40, 80, 127):
            ev.append((t, k, vel, 2.0))
            t += 3.0
    m.write(os.path.join(dirpath, "eng_keys_v40_v80_v127.wav"), pre.run(render(ev, t + 0.5, p) * g))
    prog = [(0.0, [36, 48, 52, 55, 59]), (2.0, [41, 53, 57, 60, 64]), (4.0, [43, 55, 59, 62, 65]),
            (6.0, [36, 52, 55, 60, 64])]
    ev = [(t0 + 0.012 * i, m.midi_key(n), 92 if i else 100, 1.9) for t0, ch in prog for i, n in enumerate(ch)]
    ev += [(t0, m.midi_key(n), 96, 0.45) for t0, n in [(8.0, 72), (8.5, 74), (9.0, 76), (9.5, 79), (10.0, 77),
                                                         (10.5, 76), (11.0, 74), (11.5, 72)]]
    ev += [(8.0, m.midi_key(48), 85, 3.8), (8.0, m.midi_key(55), 80, 3.8), (8.0, m.midi_key(64), 80, 3.8)]
    ev = [(math.floor(t0 * FS / BLOCK) * BLOCK / FS, k, v, d) for t0, k, v, d in ev]
    m.write(os.path.join(dirpath, "eng_progression.wav"), pre.run(render(ev, 14.0, p) * g))


if __name__ == "__main__":
    if "--demo" in sys.argv:
        demo(sys.argv[sys.argv.index("--demo") + 1])
    elif "--compare" in sys.argv:
        out = sys.argv[sys.argv.index("--compare") + 1]
        os.makedirs(out, exist_ok=True)
        g = m.ref_gain()
        pre = m.Preamp()
        R, E = [], []
        print("key vel  error dB (the difference's energy re the note's, through the preamp)  partials  cost")
        for k in (1, 13, 25, 31, 40, 49, 64, 73, 88):
            for vel in (40, 80, 127):
                ref, eng = compare_note(k, vel)
                r, e = pre.run(ref * g), pre.run(eng * g)
                err = 10 * np.log10(np.sum((e - r) ** 2) / np.sum(r ** 2))
                print(f"{k:3d} {vel:3d}  {err:6.1f}  {len(key_data(k)['f']):3d}  {cost(k):5.1f}", flush=True)
                R.append(r); E.append(e)
        m.write(os.path.join(out, "ref_keys.wav"), np.concatenate(R))
        m.write(os.path.join(out, "eng_keys.wav"), np.concatenate(E))
