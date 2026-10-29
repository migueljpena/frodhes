"""Find stretches with the 200A's vibrato (a periodic loudness wobble, 3-9 Hz) in the reference recordings.

Per 4 s window (1 s hop): the loudness (dB, 10 ms RMS) minus its slow trend, then the AM spectrum's peak in
3-9 Hz and how much it stands over the rest (a steady vibrato is a narrow, tall peak; notes starting at random
times are broad). Prints the strongest windows.

Usage: find_vibrato.py WAV...
"""
import sys

import numpy as np
from scipy.io import wavfile
from scipy.signal import savgol_filter


def am_track(x, sr):
    w = sr // 100
    e = np.sqrt(np.convolve(x ** 2, np.ones(w) / w, mode="same"))[:: sr // 400]
    return 20 * np.log10(e + 1e-7)       # 400 Hz frame rate


def scan(path):
    sr, x = wavfile.read(path)
    x = x.astype(np.float64)
    if x.ndim > 1:
        x = x.mean(axis=1)
    x /= np.abs(x).max() + 1e-12
    d = am_track(x, sr)
    fs = 400
    out = []
    W, H = 4 * fs, fs
    for s in range(0, len(d) - W, H):
        seg = d[s:s + W]
        if seg.max() < -40:
            continue
        r = seg - savgol_filter(seg, 161, 2)        # remove the trend slower than ~2.5 Hz
        R = np.abs(np.fft.rfft(r * np.hanning(W), 8192))
        F = np.fft.rfftfreq(8192, 1 / fs)
        m = (F > 3) & (F < 9)
        k = np.argmax(R[m])
        f = F[m][k]
        pk = R[m][k]
        rest = np.median(R[(F > 1.5) & (F < 20)])
        out.append((pk / rest, f, s / fs, np.percentile(r, 95) - np.percentile(r, 5)))
    return out


if __name__ == "__main__":
    allw = []
    for p in sys.argv[1:]:
        for q, f, t, dpp in scan(p):
            allw.append((q, f, t, dpp, p.rsplit("/", 1)[-1]))
    allw.sort(reverse=True)
    for q, f, t, dpp, n in allw[:40]:
        print(f"{n:28s} t {t:6.0f}s  rate {f:5.2f} Hz  prominence {q:5.1f}  depth p-p {dpp:4.1f} dB")
