#!/usr/bin/env python3
"""Measure single CP-70 notes (Pianobook "David's CP70", Conner Sawchuk; the SFZ mapping by kinwie): per sample, the
pitch, the inharmonicity B, each partial's early and late decay, the beating of a string pair (its detune), the
level (the file's own: the samples are not normalised; the mapping's volumes flatten the layers, not used) and the brightness (H2..H8 re H1).

  python3 tools/measure_cp_notes.py DIR OUT.json     DIR holds "David's CP70.sfz" and "David's CP70 Samples/"

Numbers only leave this machine (the samples stay in frodhes-refs/audio)."""
import json
import math
import os
import re
import sys

import numpy as np
from scipy import signal
from scipy.io import wavfile


def parse_sfz(path):
    regs, key, tune = [], None, 0.0
    for line in open(path, encoding="utf-8", errors="replace"):
        line = line.split("//")[0]
        if "<group>" in line:
            m = re.search(r"pitch_keycenter=(\d+)", line)
            key = int(m.group(1)) if m else key
            t = re.search(r"\btune=(-?\d+)", line)
            tune = float(t.group(1)) if t else 0.0
        if "<region>" in line:
            g = lambda k, d=None: (re.search(k + r"=([^\s]+)", line).group(1) if re.search(k + r"=", line) else d)
            smp = re.search(r"sample=(.+?\.wav)", line).group(1)
            regs.append(dict(sample=smp, key=key, tune=tune, lovel=int(g("lovel", 1)), hivel=int(g("hivel", 127)),
                             volume=float(g("volume", 0)), label=g("region_label", "")))
    return regs


def env_db(x, fs, f, bw, hop):
    """the envelope (dB) of x around f (a band-pass of +-bw Hz), every hop s"""
    lo, hi = max(f - bw, 10), min(f + bw, fs / 2 * 0.95)
    sos = signal.butter(4, [lo, hi], "bandpass", fs=fs, output="sos")
    y = signal.sosfiltfilt(sos, x)
    a = np.abs(signal.hilbert(y))
    h = int(hop * fs)
    e = np.array([np.mean(a[i:i + h]) for i in range(0, len(a) - h, h)])
    return 20 * np.log10(e + 1e-12)


def slope(t, e, t0, t1):
    k = (t >= t0) & (t < t1)
    return float(np.polyfit(t[k], e[k], 1)[0]) if k.sum() > 5 else None


def measure(path, f_nom):
    fs, x = wavfile.read(path)
    x = x.astype(float)
    if x.ndim > 1:
        x = x.mean(1)
    x /= 32768.0
    clip = int(np.sum(np.abs(x) > 0.995))
    pk = np.max(np.abs(x))
    i0 = int(np.argmax(np.abs(x) > pk * 0.05))
    x = x[max(i0 - int(0.002 * fs), 0):]
    dur = len(x) / fs
    # the pitch: the strongest peak within +-60 cents of the nominal, 0.2..2 s
    seg = x[int(0.2 * fs):int(min(2.2, dur) * fs)]
    N = 1 << 20
    sp = np.abs(np.fft.rfft(seg * np.hanning(len(seg)), N))
    fr = np.fft.rfftfreq(N, 1 / fs)

    def peak_near(f, cents):
        k = (fr > f * 2 ** (-cents / 1200)) & (fr < f * 2 ** (cents / 1200))
        if not k.any():
            return None, None
        j = np.argmax(np.where(k, sp, 0))
        if 0 < j < len(sp) - 1:                              # parabolic interpolation
            a, b, c = np.log(sp[j - 1] + 1e-20), np.log(sp[j] + 1e-20), np.log(sp[j + 1] + 1e-20)
            d = 0.5 * (a - c) / (a - 2 * b + c)
            return fr[j] + d * (fr[1] - fr[0]), sp[j]
        return fr[j], sp[j]
    lsp = np.log(sp + 1e-20)

    def comb(f):                                             # the harmonics' log spectrum (the bass's weak f1)
        idx = np.round(np.arange(1, 9) * f / (fr[1] - fr[0])).astype(int)
        idx = idx[idx < len(lsp)]
        return float(np.mean(lsp[idx]))
    cands = f_nom * 2 ** (np.linspace(-80, 80, 641) / 1200)
    f_c = cands[int(np.argmax([comb(c) for c in cands]))]
    f1, a1 = peak_near(f_c, 8)

    def fit_b(parts):
        n_ = np.array([p[0] for p in parts], float)
        r = np.array([p[1] for p in parts]) / (n_ * f1)              # = sqrt((1 + B n^2) / (1 + B))
        # r^2 (1 + B) = 1 + B n^2  ->  r^2 - 1 = B (n^2 - r^2)
        return max(float(np.sum((r ** 2 - 1) * (n_ ** 2 - r ** 2)) / np.sum((n_ ** 2 - r ** 2) ** 2)), 0.0)
    # the partials one by one, each searched where the B of those found so far puts it
    B, parts = 0.0, [(1, f1, a1)]
    for n in range(2, 17):
        f0 = f1 / math.sqrt(1 + B)
        fp = n * f0 * math.sqrt(1 + B * n * n)
        if fp > min(fs / 2 * 0.9, 12000):
            break
        f, a = peak_near(fp, 12 + 4 * n if len(parts) < 3 else 15)
        if f is not None and a > a1 * 10 ** (-50 / 20):
            parts.append((n, f, a))
            if len(parts) >= 2:
                B = fit_b(parts)
    hop = 0.01
    noise = 20 * np.log10(np.sqrt(np.mean(x[-int(0.3 * fs):] ** 2)) + 1e-12)
    out_parts = []
    for n, f, a in parts[:10]:
        e = env_db(x, fs, f, max(f * 0.03, 4.0), hop)
        t = np.arange(len(e)) * hop
        top = e[: int(0.3 / hop)].max()
        fl = np.median(e[-int(0.5 / hop):])
        usable = np.where(e > fl + 10)[0]
        t_end = (usable[-1] * hop) if len(usable) else 0.5
        out_parts.append(dict(n=n, f=round(f, 3), rel_db=round(20 * math.log10(a / a1), 2),
                              early_dbs=slope(t, e, 0.08, min(1.0, t_end)),
                              late_dbs=slope(t, e, 1.5, min(8.0, t_end)) if t_end > 3.0 else None,
                              t_end=round(t_end, 2), peak_db=round(float(top), 1)))
    # beating of the fundamental and of the 2nd partial: the detrended envelope's strongest slow modulation
    beats = []
    for pp in out_parts[:3]:
        e = env_db(x, fs, pp["f"], max(pp["f"] * 0.03, 4.0), 0.005)
        t = np.arange(len(e)) * 0.005
        k = (t > 0.2) & (t < min(pp["t_end"], 9.0))
        if k.sum() < 400:
            beats.append(None)
            continue
        d = e[k] - np.polyval(np.polyfit(t[k], e[k], 2), t[k])
        M = 1 << 16
        s2 = np.abs(np.fft.rfft(d * np.hanning(len(d)), M))
        f2 = np.fft.rfftfreq(M, 0.005)
        kk = (f2 > 0.15) & (f2 < 6)
        j = np.argmax(np.where(kk, s2, 0))
        depth = float(np.std(d))
        beats.append(dict(n=pp["n"], beat_hz=round(float(f2[j]), 3), depth_db=round(depth, 2)))
    lvl = 20 * math.log10(np.sqrt(np.mean(x[int(0.05 * fs):int(0.3 * fs)] ** 2)) + 1e-12)
    return dict(f1=round(f1, 3), B=B, clip=clip, peak=round(float(pk), 4), noise_db=round(noise, 1), dur=round(dur, 2),
                level_db=round(lvl, 2), partials=out_parts, beats=beats)


def main(d, out):
    regs = parse_sfz(os.path.join(d, "David's CP70.sfz"))
    res = []
    seen = {}
    for r in regs:
        p = os.path.join(d, "David's CP70 Samples", r["sample"])
        if r["sample"] in seen or not os.path.exists(p):
            continue
        f_nom = 440 * 2 ** ((r["key"] - 69) / 12) * 2 ** (-r["tune"] / 1200)
        m = measure(p, f_nom)
        m.update(sample=r["sample"], key=r["key"], tune=r["tune"], lovel=r["lovel"], hivel=r["hivel"],
                 volume=r["volume"], label=r["label"])   # (level_db is the file's own: the files are not normalised,
                                                         # the mapping's volume flattens them: not used)
        seen[r["sample"]] = 1
        res.append(m)
        print(f"{r['sample']:14s} key {r['key']} {r['label']:4s} f1 {m['f1']:8.2f} B {m['B']:.2e} "
              f"clip {m['clip']:4d} lvl {m['level_db']:6.1f} beats {[b and b['beat_hz'] for b in m['beats']]}",
              flush=True)
    with open(out, "w") as f:
        json.dump(res, f, indent=1)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
