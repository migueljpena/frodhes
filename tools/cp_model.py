#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""The float model of the Yamaha CP-80 (the baseline before any recording): what docs/model/data/cp80.json's
documents and physics give, in double precision. The reference a later integer engine (strings.c) is checked against.

Each key has one wound string (keys 1-22) or two (23-88: wound to 42, then plain music wire of the parts list's
gauges). A string is a sum of its modes (partials), f_n = n f0 sqrt(1 + B n^2) (stiffness, B the inharmonicity), each
decaying in two stages (fast, then slow: measured on single CP-70 notes). The hammer (rubber under leather: a hard felt law F = K c^p on the compression c)
strikes the string near its end; their contact is simulated, so the force pulse (its length, its spectrum) comes
from the hammer's mass and speed against the string's impedance: harder blows are shorter and brighter by themselves.
The pickup is a piezo under the bridge: it reads the string's force there, T dy/dx = T sum q_n n pi / L (the
partials weighted by n: brighter than the string's motion). Every key's piezo is on one bus (no trim).

After the mix, the preamp (linear): the input high-pass, BASS / MIDDLE / TREBLE, BRILLIANCE (shelves whose gains are
solved so the whole chain gives the service manual's calibration levels), the FET3 Sallen-Key low-pass (from its
parts: ~4.0 kHz, Q 1.08; the CP-70's ~2.75 kHz, Q 0.74), then the tremolo: two LDR shunts in antiphase, ch1 / ch2.

  python3 tools/cp_model.py                 the report: per-key parameters, the hammer contact, the calibration check
  python3 tools/cp_model.py --render DIR    the demo WAVs (keys x velocities, a chord progression, the tremolo,
                                            BRILLIANCE, the CP-70's filter) into DIR
"""
import json
import math
import os
import sys

import numpy as np
from scipy import signal
from scipy.io import wavfile
from scipy.optimize import brentq

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "docs", "model", "data", "cp80.json")
FS = 44100


def load(path=DATA):
    with open(path) as f:
        return json.load(f)


P = load()


def v(x):
    return x["v"] if isinstance(x, dict) else x


# ---- the keys ------------------------------------------------------------------------------------------------------

def key_f1(k, p=P):
    """the tuned pitch of key k (1..88): equal temperament on A49 = 440 Hz plus the tuning table's stretch"""
    return 440.0 * 2.0 ** ((k - 49) / 12.0 + p["tuning_cents"]["v"][k - 1] / 1200.0)


def gauge(k, p=P):
    g = None
    for key, n in sorted(((int(a), b) for a, b in p["strings"]["gauge_from_key"].items())):
        if k >= key:
            g = n
    return g


def wire_mm(n, p=P):
    gm = p["strings"]["gauge_mm"]
    return gm["n13"] + (n - 13) * 2 * gm["per_half"]


def b_from_stretch(p=P):
    """B of keys 1..76 from the octave stretch s[k+12] - s[k] (cents): 2:1 gives 2597 B, 4:2 10388 B; their
    geometric mean, smoothed over the keys"""
    s = p["tuning_cents"]["v"]
    d = np.array([s[k + 11] - s[k - 1] for k in range(1, 77)], float)
    b = np.maximum(d, 0.5) / math.sqrt(2597.0 * 10388.0)
    w = p["strings"]["b_wound_from_stretch"]["smooth_keys"]
    bs = np.convolve(np.pad(b, w // 2, mode="edge"), np.ones(w) / w, mode="valid")
    return bs                                                    # index k - 1


_BST = b_from_stretch()


def plain(k, f0, p=P):
    st = p["strings"]["steel"]
    d = wire_mm(gauge(k, p), p) * 1e-3
    a = math.pi * d * d / 4
    sig = v(p["strings"]["stress_frac"]) * st["uts_pa"]
    t = sig * a
    length = math.sqrt(sig / st["rho"]) / (2 * f0)
    b = math.pi ** 3 * st["E_pa"] * d ** 4 / (64 * t * length * length)
    return dict(d=d, T=t, L=length, mu=st["rho"] * a, B=b)


def key_params(k, p=P):
    """every physical value of key k (1..88)"""
    sp = p["strings"]
    f1 = key_f1(k, p)
    if k > sp["wound_last"]:
        f0 = f1
        for _ in range(4):                                       # the sounding pitch is the first partial's
            s = plain(k, f0, p)
            f0 = f1 / math.sqrt(1 + s["B"])
        s["kind"] = "plain"
    else:
        l43 = plain(sp["wound_last"] + 1, key_f1(sp["wound_last"] + 1, p), p)["L"]
        la = sp["wound"]["len_a0_m"]
        length = la * (l43 / la) ** ((k - 1) / (sp["wound_last"]))
        b = float(_BST[k - 1])
        f0 = f1 / math.sqrt(1 + b)
        t = sp["wound"]["tension_n"]
        s = dict(d=None, T=t, L=length, mu=t / (2 * length * f0) ** 2, B=b, kind="wound")
    s["f0"], s["f1"], s["k"] = f0, f1, k
    s["n_str"] = 1 if k <= sp["single_wound_last"] else 2
    h = p["hammer"]
    x = (k - 1) / 87.0
    s["m_h"] = (h["mass_g_low"] * (h["mass_g_high"] / h["mass_g_low"]) ** x) * 1e-3
    s["K"] = h["k_mid"] * math.exp((h["k_slope_per_key"] if k <= 44 else h["k_slope_treble"]) * (k - 44))
    s["beta"] = h["strike_frac_low"] + (h["strike_frac_high"] - h["strike_frac_low"]) * x
    s["t60"] = float(6.91 / sigma(f1, p))
    s["damped"] = k < v(sp["undamped_first"])
    return s


def sigma(f, p=P, stage="t60_early"):
    """a partial's loss at f Hz (1/s), its early or late stage: log-log between the measured points, the ends held"""
    dc = p["decay"]
    lf = np.log(np.clip(f, dc["f_hz"][0], dc["f_hz"][-1]))
    return 6.91 / np.exp(np.interp(lf, np.log(dc["f_hz"]), np.log(dc[stage])))


def decay_env(f, t, p=P):
    """the two-stage envelope of partials at f (array) over t: (1 - w) e^(-sigma_e t) + w e^(-sigma_l t)"""
    se, sl = sigma(f, p), sigma(f, p, "t60_late")
    w = np.exp(-(se - sl) * p["decay"]["knee_s"])
    return (1 - w)[:, None] * np.exp(-np.outer(se, t)) + w[:, None] * np.exp(-np.outer(sl, t))


def partials(s, fmax=18000.0, nmax=90):
    n = np.arange(1, nmax + 1, dtype=float)
    f = n * s["f0"] * np.sqrt(1 + s["B"] * n * n)
    keep = f < fmax
    return n[keep], f[keep]


def detunes(k, p=P):
    """the two strings' offsets in cents (seeded per key: a pair is never exactly in tune)"""
    dt = p["strings"]["detune_cents"]
    r = np.random.default_rng(1000 + k)
    c = dt["v"] + dt["spread"] * (r.random() - 0.5) * 2
    return (-c / 2, c / 2)


# ---- the hammer --------------------------------------------------------------------------------------------------

def strike(s, vel, p=P, os_=8):
    """the hammer against the key's strings. The contact on an ideal string simulated point by point (finite
    differences at Courant 1: no truncation, the string's impedance and the reflections from both ends as they are;
    the pair moves together, its mass doubled; stiffness, a small change over a few ms, left out), then each string's
    modes driven by that force. Returns, per string, its modes (f, sigma, the damped angular frequency wd) with A, the
    complex amplitude: q_n(t) = Im(A_n e^((-sigma + i wd) t)) after the contact; the contact length (s), the hammer's
    speed (m/s)"""
    h = p["hammer"]
    vh = h["vel_ms_min"] * (h["vel_ms_max"] / h["vel_ms_min"]) ** ((vel - 1) / 126.0)
    mu = s["mu"] * s["n_str"]
    c = math.sqrt(s["T"] / s["mu"])
    ncell = max(int(round(s["L"] / (c / (FS * os_)))), 40)
    dx = s["L"] / ncell
    dt = dx / c
    j = max(int(round(s["beta"] * ncell)), 1)
    y, yp = np.zeros(ncell + 1), np.zeros(ncell + 1)
    xh, vv = 0.0, vh
    kk, pp = s["K"], h["p"]
    force, free = [], 0
    for _ in range(int(0.03 / dt)):
        yn = np.empty_like(y)
        yn[1:-1] = y[2:] + y[:-2] - yp[1:-1]
        yn[0] = yn[-1] = 0.0
        # the force solved with the step it causes (implicit: stable however hard the hammer): the compression
        # c = c0 - beta F, the string's point moving by alpha F, the hammer by dt^2 F / m
        alpha = dt * dt / (mu * dx)
        beta = alpha + dt * dt / s["m_h"]
        c0 = xh + dt * vv - yn[j]
        fh = 0.0
        if c0 > 0:
            lo, hi = 0.0, c0 / beta
            fh = min(kk * c0 ** pp, hi)
            for _ in range(40):                                  # Newton, kept in [lo, hi] (g is increasing)
                c = max(c0 - beta * fh, 0.0)
                g = fh - kk * c ** pp
                if abs(g) < 1e-9 * (1 + fh):
                    break
                if g > 0:
                    hi = fh
                else:
                    lo = fh
                nf = fh - g / (1 + kk * pp * beta * c ** (pp - 1))
                fh = nf if lo < nf < hi else 0.5 * (lo + hi)
        yn[j] += alpha * fh
        yp, y = y, yn
        vv -= dt * fh / s["m_h"]
        xh += dt * vv
        force.append(fh)
        free = free + 1 if fh == 0.0 and vv < 0 and len(force) > 2 else 0
        if free * dt > 0.003 and xh < y[j] - 1e-4:                # away for 3 ms (a second contact can come
            break                                                 # with the reflection: it is kept)
    while force and force[-1] == 0.0:
        force.pop()
    force = np.array(force)
    tau = np.arange(len(force)) * dt
    n, f = partials(s)
    phi = np.sin(n * math.pi * j / ncell)
    M = s["mu"] * s["L"] / 2
    strings = []
    for cents in (detunes(s["k"], p) if s["n_str"] == 2 else (0.0,)):
        fc = f * 2 ** (cents / 1200)
        w = 2 * math.pi * fc
        sig = sigma(fc, p)
        wd = np.sqrt(np.maximum(w * w - sig * sig, 1.0))
        z = (np.exp(np.outer(sig - 1j * wd, tau)) @ (force / s["n_str"])) * dt
        strings.append(dict(f=fc, sig=sig, wd=wd, A=phi * z / (M * wd)))
    nz = np.nonzero(force)[0]
    contact = (nz[-1] - nz[0] + 1) * dt if len(nz) else 0.0
    return strings, contact, vh, force, dt


_VOICING = {}


def voicing_db(k, p=P):
    """the gain (dB) that brings key k to the level of the keys' mean at the voicing's ref_vel (pickup.voicing
    "flat"; smoothed over the keys so no key is fitted on its own)"""
    vo = p["pickup"]["voicing"]
    if v(vo) != "flat":
        return 0.0
    key = id(p)
    if key not in _VOICING:
        ks = np.arange(1, 89)
        lv = np.array([20 * math.log10(np.sqrt(np.mean(note(int(q), vo["ref_vel"], 0.3, 0.0, p, voiced=False) ** 2)))
                       for q in ks])
        w = vo["smooth_keys"]
        sm = np.convolve(np.pad(lv, w // 2, mode="edge"), np.ones(w) / w, mode="valid")
        _VOICING[key] = np.mean(sm) - sm
    return float(_VOICING[key][k - 1])


def note(k, vel, dur, tail=1.5, p=P, voiced=True):
    """the bridge force of key k (1..88) struck at vel (1..127), released after dur s (the damper; undamped keys ring
    on), rendered for dur + tail s. Newtons, at FS"""
    s = key_params(k, p)
    strings, _, _, _, _ = strike(s, vel, p)
    n, _ = partials(s)
    nsamp = int((dur + tail) * FS)
    t = np.arange(nsamp) / FS
    out = np.zeros(nsamp)
    w_slope = s["T"] * n * math.pi / s["L"] * (-1.0) ** n
    damp = 6.91 / p["decay"]["damper_t60_s"] if s["damped"] else 0.0
    dmp = np.exp(-damp * np.maximum(t - dur, 0.0)) if damp else 1.0
    for st in strings:
        env = decay_env(st["f"], t, p)
        for i in range(len(st["wd"])):
            a = st["A"][i]
            out += w_slope[i] * env[i] * dmp * (a.real * np.sin(st["wd"][i] * t) + a.imag * np.cos(st["wd"][i] * t))
    if k >= p["pickup"]["top_keys_first"]:
        out *= 10 ** (p["pickup"]["top_keys_db"] / 20)
    return out * 10 ** (voicing_db(k, p) / 20) if voiced else out


# ---- the preamp ----------------------------------------------------------------------------------------------------

def rbj(kind, f, g_db, q=0.7071, fs=FS):
    a = 10 ** (g_db / 40)
    w = 2 * math.pi * f / fs
    cw, sw = math.cos(w), math.sin(w)
    al = sw / (2 * q)
    if kind == "peak":
        b = [1 + al * a, -2 * cw, 1 - al * a]
        aa = [1 + al / a, -2 * cw, 1 - al / a]
    else:
        sq = 2 * math.sqrt(a) * al
        if kind == "lshelf":
            b = [a * ((a + 1) - (a - 1) * cw + sq), 2 * a * ((a - 1) - (a + 1) * cw), a * ((a + 1) - (a - 1) * cw - sq)]
            aa = [(a + 1) + (a - 1) * cw + sq, -2 * ((a - 1) + (a + 1) * cw), (a + 1) + (a - 1) * cw - sq]
        else:
            b = [a * ((a + 1) + (a - 1) * cw + sq), -2 * a * ((a - 1) + (a + 1) * cw), a * ((a + 1) + (a - 1) * cw - sq)]
            aa = [(a + 1) - (a - 1) * cw + sq, 2 * ((a - 1) - (a + 1) * cw), (a + 1) - (a - 1) * cw - sq]
    return np.array(b) / aa[0], np.array(aa) / aa[0]


def sk_analog(sk):
    """the unity-gain Sallen-Key low-pass: H(s) = 1 / (s^2 R1 R2 C1 C2 + s C2 (R1 + R2) + 1); (f0, Q)"""
    r1, r2, c1, c2 = sk["r1"], sk["r2"], sk["c1"], sk["c2"]
    f0 = 1 / (2 * math.pi * math.sqrt(r1 * r2 * c1 * c2))
    q = math.sqrt(r1 * r2 * c1 * c2) / (c2 * (r1 + r2))
    return f0, q


def _bilin_prewarp(b, a, f0, fs):
    """the bilinear transform prewarped at f0 (the low-pass's corner and Q land where the parts put them)"""
    k = 2 * math.pi * f0 / math.tan(math.pi * f0 / fs)
    return signal.bilinear(b, a, fs=k / 2)


KNOB_MID = 5.0


def knob_db(knob, lo, mid, hi):
    """a panel knob 0..10 (centre 5) to a gain: piecewise linear in dB through the three calibrated points"""
    x = min(max(knob, 0.0), 10.0)
    return lo + (mid - lo) * x / 5 if x <= 5 else mid + (hi - mid) * (x - 5) / 5


class Preamp:
    """the CP-80's TR board, linear. solve() sets the shelves' gains at the knobs' min / centre / max from the
    calibration levels (the whole chain's response relative to 500 Hz)"""

    def __init__(self, p=P, model="cp80"):
        self.p, self.model = p, model
        pa = p["preamp"]
        self.sk = pa["sk_cp70"] if model == "cp70" else pa["sk"]
        self.fb, self.ft = v(pa["bass_shelf_hz"]), v(pa["treble_shelf_hz"])
        self.fr, self.mid = v(pa["bril_shelf_hz"]), pa["middle"]
        self.g = {}
        self.solve()

    def stages(self, gb, gm, gt, gr):
        pa = self.p["preamp"]
        st = [signal.butter(1, pa["input_hp_hz"], "highpass", fs=FS),
              rbj("lshelf", self.fb, gb), rbj("peak", self.mid["hz"], gm, self.mid["q"]),
              rbj("hshelf", self.ft, gt), rbj("hshelf", self.fr, gr), _bilin_prewarp(
                  [1.0], [self.sk["r1"] * self.sk["r2"] * self.sk["c1"] * self.sk["c2"],
                          self.sk["c2"] * (self.sk["r1"] + self.sk["r2"]), 1.0], sk_analog(self.sk)[0], FS)]
        return st

    def resp_db(self, f, gb, gm, gt, gr):
        h = np.ones(len(np.atleast_1d(f)), complex)
        for b, a in self.stages(gb, gm, gt, gr):
            h *= signal.freqz(b, a, worN=np.atleast_1d(f), fs=FS)[1]
        return 20 * np.log10(np.abs(h))

    def rel(self, f, gb, gm, gt, gr):
        r = self.resp_db([f, 500.0], gb, gm, gt, gr)
        return r[0] - r[1]

    def solve(self):
        cal = self.p["preamp"]["cal_cp70" if self.model == "cp70" else "cal"]
        if self.model == "cp70":                                 # min / max its own; the centres the CP-80's
            c80 = Preamp(self.p).g
            cal = {"bass_50": dict(cal["bass_50"], mid=0.0), "treble_5k": dict(cal["treble_5k"], mid=0.0),
                   "bril_5k": {"HIGH": 0.0, "LOW": 0.0}}
        gb = {"max": 12.0, "min": -3.0, "mid": 0.0}
        gt = {"max": 8.0, "min": -3.0, "mid": 2.0}
        gr = {"HIGH": 5.0, "MEDIUM": 0.0, "LOW": -10.0}
        for _ in range(6):                                       # the stages overlap a little: a few passes
            for s in ("max", "min"):                             # bass at 50 Hz; treble at max (others max)
                gb[s] = brentq(lambda g: self.rel(50, g, 0, gt["max"], 0) - cal["bass_50"][s], -30, 30)
            gb["mid"] = brentq(lambda g: self.rel(50, g, 0, gt["mid"], 0) - cal["bass_50"]["mid"], -30, 30)
            for s in ("max", "min"):
                gt[s] = brentq(lambda g: self.rel(5000, gb["max"], 0, g, 0) - cal["treble_5k"][s], -30, 30)
            gt["mid"] = brentq(lambda g: self.rel(5000, gb["mid"], 0, g, 0) - cal["treble_5k"]["mid"], -30, 30)
            base = self.rel(5000, gb["mid"], 0, gt["mid"], 0)
            for s in ("HIGH", "LOW"):
                gr[s] = brentq(lambda g: self.rel(5000, gb["mid"], 0, gt["mid"], g) - base - cal["bril_5k"][s],
                               -40, 30)
        if self.model == "cp70":
            gb["mid"], gt["mid"], gr = c80["bass"]["mid"], c80["treble"]["mid"], {"HIGH": 0.0, "MEDIUM": 0.0, "LOW": 0.0}
        self.g = dict(bass=gb, treble=gt, bril=gr)

    def gains(self, bass=5, middle=5, treble=5, bril="MEDIUM"):
        g = self.g
        gb = knob_db(bass, g["bass"]["min"], g["bass"]["mid"], g["bass"]["max"])
        gt = knob_db(treble, g["treble"]["min"], g["treble"]["mid"], g["treble"]["max"])
        r = self.mid["range_db"]
        gm = knob_db(middle, -r, 0.0, r)
        gr = 0.0 if self.model == "cp70" else g["bril"][bril]
        return gb, gm, gt, gr

    def run(self, x, **knobs):
        y = x
        for b, a in self.stages(*self.gains(**knobs)):
            y = signal.lfilter(b, a, y)
        return y

    def cal_check(self):
        """the service manual's calibration levels against the model (dB re 500 Hz): rows (what, target, model)"""
        cal, g = self.p["preamp"]["cal_cp70" if self.model == "cp70" else "cal"], self.g
        if self.model == "cp70":
            return [(f"{w}, {c} {s}", cal[key][s], self.rel(fq, g["bass"][s if c == "BASS" else "max"], 0,
                                                             g["treble"][s if c == "TREBLE" else "max"], 0))
                    for key, fq, w, c in (("bass_50", 50, "50 Hz", "BASS"), ("treble_5k", 5000, "5 kHz", "TREBLE"))
                    for s in ("max", "min")]
        rows = [("50 Hz, BASS max", cal["bass_50"]["max"], self.rel(50, g["bass"]["max"], 0, g["treble"]["max"], 0)),
                ("50 Hz, BASS min", cal["bass_50"]["min"], self.rel(50, g["bass"]["min"], 0, g["treble"]["max"], 0)),
                ("5 kHz, TREBLE max", cal["treble_5k"]["max"],
                 self.rel(5000, g["bass"]["max"], 0, g["treble"]["max"], 0)),
                ("5 kHz, TREBLE min", cal["treble_5k"]["min"],
                 self.rel(5000, g["bass"]["max"], 0, g["treble"]["min"], 0)),
                ("5 kHz, all at 5", cal["treble_5k"]["mid"], self.rel(5000, g["bass"]["mid"], 0, g["treble"]["mid"], 0))]
        base = self.rel(5000, g["bass"]["mid"], 0, g["treble"]["mid"], 0)
        for s in ("HIGH", "LOW"):
            rows.append((f"5 kHz, BRILLIANCE {s} re MEDIUM", cal["bril_5k"][s],
                         self.rel(5000, g["bass"]["mid"], 0, g["treble"]["mid"], g["bril"][s]) - base))
        return rows


# ---- the tremolo ---------------------------------------------------------------------------------------------------

def tremolo(x, hz, depth, p=P):
    """x mono -> (ch1, ch2): the Wien oscillator (a sine soft-limited: a little 3rd harmonic), each LDR's light
    through its asymmetric response (fast on, slow off), the shunts in antiphase. depth 0..1: the knob's span between
    the spec's min and max modulation, m = (gmax - gmin) / (gmax + gmin)"""
    tr = p["tremolo"]
    m = tr["depth_min"] + (tr["depth_max"] - tr["depth_min"]) * depth
    d = 2 * m / (1 + m)
    t = np.arange(len(x)) / FS
    ph = 2 * math.pi * hz * t
    osc = np.sin(ph) - tr["h3"] * np.sin(3 * ph)
    osc /= np.max(np.abs(osc))
    out = []
    for sgn in (1.0, -1.0):
        light = 0.5 + 0.5 * sgn * osc
        a_on = 1 - math.exp(-1 / (FS * tr["ldr_on_ms"] * 1e-3))
        a_off = 1 - math.exp(-1 / (FS * tr["ldr_off_ms"] * 1e-3))
        l = np.empty_like(light)
        y = light[0]
        for i in range(len(light)):                              # (a demo's length: plain Python is fine)
            y += (a_on if light[i] > y else a_off) * (light[i] - y)
            l[i] = y
        out.append(x * (1 - d * l))
    return out


# ---- renders -------------------------------------------------------------------------------------------------------

def mix(events, length, p=P):
    """events: (start s, key 1..88, vel, dur s) -> the bus (N)"""
    out = np.zeros(int(length * FS))
    for t0, k, vel, dur in events:
        y = note(k, vel, dur, tail=max(length - t0 - dur, 0.05), p=p)
        i = int(t0 * FS)
        y = y[:len(out) - i]
        out[i:i + len(y)] += y
    return out


def ref_gain(p=P):
    """the bus to the output's scale: A4 (key 49) at vel 100 peaks at -12 dBFS (the line out's -20 dBm nominal)"""
    y = note(49, 100, 1.0, 0.2, p)
    return 10 ** (-12 / 20) / np.max(np.abs(y))


def write(path, l, r=None):
    st = np.stack([l, l if r is None else r], axis=1)
    pk = np.max(np.abs(st))
    if pk > 0.99:
        st *= 0.99 / pk
    wavfile.write(path, FS, (st * 32767).astype(np.int16))


def midi_key(m):
    return m - 20


def render(dirpath, p=P):
    os.makedirs(dirpath, exist_ok=True)
    g = ref_gain(p)
    pre, pre70 = Preamp(p), Preamp(p, "cp70")
    files = []
    keys = [1, 13, 25, 40, 49, 64, 73, 88]                       # A0 A1 A2 C4 A4 C6 A6 C8
    ev, t = [], 0.0
    for k in keys:
        for vel in (40, 80, 127):
            ev.append((t, k, vel, 2.0))
            t += 3.0
    y = pre.run(mix(ev, t + 0.5, p) * g)
    write(os.path.join(dirpath, "cp80_keys_v40_v80_v127.wav"), y)
    files.append("cp80_keys_v40_v80_v127.wav: A0 A1 A2 C4 A4 C6 A6 C8, each at v40 / v80 / v127")
    prog = [(0.0, [36, 48, 52, 55, 59]), (2.0, [41, 53, 57, 60, 64]), (4.0, [43, 55, 59, 62, 65]),
            (6.0, [36, 52, 55, 60, 64])]                         # Cmaj7 Fmaj7 G7 C (MIDI notes)
    ev = []
    for t0, ch in prog:
        for i, m in enumerate(ch):
            ev.append((t0 + 0.012 * i, midi_key(m), 92 if i else 100, 1.9))
    melody = [(8.0, 72), (8.5, 74), (9.0, 76), (9.5, 79), (10.0, 77), (10.5, 76), (11.0, 74), (11.5, 72)]
    for t0, m in melody:
        ev.append((t0, midi_key(m), 96, 0.45))
    ev += [(8.0, midi_key(48), 85, 3.8), (8.0, midi_key(55), 80, 3.8), (8.0, midi_key(64), 80, 3.8)]
    bus = mix(ev, 14.0, p) * g
    write(os.path.join(dirpath, "cp80_progression.wav"), pre.run(bus))
    files.append("cp80_progression.wav: Cmaj7 Fmaj7 G7 C, then a line over C (knobs at 5, BRILLIANCE MEDIUM)")
    l, r = tremolo(pre.run(bus), 5.0, 1.0, p)
    write(os.path.join(dirpath, "cp80_progression_tremolo.wav"), l, r)
    files.append("cp80_progression_tremolo.wav: the same, TREMOLO 5 Hz, DEPTH max (stereo: ch1 left, ch2 right)")
    for b in ("HIGH", "LOW"):
        write(os.path.join(dirpath, f"cp80_progression_bril_{b.lower()}.wav"), pre.run(bus, bril=b))
        files.append(f"cp80_progression_bril_{b.lower()}.wav: the same, BRILLIANCE {b}")
    write(os.path.join(dirpath, "cp70_progression.wav"), pre70.run(bus))
    files.append("cp70_progression.wav: the same strings through the CP-70's low-pass (2.75 kHz), no BRILLIANCE")
    with open(os.path.join(dirpath, "README.txt"), "w") as f:
        f.write("tools/cp_model.py --render (the CP-80 baseline, before any recording)\n" +
                "\n".join(files) + "\n")
    return files


# ---- the report ----------------------------------------------------------------------------------------------------

def report(p=P):
    print("key  note  f1 Hz    kind   str  L m    T N    B         T60 s  contact ms (v40/v100/v127)  partials")
    names = ["A", "A#", "B", "C", "C#", "D", "D#", "E", "F", "F#", "G", "G#"]
    for k in (1, 13, 22, 23, 25, 37, 42, 43, 49, 61, 64, 73, 74, 81, 88):
        s = key_params(k, p)
        nm = names[(k - 1) % 12] + str((k + 8) // 12)
        cts = [strike(s, vv, p)[1] * 1e3 for vv in (40, 100, 127)]
        n, _ = partials(s)
        print(f"{k:3d}  {nm:4s} {s['f1']:8.2f}  {s['kind']:6s} {s['n_str']}   {s['L']:.3f}  {s['T']:5.0f}  "
              f"{s['B']:.2e}  {s['t60']:5.1f}  {cts[0]:5.2f} {cts[1]:5.2f} {cts[2]:5.2f}"
              f"{'' if s['damped'] else '  (undamped)':14s}  {len(n)}")
    for model in ("cp80", "cp70"):
        pre = Preamp(p, model)
        f0, q = sk_analog(pre.sk)
        print(f"\n{model}: Sallen-Key low-pass {f0:.0f} Hz, Q {q:.2f}; calibration (dB re 500 Hz), target / model:")
        for what, tgt, got in pre.cal_check():
            print(f"  {what:34s} {tgt:+6.1f} {got:+6.1f}")
        if model == "cp80":
            print("  shelf gains (dB) min/mid/max: BASS " +
                  " / ".join(f"{pre.g['bass'][s]:+.1f}" for s in ("min", "mid", "max")) + ", TREBLE " +
                  " / ".join(f"{pre.g['treble'][s]:+.1f}" for s in ("min", "mid", "max")) + ", BRILLIANCE " +
                  " / ".join(f"{pre.g['bril'][s]:+.1f}" for s in ("LOW", "MEDIUM", "HIGH")))


if __name__ == "__main__":
    if "--render" in sys.argv:
        for line in render(sys.argv[sys.argv.index("--render") + 1]):
            print(line)
    else:
        report()
