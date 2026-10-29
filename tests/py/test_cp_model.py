# SPDX-License-Identifier: GPL-3.0-only
"""The CP-80 baseline model (tools/cp_model.py, docs/model/data/cp80.json): the documents' numbers where they are
exact, the calibration levels the preamp is fitted to, and the physics' sanity (a hammer force a grand's, levels
even over the keys, decays as the data says)."""
import math
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "tools"))
import cp_model as m  # noqa: E402


def test_sallen_key_from_its_parts():
    f0, q = m.sk_analog(m.P["preamp"]["sk"])
    assert abs(f0 - 4013) < 5 and abs(q - 1.08) < 0.01
    f0, q = m.sk_analog(m.P["preamp"]["sk_cp70"])
    assert abs(f0 - 2751) < 5 and abs(q - 0.74) < 0.01


@pytest.mark.parametrize("model", ["cp80", "cp70"])
def test_the_calibration_levels(model):
    for what, tgt, got in m.Preamp(model=model).cal_check():
        assert abs(tgt - got) < 0.05, what


def test_tuning_and_strings():
    assert abs(m.key_f1(49) - 440.0) < 1e-9
    assert abs(m.key_f1(88) / (440 * 2 ** (39 / 12)) - 2 ** (35 / 1200)) < 1e-12
    s = m.key_params(49)
    assert s["kind"] == "plain" and s["n_str"] == 2 and abs(s["B"] - 7.3e-4) < 0.3e-4
    assert m.key_params(22)["n_str"] == 1 and m.key_params(23)["n_str"] == 2
    assert m.key_params(73)["damped"] and not m.key_params(74)["damped"]
    n, f = m.partials(s)
    assert abs(f[0] * 1 - s["f1"]) < 1e-6 * s["f1"]                # the first partial is the tuned pitch


def test_the_pitch_rendered():
    y = m.note(49, 90, 4.5, 0.0)
    seg = y[int(0.3 * m.FS):int(4.3 * m.FS)]                         # long: the pair's two strings resolved
    sp = np.abs(np.fft.rfft(seg * np.hanning(len(seg)), 1 << 20))
    fr = np.fft.rfftfreq(1 << 20, 1 / m.FS)
    band = (fr > 400) & (fr < 480)
    lo, hi = (440.0 * 2 ** (c / 1200) for c in m.detunes(49))      # the pair's two strings
    assert lo - 0.3 < fr[band][np.argmax(sp[band])] < hi + 0.3


def test_hammer_force_and_contact():
    for k, lo, hi in ((1, 1.5, 5.0), (49, 1.0, 2.5), (80, 0.2, 0.8)):
        st, ct, vh, force, dt = m.strike(m.key_params(k), 80)
        assert lo < ct * 1e3 < hi, k
        assert 5 < force.max() < 100, k                          # N: a grand's hammer at mf


def test_harder_is_louder_and_brighter():
    def cen(y):
        sp = np.abs(np.fft.rfft(y[:int(0.2 * m.FS)] * np.hanning(int(0.2 * m.FS))))
        fr = np.fft.rfftfreq(int(0.2 * m.FS), 1 / m.FS)
        return np.sum(sp * fr) / np.sum(sp)
    a, b = m.note(40, 40, 0.3, 0.0), m.note(40, 120, 0.3, 0.0)
    assert np.sqrt(np.mean(b ** 2)) > 3 * np.sqrt(np.mean(a ** 2))
    assert cen(b) > cen(a) * 1.15


def test_levels_even_over_the_keys():
    lv = [20 * math.log10(np.sqrt(np.mean(m.note(k, 80, 0.3, 0.0) ** 2))) for k in (1, 25, 49, 64, 73, 80, 88)]
    assert max(lv) - min(lv) < 3


def test_the_voicing_is_smooth():
    g = np.array([m.voicing_db(k) for k in range(1, 89)])
    assert np.max(np.abs(np.diff(g))) < 2.5                       # follows the treble's slope (~2 dB a key at A6)
    assert np.max(np.abs(np.diff(g, 2))) < 1.0                    # with no step


def test_the_damper_and_the_undamped_top():
    for k, damped in ((60, True), (76, False)):
        y = m.note(k, 90, 0.5, 0.6)
        a = np.sqrt(np.mean(y[int(0.4 * m.FS):int(0.5 * m.FS)] ** 2))
        b = np.sqrt(np.mean(y[int(0.9 * m.FS):int(1.0 * m.FS)] ** 2))
        drop = 20 * math.log10(b / a)
        t60 = m.key_params(k)["t60"]
        assert (drop < -60) if damped else (drop > -60 * 0.5 / t60 - 15), k


def test_tremolo_depth_and_antiphase():
    x = np.ones(int(2 * m.FS))
    l, r = m.tremolo(x, 4.0, 1.0)
    l, r = l[m.FS:], r[m.FS:]
    md = (l.max() - l.min()) / (l.max() + l.min())
    assert abs(md - m.P["tremolo"]["depth_max"]) < 0.05
    assert np.corrcoef(l, r)[0, 1] < -0.9
