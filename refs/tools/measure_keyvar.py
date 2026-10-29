"""Key-to-key variation of the WoerliTzer line samples: per key (64), the middle and loudest layers' h2 (vs h1),
level, h1 decay and tuning (cents off equal temperament). Each quantity minus a smooth curve over the keys (a cubic
in midi) = the regulation scatter; prints its spread (std) and writes the residuals as CSV.

Usage: measure_keyvar.py OUT.csv
"""
import csv
import math
import re
import sys

import numpy as np

sys.path.insert(0, "/Users/migueljpena/Projects/frodhes-refs/tools")
import fit_reed as F  # noqa: E402

SR = F.SR


def f0_cents(x, midi):
    a = np.abs(x)
    x = x[int(np.argmax(a > 0.05 * a.max())):]
    f0 = 440.0 * 2 ** ((midi - 69) / 12.0)
    n = 1 << 16
    seg = x[int(0.1 * SR): int(0.1 * SR) + n]
    if len(seg) < n:
        seg = np.pad(seg, (0, n - len(seg)))
    sp = np.abs(np.fft.rfft(seg * np.hanning(n)))
    b = int(round(f0 * n / SR))
    lo, hi = int(b * 0.97), int(b * 1.03) + 1
    k = lo + int(np.argmax(sp[lo:hi]))
    y0, y1, y2 = np.log(sp[k - 1:k + 2] + 1e-12)
    p = 0.5 * (y0 - y2) / (y0 - 2 * y1 + y2)
    return 1200 * math.log2((k + p) * SR / n / f0)


def main():
    txt = open(F.PRESET, encoding="utf-8").read()
    g = re.search(r'<group name="Line"(.*?)</group>', txt, re.S).group(1)
    by = {}
    for s in re.findall(r"<sample ([^>]*)", g):
        a = dict(re.findall(r'(\w+)="([^"]*)"', s))
        by.setdefault(int(a["rootNote"]), []).append(((int(a["loVel"]) + int(a["hiVel"])) // 2, a["path"].rsplit("/", 1)[-1]))
    base = F.PRESET.rsplit("/", 1)[0] + "/Samples/"
    rows = []
    for k in sorted(by):
        layers = sorted(by[k])
        mid, top = layers[len(layers) // 2], layers[-1]
        xm, xt = F.load(base + mid[1]), F.load(base + top[1])
        fm, ft = F.feats(xm, k), F.feats(xt, k)
        rows.append(dict(midi=k, h2_mid=fm["h2"], h2_top=ft["h2"], lvl_mid=fm["peak"], lvl_top=ft["peak"],
                         dec1_mid=fm["dec1"], cents=f0_cents(xt, k)))
    keys = np.array([r["midi"] for r in rows], float)
    out = {}
    for q in ("h2_mid", "h2_top", "lvl_mid", "lvl_top", "dec1_mid", "cents"):
        v = np.array([r[q] for r in rows])
        ok = np.isfinite(v)
        c = np.polyfit(keys[ok], v[ok], 3)
        res = v - np.polyval(c, keys)
        for r, e in zip(rows, res):
            r[q + "_res"] = e
        # the robust spread (MAD): a few bad keys (noise floor, f0 misses) do not set it
        m = np.nanmedian(res)
        out[q] = 1.4826 * np.nanmedian(np.abs(res - m))
        # neighbour correlation: regulation is per key (near 0), a smooth trend left over is near 1
        rr = res[ok]
        out[q + "_nbr"] = np.corrcoef(rr[:-1], rr[1:])[0, 1]
    for q in ("h2_mid", "h2_top", "lvl_mid", "lvl_top", "dec1_mid", "cents"):
        print(f"{q:9s} spread {out[q]:6.2f}   neighbour corr {out[q + '_nbr']:+.2f}")
    with open(sys.argv[1], "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


if __name__ == "__main__":
    main()
