"""Fit the reed model (frodhes tools/reed_model.py) to the WoerliTzer line samples.

Usage: fit_reed.py OUT.json [maxiter] [gap|bell|bell3|bellp [START.json]]

bellp: bell3 plus the bell's sharpness bell_p, the loss also on h6 / h8 / h10 (from START.json, required).
v4: from START.json (bellp's), every layer of the 12 keys; the pickup a differentiator (fc 1 MHz, fixed), the T60
fixed per key to the measured ones (REG.json: regulation_v4.py base), d per register (33 / 64 / 96), the top
octaves' hammer contact (tc_top), the strike's spike in the loss, the level range (rel) weighted 3 and, over the
middle keys (50-69), a bark target: h2 loudest - softest layer = BARK dB (between the WoerliTzer's ~5 and
BaltiWurli's ~28 dB). Usage: fit_reed.py OUT.json maxiter v4 START.json REG.json

bellpd: bellp started from a differentiating input (fc 6 kHz, bell_p 1), as the 200A schematic implies.

bell3: the bell pickup with 3-point swing and T60 curves (low, middle, high keys), from START.json if given.

Both the recordings and the model go through the same quick features: h2..h5 against h1 50-143 ms in, the level
(1 ms RMS peak) against the key's loudest layer, and the decay of h1 and h2 (dB/s) between windows at 0.3 s and 1.0 s.
12 keys x 3 layers (the softest, the middle, the loudest). Nelder-Mead on the parameters in log space.
"""
import csv
import json
import math
import os
import re
import sys

import numpy as np
from scipy.io import wavfile
from scipy.optimize import minimize

sys.path.insert(0, "/Users/migueljpena/Projects/frodhes/tools")
import reed_model as M  # noqa: E402

SR = 44100
PRESET = "/Users/migueljpena/Projects/frodhes-refs/audio/pianobook/worlitzer/WörliTzer 1.2/01 WörliTzer.dspreset"
KEYS = (36, 41, 45, 50, 55, 60, 64, 69, 74, 79, 84, 90)
FREE = ("u_lo", "u_hi", "gamma", "tc_soft", "tc_hard", "w2", "q2", "fc", "drive")
FLOOR = -60.0


def load(path):
    sr, x = wavfile.read(path)
    scale = 1.0 if x.dtype.kind == "f" else np.iinfo(x.dtype).max + 1.0
    x = x.astype(np.float64) / scale
    return x.mean(axis=1) if x.ndim > 1 else x


def level_at(x, f, t, win=4096):
    s = int(t * SR)
    seg = x[s:s + win]
    if len(seg) < win:
        return np.nan
    sp = np.abs(np.fft.rfft(seg * np.hanning(win)))
    b = int(round(f * win / SR))
    return 20 * math.log10(sp[b - 2:b + 3].max() + 1e-12)


def feats(x, midi):
    a = np.abs(x)
    x = x[int(np.argmax(a > 0.05 * a.max())):]
    f0 = 440.0 * 2 ** ((midi - 69) / 12.0)
    h = [level_at(x, k * f0, 0.05) for k in range(1, 6)]
    w1 = int(0.001 * SR)
    e = np.sqrt(np.convolve(x[: int(0.2 * SR)] ** 2, np.ones(w1) / w1, mode="same"))
    out = {f"h{k}": max(h[k - 1] - h[0], FLOOR) for k in range(2, 6)}
    for k in (6, 8, 10):
        out[f"h{k}"] = max(level_at(x, k * f0, 0.05) - h[0], FLOOR) if k * f0 < SR * 0.45 else np.nan
    out["peak"] = 20 * math.log10(e.max() + 1e-12)
    # the strike's spike: the peak in the tone's first 3 ms over the RMS 50-100 ms in (the tone from 0.3 of the peak)
    t = int(np.argmax(a[int(np.argmax(a > 0.05 * a.max())):] > 0.3 * a.max()))
    body = x[t + int(0.05 * SR): t + int(0.1 * SR)]
    out["spike"] = 20 * math.log10(np.abs(x[t: t + int(0.003 * SR)]).max() / (np.sqrt((body ** 2).mean()) + 1e-12))
    for k in (1, 2):
        out[f"dec{k}"] = (level_at(x, k * f0, 1.0) - level_at(x, k * f0, 0.3)) / 0.7
    return out


def targets():
    txt = open(PRESET, encoding="utf-8").read()
    g = re.search(r'<group name="Line"(.*?)</group>', txt, re.S).group(1)
    by = {}
    for s in re.findall(r"<sample ([^>]*)", g):
        a = dict(re.findall(r'(\w+)="([^"]*)"', s))
        by.setdefault(int(a["rootNote"]), []).append(((int(a["loVel"]) + int(a["hiVel"])) // 2, a["path"].rsplit("/", 1)[-1]))
    base = PRESET.rsplit("/", 1)[0] + "/Samples/"
    T = []
    for k in KEYS:
        layers = sorted(by[k])
        pick = [layers[0], layers[len(layers) // 2], layers[-1]]
        fs = [(v, feats(load(base + p), k)) for v, p in pick]
        top = fs[-1][1]["peak"]
        for v, f in fs:
            f["rel"] = f["peak"] - top
            T.append((k, max(v, 1), f))
    return T


def targets_all():
    """targets() with every layer of the keys."""
    txt = open(PRESET, encoding="utf-8").read()
    g = re.search(r'<group name="Line"(.*?)</group>', txt, re.S).group(1)
    by = {}
    for s in re.findall(r"<sample ([^>]*)", g):
        a = dict(re.findall(r'(\w+)="([^"]*)"', s))
        by.setdefault(int(a["rootNote"]), []).append(((int(a["loVel"]) + int(a["hiVel"])) // 2, a["path"].rsplit("/", 1)[-1]))
    base = PRESET.rsplit("/", 1)[0] + "/Samples/"
    T = []
    for k in KEYS:
        fs = [(v, feats(load(base + p), k)) for v, p in sorted(by[k])]
        top = fs[-1][1]["peak"]
        for v, f in fs:
            f["rel"] = f["peak"] - top
            T.append((k, max(v, 1), f))
    return T


BARK = 17.0                      # v4: the middle keys' h2 rise, softest to loudest layer (dB)
BARK_KEYS = (50, 55, 60, 64, 69)
V4 = False
W = {"h2": 1.0, "h3": 1.0, "h4": 0.5, "h5": 0.5, "rel": 1.5, "dec1": 0.5, "dec2": 0.2}
W_HI = {"h6": 0.5, "h8": 0.5, "h10": 0.5}     # bellp: the high harmonics too (the 2026-10-27 check: ~13 dB short)


def _key_feats(args):
    k, P = args
    fs = {}
    for v in VELS[k]:
        fs[v] = feats(M.render(k, v, 1.4, P), k)
    top = fs[max(fs)]["peak"]
    for v, f in fs.items():
        f["rel"] = f["peak"] - top
    return k, fs


POOL = None     # v4: the keys over the machine's cores (a fork pool, made once VELS is set)


def model_feats(P):
    out = {}
    res = POOL.map(_key_feats, [(k, P) for k in KEYS]) if POOL else map(_key_feats, [(k, P) for k in KEYS])
    for k, fs in res:
        for v, f in fs.items():
            out[(k, v)] = f
    return out


def loss(theta, base, T, report=False):
    P = dict(base)
    for n, t in zip(FREE, theta):
        P[n] = math.exp(t)
    if not (0.5 <= P.get("tc_top", 1.0) <= 4.0 and 0.05 <= P["d"] <= 3.0):
        return 1e9
    if not (0.05 <= P.get("d_mid", 0.3) <= 3.0 and 0.05 <= P.get("d_hi", 0.3) <= 3.0 and 2.0 <= P["q2"] <= 50.0 and P["w2"] <= 0.6 and max(P["u_lo"], P["u_hi"], P.get("u_mid", 0)) <= 5.0
            and 0.5 <= P.get("bell_p", 1.0) <= 12.0 and 1.0 <= P.get("t60_hi", 3) <= P.get("t60_mid", 6) <= P.get("t60_lo", 25) <= 40.0):
        return 1e9
    if not ((P.get("pickup") == "bell" or (P["u_lo"] < 0.95 and P["u_hi"] < 0.95)) and P["tc_hard"] < P["tc_soft"] < 8e-3):
        return 1e9
    mf = model_feats(P)
    err, rows = 0.0, []
    for k, v, f in T:
        m = mf[(k, v)]
        e = 0.0
        for c, w in W.items():
            if np.isnan(f[c]) or np.isnan(m[c]):
                continue
            if V4 and c == "h2" and k in BARK_KEYS:
                w = 0.3                      # the WoerliTzer's own middle bark: an anchor, not the target
            e += w * min((m[c] - f[c]) ** 2, 400.0)
        err += e
        rows.append((k, v, f, m))
    if V4:
        for k in BARK_KEYS:
            vs = sorted(v for kk, v, _ in T if kk == k)
            err += 3.0 * min((mf[(k, vs[-1])]["h2"] - mf[(k, vs[0])]["h2"] - BARK) ** 2, 400.0)
    if report:
        return err / len(T), rows
    return err / len(T)


def main():
    global VELS
    out = sys.argv[1]
    maxiter = int(sys.argv[2]) if len(sys.argv) > 2 else 400
    T = targets()
    VELS = {}
    for k, v, _ in T:
        VELS.setdefault(k, []).append(v)
    base = dict(M.DEFAULT)
    global FREE
    if len(sys.argv) > 3 and sys.argv[3] == "bell":
        base.update(pickup="bell", d=0.5, u_lo=1.6, u_hi=0.5)
        FREE = FREE + ("d",)
    if len(sys.argv) > 3 and sys.argv[3] == "bell3":
        base.update(pickup="bell", d=0.5, u_lo=1.6, u_hi=0.5, u_mid=0.9, w2=0.25, q2=8.0,
                    t60_lo=25.0, t60_mid=6.1, t60_hi=2.6)
        if len(sys.argv) > 4:
            st = json.load(open(sys.argv[4]))
            for n in ("u_lo", "u_hi", "gamma", "tc_soft", "tc_hard", "fc", "drive", "d"):
                base[n] = st[n]
            base["u_mid"] = M.interp_log(st["u_lo"], st["u_hi"], 64)
            for n in ("u_lo", "u_mid", "u_hi"):
                base[n] = min(base[n], 4.5)
        FREE = FREE + ("d", "u_mid", "t60_lo", "t60_mid", "t60_hi")
    if len(sys.argv) > 3 and sys.argv[3] == "bellp":
        # bell3 plus the bell's sharpness (bell_p), fitted on the high harmonics as well
        st = json.load(open(sys.argv[4]))
        base.update({k: v for k, v in st.items() if k not in ("loss", "pickup", "_name")}, pickup="bell", bell_p=2.0)
        FREE = FREE + ("d", "u_mid", "t60_lo", "t60_mid", "t60_hi", "bell_p")
        W.update(W_HI)
    if len(sys.argv) > 3 and sys.argv[3] == "bellpd":
        # bellp from the pickup as the schematic draws it (200A p.23): its tiny capacitance into ~330 kOhm makes the
        # input a differentiator over the whole audio band, so the high-pass starts far above 420 Hz
        st = json.load(open(sys.argv[4]))
        base.update({k: v for k, v in st.items() if k not in ("loss", "pickup", "_name")}, pickup="bell", bell_p=1.0,
                    fc=6000.0)
        FREE = FREE + ("d", "u_mid", "t60_lo", "t60_mid", "t60_hi", "bell_p")
        W.update(W_HI)
    if len(sys.argv) > 3 and sys.argv[3] == "v4":
        global V4
        V4 = True
        T = targets_all()
        VELS.clear()
        for k, v, _ in T:
            VELS.setdefault(k, []).append(v)
        st = json.load(open(sys.argv[4]))
        R = json.load(open(sys.argv[5]))
        again = "d_mid" in st              # START is a v4 result already: go on from it
        base.update({k: v for k, v in st.items() if k not in ("loss", "pickup", "_name")}, pickup="bell", fc=1e6,
                    t60_lo=R["t60_lo"], t60_mid=R["t60_mid"], t60_hi=R["t60_hi"])
        if not again:
            base.update(tc_top=1.0, d_mid=st["d"], d_hi=st["d"])
            # the same level into the preamp: a first difference passes f0 at pi f0 / FS, the old high-pass at f0 / fc
            base["drive"] = st["drive"] * M.FS / (math.pi * st["fc"])
        for kv in os.environ.get("FIT_SET", "").split(","):      # a hand-picked start (k=v,...)
            if kv:
                base[kv.split("=")[0]] = float(kv.split("=")[1])
        if os.environ.get("FIT_EXTRA"):                          # fixed keys from a JSON (v4.1: "early")
            base.update({k: v for k, v in json.load(open(os.environ["FIT_EXTRA"])).items() if not k.startswith("_")})
        W["spike"] = float(os.environ.get("FIT_SPIKE", 0.5))
        base["keys"] = {"t60": R["regulation"]["t60_x"], "d": [1.0] * 64, "g": [1.0] * 64, "cents": [0.0] * 64}
        FREE = ("u_lo", "u_mid", "u_hi", "gamma", "tc_soft", "tc_hard", "tc_top", "w2", "q2", "drive", "bell_p", "d",
                "d_mid", "d_hi")
        W.update(W_HI, rel=3.0, spike=W.get("spike", 0.5))
        # FIT_FIX=k,...: held at their start (drive: the fit sees single notes only, and the preamp is shared, so a
        # free drive buys a single note's compression with every chord's distortion)
        FREE = tuple(n for n in FREE if n not in os.environ.get("FIT_FIX", "").split(","))
        global POOL
        import multiprocessing as mp
        POOL = mp.get_context("fork").Pool(4)
    if len(sys.argv) > 3 and sys.argv[3] == "bell5":
        # bell3 plus the reed's rest point per register (d at 33 / 64 / 96) and the strike curve's top (gamma_hi)
        st = json.load(open(sys.argv[4]))
        base.update({k: v for k, v in st.items() if k not in ("loss", "pickup")}, pickup="bell")
        base.update(d_mid=st["d"], d_hi=st["d"], gamma_hi=st["gamma"])
        FREE = FREE + ("d", "u_mid", "t60_lo", "t60_mid", "t60_hi", "d_mid", "d_hi", "gamma_hi")
    th0 = [math.log(base[n]) for n in FREE]
    print("start loss", loss(th0, base, T), flush=True)
    best = [float("inf")]

    def progress(th):
        it = progress.n = getattr(progress, "n", 0) + 1
        if it % 25 == 0:
            L = loss(th, base, T)
            best[0] = min(best[0], L)
            print(f"iter {it} loss {L:.1f}", flush=True)
        if os.environ.get("FIT_METHOD") == "powell":
            L = loss(th, base, T)
            print(f"iter {it} loss {L:.1f}", flush=True)
    if os.environ.get("FIT_METHOD") == "powell":     # line searches one direction at a time: a bumpy loss
        res = minimize(loss, th0, args=(base, T), method="Powell", callback=progress,
                       options={"maxiter": maxiter, "xtol": 1e-3, "ftol": 1e-4})
    else:
        res = minimize(loss, th0, args=(base, T), method="Nelder-Mead", callback=progress,
                       options={"maxiter": maxiter, "xatol": 1e-3, "fatol": 1e-3, "adaptive": True})
    P = dict(base)
    for n, t in zip(FREE, res.x):
        P[n] = math.exp(t)
    L, rows = loss(res.x, base, T, report=True)
    print("end loss", L, res.message, flush=True)
    print(f"{'key':>4}{'vel':>5} " + " ".join(f"{c:>13}" for c in W))
    for k, v, f, m in rows:
        print(f"{k:4d}{v:5d} " + " ".join(f"{f[c]:6.1f}/{m[c]:6.1f}" for c in W))
    extra = {n: P[n] for n in ("fc", "t60_lo", "t60_mid", "t60_hi", "drive", "tc_top", "d_mid", "d_hi", "early", "tc_per")
             if V4 and n in P}
    json.dump({**{n: P[n] for n in FREE}, **extra, "pickup": P.get("pickup", "gap"), "loss": L}, open(out, "w"), indent=1)
    print({n: round(P[n], 5) for n in FREE})


if __name__ == "__main__":
    main()
