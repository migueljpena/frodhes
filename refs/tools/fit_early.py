"""Fit the early decay (reed_model "early": w_lo, w_hi, t60) on a play set to the WoerliTzer line samples' first
400 ms: the RMS envelope (one period's window at 1, 3, 6, 12, 25, 50, 100, 200, 400 ms, dB re the 50-100 ms RMS)
and the strike's spike (fit_reed.feats), 12 keys x 3 layers (the softest, the middle, the loudest). A grid on t60,
then on w at the low and the high end.

Usage: fit_early.py SET.json OUT.json
"""
import json
import math
import multiprocessing as mp
import re
import sys

import numpy as np

sys.path.insert(0, "/Users/migueljpena/Projects/frodhes/tools")
import fit_reed as F  # noqa: E402
import reed_model as M  # noqa: E402

SR = 44100
TS = (1, 3, 6, 12, 25, 50, 100, 200, 400)


def env(x, k):
    a = np.abs(x)
    x = x[int(np.argmax(a > 0.3 * a.max())):]
    T = max(int(SR / M.f0_of(k, {})), 32)
    ref = np.sqrt((x[int(0.05 * SR): int(0.1 * SR)] ** 2).mean())
    return np.array([20 * math.log10(np.sqrt((x[int(t * SR / 1000): int(t * SR / 1000) + T] ** 2).mean()) / ref + 1e-12)
                     for t in TS])


def layers():
    txt = open(F.PRESET, encoding="utf-8").read()
    g = re.search(r'<group name="Line"(.*?)</group>', txt, re.S).group(1)
    by = {}
    for s in re.findall(r"<sample ([^>]*)", g):
        a = dict(re.findall(r'(\w+)="([^"]*)"', s))
        by.setdefault(int(a["rootNote"]), []).append(((int(a["loVel"]) + int(a["hiVel"])) // 2, a["path"].rsplit("/", 1)[-1]))
    base = F.PRESET.rsplit("/", 1)[0] + "/Samples/"
    out = []
    for k in F.KEYS:
        L = sorted(by[k])
        for v, p in (L[0], L[len(L) // 2], L[-1]):
            x = F.load(base + p)
            out.append((k, max(v, 1), env(x, k), F.feats(x, k)["spike"]))
    return out


def one(a):
    P, k, v = a
    x = M.render(k, v, 0.6, P)
    return env(x, k), F.feats(x, k)["spike"]


def loss(P, R, pool):
    res = pool.map(one, [(P, k, v) for k, v, _, _ in R])
    e = 0.0
    for (k, v, er, sr), (em, sm) in zip(R, res):
        e += ((em - er) ** 2).mean() + 0.5 * (sm - sr) ** 2
    return e / len(R)


def main():
    P0 = json.load(open(sys.argv[1]))
    R = layers()
    with mp.get_context("fork").Pool(4) as pool:
        print("no early: %.2f" % loss(P0, R, pool), flush=True)
        best = None
        for t60 in (0.1, 0.15, 0.2, 0.3, 0.45):
            for w in (0.3, 0.5, 0.7, 1.0, 1.4):
                L = loss(dict(P0, early={"w_lo": w, "w_hi": w, "t60": t60}), R, pool)
                print("t60 %.2f w %.1f: %.2f" % (t60, w, L), flush=True)
                if best is None or L < best[0]:
                    best = (L, t60, w)
        _, t60, w = best
        for wl in (w * 0.6, w * 0.8, w, w * 1.25, w * 1.5):
            for wh in (w * 0.6, w * 0.8, w, w * 1.25, w * 1.5):
                L = loss(dict(P0, early={"w_lo": wl, "w_hi": wh, "t60": t60}), R, pool)
                if L < best[0]:
                    best = (L, t60, w, wl, wh)
                    print("w_lo %.2f w_hi %.2f: %.2f" % (wl, wh, L), flush=True)
    wl, wh = (best[3], best[4]) if len(best) > 3 else (w, w)
    json.dump({"early": {"w_lo": wl, "w_hi": wh, "t60": t60}, "_early_loss": best[0]}, open(sys.argv[2], "w"), indent=1)
    print("best", best)


if __name__ == "__main__":
    main()
