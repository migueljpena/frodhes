"""Does the 200A's vibrato move the bark? If the LDR modulates the gain of the stage that saturates (the 200A
schematic: the LDR shunts the reed-bar preamp's emitter feedback), the loud phase of each cycle is also the rougher
one: the spectral balance (energy above 1.5 kHz re below) rises and falls with the loudness. If the vibrato acts
after the saturation, the balance does not follow the cycle.

Per stretch: 10 ms frames, loudness (dB) and brightness (dB, >1.5 kHz minus <1.5 kHz), both detrended; each folded
on the vibrato's cycle (rate from the loudness); prints the brightness swing and its correlation with the loudness
over the cycle. A control: the same on the model rendered with the vibrato after (as the page) and before the tanh.

Usage: vibrato_bark.py [FILE:START:LEN ...] [LOWPASS_HZ ...]   (each LOWPASS_HZ: the model with trem_stage at it)
"""
import json
import sys

import numpy as np
from scipy.io import wavfile
from scipy.signal import savgol_filter, stft

sys.path.insert(0, "/Users/migueljpena/Projects/frodhes/tools")
import reed_model as M  # noqa: E402

FS = 100


def tracks(x, sr):
    f, t, Z = stft(x, fs=sr, nperseg=1024, noverlap=1024 - sr // FS)
    P = np.abs(Z) ** 2
    lo, hi = P[(f > 60) & (f < 1500)].sum(0), P[(f >= 1500) & (f < 8000)].sum(0)
    L = 10 * np.log10(lo + hi + 1e-12)
    B = 10 * np.log10(hi + 1e-12) - 10 * np.log10(lo + 1e-12)
    return L - savgol_filter(L, 41, 2), B - savgol_filter(B, 41, 2)


def fold(L, B):
    n = 1 << 14
    R = np.abs(np.fft.rfft(L * np.hanning(len(L)), n))
    F = np.fft.rfftfreq(n, 1 / FS)
    m = (F > 3) & (F < 9)
    f = F[m][np.argmax(R[m])]
    ph = (np.arange(len(L)) * f / FS) % 1.0
    bins = np.minimum((ph * 16).astype(int), 15)
    l = np.array([np.median(L[bins == b]) for b in range(16)])
    b = np.array([np.median(B[bins == b]) for b in range(16)])
    return f, l.max() - l.min(), b.max() - b.min(), np.corrcoef(l, b)[0, 1]


def report(name, x, sr):
    f, dl, db, c = fold(*tracks(x, sr))
    print(f"{name:34s} rate {f:4.2f} Hz  loudness swing {dl:5.1f} dB  brightness swing {db:4.1f} dB  corr {c:+.2f}")


def model(before, lp_hz=None):
    P = json.load(open("/Users/migueljpena/Projects/frodhes-refs/play/params/bell3_v2.json"))
    rng = np.random.default_rng(5)
    notes, t = [], 0.0
    while t < 8:
        for m in rng.integers(48, 76, 3):
            notes.append((t, int(m), int(rng.integers(70, 115)), 0.9))
        t += 0.6
    n = int(9 * M.FS)
    y = np.zeros(n)
    for t0, m, v, ln in notes:
        i = int(t0 * M.FS)
        s = M.voice(m, v, 9 - t0, P, off=ln)
        y[i:i + len(s)] += s[: n - i]
    a = np.exp(-2 * np.pi * P["fc"] / M.FS)
    from scipy.signal import lfilter
    y = lfilter([(1 + a) / 2, -(1 + a) / 2], [1, -a], y)
    g = M.trem_gain(np.arange(n) * 5.4 / M.FS, 0.5)
    d = P["drive"]
    if lp_hz:
        return M.trem_stage(np.tanh(d * y), 0.5, 5.4, lp_hz=lp_hz), M.FS
    return (np.tanh(d * y * g) if before else np.tanh(d * y) * g), M.FS


if __name__ == "__main__":
    for arg in [a for a in sys.argv[1:] if ":" in a]:
        p, t0, ln = arg.rsplit(":", 2)
        sr, x = wavfile.read(p)
        x = x.astype(np.float64)
        x = x.mean(axis=1) if x.ndim > 1 else x
        report(f"{p.rsplit('/', 1)[-1]}@{t0}", x[int(float(t0) * sr): int((float(t0) + float(ln)) * sr)], sr)
    report("model: vibrato after the tanh", *model(False))
    report("model: vibrato into the tanh", *model(True))
    for hz in [float(a) for a in sys.argv[1:] if a.replace(".", "").isdigit()]:
        report(f"model: trem_stage, low-pass {hz:.0f} Hz", *model(False, hz))
