# SPDX-License-Identifier: GPL-3.0-only
"""The CP-80 engine's algorithm (tools/cp_multirate.py) against the reference (tools/cp_model.py): the same note,
after its first 10 ms (the onset: the reference's partials start at full amplitude, the engine's lower bands rise
over ~1 ms), within -38 dB; the interpolators' images under -65 dB; the cost per key bounded."""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "tools"))
import cp_model as m  # noqa: E402
import cp_multirate as e  # noqa: E402


def test_a_note_as_the_reference():
    g, pre = m.ref_gain(), m.Preamp()
    for k in (13, 40, 73):
        ref, eng = e.compare_note(k, 80, 1.0, 0.6)
        r, q = pre.run(ref * g), pre.run(eng * g)
        s = slice(int(0.01 * m.FS), None)
        assert 10 * np.log10(np.sum((q[s] - r[s]) ** 2) / np.sum(r[s] ** 2)) < -38, k


def test_the_half_band_images():
    w = np.linspace(0.3, 0.5, 500) * 2 * np.pi                # a stage's image band (of its output rate)
    assert 20 * np.log10(np.max(np.abs(e.hb_resp(w)))) < -65
    wp = np.linspace(0.0, 0.2, 200) * 2 * np.pi
    assert np.allclose(np.abs(e.hb_resp(wp)), 1.0, atol=1e-3)  # (all-pass branches: a flat pass band)


def test_the_cost():
    c = [e.cost(k) for k in range(1, 89)]
    assert max(c) < 18 and np.mean(c) < 10                       # full-rate resonators' worth a key
