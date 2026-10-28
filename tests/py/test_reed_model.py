# SPDX-License-Identifier: GPL-3.0-only
"""The Wurlitzer reed model (tools/reed_model.py, the float reference the play page and a firmware port follow): the
v4.3 additions do what their notes say."""
import os
import sys

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("scipy")
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import reed_model as M  # noqa: E402


def h(x, f, t=0.05, win=4096):
    s = x[int(t * M.FS):int(t * M.FS) + win]
    sp = np.abs(np.fft.rfft(s * np.hanning(len(s))))
    b = int(round(f * len(s) / M.FS))
    return sp[b - 2:b + 3].max()


def test_renders_finite():
    for m in (33, 60, 96):
        y = M.render(m, 100, 0.3)
        assert np.isfinite(y).all() and np.abs(y).max() > 0


def test_u_boost_held_then_faded_out():
    P = dict(M.DEFAULT, u_mid=0.9, u_boost={"x": 1.3, "full": 69, "off": 72})
    Q = {k: v for k, v in P.items() if k != "u_boost"}
    assert M.swing_of(60, P) == pytest.approx(1.3 * M.swing_of(60, Q))
    assert 1.0 < M.swing_of(70, P) / M.swing_of(70, Q) < 1.3
    assert M.swing_of(72, P) == pytest.approx(M.swing_of(72, Q)) and M.swing_of(90, P) == pytest.approx(M.swing_of(90, Q))


def test_attack_raises_the_strike_not_the_sustain():
    P = dict(M.DEFAULT, attack={"w_lo": 1.4, "w_mid": 1.5, "top": 70, "t60": 0.02})
    assert M.attack_w(33, P) == pytest.approx(1.4) and M.attack_w(64, P) == pytest.approx(1.5)
    assert M.attack_w(67, P) == pytest.approx(0.75) and M.attack_w(70, P) == 0.0 and M.attack_w(80, P) == 0.0
    m = 48
    a, b = M.reed(m, 120, int(0.3 * M.FS), M.DEFAULT), M.reed(m, 120, int(0.3 * M.FS), P)
    f0 = M.f0_of(m, P)
    assert np.abs(b[:int(0.003 * M.FS)]).max() > 1.3 * np.abs(a[:int(0.003 * M.FS)]).max()   # the knock
    assert h(b, f0, 0.1) == pytest.approx(h(a, f0, 0.1), rel=0.02)                            # the tone from 100 ms


def test_u_key_and_g_key_per_key():
    P = dict(M.DEFAULT, u_key=[1.0] * 63 + [0.5], g_key=[1.0] * 63 + [2.0])
    assert M.swing_of(96, P) == pytest.approx(0.5 * M.swing_of(96, M.DEFAULT))
    assert M.g_key(96, P) == 2.0 and M.g_key(60, P) == 1.0 and M.g_key(60, M.DEFAULT) == 1.0
