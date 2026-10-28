# SPDX-License-Identifier: GPL-3.0-only
"""The float model of the reed voice (the 200A / 140B TYPEs): what the C engine will compute, in double precision.

A felt hammer strikes a steel reed fixed at one end. The reed rings in its first two bending modes (the second near
6.3 x f0 for a cantilever, much shorter-lived). The reed's tip moves in a slot of the pickup, a comb of plates at a
high DC voltage: the reed and the plates make a capacitor whose value goes as 1 / (gap - x), so the signal is

    u / (1 - u),   u = the tip's displacement as a fraction of the gap

which is linear for small swings and grows its even harmonics with the strike: the 200A's bark. The pickup feeds
the preamp through a high-pass input network (it thins the bass fundamentals, as the line-out recordings show), and
the preamp saturates softly.

Per voice: 2 modes, the hammer pulse, the pickup. After the mix: the high-pass and the preamp (as in the
instrument, where every reed shares one pickup and one preamp). No tables of samples; the hammer pulse is a raised
half-sine computed from its length.

Optional, each only when P has its key (the bell3_v1 set has none of them and sounds as before):

- "keys" (key_table): every key's own regulation, fixed: the reed's rest point d, a level, a T60 and a tuning, seeded
  per key (jitter_of). The WoerliTzer line samples' scatter around a smooth curve over the keys: h2 ~7 dB, level
  ~1.5 dB, tuning ~3 cents (frodhes-refs tools/measure_keyvar.py). The level the new d brings is undone (the level
  scatter is set on its own: in the recordings h2 moves far more than the level).
- "damp2": the damper in two stages: the felt stops the reed fast (T60 damp_fast) down to damp_knee, then a slow
  tail (T60 damp_slow). BaltiWurli's release tails (E4, F#5, C#6): ~-20 dB in 20 ms, ~-40 dB at 50 ms, then
  ~-150 dB/s.
- "thump_db": the key's return, a knock heard at every key release: noise through two band-passes at thump_hz
  (the WoerliTzer key-release samples peak in the 250 Hz octave, -9 / -7 dB an octave either side), a 5 ms rise,
  T60 thump_t60; its peak thump_db under the key's mf (vel 80) note peak (BaltiWurli: release over note ~-25 dB).
  Added after the mix stage, through the speaker route only (it is heard, not picked up). No knock on the strike: neither the line nor the speaker
  recordings show one over the reed's own attack.
- trem_gain: the vibrato (the 200A's: a loudness wobble). In dB its cycle is not a sine: a narrow trough and a
  broad top, the 2nd harmonic ~-13 dB under the 1st, in antiphase (10 stretches of 200A recordings, 4.7-5.9 Hz,
  5-14 dB deep: frodhes-refs tools/vibrato_shape.py).
- trem_stage: the vibrato with the darkening of its loud phase (the 200A schematic: the LDR sets the preamp's gain,
  and with it its bandwidth; measured in 5 of 7 200A recordings: frodhes-refs tools/vibrato_bark.py).
- speaker: the 200A's own amp and speaker heard through a mic (the line signal through 5 biquads fitted on
  BaltiWurli's DI / speaker pairs: frodhes-refs tools/measure_speaker.py, fit_speaker.py).

Added 2026-10-27 for the v4 set (each optional too):

- fc 1 MHz: the input a first difference, the pickup a differentiator over the whole band, as the 200A schematic
  draws it (its few pF into ~330 kOhm): i = u0 dC/dt (Pfeifle, DAFx-17, eq. 11).
- "regulation" (key_table): one real instrument's per-key offsets (d, level, T60, tuning) measured on the WoerliTzer
  line samples (frodhes-refs tools/regulation_v4.py), in place of the hash's made-up ones.
- d / d_mid / d_hi: the reed's rest point per register (keys 33 / 64 / 96). Mind: the bell's slope is 0 at z = 0
  (a reed there gives pure h2) and its h2 vanishes at the inflection z^2 = 1 / (2p + 1).
- "tc_top" (tc_of): the top octaves' hammers stay on longer, a softer spike there.
- "snoise": the strike's action noise, in the line (snoise).
- "trem_ldr": the vibrato's LDR lag (ldr_db, ldr_comp in trem_stage).

Added for v4.1:

- "early" (reed): the early decay. The WoerliTzer line samples' notes start 3-5 dB over their sustain and fall to it
  in ~100-150 ms, at every key and every layer (h1 itself: +3-6 dB at the strike re 75 ms); one exponential gives
  none of it, and so too weak a strike. A second share of the first mode, w (w_lo at key 33 to w_hi at key 96) of
  the sustain's swing, with its own short T60.
- "tune": cents over every key (the TUNE knob); apply_knobs: the VOICE knobs (TONE BARK BELL HAMMER DECAY RELEASE
  NOISE TUNE REG) over a set; "reg": the REG knob's anchors (reg_keys).
- "tc_per" (tc_of): the hammer's contact held under tc_per periods of the key's f0. With one length for every key
  (~1.55 ms) the keys near 950 and 1600 Hz sat on the half-sine's nulls: a 16-28 dB click over a weak note.

Added for v4.3:

- "u_boost" (swing_of): the middle's swing raised up to one key and faded out over a few (v4.2's x1.3 had reached
  C5-B5 too: their h2 6 dB over v4's).
- "u_key" (swing_of): a factor per key; export_play's "tc_per_keep_swing" sets it so each key tc_per shortens keeps
  the swing (and so the h2) it had without (G#5 up: 11-17 dB of h2 over the real with tc_per alone).
- "attack" (reed, attack_w): the strike's knock, a share of the first mode gone in ~20 ms, keys 33 to "top": the
  spike 3 dB short of the WoerliTzer's at A1-B4, the bark (from 50 ms) untouched.
- "g_key" (voice, g_key): an output gain per key after the pickup (no timbre change): export_play's "level_like"
  sets it so each key's sustain is another set's (u_key gave back v4's weak notes at A#5, B5, G6, G#6).

    y = render(midi, vel, dur, P)       # one note, float, the mix stage included
    P = dict(DEFAULT)                   # the parameters (tools/fit_reed.py fits them against recordings)
"""
import hashlib
import math

import numpy as np
from scipy.signal import lfilter

FS = 44100.0

DEFAULT = {
    # the reed's swing at full strike as a fraction of the gap, at the lowest and highest key (log-interpolated)
    "u_lo": 0.55, "u_hi": 0.25,
    "gamma": 1.0,          # swing ~ (vel / 127) ** gamma
    "tc_soft": 2.5e-3,     # the hammer's contact time (s) at vel 1 and at vel 127 (a harder strike is shorter)
    "tc_hard": 0.9e-3,
    "r2": 6.3,             # the second mode's frequency / f0
    "w2": 0.25,            # the second mode's share of the strike
    "q2": 8.0,             # the second mode decays q2 times faster than the first
    "fc": 250.0,           # the input network's high-pass corner (Hz)
    "drive": 1.5,          # the preamp: tanh(drive x) / drive
    # T60 of the first mode (s): log(T60) quadratic in midi, from the WoerliTzer line samples; capped
    "t60_c": (5.47119450e-04, -1.14532759e-01, 6.90293196e+00), "t60_max": 30.0,
}


def interp_log(a, b, midi, mid=None):
    """a at midi 33, b at midi 96, geometric in between; with mid, two pieces through mid at midi 64."""
    if mid is None:
        t = min(max((midi - 33) / 63.0, 0.0), 1.0)
        return a * (b / a) ** t
    if midi <= 64:
        return interp_log(a, mid, 33 + (midi - 33) * 63.0 / 31.0)
    return interp_log(mid, b, 33 + (midi - 64) * 63.0 / 32.0)


def key_of(midi, P, what, default):
    """This key's own value of a "keys" quantity (key_table), or default."""
    k = P.get("keys")
    return k[what][midi - 33] if k and 33 <= midi <= 96 else default


def f0_of(midi, P):
    return 440.0 * 2 ** ((midi - 69) / 12.0 + (key_of(midi, P, "cents", 0.0) + P.get("tune", 0.0)) / 1200.0)


def t60_of(midi, P):
    if "t60_lo" in P:
        t = interp_log(P["t60_lo"], P["t60_hi"], midi, P["t60_mid"])
    else:
        t = min(math.exp(np.polyval(P["t60_c"], midi)), P["t60_max"])
    return t * key_of(midi, P, "t60", 1.0)


def d_of(midi, P):
    d = interp_log(P["d"], P["d_hi"], midi, P["d_mid"]) if "d_mid" in P else P["d"]
    return d * key_of(midi, P, "d", 1.0)


def swing_of(midi, P):
    """The full strike's swing (in the pickup's units) for this key. "u_boost" ({x, full, off}): x times it up to key
    full, faded (in log) to 1 at key off and over: the middle's bark raised without the treble's timbre. "u_key": 64
    factors, keys 33..96."""
    u = interp_log(P["u_lo"], P["u_hi"], midi, P.get("u_mid"))
    if "u_boost" in P:
        B = P["u_boost"]
        u *= B["x"] ** min(max((B["off"] - midi) / (B["off"] - B["full"]), 0.0), 1.0)
    if "u_key" in P and 33 <= midi <= 96:
        # per key (export_play "tc_per_keep_swing"): the swing the key had before tc_per, its timbre kept
        u *= P["u_key"][midi - 33]
    return u


def tc_of(vel, P, midi=None, ref=False):
    """The hammer's contact time (s). With "tc_top", the top two octaves' hammers stay on longer (x tc_top at key 96,
    log-interpolated from key 72): the WoerliTzer line samples' last octave spikes ~9 dB over its sustain, the
    plain model's ~13-18 dB. "hammer_x" (the HAMMER knob) times it, but not for ref (the reference hammer that
    sets the level: reed, _hard_gain)."""
    t = interp_log(P["tc_soft"], P["tc_hard"], 33 + 63 * (vel - 1) / 126.0)
    if midi is not None and "tc_top" in P and midi > 72:
        t *= P["tc_top"] ** ((min(midi, 96) - 72) / 24.0)
    if not ref:
        t *= P.get("hammer_x", 1.0)
    if midi is not None and "tc_per" in P:
        # the reed throws the hammer off within tc_per of its periods: no key's f0 sits on the pulse's spectral nulls
        # (a half-sine's first at 1.5 / t: there the reed keeps almost nothing of the strike but its click)
        t = min(t, P["tc_per"] / f0_of(midi, P))
    return t


def half_sine(t):
    """A half-sine pulse t s long (2 samples at least), its area 1."""
    n = max(int(round(t * FS)), 2)
    f = np.sin(np.pi * (np.arange(n) + 0.5) / n)
    return f / f.sum()


def hammer(vel, P, midi=None, ref=False):
    """The strike's force: a half-sine of the contact time, its area 1."""
    return half_sine(tc_of(vel, P, midi, ref))


def reed(midi, vel, n, P):
    """The tip's displacement, normalised so a full strike's first-mode swing is 1 (before the key's u)."""
    f0 = f0_of(midi, P)
    t = np.arange(n) / FS
    t60 = t60_of(midi, P)
    x = np.zeros(n)
    for f, w, T in ((f0, 1.0, t60), (f0 * P["r2"], P["w2"], t60 / P["q2"])):
        if f >= FS * 0.45:
            continue
        # a mode's impulse response, scaled so its displacement per unit impulse is w at any f (the reed's
        # stiffness sets the first mode's swing; the hammer pulse filters what reaches each mode)
        x += w * np.exp(-6.907755 * t / T) * np.sin(2 * np.pi * f * t)
    h = hammer(vel, P, midi)
    y = np.convolve(x, h)[:n]
    # normalise on a full, short strike so the strike's spectrum, not its area, sets the level
    g = interp_log(P["gamma"], P["gamma_hi"], midi) if "gamma_hi" in P else P["gamma"]
    # (with the HAMMER knob, on the reference hammer's strike: a softer felt then passes less of the strike, darker
    # and quieter; a harder one more)
    yr = np.convolve(x, hammer(vel, P, midi, ref=True))[:n] if P.get("hammer_x", 1.0) != 1.0 else y
    pk = max(np.abs(yr[: int(0.05 * FS)]).max(), 1e-12)
    bx = P.get("bell_x", 0.0)
    if bx > 0 and f0 * P["r2"] < FS * 0.45:
        # the BELL knob's ping: the second mode once more, struck by a pulse short enough (0.25 ms, at most half its
        # period) that no hammer null swallows it, over the normalisation as "early"
        fb = f0 * P["r2"]
        xb = bx * np.exp(-6.907755 * t / (t60 / P["q2"])) * np.sin(2 * np.pi * fb * t)
        y = y + np.convolve(xb, half_sine(min(2.5e-4, 0.5 / fb)))[:n]
    if "early" in P:
        # the early decay: a share of the first mode that dies in early_t60, over the sustain's level (the
        # normalisation above leaves it out, so the sustain stays where the fit put it)
        E = P["early"]
        xe = interp_log(E["w_lo"], E["w_hi"], midi) * np.exp(-6.907755 * t / E["t60"]) * np.sin(2 * np.pi * f0 * t)
        y = y + np.convolve(xe, h)[:n]
    w = attack_w(midi, P)
    if w > 0:
        # the strike's knock: a share of the first mode gone within attack t60 (a few ms), over the normalisation
        # as "early": the spike rises, the tone from 50 ms on (its harmonics, the bark) stays
        xa = w * np.exp(-6.907755 * t / P["attack"]["t60"]) * np.sin(2 * np.pi * f0 * t)
        y = y + np.convolve(xa, h)[:n]
    return y / pk * (vel / 127.0) ** g * _hard_gain(midi, P)


def g_key(midi, P):
    """The key's own output gain over the "keys" table's (export_play "level_like"; REG leaves it alone)."""
    return P["g_key"][midi - 33] if "g_key" in P and 33 <= midi <= 96 else 1.0


def attack_w(midi, P):
    """The "attack" share for this key ({w_lo, w_mid, top, t60}): w_lo at key 33 to w_mid at key 64 (log), faded
    linearly to 0 at key top."""
    if "attack" not in P:
        return 0.0
    A = P["attack"]
    if midi <= 64:
        return interp_log(A["w_lo"], A["w_mid"], 33 + (midi - 33) * 63.0 / 31.0)
    return A["w_mid"] * min(max((A["top"] - midi) / (A["top"] - 64.0), 0.0), 1.0)


_HG = {}


def _hard_gain(midi, P):
    """1 / the first 50 ms peak of a full-velocity strike (so u is the swing at vel 127)."""
    f0 = f0_of(midi, P)
    key = (f0, tc_of(127, P, midi, ref=True), P["r2"], P["w2"], P["q2"])
    if key not in _HG:
        n = int(0.05 * FS)
        t = np.arange(n) / FS
        x = np.sin(2 * np.pi * f0 * t)
        if f0 * P["r2"] < FS * 0.45:
            x = x + P["w2"] * np.sin(2 * np.pi * f0 * P["r2"] * t)
        y = np.convolve(x, hammer(127, P, midi, ref=True))[:n]
        _HG[key] = 1.0 / max(np.abs(y).max(), 1e-12)
    return _HG[key]


def pickup(x, midi, P):
    """The capacitance's change for a tip swing x (1 = a full strike).

    "gap": the tip nears a plate, C ~ 1 / (gap - x): u / (1 - u), u kept under 0.95.
    "bell": the tip passes through the comb's slot, C follows the overlap of tip and plates, a bell in the tip's
    height z (1 / (1 + z^2), z in the overlap's half-width) whose rest point sits d off its centre: small swings
    are nearly linear, a hard strike crosses the bell and folds the wave (every harmonic grows)."""
    u = x * swing_of(midi, P)
    if P.get("pickup", "gap") == "gap":
        u = np.minimum(u, 0.95)
        return u / (1.0 - u)
    d = d_of(midi, P)
    p = P.get("bell_p", 1.0)        # the bell's sharpness: (1 + z^2)^-p (1: the plain bell; higher: narrower, more
    bell = lambda z: (1.0 + z * z) ** -p  # noqa: E731   # high harmonics as a strike crosses it)
    return (bell(d) - bell(u + d)) * (1 + d * d) ** (p + 1) / (2 * p * d)   # slope 1 at rest


def mix_stage(y, P):
    """The input high-pass and the preamp, shared by every voice."""
    a = math.exp(-2 * math.pi * P["fc"] / FS)
    y = lfilter([(1 + a) / 2, -(1 + a) / 2], [1, -a], y)
    d = P["drive"]
    return np.tanh(d * y) / d


def damper(n, P):
    """The damper's envelope on the reed's swing, n samples from the release."""
    t = np.arange(n) / FS
    if "damp2" not in P:
        return np.exp(-6.907755 * t / P.get("t60_rel", 0.12))
    D = P["damp2"]
    fast = np.exp(-6.907755 * t / D["fast"])
    knee = 10 ** (D["knee_db"] / 20)
    tk = D["fast"] * D["knee_db"] / -60.0             # when the fast stage reaches the knee
    return np.where(t < tk, fast, knee * np.exp(-6.907755 * (t - tk) / D["slow"]))


def voice(midi, vel, dur, P=DEFAULT, off=None):
    """One reed's share of the pickup signal, before the mix stage. off: the key's release (s from the strike);
    the damper's felt then stops the reed (damper) before the pickup reads it."""
    n = int(dur * FS)
    x = reed(midi, vel, n, P)
    if off is not None and off * FS < n:
        i = int(off * FS)
        x[i:] *= damper(n - i, P)
    return pickup(x, midi, P) * key_of(midi, P, "g", 1.0) * g_key(midi, P)


def lcg(seed, n):
    """White noise in [-1, 1): a 32-bit LCG (Numerical Recipes), as the C engine and the play page compute it."""
    out = np.empty(n)
    s = seed & 0xFFFFFFFF
    for i in range(n):
        s = (1664525 * s + 1013904223) & 0xFFFFFFFF
        out[i] = (s >> 8) / 8388608.0 - 1.0
    return out


def biquad_bp(f, q):
    """RBJ band-pass, 0 dB at f."""
    w = 2 * math.pi * f / FS
    al = math.sin(w) / (2 * q)
    a0 = 1 + al
    return [al / a0, 0.0, -al / a0], [1.0, -2 * math.cos(w) / a0, (1 - al) / a0]


def thump(midi, P, n):
    """The key's return knock (n samples), its peak at the key's "ref" level (key_table)."""
    T = P["thump"]
    x = lcg(midi * 2654435761, n)
    b, a = biquad_bp(T["hz"], T["q"])
    x = lfilter(b, a, lfilter(b, a, x))
    t = np.arange(n) / FS
    env = np.minimum(t / 0.005, 1.0) * np.exp(-6.907755 * t / T["t60"])
    return x * env * T["norm"] * key_of(midi, P, "ref", 0.0) * 10 ** (T["db"] / 20)


def snoise_db(midi, S):
    """The action noise's level (dB re the tone's first 50 ms RMS at vel 127): S["db_lo"] at key 40 to S["db_hi"] at
    key 78, linear, held beyond."""
    t = min(max((midi - 40) / 38.0, 0.0), 1.0)
    return S["db_lo"] + (S["db_hi"] - S["db_lo"]) * t


def snoise_raw(midi, S, n):
    """The action noise's shape (n samples, unscaled): LCG noise (its own seed per key) through two band-passes at
    S["hz"], a rise of S["rise"] s and a T60 of S["t60"] s."""
    x = lcg((midi * 2654435761) ^ 0x5BD1E995, n)
    b, a = biquad_bp(S["hz"], S["q"])
    x = lfilter(b, a, lfilter(b, a, x))
    t = np.arange(n) / FS
    return x * np.minimum(t / S["rise"], 1.0) * np.exp(-6.907755 * t / S["t60"])


def snoise_vel(vel, S):
    """The action noise's share at this velocity: none up to vel0, all from vel1 on, linear between."""
    return min(max((vel - S["vel0"]) / (S["vel1"] - S["vel0"]), 0.0), 1.0)


def snoise(midi, vel, P, n):
    """The strike's action noise (n samples), in the line signal after the preamp: the key and the hammer's travel
    through the reed bar, picked up. The WoerliTzer line samples: from mf up a burst of 8-18 ms ahead of the tone,
    ~300 Hz, -11 (bass) to -20 dB (treble) under the tone's first 50 ms at the loudest layer; none in the soft layers
    (frodhes-refs tools/measure_regulation.py). Here it starts with the note (a lead would delay the tone: latency).
    Its gain per key ("sn" in key_table) sets the level at vel 127."""
    S = P["snoise"]
    return snoise_raw(midi, S, n) * key_of(midi, P, "sn", 0.0) * snoise_vel(vel, S)


TREM_DB = 20.0       # the vibrato's depth at the knob's top (the recordings: 5-14 dB)
TREM_H2 = 0.22       # its loudness cycle's 2nd harmonic re the 1st, in antiphase (-13 dB)


def trem_gain(phase, depth, h2=TREM_H2):
    """The vibrato's gain at phase (cycles; the trough at 0) for depth 0..1: in dB -depth * TREM_DB * dip, dip 0 at
    the top and 1 in the trough, -(cos x + h2 cos 2x) rescaled. h2 0: the plain sine some restored and custom
    instruments show (the Instagram reels of 2026-10-27: h2 -19 to -29 dB, its phase anywhere)."""
    x = 2 * np.pi * np.asarray(phase)
    dip = (1 + np.cos(x) + h2 * (np.cos(2 * x) - 1)) / 2
    return 10 ** (-depth * TREM_DB * dip / 20)


def speaker_biquads(S):
    """The 200A's speaker (S from frodhes-refs tools/fit_speaker.py, measured on BaltiWurli's DI / speaker pairs):
    RBJ high-pass, low peak, mid peak (a dip), presence peak, low-pass, as (b, a) pairs; the gain apart."""
    def rbj(kind, f, q, db=0.0):
        w = 2 * math.pi * f / FS
        c, al, A = math.cos(w), math.sin(w) / (2 * q), 10 ** (db / 40)
        if kind == "hp":
            b, a = [(1 + c) / 2, -(1 + c), (1 + c) / 2], [1 + al, -2 * c, 1 - al]
        elif kind == "lp":
            b, a = [(1 - c) / 2, 1 - c, (1 - c) / 2], [1 + al, -2 * c, 1 - al]
        else:
            b, a = [1 + al * A, -2 * c, 1 - al * A], [1 + al / A, -2 * c, 1 - al / A]
        return [x / a[0] for x in b], [x / a[0] for x in a]
    return [rbj("hp", S["hp_hz"], S["hp_q"]), rbj("pk", S["lo_hz"], S["lo_q"], S["lo_db"]),
            rbj("pk", S["mid_hz"], S["mid_q"], S["mid_db"]), rbj("pk", S["pr_hz"], S["pr_q"], S["pr_db"]),
            rbj("lp", S["lp_hz"], S["lp_q"])]


def speaker_response(S, f):
    """The speaker's complex response at the frequencies f (Hz)."""
    z = np.exp(-2j * np.pi * np.asarray(f) / FS)
    H = np.full(len(z), 10 ** (S["gain_db"] / 20), complex)
    for b, a in speaker_biquads(S):
        H *= (b[0] + b[1] * z + b[2] * z * z) / (a[0] + a[1] * z + a[2] * z * z)
    return H


def speaker(y, S):
    """The line signal through the speaker (after the preamp and the vibrato, as in the instrument)."""
    for b, a in speaker_biquads(S):
        y = lfilter(b, a, y)
    return y * 10 ** (S["gain_db"] / 20)


TREM_LP_HZ = 1000.0  # the vibrato's darkening low-pass (trem_stage), calibrated by frodhes-refs tools/vibrato_bark.py


def trem_dip(phase, h2):
    """The vibrato's dip, 0 at the top and 1 in the trough (phase 0)."""
    x = 2 * np.pi * np.asarray(phase)
    return (1 + np.cos(x) + h2 * (np.cos(2 * x) - 1)) / 2


def ldr_db(target, on, off, s0=0.0):
    """The LDR's lag on the vibrato's gain (dB, per sample): toward a louder gain (the LED lighting the cell) with
    the time constant on, toward a quieter one (the cell going dark) with off. A CdS cell lights in a few ms and
    goes dark in tens of ms (typical datasheet figures, VTL5C class: not measured on a 200A)."""
    kon, koff = 1 - math.exp(-1 / (on * FS)), 1 - math.exp(-1 / (off * FS))
    out = np.empty(len(target))
    sv = s0
    for i, t in enumerate(target):
        sv += (t - sv) * (kon if t > sv else koff)
        out[i] = sv
    return out


def ldr_comp(h2, on, off, rate=5.4):
    """The drive (depth scale C, 2nd harmonic h2d) whose LDR-lagged cycle at the factory rate is the measured one
    (trem_dip with h2, TREM_DB deep): the lag then only shows away from 5.4 Hz (shallower and rounder above, the
    drive's own shape below). Least squares over a cycle, the lag's delay aside."""
    from scipy.optimize import minimize
    per = int(round(FS / rate))
    ph = (np.arange(per * 8) % per) / per
    want = -TREM_DB * trem_dip(np.arange(per) / per, h2)

    def err(th):
        got = ldr_db(-TREM_DB * th[0] * trem_dip(ph, th[1]), on, off)[-per:]
        return min(((np.roll(got, -k) - want) ** 2).mean() for k in range(0, per // 4, 8))
    r = minimize(err, [1.2, h2], method="Nelder-Mead", options={"xatol": 1e-4, "fatol": 1e-6})
    return [float(r.x[0]), float(r.x[1]), float(math.sqrt(r.fun))]


def trem_stage(y, depth, rate, h2=TREM_H2, lp_hz=TREM_LP_HZ, ldr=None):
    """The vibrato on the line signal, as the 200A's LDR does it: the gain of the reed-bar preamp rises over the
    cycle and, as its bandwidth falls with the gain (the 100 pF Miller caps of TR-1 / TR-2), the loud phase is the
    darker one (200A recordings: 2.6-5.3 dB less over 1.5 kHz in the loud phase, ~0.44 x the loudness swing). A
    one-pole low-pass at lp_hz mixed in by m = 1 - 1 / r, r the gain over the cycle's trough: no vibrato (depth 0)
    or the trough is the plain line signal.

    ldr (a set's "trem_ldr": on, off, comp {h2: [C, h2d, rms]}): the cell's lag (ldr_db) on a drive compensated so
    the factory rate keeps the measured cycle; the phase advances before each sample, as the play page does it."""
    n = len(y)
    if ldr is None:
        g = trem_gain(np.arange(n) * rate / FS, depth, h2)
    else:
        C, h2d = ldr["comp"].get("%g" % h2, [1.0, h2])[:2]
        ph = np.empty(n)
        p, dt = 0.0, rate / FS
        for i in range(n):
            p += dt
            if p >= 1:
                p -= 1
            ph[i] = p
        g = 10 ** (ldr_db(-depth * TREM_DB * C * trem_dip(ph, h2d), ldr["on"], ldr["off"]) / 20)
    r = g / 10 ** (-depth * TREM_DB / 20)
    m = np.maximum(1.0 - 1.0 / r, 0.0)
    a = math.exp(-2 * math.pi * lp_hz / FS)
    lp = lfilter([1 - a], [1, -a], y)
    return (y + (lp - y) * m) * g


def render(midi, vel, dur, P=DEFAULT, pre=0.01):
    v = np.concatenate([np.zeros(int(pre * FS)), voice(midi, vel, dur, P)])
    return mix_stage(v, P)


def render_notes(notes, dur, P=DEFAULT, spk=None):
    """notes: (start s, midi, vel, length s); every voice summed, then the shared mix stage (and with spk, the
    speaker: speaker_biquads); with spk the knocks after it (heard in the room: the line does not pick them up).

    One reed per key: a key struck again while its reed still sounds adds the new strike to the same motion (the
    damper lifts at the strike), and the pickup reads the sum. The damper of a release acts on everything the reed
    carries, from the release to the key's next strike."""
    n = int(dur * FS)
    y = np.zeros(n)
    kn = np.zeros(n)
    sn = np.zeros(n)
    by = {}
    for t0, midi, vel, ln in notes:
        by.setdefault(midi, []).append((int(round(t0 * FS)), int(round((t0 + ln) * FS)), vel))   # round: a float time a hair short of a sample is that sample
    for midi, ns in by.items():
        ns.sort()
        x = np.zeros(n)
        for j, (i0, i1, vel) in enumerate(ns):
            x[i0:] += reed(midi, vel, n - i0, P)
            if "snoise" in P and snoise_vel(vel, P["snoise"]) > 0:
                k = snoise(midi, vel, P, min(int(0.1 * FS), n - i0))
                sn[i0:i0 + len(k)] += k
            if i1 < n:
                nxt = ns[j + 1][0] if j + 1 < len(ns) else n
                if i1 < nxt:
                    env = damper(n - i1, P)
                    env[nxt - i1:] = env[nxt - i1 - 1]       # the damper lifts again at the next strike
                    x[i1:] *= env
        y += pickup(x, midi, P) * key_of(midi, P, "g", 1.0) * g_key(midi, P)
        if spk and "thump" in P:
            for i1 in sorted({i1 for _, i1, _ in ns}):       # one knock per release of the key
                if i1 < n:
                    k = thump(midi, P, min(int(0.5 * FS), n - i1))
                    kn[i1:i1 + len(k)] += k
    y = mix_stage(y, P) + sn          # the action noise: picked up, so on the line too
    # out_gain: a set's fixed output level (export_play: its loudest chord peaks where bell3_v2's does)
    return ((speaker(y, spk) + kn) if spk else y) * P.get("out_gain", 1.0)


# ---- the per-key regulation (the "keys" table), generated once per parameter set

def jitter_of(seed, midi, what):
    """A deterministic number in [-1, 1) from the seed, the key and the quantity (as gen_rhodes.py jit)."""
    h = hashlib.sha256(("%s:%d:%s" % (seed, midi, what)).encode()).digest()
    return int.from_bytes(h[:8], "little") / 2.0 ** 63 - 1.0


def _peak(y):
    """The 1 ms RMS peak over the first 200 ms (fit_reed's "peak")."""
    w = int(0.001 * FS)
    y = y[: int(0.2 * FS)]
    return math.sqrt(np.convolve(y * y, np.ones(w) / w, mode="same").max())


def key_table(P):
    """The "keys" table: per key 33..96 d (x), g (gain), t60 (x), cents, ref (the key's mf note peak after the mix
    stage, the knock's reference) and, with "snoise", sn (the action noise's gain).

    From P["regulation"] (measured: lists over the 64 keys of d_ln, level_db, t60_x, cents; frodhes-refs
    tools/fit_reed.py v4 writes them from the WoerliTzer line samples, one real instrument's regulation) or else from
    P["jitter"] (seed, d_ln, level_db, t60_pct, cents: a made-up instrument, every key's offsets from the hash)."""
    base = {k: v for k, v in P.items() if k != "keys"}
    tab = {"d": [], "g": [], "t60": [], "cents": [], "ref": []}
    R, J = P.get("regulation"), P.get("jitter")
    lvl = []
    for i, m in enumerate(range(33, 97)):
        if R:
            tab["d"].append(math.exp(R["d_ln"][i]))
            tab["t60"].append(R["t60_x"][i])
            tab["cents"].append(R["cents"][i])
            lvl.append(R["level_db"][i])
        else:
            j = lambda w: jitter_of(J["seed"], m, w)  # noqa: E731
            tab["d"].append(math.exp(J["d_ln"] * j("d")))
            tab["t60"].append(1.0 + J["t60_pct"] / 100.0 * j("t60"))
            tab["cents"].append(J["cents"] * j("cents"))
            lvl.append(J["level_db"] * j("level"))
        tab["g"].append(1.0)
        tab["ref"].append(0.0)
    for i, m in enumerate(range(33, 97)):
        # the gain that keeps the key's vel-100 level where its own smooth d had it, times the level offset
        one = {**base, "keys": {q: [tab[q][i] if q in ("d",) else (1.0 if q != "cents" else 0.0)] * 64 for q in tab}}
        a = _peak(voice(m, 100, 0.25, base))
        b = _peak(voice(m, 100, 0.25, one))
        tab["g"][i] = a / b * 10 ** (lvl[i] / 20)
    keyed = {**base, "keys": tab}
    for i, m in enumerate(range(33, 97)):
        tab["ref"][i] = _peak(render(m, 80, 0.25, keyed, pre=0.0))
    if "snoise" in P:
        # the noise's 12 ms RMS at snoise_db under the tone's first 50 ms RMS, at vel 127
        S = P["snoise"]
        tab["sn"] = []
        for i, m in enumerate(range(33, 97)):
            tone = render(m, 127, 0.06, keyed, pre=0.0)[: int(0.05 * FS)]
            raw = snoise_raw(m, S, int(0.012 * FS))
            tab["sn"].append(math.sqrt((tone ** 2).mean() / (raw ** 2).mean()) * 10 ** (snoise_db(m, S) / 20))
    return tab


# ---- the voice's knobs (the play page's VOICE cards; the FM-1's VOICE pages later), as the Rhodes': 0.0-10.0, 5.0
# the set as fitted, every knob read at the next strike

KNOBS = {"tone": 5.0, "bark": 5.0, "bell": 5.0, "hammer": 5.0, "decay": 5.0, "release": 5.0, "noise": 5.0,
         "tune": 0.0, "reg": 10.0}


BELL_MAX = 0.3      # the BELL ping's swing at knob 10, re the sustain's (reed): about +2 dB of peak, a 10 dB spike


def reg_keys(P, r):
    """The "keys" table at REG r (0-10) from the set's anchors (P["reg"]: export_play from tools/reg_tables.py):
    10 the measured regulation (the set's own table), 0 every key on the smooth curves. Between two anchors d, g
    and t60 interpolate in log, cents, ref and sn linearly."""
    R = P["reg"]
    a = min(max(r / 10.0, 0.0), 1.0)
    amts = R["amt"]
    i = 0
    while i < len(amts) - 2 and a > amts[i + 1]:
        i += 1
    t = (a - amts[i]) / (amts[i + 1] - amts[i])
    A, B = R["tables"][i], R["tables"][i + 1]
    out = {}
    for q in A:
        if q in ("d", "g", "t60"):
            out[q] = [math.exp((1 - t) * math.log(x) + t * math.log(y)) for x, y in zip(A[q], B[q])]
        else:
            out[q] = [(1 - t) * x + t * y for x, y in zip(A[q], B[q])]
    return out


def apply_knobs(P, K):
    """P with the knobs K (KNOBS' keys; missing ones at their default) applied; x = (knob - 5) / 5:
    TONE the pickup's bell sharpness bell_p x 2^x (the plates' shape: rounder down, more mids and edge up, the level
    kept by the bell's slope-1 normalisation); BARK the swing u x 2^(0.75 x) (more growl and more output); BELL
    under 5 the second mode's share w2 x (knob / 5)^2 (0: none), over 5 its ping (reed: bell_x up to BELL_MAX); HAMMER
    the contact time x 2^-x on the level of the reference hammer (tc_of: softer passes less, darker and quieter;
    harder more); DECAY the T60s x 2^x; RELEASE the damper's T60s x 3^x (higher: longer);
    NOISE the action noise and the key-return knock x (knob / 5)^1.5 (0: none); TUNE cents (-50..50); REG the
    regulation (reg_keys; a set without anchors keeps its table)."""
    K = {**KNOBS, **K}
    x = {k: (K[k] - 5.0) / 5.0 for k in KNOBS if k not in ("tune", "reg")}
    Q = dict(P)
    Q["bell_p"] = Q.get("bell_p", 1.0) * 2 ** x["tone"]
    for n in ("u_lo", "u_mid", "u_hi"):
        if Q.get(n) is not None:
            Q[n] = Q[n] * 2 ** (0.75 * x["bark"])
    if K["bell"] <= 5.0:
        Q["w2"] = Q["w2"] * (K["bell"] / 5.0) ** 2
    else:
        Q["bell_x"] = BELL_MAX * x["bell"] ** 2
    Q["hammer_x"] = 2 ** -x["hammer"]
    for n in ("t60_lo", "t60_mid", "t60_hi"):
        if n in Q:
            Q[n] = Q[n] * 2 ** x["decay"]
    if "damp2" in Q:
        Q["damp2"] = dict(Q["damp2"], fast=Q["damp2"]["fast"] * 3 ** x["release"],
                          slow=Q["damp2"]["slow"] * 3 ** x["release"])
    Q["t60_rel"] = Q.get("t60_rel", 0.12) * 3 ** x["release"]
    Q["tune"] = K["tune"]
    if "reg" in Q and K["reg"] != 10.0:
        Q["keys"] = reg_keys(Q, K["reg"])
    n = (K["noise"] / 5.0) ** 1.5
    if "keys" in Q and "sn" in Q["keys"]:
        Q["keys"] = dict(Q["keys"], sn=[s * n for s in Q["keys"]["sn"]])
    if "thump" in Q:
        if n > 0:
            Q["thump"] = dict(Q["thump"], db=Q["thump"]["db"] + 20 * math.log10(n))
        else:
            Q.pop("thump")
    return Q
