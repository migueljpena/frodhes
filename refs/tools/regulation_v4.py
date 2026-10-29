"""The measured per-key regulation of the WoerliTzer 200A (its line samples): "this instrument" for the v4 set.

Usage:
  regulation_v4.py base OUT.json           T60 (from each key's softest clean layer), level and tuning offsets
  regulation_v4.py dbark FIT.json BASE.json OUT.json AMT
                                           as d below, the bark zigzag softened (AMT 0.5: halved over the fit's curve)
  regulation_v4.py d FIT.json BASE.json OUT.json
                                           plus every key's reed rest point d as an offset over the fit's register
                                           curve (FIT: fit_reed.py v4's result)

- T60: 60 / -(the h1 decay, dB/s, 0.3-1.0 s) of the softest layer whose decay is real (under -0.5 dB/s): there
  the pickup is nearly linear, so h1 follows the reed. Kept whole (trend and scatter), held to 1.5-40 s; t60_x is
  the key's T60 over the 3-point curve (t60_lo / t60_mid / t60_hi at keys 33 / 64 / 96 of a cubic fit in log T60).
- level_db: the key's level off a smooth curve over the keys (fits/keyvar/worlitzer_keys.csv: the middle and the
  loudest layers' peak residuals, averaged), held to +-4 dB.
- cents: the key's tuning off a smooth curve (the same CSV), held to +-10 cents.
- d_ln: per key the d (x the fit's curve) whose h2 best matches the key's every layer (a grid of 33 values from
  e^-1.4 to e^1.4, then the best's neighbours halved twice), and of its log the part off a cubic over the keys:
  the regulation's zigzag, not the WoerliTzer's own bark curve (the fit sets that: fit_reed.py v4).
"""
import csv
import json
import math
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, "/Users/migueljpena/Projects/frodhes/tools")
import fit_reed as F  # noqa: E402
import reed_model as M  # noqa: E402

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LAYERS = os.path.join(HERE, "fits", "v4", "worlitzer_layers.csv")
KEYVAR = os.path.join(HERE, "fits", "keyvar", "worlitzer_keys.csv")
KEYS = list(range(33, 97))


def rows():
    return list(csv.DictReader(open(LAYERS)))


def smooth(y, deg=3, it=3):
    """A cubic over the keys, refitted without the points more than 2.5 robust spreads off it."""
    x = np.array(KEYS, float)
    y = np.asarray(y, float)
    ok = np.isfinite(y)
    for _ in range(it):
        c = np.polyfit(x[ok], y[ok], deg)
        r = y - np.polyval(c, x)
        s = 1.4826 * np.nanmedian(np.abs(r[ok])) + 1e-9
        ok = np.isfinite(y) & (np.abs(r) < 2.5 * s)
    return np.polyval(c, x)


def base(out):
    R = rows()
    t60 = []
    for k in KEYS:
        ls = sorted((r for r in R if int(r["midi"]) == k), key=lambda r: int(r["vel"]))
        dec = next((float(r["dec1"]) for r in ls if float(r["dec1"]) < -0.5), float("nan"))
        t60.append(min(max(60.0 / -dec, 1.5), 40.0) if np.isfinite(dec) else float("nan"))
    lt = smooth(np.log(t60))
    t60 = [t if np.isfinite(t) else math.exp(s) for t, s in zip(t60, lt)]
    lo, mid, hi = (math.exp(lt[k - 33]) for k in (33, 64, 96))
    P = {"t60_lo": lo, "t60_mid": mid, "t60_hi": hi}
    t60_x = [t / M.interp_log(lo, hi, k, mid) for t, k in zip(t60, KEYS)]
    kv = {int(r["midi"]): r for r in csv.DictReader(open(KEYVAR))}
    lvl = [min(max((float(kv[k]["lvl_mid_res"]) + float(kv[k]["lvl_top_res"])) / 2, -4.0), 4.0) for k in KEYS]
    cents = [min(max(float(kv[k]["cents_res"]), -10.0), 10.0) for k in KEYS]
    json.dump({"_about": __doc__.split("\n")[0], **P, "_t60": t60, "regulation": {
        "t60_x": t60_x, "level_db": lvl, "cents": cents, "d_ln": [0.0] * 64}}, open(out, "w"), indent=1)
    print("T60 curve (s): lo %.1f mid %.1f hi %.1f; T60 range %.1f-%.1f s" % (lo, mid, hi, min(t60), max(t60)))


def best_d(P, k, layers):
    """The d multiplier for key k whose h2 best matches the layers' (vel, h2)."""
    def err(x):
        Q = dict(P, keys=dict(P["keys"], d=[x if kk == k else 1.0 for kk in KEYS]))
        e = 0.0
        for v, h2 in layers:
            m = F.feats(M.render(k, v, 1.0, Q), k)["h2"]
            e += min((m - h2) ** 2, 400.0)
        return e
    xs = np.exp(np.linspace(-1.4, 1.4, 33))
    es = [err(x) for x in xs]
    i = int(np.argmin(es))
    x, step = xs[i], math.log(xs[1] / xs[0])
    for _ in range(2):
        step /= 2
        cand = [x * math.exp(-step), x, x * math.exp(step)]
        x = min(cand, key=err)
    return x


def dstep(fit, basef, out):
    B = json.load(open(basef))
    P = dict(M.DEFAULT)
    P.update({k: v for k, v in json.load(open(fit)).items() if k not in ("loss",)})
    P["keys"] = {"t60": B["regulation"]["t60_x"], "d": [1.0] * 64, "g": [1.0] * 64, "cents": [0.0] * 64}
    R = rows()
    xs = []
    for k in KEYS:
        layers = [(int(r["vel"]), float(r["h2"])) for r in R if int(r["midi"]) == k]
        x = best_d(P, k, layers)
        xs.append(x)
        print(k, round(x, 3), flush=True)
    lx = np.log(xs)
    d_ln = list(np.clip(lx - smooth(lx), -1.2, 1.2))
    B["regulation"]["d_ln"] = [float(v) for v in d_ln]
    B["_d_fit_x"] = [float(v) for v in xs]
    json.dump(B, open(out, "w"), indent=1)
    print("d_ln spread (robust) %.2f" % (1.4826 * np.median(np.abs(np.array(d_ln)))))


def _dbark_key(a):
    """One key's d for dbark: the bark (h2 loudest - softest layer) at its target, h2 and h3 near the recording's."""
    P, k, layers, target = a
    vs = [v for v, _, _ in layers]

    def feats_at(x):
        Q = dict(P, keys=dict(P["keys"], d=[x if kk == k else 1.0 for kk in KEYS]))
        return [F.feats(M.render(k, v, 1.0, Q), k) for v in vs]

    def err(x):
        fs = feats_at(x)
        e = (fs[-1]["h2"] - fs[0]["h2"] - target) ** 2
        e += 0.2 * np.mean([min((f["h2"] - h2) ** 2, 400.0) + min((f["h3"] - h3) ** 2, 400.0)
                            for f, (_, h2, h3) in zip(fs, layers)])
        return e
    xs = np.exp(np.linspace(-0.8, 0.8, 17))      # near the curve: not across the bell's h2 null
    x = xs[int(np.argmin([err(x) for x in xs]))]
    step = 0.05
    for _ in range(2):
        step /= 2
        x = min((x * math.exp(-step), x, x * math.exp(step)), key=err)
    fs = feats_at(x)
    return k, x, fs[-1]["h2"] - fs[0]["h2"]


def dbark(fit, basef, out, amt):
    """Every key's d with the zigzag softened in its bark: the target is the fit's own bark curve (the key's bark at
    d x 1, between its softest and loudest recorded layers) plus amt x the recording's zigzag (the key's recorded
    bark off a smooth curve over the keys), h2 and h3 kept near the recording's (weight 0.2). Halving d_ln does not
    soften: h2 is not monotonic in d (it vanishes at the bell's inflection), so a halved offset can land on that null;
    matching the recording's h2 everywhere brings back its own tame middle bark (~5 dB), not the fit's 17."""
    import multiprocessing as mp
    B = json.load(open(basef))
    P = dict(M.DEFAULT)
    P.update({k: v for k, v in json.load(open(fit)).items() if k not in ("loss",)})
    P["keys"] = {"t60": B["regulation"]["t60_x"], "d": [1.0] * 64, "g": [1.0] * 64, "cents": [0.0] * 64}
    R = rows()
    L = {k: sorted(((int(r["vel"]), float(r["h2"]), float(r["h3"])) for r in R if int(r["midi"]) == k)) for k in KEYS}
    real = np.array([L[k][-1][1] - L[k][0][1] for k in KEYS])
    zig = real - smooth(real)
    jobs = []
    for i, k in enumerate(KEYS):
        own = [F.feats(M.render(k, v, 1.0, P), k)["h2"] for v in (L[k][0][0], L[k][-1][0])]
        jobs.append((P, k, L[k], own[1] - own[0] + amt * zig[i]))
    with mp.get_context("fork").Pool(4) as pool:
        res = pool.map(_dbark_key, jobs)
    for (k, x, b), j in zip(res, jobs):
        print(k, "d x %.3f  bark %.1f (target %.1f, recorded %.1f)" % (x, b, j[3], real[k - 33]), flush=True)
    xs = [x for _, x, _ in res]
    B["regulation"]["d_ln"] = [float(v) for v in np.log(xs)]
    B["_d_fit_x"] = [float(v) for v in xs]
    B["_d_bark"] = {"amt": amt, "target": [float(j[3]) for j in jobs], "model": [float(b) for _, _, b in res]}
    json.dump(B, open(out, "w"), indent=1)


if __name__ == "__main__":
    if sys.argv[1] == "base":
        base(sys.argv[2])
    elif sys.argv[1] == "dbark":
        dbark(sys.argv[2], sys.argv[3], sys.argv[4], float(sys.argv[5]))
    else:
        dstep(sys.argv[2], sys.argv[3], sys.argv[4])
