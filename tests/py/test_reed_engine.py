# SPDX-License-Identifier: GPL-3.0-only
"""The reed engine (firmware/src/reed.c, integers) against its float model (tools/reed_model.py, the play page's v4.3):
the same notes through both, the line with the action noise, at the knobs' centre and with each VOICE knob moved
(reed_model.apply_knobs: knob = 5 + 5 (v - 64) / 64; REG 10 v / 127). tests/c/reed_ref.c runs the engine alone; events land
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
                    "-o", exe, os.path.join(ROOT, "tests", "c", "reed_ref.c"), "-lm"], check=True)
    return exe, d


RK = ["tone", "bark", "bell", "hammer", "decay", "release", "noise", "tune", "reg"]


def render_c(ref, notes, nblk, knobs=None):
    exe, d = ref
    ev = sorted([(b0 * B, "on %d %d" % (m, v)) for b0, m, v, ln in notes] +
                [((b0 + ln) * B, "off %d" % m) for b0, m, v, ln in notes], key=lambda e: e[0])
    ev = [(0, "knob %d %d" % (RK.index(k), v * 65536 if k == "tune" else v)) for k, v in (knobs or {}).items()] + ev
    (d / "ev.txt").write_text("".join("%d %s\n" % e for e in ev))
    subprocess.run([exe, str(d / "ev.txt"), str(d / "out.raw"), str(nblk * B)], check=True)
    return np.fromfile(str(d / "out.raw"), dtype=np.int32)[:nblk * B] / 2.0 ** 30


@pytest.mark.parametrize("notes", [
    [(0, 60, 100, 40)], [(0, 33, 127, 60)], [(0, 36, 20, 60)], [(0, 82, 110, 20), (40, 91, 90, 20)],
    [(0, m, 120, 50) for m in (36, 40, 43, 48, 52, 55, 60, 67)]])
def test_engine_follows_the_model(ref, notes):
    P = json.load(open(SET))
    nblk = 110
    c = render_c(ref, notes, nblk)
    p = M.render_notes([(b0 * B / M.FS, m, v, ln * B / M.FS) for b0, m, v, ln in notes], nblk * B / M.FS, P) * P["drive"]
    err = np.sqrt(((c - p) ** 2).mean()) / np.sqrt((p ** 2).mean())
    # -55: the words worked out in integers tune each mode within ~0.01 cent, a phase drift over the notes' seconds
    # (A1's first mode: 2 LSB of 303770); the rest -70 dB and under
    assert 20 * np.log10(err) < -55


# the knobs where the engine keeps the set's strike normalisation (the model recomputes it: under 0.4 dB) a looser gate
LOOSE = {"decay": -45, "tune": -25, "reg": -50, "bell": -55}


@pytest.mark.parametrize("knob,v", [("tone", 0), ("tone", 127), ("bark", 0), ("bark", 127), ("bell", 0), ("bell", 127),
                                    ("hammer", 0), ("hammer", 127), ("decay", 0), ("decay", 127), ("release", 0),
                                    ("release", 127), ("noise", 0), ("noise", 127), ("tune", -50), ("tune", 50),
                                    ("reg", 0), ("reg", 64)])
def test_knobs_follow_the_model(ref, knob, v):
    notes = [(1, 45, 110, 30), (11, 60, 90, 25), (21, 72, 120, 20), (31, 84, 70, 15)]
    nblk = 90
    c = render_c(ref, notes, nblk, {knob: v})
    K = {knob: v if knob == "tune" else 10 * v / 127 if knob == "reg" else 5 + 5 * (v - 64) / 64}
    P = M.apply_knobs(json.load(open(SET)), K)
    p = M.render_notes([(b0 * B / M.FS, m, vv, ln * B / M.FS) for b0, m, vv, ln in notes], nblk * B / M.FS, P) * P["drive"]
    err = np.sqrt(((c - p) ** 2).mean()) / np.sqrt((p ** 2).mean())
    assert 20 * np.log10(err) < LOOSE.get(knob, -70)


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


def run_ev(ref, ev, nblk, side=False):
    """Raw events [(block, text)] through the engine: the preamp's output (and SPREAD's side, Q15)."""
    exe, d = ref
    (d / "ev2.txt").write_text("".join("%d %s\n" % (b * B, t) for b, t in sorted(ev, key=lambda e: e[0])))
    args = [exe, str(d / "ev2.txt"), str(d / "o2.raw"), str(nblk * B)] + ([str(d / "s2.raw")] if side else [])
    subprocess.run(args, check=True)
    o = np.fromfile(str(d / "o2.raw"), dtype=np.int32) / 2.0 ** 30
    return (o, np.fromfile(str(d / "s2.raw"), dtype=np.int32) / 32768.0) if side else o


def f0_of(x, f_guess):
    w = np.hanning(len(x))
    sp = np.abs(np.fft.rfft(x * w, 1 << 20))
    fr = np.fft.rfftfreq(1 << 20, 1 / M.FS)
    band = (fr > f_guess * 0.9) & (fr < f_guess * 1.1)
    return fr[band][np.argmax(sp[band])]


def rms(x):
    return float(np.sqrt((x ** 2).mean()))


def test_pitch_bend_retunes_the_sounding_note(ref):
    base = run_ev(ref, [(0, "on 57 90")], 60)
    up = run_ev(ref, [(0, "on 57 90"), (20, "bend 2000")], 60)
    seg = slice(30 * B, 60 * B)
    r = f0_of(up[seg], 247) / f0_of(base[seg], 220)
    assert abs(1200 * np.log2(r) - 200) < 1.0                    # +2 semitones, within a cent
    assert np.abs(np.diff(up[19 * B:22 * B])).max() < 4 * np.abs(np.diff(base[19 * B:22 * B])).max()   # no click


def test_half_pedal_damps_slower_than_up_and_faster_than_down(ref):
    tail = slice(40 * B, 70 * B)
    def with_pedal(cc):
        return rms(run_ev(ref, [(0, "ped %d" % cc), (1, "on 60 100"), (20, "off 60")], 70)[tail])
    up, half, down = with_pedal(0), with_pedal(52), with_pedal(127)
    assert up < half < down


def test_release_velocity_quick_lift_damps_faster(ref):
    tail = slice(30 * B, 50 * B)
    slow = rms(run_ev(ref, [(0, "on 60 100"), (20, "offv 60 1")], 50)[tail])
    quick = rms(run_ev(ref, [(0, "on 60 100"), (20, "offv 60 127")], 50)[tail])
    neutral = rms(run_ev(ref, [(0, "on 60 100"), (20, "off 60")], 50)[tail])
    assert quick < neutral < slow


def test_spread_places_low_left_high_right_and_keeps_the_centre(ref):
    o0, s0 = run_ev(ref, [(0, "on 40 100"), (0, "on 88 100")], 40, side=True)
    assert not np.any(s0)                                         # SPREAD 0: no side at all
    o, sd = run_ev(ref, [(0, "spread 127"), (0, "on 40 100")], 40, side=True)
    m = o / 2.0                                                   # (the tap is Q30; the side Q15 of the same scale)
    assert np.dot(sd, m * 0 + o) < 0                              # the low note: left (S against M: L = M + S)
    o2, sd2 = run_ev(ref, [(0, "spread 127"), (0, "on 88 100")], 40, side=True)
    assert np.dot(sd2, o2) > 0


def test_amp_follows_the_model_speaker_and_knocks(ref):
    """200A AMP (TYPE 7): the line through reed_model.speaker (the play page's speaker.json) and the key-up knocks
    after it (reed_model.render_notes with spk)"""
    S = json.load(open(os.path.join(ROOT, "docs", "model", "data", "wurli_speaker.json")))
    notes = [(1, 45, 110, 30), (11, 60, 90, 25), (21, 72, 120, 20)]
    nblk = 100
    ev = [(0, "amp 2")] + [(b, "on %d %d" % (m, v)) for b, m, v, ln in notes] + [(b + ln, "off %d" % m) for b, m, v, ln in notes]
    c = run_ev(ref, ev, nblk)
    P = json.load(open(SET))
    p = M.render_notes([(b * B / M.FS, m, v, ln * B / M.FS) for b, m, v, ln in notes], nblk * B / M.FS, P, spk=S) * P["drive"]
    assert 20 * np.log10(rms(c - p) / rms(p)) < -55
