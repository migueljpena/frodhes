# SPDX-License-Identifier: GPL-3.0-only
"""The reed engine (firmware/src/reed.c, integers) against its float model (tools/reed_model.py, the play page's v4.3):
the same notes through both, the line, without the action noise. tests/c/reed_ref.c runs the engine alone; events land
on the firmware's 256-sample blocks. And the tables are tools/gen_reed.py's."""
import json
import os
import subprocess
import sys

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("scipy")
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import gen_reed  # noqa: E402
import reed_model as M  # noqa: E402

SET = os.path.join(ROOT, "docs", "model", "data", "wurli_v43.json")
B = 256


@pytest.fixture(scope="module")
def ref(tmp_path_factory):
    d = tmp_path_factory.mktemp("reed")
    exe = str(d / "reed_ref")
    subprocess.run(["cc", "-O2", "-Wall", "-Werror", "-Wno-unused-function", "-I" + os.path.join(ROOT, "firmware", "gen"),
                    "-o", exe, os.path.join(ROOT, "tests", "c", "reed_ref.c")], check=True)
    return exe, d


def render_c(ref, notes, nblk):
    exe, d = ref
    ev = sorted([(b0 * B, "on %d %d" % (m, v)) for b0, m, v, ln in notes] +
                [((b0 + ln) * B, "off %d" % m) for b0, m, v, ln in notes], key=lambda e: e[0])
    (d / "ev.txt").write_text("".join("%d %s\n" % e for e in ev))
    subprocess.run([exe, str(d / "ev.txt"), str(d / "out.raw"), str(nblk * B)], check=True)
    return np.fromfile(str(d / "out.raw"), dtype=np.int32)[:nblk * B] / 2.0 ** 30


@pytest.mark.parametrize("notes", [
    [(0, 60, 100, 40)], [(0, 33, 127, 60)], [(0, 36, 20, 60)], [(0, 82, 110, 20), (40, 91, 90, 20)],
    [(0, m, 120, 50) for m in (36, 40, 43, 48, 52, 55, 60, 67)]])
def test_engine_follows_the_model(ref, notes):
    P = json.load(open(SET))
    P.pop("snoise", None)
    nblk = 110
    c = render_c(ref, notes, nblk)
    p = M.render_notes([(b0 * B / M.FS, m, v, ln * B / M.FS) for b0, m, v, ln in notes], nblk * B / M.FS, P) * P["drive"]
    err = np.sqrt(((c - p) ** 2).mean()) / np.sqrt((p ** 2).mean())
    assert 20 * np.log10(err) < -75


def test_tables_are_the_generators(tmp_path):
    out = tmp_path / "reed_tables.h"
    subprocess.run([sys.executable, os.path.join(ROOT, "tools", "gen_reed.py"), SET, "--out", str(out)], check=True)
    assert out.read_text() == open(os.path.join(ROOT, "firmware", "gen", "reed_tables.h")).read()


def test_no_rom_window_words():
    txt = open(os.path.join(ROOT, "firmware", "gen", "reed_tables.h")).read()
    import re
    for w in re.findall(r"-?\d+", txt.split("sha256")[0]):
        u = int(w) & 0xFFFFFFFF
        assert not (gen_reed.ROM_LO <= u <= gen_reed.ROM_HI), w
