# SPDX-License-Identifier: GPL-3.0-only
"""The string engine (firmware/src/strings.c, the CP-80) against its float algorithm (tools/cp_multirate.py) and
the reference's preamp and tremolo (tools/cp_model.py). tests/c/cp_ref.c runs the engine alone; notes land on the
firmware's 256-sample blocks. The strings' bus: within -45 dB at v100 (the velocity layers' interpolation: within
-30 dB between two layers, at v40); the preamp at three knob settings within -50 dB; the tremolo's two channels
(M, S) within -45 dB once its depth has glided in."""
import os
import subprocess
import sys

import numpy as np
import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import cp_model as m  # noqa: E402
import cp_multirate as e  # noqa: E402
import gen_cp as G  # noqa: E402

B = 256


@pytest.fixture(scope="module")
def ref(tmp_path_factory):
    d = tmp_path_factory.mktemp("cp")
    exe = str(d / "cp_ref")
    subprocess.run(["cc", "-O2", "-Wall", "-Werror", "-Wno-unused-function", "-I" + os.path.join(ROOT, "firmware", "gen"),
                    "-o", exe, os.path.join(ROOT, "tests", "c", "cp_ref.c"), "-lm"], check=True)
    return exe, d


def run(ref, ev, nblk, side=False):
    exe, d = ref
    (d / "ev.txt").write_text("".join(f"{t} {s}\n" for t, s in ev))
    args = [exe, str(d / "ev.txt"), str(d / "bus.raw"), str(d / "out.raw"), str(nblk * B)] + ([str(d / "side.raw")] if side else [])
    subprocess.run(args, check=True)
    r = [np.fromfile(d / "bus.raw", np.int32) / 2.0 ** 26, np.fromfile(d / "out.raw", np.int32) / 32768.0]
    if side:
        r.append(np.fromfile(d / "side.raw", np.int32) / 32768.0)
    return r


def db(err, ref):
    return 10 * np.log10(np.sum(err ** 2) / np.sum(ref ** 2))


@pytest.mark.parametrize("k,vel,lim", [(1, 100, -45), (25, 100, -45), (49, 100, -45), (73, 100, -45), (88, 127, -45),
                                       (13, 40, -30)])
def test_the_strings_as_the_algorithm(ref, k, vel, lim):
    e.PAIR = "cos"
    held = int(1.0 * m.FS / B)
    nb = held + int(0.6 * m.FS / B)
    bus, _ = run(ref, [(0, f"on {k + 20} {vel}"), (held * B, f"off {k + 20}")], nb)
    flt = e.render([(0.0, k, vel, held * B / m.FS)], nb * B / m.FS) * m.ref_gain()
    s = slice(int(0.01 * m.FS), min(len(bus), len(flt)))
    assert db(bus[s] - flt[s], flt[s]) < lim


def test_the_preamp_and_the_tremolo(ref):
    pre = m.Preamp()
    notes = [(0, "on 45 100"), (0, "on 69 90"), (0, "on 88 90")]
    nb = int(1.0 * m.FS / B)
    for kn in ({}, {0: 127, 1: 0, 2: 127, 3: 2}, {0: 0, 2: 0, 3: 0}):
        bus, out = run(ref, [(0, f"knob {k} {v}") for k, v in kn.items()] + notes, nb)
        want = pre.run(bus, bass=G.knob(kn.get(0, 64)), middle=G.knob(kn.get(1, 64)), treble=G.knob(kn.get(2, 64)),
                       bril=["LOW", "MEDIUM", "HIGH"][kn.get(3, 1)])
        assert db(out - want, want) < -50, kn
    inc = int(round(5.0 / m.FS * 2 ** 32))
    bus, out, side = run(ref, [(0, f"trem 127 {inc}")] + notes, nb, side=True)
    l, r = m.tremolo(pre.run(bus), 5.0, 1.0)
    s = slice(int(0.3 * m.FS), None)
    assert db(out[s] - (l[s] + r[s]) / 2, (l[s] + r[s]) / 2) < -45
    assert db(side[s] - (l[s] - r[s]) / 2, (l[s] - r[s]) / 2) < -45
