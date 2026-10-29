"""The 200A vibrato's cycle shape from stretches found by find_vibrato.py.

Per stretch (file, start s, length s): the loudness in dB minus its slow trend, the rate refined (the AM
spectrum's peak, parabolic), then every cycle folded onto one phase grid; the median over cycles is the shape
(robust to note onsets). Aligned so the loudness minimum sits at phase 0. Prints per stretch the depth and the
shape's harmonics (dB and phase re the fundamental), and writes the median shapes as CSV (phase, one column each).

Usage: vibrato_shape.py OUT.csv FILE:START:LEN ...
"""
import sys

import numpy as np
from scipy.io import wavfile
from scipy.signal import savgol_filter

FS = 400
NPH = 64


def track(path, t0, ln):
    sr, x = wavfile.read(path)
    x = x.astype(np.float64)
    if x.ndim > 1:
        x = x.mean(axis=1)
    x = x[int(t0 * sr): int((t0 + ln) * sr)]
    w = sr // 200
    e = np.sqrt(np.convolve(x ** 2, np.ones(w) / w, mode="same"))[:: sr // FS]
    d = 20 * np.log10(e + 1e-9)
    return d - savgol_filter(d, 161, 2)


def rate_of(r):
    n = 1 << 16
    R = np.abs(np.fft.rfft(r * np.hanning(len(r)), n))
    F = np.fft.rfftfreq(n, 1 / FS)
    m = np.where((F > 3) & (F < 9))[0]
    k = m[np.argmax(R[m])]
    y0, y1, y2 = np.log(R[k - 1:k + 2])
    return (k + 0.5 * (y0 - y2) / (y0 - 2 * y1 + y2)) * FS / n


def shape(r, f):
    ph = (np.arange(len(r)) * f / FS) % 1.0
    cyc = (np.arange(len(r)) * f / FS).astype(int)
    grid = np.zeros((cyc.max() + 1, NPH))
    grid[:] = np.nan
    for c in range(cyc.max() + 1):
        m = cyc == c
        if m.sum() < FS / f * 0.9:
            continue
        grid[c] = np.interp(np.arange(NPH) / NPH, ph[m], r[m], period=1.0)
    s = np.nanmedian(grid, axis=0)
    s -= s.mean()
    # the minimum at phase 0 (by the fundamental's phase, sturdier than argmin)
    c1 = np.fft.rfft(s)[1]
    sh = (np.angle(c1) + np.pi) / (2 * np.pi)     # where the fundamental's trough sits, as a fraction
    i = np.arange(NPH) / NPH
    return np.interp((i - (1 - sh)) % 1.0, i, s, period=1.0)


def main():
    out = sys.argv[1]
    cols, names = [], []
    for arg in sys.argv[2:]:
        p, t0, ln = arg.rsplit(":", 2)
        r = track(p, float(t0), float(ln))
        f = rate_of(r)
        s = shape(r, f)
        H = np.fft.rfft(s) / NPH * 2
        h1 = abs(H[1])
        harm = "  ".join(f"h{k} {20 * np.log10(abs(H[k]) / h1 + 1e-9):6.1f} dB {np.degrees(np.angle(H[k]) - k * np.angle(H[1])) % 360:5.0f}°" for k in (2, 3))
        print(f"{p.rsplit('/', 1)[-1]:28s} {t0:>5s}s  rate {f:5.2f} Hz  depth p-p {s.max() - s.min():5.1f} dB  {harm}")
        cols.append(s)
        names.append(p.rsplit("/", 1)[-1] + "@" + t0)
    with open(out, "w") as fo:
        fo.write("phase," + ",".join(names) + "\n")
        for i in range(NPH):
            fo.write(f"{i / NPH:.4f}," + ",".join(f"{c[i]:.3f}" for c in cols) + "\n")


if __name__ == "__main__":
    main()
