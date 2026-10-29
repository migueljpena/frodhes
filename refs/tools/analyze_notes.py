"""Find isolated notes in a reference recording and measure them.

Usage: analyze_notes.py OUT.csv FILE.wav [FILE.wav ...]

For each onset: the pitch (f0), how clean a single note it is (harmonicity: the share of the
spectrum's energy on one harmonic series), the level, the attack time, the first 12 harmonics
relative to the fundamental 80 ms in, the decay of h1 / h2 / h3 in dB per second, and the
odd / even balance (the 200A's electrostatic pickup is expected to add even harmonics when
struck hard: the "bark").
"""
import csv
import sys

import numpy as np
from scipy.io import wavfile
from scipy.signal import find_peaks

SR = 44100
HOP = 256
NFFT = 4096


def load(path):
    sr, x = wavfile.read(path)
    if x.dtype.kind == "i":
        x = x.astype(np.float64) / np.iinfo(x.dtype).max
    if x.ndim > 1:
        x = x.mean(axis=1)
    assert sr == SR, (path, sr)
    return x


def stft_mag(x):
    w = np.hanning(NFFT)
    n = 1 + (len(x) - NFFT) // HOP
    frames = np.lib.stride_tricks.as_strided(
        x, shape=(n, NFFT), strides=(x.strides[0] * HOP, x.strides[0]))
    return np.abs(np.fft.rfft(frames * w, axis=1))


def onsets(mag):
    """Spectral flux on a log magnitude, peaks above an adaptive threshold."""
    lm = np.log1p(100 * mag)
    flux = np.maximum(lm[1:] - lm[:-1], 0).sum(axis=1)
    flux = np.concatenate([[0], flux])
    med = np.convolve(flux, np.ones(43) / 43, mode="same")
    pk, _ = find_peaks(flux, height=med * 2.0 + flux.std() * 0.5, distance=int(0.12 * SR / HOP))
    return pk


NF0 = 8192    # the f0's own window (186 ms): at 4096 a harmonic's +-2 bins (+-21 Hz) let a false low f0
              # catch the true harmonics (a 110 Hz note read as 41 Hz)


def f0_of(seg):
    """Harmonic sum over candidate f0s from 40 to 1200 Hz on NF0 samples; the best and its harmonicity."""
    spec = np.abs(np.fft.rfft(seg[:NF0] * np.hanning(NF0)))
    freqs = np.fft.rfftfreq(NF0, 1 / SR)
    p = spec ** 2
    total = p[(freqs > 30) & (freqs < 8000)].sum() + 1e-20
    best, best_s = None, 0.0
    for f0 in np.geomspace(40, 1200, 1200):
        s = 0.0
        for h in range(1, 9):
            b = int(round(h * f0 * NF0 / SR))
            if b + 2 >= len(p):
                break
            s += p[b - 1:b + 2].max() / h ** 0.5
        if s > best_s:
            best, best_s = f0, s
    # octave check: a sub-octave guess has its odd harmonics (f0, 3 f0, 5 f0 ...) empty; double it
    # while they hold under 5 % of the even ones
    def at(f):
        b = int(round(f * NF0 / SR))
        return p[max(b - 1, 1):b + 2].max()
    while best * 2 <= 1200:
        if sum(at(h * best) for h in (1, 3, 5, 7)) >= 0.05 * sum(at(h * best) for h in (2, 4, 6, 8)):
            break
        best *= 2
    # refine on the fundamental's peak, then harmonicity on the refined f0
    b1 = int(round(best * NF0 / SR))
    lo, hi = max(b1 - 2, 1), b1 + 3
    k = lo + int(np.argmax(spec[lo:hi]))
    if 1 <= k < len(spec) - 1:   # parabolic interpolation
        a, b, c = np.log(spec[k - 1:k + 2] + 1e-20)
        k = k + 0.5 * (a - c) / (a - 2 * b + c + 1e-20)
    f0 = k * SR / NF0
    on = 0.0
    for h in range(1, 60):
        b = int(round(h * f0 * NF0 / SR))
        if b + 3 >= len(p) or h * f0 > 8000:
            break
        on += p[b - 3:b + 4].sum()
    return f0, on / total


def harm_amps(spec, f0, n=12):
    out = []
    for h in range(1, n + 1):
        b = int(round(h * f0 * NFFT / SR))
        out.append(spec[b - 2:b + 3].max() if b + 3 < len(spec) else 0.0)
    return np.array(out)


def midi_of(f):
    return 69 + 12 * np.log2(f / 440.0)


def measure(path, writer):
    x = load(path)
    if len(x) < NFFT * 4:
        return 0
    mag = stft_mag(x)
    env = np.sqrt((mag ** 2).mean(axis=1))
    on = onsets(mag)
    kept = 0
    for i, fr in enumerate(on):
        nxt = on[i + 1] if i + 1 < len(on) else len(mag)
        span = nxt - fr
        if span * HOP / SR < 0.35:            # too short to see a decay
            continue
        a = fr + int(0.08 * SR / HOP)          # 80 ms in: past the hammer
        if a >= nxt:
            continue
        if a * HOP + NF0 > len(x) or span * HOP < NF0:
            continue
        f0, harm = f0_of(x[a * HOP:a * HOP + NF0])
        if harm < 0.6 or not 35 < f0 < 1300:    # a chord or the band: not one note
            continue
        h = harm_amps(mag[a], f0)
        hdb = 20 * np.log10(h / (h[0] + 1e-20) + 1e-20)
        # decay of h1..h3: a line through their level, 80 ms .. min(span, 1.5 s)
        end = min(nxt, fr + int(1.5 * SR / HOP))
        t = (np.arange(a, end) - fr) * HOP / SR
        dec = []
        for k in range(3):
            b = int(round((k + 1) * f0 * NFFT / SR))
            lv = 20 * np.log10(mag[a:end, b - 2:b + 3].max(axis=1) + 1e-20)
            dec.append(np.polyfit(t, lv, 1)[0] if len(t) > 8 else np.nan)
        # attack, on the waveform (1 ms RMS): the onset lies somewhere in frame fr's window; from the
        # envelope's peak in that window + 100 ms back to where it last stood under 10 % of the peak
        s0, s1 = fr * HOP, min(fr * HOP + NFFT + int(0.1 * SR), len(x))
        w1 = int(0.001 * SR)
        e = np.sqrt(np.convolve(x[s0:s1] ** 2, np.ones(w1) / w1, mode="same"))
        ipk = int(np.argmax(e))
        pk = e[ipk]
        under10 = np.nonzero(e[:ipk] < 0.1 * pk)[0]
        i10 = under10[-1] if len(under10) else 0
        i90 = i10 + int(np.argmax(e[i10:ipk + 1] >= 0.9 * pk))
        odd = (h[2::2] ** 2).sum()             # h3, h5, ...
        even = (h[1::2] ** 2).sum()            # h2, h4, ...
        writer.writerow({
            "file": path.rsplit("/", 1)[-1], "t_s": round(fr * HOP / SR, 3),
            "dur_s": round(span * HOP / SR, 2), "f0": round(f0, 2),
            "midi": round(midi_of(f0), 2), "cents_off": round(100 * (midi_of(f0) - round(midi_of(f0)))),
            "harmonicity": round(harm, 3), "level_db": round(20 * np.log10(pk + 1e-20), 1),
            "attack_ms": round((i90 - i10) / SR * 1000, 1),
            "even_minus_odd_db": round(10 * np.log10((even + 1e-20) / (odd + 1e-20)), 1),
            "dec_h1_dbs": round(dec[0], 1), "dec_h2_dbs": round(dec[1], 1), "dec_h3_dbs": round(dec[2], 1),
            **{f"h{k + 1}_db": round(hdb[k], 1) for k in range(1, 12)},
        })
        kept += 1
    return kept


def main():
    out, files = sys.argv[1], sys.argv[2:]
    cols = ["file", "t_s", "dur_s", "f0", "midi", "cents_off", "harmonicity", "level_db", "attack_ms",
            "even_minus_odd_db", "dec_h1_dbs", "dec_h2_dbs", "dec_h3_dbs"] + [f"h{k}_db" for k in range(2, 13)]
    with open(out, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for f in files:
            n = measure(f, w)
            fh.flush()
            print(f"{f.rsplit('/', 1)[-1]}: {n} isolated notes")


if __name__ == "__main__":
    main()
