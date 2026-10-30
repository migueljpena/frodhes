#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Generate the CP-80 engine's tables (firmware/src/strings.c) from the float model.

    tools/gen_cp.py [--out FILE]   -> firmware/gen/cp_tables.h   (from docs/model/data/cp80.json)

The engine computes tools/cp_multirate.py's algorithm in integers: each key's partials (the ones it keeps, sorted by
band), each at its band's rate (44.1 kHz / 2^b). The header holds, per key, its partials' angles (Q32 of a cycle a
band sample), the strike's start of each partial at a few velocities (the layers: the reference's complex amplitude
x the pickup's weight x the voicing x the level, divided by the interpolators' response at its frequency and by the
hammer's speed, so it varies slowly over the velocities; 12-bit re and im with one exponent a layer), the pair's
beat; the decays by frequency (the two stages' losses, the knee's ratio); the hammer's speed per velocity; the
interpolators' and the preamp's coefficients (BASS / MIDDLE / TREBLE at 33 knob positions, BRILLIANCE's three, the
input high-pass and the Sallen-Key low-pass); the tremolo's constants.

Velocity layers: 9 for keys 1-60 (their amplitudes vary smoothly: -39..-49 dB interpolated, energy-weighted), 32 for
61-88 (fewer partials, but their fundamental crosses a dip of the hammer pulse's spectrum as the contact shortens).
"""
import argparse
import math
import os
import sys

import numpy as np

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import cp_model as m  # noqa: E402
import cp_multirate as e  # noqa: E402

FS = m.FS
ROM_LO, ROM_HI = 0xFFC00000, 0xFFCFFFFF
V9 = [1, 4, 10, 20, 34, 52, 74, 100, 127]
V32 = sorted(set(int(round(1 + 126 * x ** 1.6)) for x in np.linspace(0, 1, 33)))
DENSE_FROM = 61                  # keys from here: V32
Z_Q = 26                         # the engine's level: 1.0 (the model's full scale after ref_gain) = 2^26
LUT_F0, LUT_PER_OCT, LUT_N = 20.0, 16, 161    # the decays by frequency: 20 Hz .. 20.48 kHz, 1/16 octave
KNOBS = list(range(0, 128, 4)) + [127]        # the tone knobs' positions in the tables (0..127, 64 = 5 on the panel)


def rom_safe(v):
    u = v & 0xFFFFFFFF
    while ROM_LO <= u <= ROM_HI:
        v += 1 if v < 0 else -1
        u = v & 0xFFFFFFFF
    return v


def qi(x, bits):
    v = int(round(x * (1 << bits)))
    assert -(1 << 31) <= v < (1 << 31), (x, bits)
    return rom_safe(v)


def knob(v):
    """a 0..127 value to the panel's 0..10 (64 = 5)"""
    return 5.0 * v / 64 if v <= 64 else 5.0 + 5.0 * (v - 64) / 63


def vh(vel, p=m.P):
    h = p["hammer"]
    return h["vel_ms_min"] * (h["vel_ms_max"] / h["vel_ms_min"]) ** ((vel - 1) / 126.0)


def pack12(re, im):
    re, im = int(re) & 0xFFF, int(im) & 0xFFF
    return [re & 0xFF, (re >> 8) | ((im & 0xF) << 4), im >> 4]


def keys(p=m.P):
    """per key: its partials (band-sorted), their angles, the layers' packed starts and exponents"""
    e.PAIR = "cos"
    g = m.ref_gain(p)
    rows = []
    for k in range(1, 89):
        d = e.key_data(k, p)
        order = np.argsort(d["band"], kind="stable")
        f, band = d["f"][order], d["band"][order]
        H = np.array([e.chain_resp(fi, bi)[0] for fi, bi in zip(f, band)])
        inc = [int(round(fi * 2.0 ** bi / FS * 2 ** 32)) for fi, bi in zip(f, band)]
        V = V32 if k >= DENSE_FROM else V9
        amp, exps = [], []
        for vel in V:
            A, _ = e.amps(k, vel, p)
            z = A[order] * d["g"][order] * g / H / vh(vel, p) * 2 ** Z_Q
            mx = max(np.max(np.abs(z.real)), np.max(np.abs(z.imag)), 1e-30)
            ex = int(math.ceil(math.log2(mx / 2047.0)))
            q = np.round(z / 2.0 ** ex)
            for re, im in zip(q.real, q.imag):
                amp += pack12(max(min(re, 2047), -2048), max(min(im, 2047), -2048))
            exps.append(ex)
        nb = [int(np.sum(band == b)) for b in range(e.NB)]
        # the beat: beat Hz = f x kd (the pair's two strings, its mean f); cos(pi beat t)'s cycle a block, Q32 =
        # inc x kd x (128 >> b): kd Q32
        kd = float(np.median(d["beat"] / d["f"])) if d["n_str"] == 2 else 0.0
        rows.append(dict(k=k, inc=inc, nb=nb, amp=amp, exps=exps, nl=len(V), kd=kd, damped=d["damped"], f=f))
        print(f"key {k}: {len(f)} partials, bands {nb}", file=sys.stderr, flush=True)
    return rows


def decay_lut(p=m.P):
    """by log2 frequency (20 Hz up, 1/16 octave): the late stage's loss and the early's over it (Q40 a 44.1 kHz
    sample), the knee's ratio rho0 = (1 - w) / w (Q24)"""
    f = LUT_F0 * 2.0 ** (np.arange(LUT_N) / LUT_PER_OCT)
    se, sl = m.sigma(f, p), m.sigma(f, p, "t60_late")
    w = np.exp(-(se - sl) * p["decay"]["knee_s"])
    return [qi(x / FS, 40) for x in sl], [qi(x / FS, 40) for x in se - sl], [qi(x, 24) for x in (1 - w) / w]


def preamp(p=m.P):
    pre = m.Preamp(p)
    out = {}

    def q28(ba):
        b, a = ba
        b = list(b) + [0.0] * (3 - len(b))
        a = list(a) + [0.0] * (3 - len(a))
        return [qi(b[0], 28), qi(b[1], 28), qi(b[2], 28), qi(a[1], 28), qi(a[2], 28)]
    st = pre.stages(*pre.gains())
    out["hp"], out["sk"] = q28(st[0]), q28(st[5])
    for name, idx, arg in (("bass", 1, "bass"), ("middle", 2, "middle"), ("treble", 3, "treble")):
        out[name] = [q28(pre.stages(*pre.gains(**{arg: knob(v)}))[idx]) for v in KNOBS]
    out["bril"] = [q28(pre.stages(*pre.gains(bril=b))[4]) for b in ("LOW", "MEDIUM", "HIGH")]
    return out


def tremolo(p=m.P):
    tr = p["tremolo"]
    ph = np.linspace(0, 2 * math.pi, 200001)
    mx = np.max(np.abs(np.sin(ph) - tr["h3"] * np.sin(3 * ph)))
    d = []
    for v in range(128):
        mm = tr["depth_min"] + (tr["depth_max"] - tr["depth_min"]) * v / 127.0
        d.append(qi(2 * mm / (1 + mm), 30))
    a_on = 1 - math.exp(-1 / (FS * tr["ldr_on_ms"] * 1e-3))
    a_off = 1 - math.exp(-1 / (FS * tr["ldr_off_ms"] * 1e-3))
    return dict(h3=qi(tr["h3"], 30), norm=qi(1 / mx, 30), d=d, a_on=qi(a_on, 30), a_off=qi(a_off, 30))


def table(w, name, ctype, vals, per=12):
    w.append(f"static const {ctype} {name}[{len(vals)}] = {{")
    for i in range(0, len(vals), per):
        w.append("    " + ", ".join(str(v) for v in vals[i:i + per]) + ",")
    w.append("};")


def header(p=m.P):
    rows = keys(p)
    w = ["/* SPDX-License-Identifier: GPL-3.0-only */",
         "/* GENERATED by tools/gen_cp.py from docs/model/data/cp80.json: do not edit. The CP-80 engine's tables "
         "(strings.c). */",
         "#define CP_KEY_LO 21u                 /* MIDI: key 1 (A0) */",
         "#define CP_NKEY 88u",
         f"#define CP_NB {e.NB}u                     /* bands: 44.1 kHz / 2^b */",
         f"#define CP_NPMAX {max(len(r['inc']) for r in rows)}u                /* partials a key at most */",
         f"#define CP_ZQ {Z_Q}                      /* the level: 1.0 = 2^CP_ZQ */",
         f"#define CP_DENSE_FROM {DENSE_FROM}u           /* keys from here: CP_V32's layers, under it CP_V9's */"]
    # velocity -> layer and position (Q16), for both layer sets
    for nm, V in (("CP_VL9", V9), ("CP_VL32", V32)):
        lay, pos = [], []
        for vel in range(128):
            v = max(vel, 1)
            j = min(int(np.searchsorted(V, v, side="right")) - 1, len(V) - 2)
            lay.append(j)
            pos.append(int(round((v - V[j]) / (V[j + 1] - V[j]) * 65536)))
        table(w, nm + "_J", "uint8_t", lay, 32)
        table(w, nm + "_T", "uint32_t", pos, 16)
    table(w, "CP_VH", "int32_t", [qi(vh(max(v, 1), p), 16) for v in range(128)], 12)
    w.append("/* per key: its first partial, the partials per band (0..4), its layers, its first layer's exponent "
             "(CP_EXP), its amplitudes' first byte (CP_AMP: layer by layer, 3 bytes a partial, re / im 12-bit), the "
             "pair's beat over f (Q32: 0 one string), damped */")
    w.append("typedef struct { uint16_t p0; uint8_t nb[CP_NB]; uint8_t nl; uint16_t e0; uint32_t a0; uint32_t kd; "
             "uint8_t damped; } cp_key_t;")
    w.append("static const cp_key_t CP_KEYS[CP_NKEY] = {")
    p0 = e0 = a0 = 0
    incs, amps, exps = [], [], []
    for r in rows:
        w.append(f"    {{{p0}, {{{', '.join(str(x) for x in r['nb'])}}}, {r['nl']}, {e0}, {a0}, "
                 f"{int(round(r['kd'] * 2 ** 32))}u, {1 if r['damped'] else 0}}},   /* key {r['k']} */")
        p0 += len(r["inc"])
        e0 += r["nl"]
        a0 += len(r["amp"])
        incs += r["inc"]
        amps += r["amp"]
        exps += r["exps"]
    w.append("};")
    w.append("/* each partial's angle a sample of its band (Q32 of a cycle) */")
    table(w, "CP_INC", "uint32_t", [f"{x}u" for x in incs], 8)
    table(w, "CP_EXP", "int8_t", exps, 32)
    table(w, "CP_AMP", "uint8_t", amps, 36)
    sl, ds, r0 = decay_lut(p)
    w.append(f"#define CP_LUT_N {LUT_N}u                 /* the decays by log2 f: 20 Hz up, 1/{LUT_PER_OCT} octave */")
    w.append(f"#define CP_LUT_L0 {qi(math.log2(LUT_F0), 26)}          /* log2(20 Hz), Q26 */")
    table(w, "CP_SL", "int32_t", sl, 10)
    table(w, "CP_DS", "int32_t", ds, 10)
    table(w, "CP_R0", "int32_t", r0, 10)
    w.append(f"#define CP_DAMP {qi(6.91 / m.P['decay']['damper_t60_s'] / FS, 40)}          /* the damper's loss, Q40 a sample */")
    w.append(f"#define CP_L_FS {qi(math.log2(FS), 26)}            /* log2(44100), Q26 */")
    w.append("/* the interpolators (cp_multirate.HB_A0 / HB_A1): first-order all-pass coefficients, Q30 */")
    w.append(f"static const int32_t CP_HB[4] = {{{', '.join(str(qi(c, 30)) for c in e.HB_A0 + e.HB_A1)}}};")
    pa = preamp(p)
    w.append("/* the preamp's biquads (b0 b1 b2 a1 a2, Q28): the input high-pass, the Sallen-Key low-pass; BASS, MIDDLE, "
             f"TREBLE at the knob positions {KNOBS[0]}, {KNOBS[1]} .. {KNOBS[-2]}, {KNOBS[-1]} (0..127), BRILLIANCE "
             "LOW / MEDIUM / HIGH */")
    w.append(f"#define CP_NKNOB {len(KNOBS)}u")
    w.append(f"static const int32_t CP_HP[5] = {{{', '.join(str(x) for x in pa['hp'])}}};")
    w.append(f"static const int32_t CP_SK[5] = {{{', '.join(str(x) for x in pa['sk'])}}};")
    for nm in ("bass", "middle", "treble"):
        w.append(f"static const int32_t CP_{nm.upper()}[CP_NKNOB][5] = {{" +
                 ", ".join("{" + ", ".join(str(x) for x in r) + "}" for r in pa[nm]) + "};")
    w.append("static const int32_t CP_BRIL[3][5] = {" + ", ".join("{" + ", ".join(str(x) for x in r) + "}" for r in pa["bril"]) + "};")
    tr = tremolo(p)
    w.append("/* the tremolo (cp_model.tremolo): the oscillator's 3rd harmonic and its normalisation (Q30), the LDRs' "
             "lag (on, off: Q30 steps), the shunt's depth d = 2m / (1 + m) per DEPTH 0..127 (Q30) */")
    w.append(f"#define CP_TR_H3 {tr['h3']}")
    w.append(f"#define CP_TR_NORM {tr['norm']}")
    w.append(f"#define CP_TR_KON {tr['a_on']}")
    w.append(f"#define CP_TR_KOFF {tr['a_off']}")
    table(w, "CP_TR_D", "int32_t", tr["d"], 10)
    return "\n".join(w) + "\n"


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", default=os.path.join(ROOT, "firmware", "gen", "cp_tables.h"))
    a = ap.parse_args()
    open(a.out, "w").write(header())


if __name__ == "__main__":
    main()
