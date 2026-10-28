# SPDX-License-Identifier: GPL-3.0-only
"""tools/hil_scan.py: the rows that failed on every version (2026-10-24, the faster release test plan: no permanent
red). Each was decided from the saved FM-1 reports (build/hil, build/marimba/p5, build/pr4test, build/release-0.9.4,
build/final) and the host build, which reads the same numbers as the FM-1's USB:

- fx_all: the phaser has no width compensation (the chorus' -2.5 LU target judged 'phaser 127 fb127'); 'drive 127'
  is judged against the level the effect sets by design (FXALL_EXPECT; 'phaser 32' was too, at -2.5, until the
  phaser's level was held at every DEPTH in 0.9.6: now 0 +- 1); 'wah 32' / 'wah 127' failed
  (-2.1 / +2.1 LU): fx.c's wah depth law was applied on top of the 0.9.3 make-up gain, a defect fixed 2026-10-24
  (the depth law removed; test_fx_all_wah_level below);
- sdist: the jack's distances over the bands the sound has on USB (the jack's own harmonics diluted them)."""
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "tools"))
sys.path.insert(0, os.path.dirname(__file__))
import hil_scan as h  # noqa: E402


# ---- the jack's harmonics and sdist
def test_sdist_on_usbs_bands_ignores_bands_both_takes_share():
    """two takes that differ in two partials; the jack adds harmonics of its own at -50 .. -57 dB, the same in both
    (FM-1, F3 v100: H2 -56, H3 -50, H5 -57 dB re the strike, absent on USB): sdist's mean over every band within 60 dB
    dilutes the distance; over USB's bands the jack reads what USB reads"""
    a = np.full(26, -100.0)
    a[[5, 6, 11, 16, 17]] = [0, -35, -19, -20, -48]
    b = a.copy()
    b[[16, 17]] = [-16, -41]                       # MALLET 64 -> 127 on F3 (USB, the FM-1): +4 and +7 dB
    ja, jb = a.copy(), b.copy()
    for k, v in ((8, -56), (10, -50), (12, -57), (13, -55), (14, -54)):
        ja[k] = jb[k] = v                          # the jack's harmonics, the same in both takes
    usb = h.sdist(a, b)
    assert usb >= 3.0
    assert h.sdist(ja, jb) < 3.0                   # the row's FAIL on the jack
    assert abs(h.sdist(ja, jb, h.sdist_bands(a, b)) - usb) < 0.05
    assert h.sdist(a, b, h.sdist_bands(a, b)) == usb


# ---- the host build, played as the suites play the FM-1
def test_fx_all_on_the_host_passes():
    """every fx_all setting within its limit; 'drive 127' at its FXALL_EXPECT value, 'phaser 32' at 0 +- 1 (0.9.5 read
    -2.5: the phaser's level drop, fixed in 0.9.6), 'phaser 127 fb127' at 0 +- 2 (no width target)"""
    r, rep = h.HostRig(1), {}
    ok, msg = h.fx_all(r, rep)
    rows = {q["name"]: q["usb_lu"] for q in rep["fx_all"]["rows"]}
    for name, (target, tol) in h.FXALL_EXPECT.items():
        assert abs(rows[name] - target) <= tol, (name, rows[name])
    assert abs(rows["phaser 127 fb127"]) <= 2.0
    assert abs(rows["phaser 32"]) <= 1.0, rows["phaser 32"]
    assert ok and "out of tolerance: none" in msg, msg


def test_fx_all_wah_level():
    """the wah holds the clean level at every depth (fx.c fx_trem: the 0.9.3 make-up gain alone; the depth law wg of
    dba3831, x0.80 .. x1.26 on top of it, read -2.1 / +2.1 LU at DEPTH 32 / 127; without it -0.1 / +0.0)"""
    r, rep = h.HostRig(1), {}
    h.fx_all(r, rep)
    rows = {q["name"]: q["usb_lu"] for q in rep["fx_all"]["rows"]}
    assert abs(rows["wah 32"]) <= 0.5 and abs(rows["wah 127"]) <= 0.5, rows


@pytest.mark.parametrize("engine, notes", [
    ("rhodes", ((48, 108), (55, 108), (64, 108))), ("rhodes", ((60, 100),)), ("rhodes", ((72, 100),)),
])
def test_wah_level_every_depth(engine, notes):
    """the wah's loudness re dry (LUFS, as fx_all reads it) at DEPTH 0 .. 127, RATE 64: within 0.5 LU of dry and
    within 0.5 LU across the depths, on the Rhodes, chords and single notes (the depth law wg gave -2.9 .. +2.0 LU on
    the Rhodes chord). Measured: the Rhodes chord -0.1 .. +0.1, C4 0.0, C5 -0.2"""
    r = h.HostRig(1)
    sel = dict(TYPE=8)
    ev = [(0.1, "on", n, v) for n, v in notes] + [(2.1, "off", n, 0) for n, v in notes]

    def lv(**kv):
        r.dry(**sel, **kv)
        u = r.take(2.6, ev) / 32768.0
        a = np.abs(u).max(axis=1)
        i = int(np.argmax(a > 0.3 * a.max()))
        return h.lufs(u[i:i + int(1.8 * h.FS)])
    base = lv()
    d = [lv(TW_SW=127, TW_DEPTH=x, TW_RATE=64) - base for x in (0, 32, 64, 100, 127)]
    assert max(abs(x) for x in d) <= 0.5, d
    assert max(d) - min(d) <= 0.5, d


@pytest.mark.parametrize("engine, keys", [
    ("rhodes", (36, 48, 60, 72, 84, 96)),
])
def test_wah_level_across_the_keyboard(engine, keys):
    """single notes C2 .. C7 at v40 / v100, RATE 0 / 127 (the resonance), DEPTH 64: within 1.0 LU of dry. The make-up's
    46 ms envelopes and x8 ceiling lost a soft high note under a closed resonant wah (Rhodes C6 v40 RATE 127 -3.4,
    C7 -10.6 LU); now the worst is 0.7 LU"""
    r = h.HostRig(1)
    sel = dict(TYPE=8)

    def lv(k, v, **kv):
        r.dry(**sel, **kv)
        u = r.take(2.6, [(0.1, "on", k, v), (2.1, "off", k, 0)]) / 32768.0
        a = np.abs(u).max(axis=1)
        i = int(np.argmax(a > 0.3 * a.max()))
        return h.lufs(u[i:i + int(1.8 * h.FS)])
    bad = {}
    for k in keys:
        for v in (40, 100):
            base = lv(k, v)
            for rate in (0, 127):
                d = lv(k, v, TW_SW=127, TW_DEPTH=64, TW_RATE=rate) - base
                if abs(d) > 1.0:
                    bad[(k, v, rate)] = round(d, 2)
    assert not bad, bad


def test_presets_on_the_host_every_preset_on_target():
    """hil_scan's presets suite on the host (the FM-1's USB to 0.1 LU): F01..F17, each preset re F01 within its
    PRESET_TARGET (the Rhodes F01..F11 0 +- 1.5, F12 AUTO WAH's deep sweep 0 +- 2.0, F13..F15 0 +- 1.0: the starters
    U10..U12 until 0.9.5's RC; the 200A's F16 / F17 0 +- 1.5).
    Before 2026-10-24: F07 -1.9, F08 -1.7, F10 -3.9, F09 +1.47; the 0.9.5 presets: F02..F12 -0.7..+0.8. 2026-10-25: F11 LEVEL 18 -> 10 (the FM-1 read +1.8, the
    host -0.1; now the host -1.6, judged at HOST_SHIFT's -1.9). The drive-type merge: F03 +1.1, F07 +1.3, F08 +1.1 (driven:
    MANUAL 'Factory presets', within 1.5 LU; the 1 LU is the chord-matched ones', not a limit for every Rhodes preset).
    0.9.5: the user bank's starters U01..U09 follow (presets 18..26 in the rows), each within 1 LU of F01"""
    r, rep = h.HostRig(1), {}
    ok, msg = h.presets(r, rep)
    every = list(range(1, h.PR_COUNT + h.PR_STARTERS + 1))       # F01..F17, then the starters U01..U09 (the host only)
    assert h.PR_COUNT == 17 and [q["preset"] for q in rep["presets"]] == every
    lv = [q["level"] for q in rep["presets"]]
    for p, v in zip(every, lv):
        target, tol = h.preset_target(p, host=True)   # (F11: the host reads it 1.9 LU under the FM-1, HOST_SHIFT)
        assert abs(v - lv[0] - target) <= tol, (p, round(v - lv[0], 2))   # (the Rhodes F01..F11: 1.5 LU, the spec; F12 2.0)
    assert ok and "off target: none" in msg, msg
