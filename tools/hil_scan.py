#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Hardware-in-the-loop scan of a connected FM-1 running Frodhes: MIDI in, USB audio back.

The FM-1's USB audio is exactly what its output plays (after MASTER), so this checks the real thing:
silence, latency, every key (level, tuning), velocity, the TYPEs, every effect (level against dry,
clipping, what it is for), the bypass, sustain, the held-note tail, polyphony stress (dropouts) and
every factory preset. Captures and a report go to build/hil/.

  tools/hil_scan.py [--only NAME[,NAME]] [--list]
  tools/hil_scan.py --tier must [--fwsc PKG] [--dry-run]    T1: the release's must-pass scenarios (tools/hil_must.py)
  tools/hil_scan.py --tier full [options]                  T2: every classic suite
  tools/hil_scan.py --only stress_max [--rate 44100|48000] [--source both|usb] [--host | --dry-run]
                                        the cracks test (tools/hil_crackle.py: every engine, minutes of the worst case,
                                        USB + the jack; in T1; not in the default run)

Before: MASTER fully up and left alone. The scan changes the current panel settings (not the user
presets); it ends on F01. Needs mido + python-rtmidi, sounddevice, numpy.

Determinism (2026-10-18): the same firmware scanned twice gives the same verdicts. Before, free-running parts started
each take at another phase (the tremolo / phaser / chorus / filter LFOs, the tape's wow, the reverb's modulation, the
200A's vibrato, the engines' noise) and a delay / reverb line could still hold the last take: 'types' read a minimum
spectral distance of 0.9 dB in one run and 1.8 in the next, 'presets' spanned 7.5 / 6.7 / 5.3 LU, fx_all's extremes
flipped. Now:
  - the developer reset, MIDI CC 10 >= 64 (fx.c fx_reset_state: every LFO, the delay / chorus / pre-delay / reverb
    lines, the phaser, the noise generators, the motor back to their boot state at the next audio block; every voice
    ends and the pickup RLC / body states go to 0, so no tail under the gate meets the next take; the delay line is
    cleared over the next ~16 blocks, with no heavy block), goes out
    RESET_LEAD (50 ms) before the first note of every take (with_reset(); classic Rig.take, fx_all's jack takes),
    after the settings, and once more at the end of every r.dry() (which also empties the delay line). What is left is the note's arrival within an audio block:
    the reset-to-note gap is a whole number of 256-frame blocks, +-1 block (5.8 ms) of MIDI / scheduling jitter, so an
    LFO's phase at the note moves by at most 5.8 ms of its period (10 degrees of the 4.75 Hz tremolo);
  - every window is anchored on the measured onset (Rig.lag(), align()), never on the recording's start (the USB
    capture starts 10-60 ms off the MIDI clock from take to take: 'types' cut its windows into the attack at another
    place each time; sustain, held_tail, tremolo_rate, fx_toggle, stress did the same);
  - a level over a modulated sound is taken over whole cycles of its one LFO where they fit (cyc_win(): effects,
    fx_all; fx_toggle's 0.4 s windows become whole cycles in 0.38-0.6 s);
  - the noisy ones take the median of REPEATS takes (types 3, presets 3, fx_all 2; --repeats N overrides) and report
    the takes' own spread next to the verdict;
  - the classic chords hold no octave (CHORD A3 C4 E4, was A3 E4 A4; fx_all / fx_toggle C3 G3 E4, was C3 G3 C4 E4):
    an octave's coinciding partials sum at the phase the notes' onsets set, and one note landing a block late moved
    a preset's loudness by up to 2.0 LU (the main cause of 'presets' 7.5 / 6.7 / 5.3); now <= 0.2 LU;
  - 'types' sums the two notes' band powers instead of joining the two windows end to end (the junction's step
    splashed broadband into the gaps between the partials: up to 0.8 dB of the distance from take to take).
One recording stream for the whole run (2026-10-18): the FM-1 held its automatic flash saves (the settings 2 s after a
change, the sequencer's pattern) back while a computer read its USB audio, because a save held the interrupts off
~24 ms, no USB audio packet went out and macOS then handed ~250 ms of stale input (since the split erase, 0.9.5,
fm1_flash.h fl_split_op: slices of ~0.5 ms, and the saves go while the stream runs). The scan
used to open a new input stream per take and per silence poll, so between them nobody read the audio and a save could
land at the start of the next take. Now Rig opens one stream per device (Capture: the FM-1's USB, and the jack's
interface input where a suite records it) at the first take and keeps it running; each take is cut out of it by the
streams' ADC times (Ring.cut, tests/py/test_hil_capture.py). Rig.persistent = False brings back a stream per take
(tools/hil_usb_onset.py, which measures exactly that case).
Expected run-to-run spread (host, block jitter alone, tests/py/test_hil_determinism.py: types' distances <= 0.05 dB,
presets <= 0.04 LU, effects / fx_all / fx_toggle <= 0.09 LU; without CC 10 presets moved 4.6 LU). On the device add
the USB capture (44.1 -> 48 kHz, stream slips): expect types +-0.1 dB, presets +-0.2 LU, the others +-0.3 LU; a
verdict closer than that to its limit is a real borderline, not noise. The same checks run on the host build: tools/hil_scan.py --host [SEED] --only types,presets (no FM-1;
report build/hil/host-report.txt): each take a fresh host run that replays the panel's CCs, idles a random 0.3-1.3 s
(the LFOs run on, as on the device between takes) and plays the take 3-25 ms late (MIDI + USB latency).
"""
import argparse
import json
import os
import sys
import time

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "build", "hil")
PORT_KEYS = ("frodhes", "felucca", "fm-1")   # the FM-1's MIDI port / USB audio names: Frodhes, Felucca before
FS = 48000
A4 = 440.0

# the CC map (firmware/src/params.c)
CC = dict(TYPE=80, DRIVE=81, TW_SW=17, TW_DEPTH=18, TW_RATE=19, TW_MODE=21, CP_SW=85, CP_DEPTH=86, CP_SPEED=87,
          DL_SW=88, DL_DEPTH=89, DL_TIME=90, DL_FDBK=26, REV=91, PH_SW=12, PH_DEPTH=13, PH_SPEED=14, PH_FDBK=16,
          CMP_SW=106, CMP_AMT=107, CMP_ATK=108, CMP_REL=109, NOISE=104, SUS=64, RAW=119, CMP_RAT=112, CMP_GAIN=113,
          PH_SPEED2=14, CP_SPEED2=87, TW_SHAPE=22, DL_TONE=27, LEVEL=110, BYPASS=111, ARP=52, CHORD=56, SEQ=60, SQREC=63,
          FL_TYPE=70, FL_FREQ=74, FL_RESO=71, FL_LFO=72)
DRY = dict(DRIVE=0, REV=0, TW_SW=0, CP_SW=0, PH_SW=0, DL_SW=0, CMP_SW=0, RAW=0, SUS=0, ARP=0, CHORD=0, SQREC=0, SEQ=0,
           FL_TYPE=0)
# (ARP, CHORD, SEQ, SQREC: the arpeggiator, chord mode, the sequencer and its recording off)


RHODES_TYPES = 4           # TYPE 0..3 (ST77 SUIT FELT DYNO); 4 / 5 are the 200A's (WURLI 200A, 200A AMP)


def n_types():
    import re
    src = open(os.path.join(ROOT, "firmware", "src", "params.c")).read()
    return int(re.search(r'\[P_TYPE\] = \{"[^"]*", (\d+),', src).group(1)) + 1


def type_cc(t):
    """the CC value selecting TYPE t on the CC that type_ccnum(t) names: CC 80 (by 32) for the Rhodes types 0..3 as
    before, CC 115 for the 200A types (frodhes.c midi_cc): 0..63 WURLI 200A, 64..127 200A AMP, sent as 0 / 127"""
    if t < 4:
        return t * 32 + 8
    return {4: 0, 5: 127}[t]


def type_ccnum(t):
    return 80 if t < 4 else 115


def drv_cc(x):
    """the CC 84 value that selects drive TYPE x (params.c DRV_CC84, frodhes.c midi_cc: a band table, as CC 80's):
    OFF 0, FDRV 127 (as off / on before), any other TYPE the middle of its band"""
    import re
    src = open(os.path.join(ROOT, "firmware", "src", "params.c")).read()
    lo = [int(v) for v in re.search(r"DRV_CC84\[\d*\] = \{([^}]*)\}", src).group(1).split(",")]
    if x <= 1:
        return 127 * int(x)
    ends = sorted(lo) + [128]
    return (lo[x] + ends[ends.index(lo[x]) + 1] - 1) // 2


def param_defaults():
    """every parameter's CC and the CC value that sets its default (parsed from firmware/src/params.c), so each test
    starts from the same state whatever the FM-1 had saved"""
    import re
    src = open(os.path.join(ROOT, "firmware", "src", "params.c")).read()
    out = []
    for name, hi, de, cc in re.findall(r'\[P_(\w+)\] = \{"[^"]*", (\d+), (\d+), F_\w+, (\d+)', src):
        hi, de, cc = int(hi), int(de), int(cc)
        if not cc:                                   # (records v3: a parameter without a CC yet)
            continue
        if name == "TYPE":
            v = type_cc(de)
        elif name == "DRV_SW":
            v = drv_cc(de)
        elif hi == 2:
            v = (0, 64, 127)[de]
        elif hi < 127:
            v = min(127, (de * 128 + 64) // (hi + 1))
        else:
            v = de
        out.append((cc, v))
    return out


def db(x):
    x = np.asarray(x, float)
    return 20 * np.log10(np.sqrt(np.mean(x * x)) / 32767 + 1e-12) if x.size else -240.0


RESET_CC, RESET_LEAD = 10, 0.05   # the developer reset (fx.c fx_reset_state) and how long before a take's first note
REPEATS = dict(types=3, presets=3, fx_all=2)   # takes per measure, their median judged (--repeats N: all of them)


def with_reset(events, lead=RESET_LEAD):
    """the take's events with CC 10 = 127 lead s before its first note-on (at 0 when the note comes sooner): every LFO,
    line and noise generator starts the note from its boot state, +-1 audio block"""
    on = [e[0] for e in events if e[1] == "on"]
    if not on:
        return list(events)
    return sorted(list(events) + [(max(0.0, min(on) - lead), "cc", RESET_CC, 127)], key=lambda e: e[0])


def reps(name):
    return max(1, int(REPEATS.get(name, 1)))


class _Msg:
    """a stand-in for mido.Message (HostRig: no mido needed)"""
    def __init__(self, type, **kw):
        self.type = type
        self.note = self.velocity = self.control = self.value = self.program = 0
        self.__dict__.update(kw)


class _Mido:
    Message = _Msg


class RigBase:
    """what the classic suites call: the panel (cc / dry / setup / pc ...) on _send(), takes on _rec(); the device
    (Rig) or the host build (HostRig)"""
    reset = True                                         # CC 10 before every take's first note

    def _send(self, kind, a, b=0):
        raise NotImplementedError

    def send(self, msg):                                 # r.out.send(r.mido.Message(...)) as before
        k = msg.type
        if k == "note_on":
            self._send("on", msg.note, msg.velocity)
        elif k == "note_off":
            self._send("off", msg.note, 0)
        elif k == "control_change":
            self._send("cc", msg.control, msg.value)
        elif k == "program_change":
            self._send("pc", msg.program, 0)

    def cc(self, name, v):
        self._send("cc", CC[name], int(v))

    def cc_raw(self, n, v):
        self._send("cc", int(n), int(v))

    def pc(self, p):
        self._send("pc", int(p), 0)

    def note_off(self, n):
        self._send("off", int(n), 0)

    def fx_reset(self):
        """CC 10: the effects' and engines' free-running state to boot (at the next audio block)"""
        self._send("cc", RESET_CC, 127)

    def setup(self, **kv):
        for k, v in kv.items():
            self.cc(k, v)
        time.sleep(0.05)

    def settle(self):
        """the reset, then the gate's exact silence (the reset empties the delay / reverb lines: no tail to wait out)"""
        self.fx_reset()
        time.sleep(0.02)
        return self.wait_silence()

    def dry(self, **kv):
        self._send("cc", 120, 0)
        if not hasattr(self, "defaults"):
            self.defaults = param_defaults()
        for cc, v in self.defaults:                    # a known state: every parameter at its default
            self._send("cc", cc, v)
        d = dict(DRY)
        d.update(kv)
        self.setup(**d)
        self.settle()

    def take(self, dur, events):
        """record dur s; events: [(t_s, kind, a, b)], kind on/off/cc/pc; CC 10 goes out RESET_LEAD before the first
        note (self.reset)"""
        ev = with_reset(events) if self.reset else sorted(events, key=lambda e: e[0])
        return self._rec(dur, ev)

    @staticmethod
    def lag(x, events, thr=200):
        """samples from the take's event clock to its recording: the first sound's onset minus the first note-on's
        time (the USB capture starts 10-60 ms off the MIDI clock, differently each take; 0 when nothing sounds)"""
        on = [e[0] for e in events if e[1] == "on"]
        i = onset(x, thr, min(on)) if on else -1
        return i - int(round(min(on) * FS)) if i >= 0 else 0


class Ring:
    """one input stream's recent history (secs at the device's own rate, float32; only the channels kept): every
    callback block pushed with its ADC time (PortAudio's stream clock), so a take is cut by TIME, not by stream start.
    The persistent recorder (Capture) keeps the FM-1's USB stream open for a whole suite: the FM-1 held its automatic
    flash saves back while a computer read its USB audio (before the split erase, fm1_flash.h), and a
    stream opened per take let 'flowing' drop between takes, so a save (interrupts off ~24 ms: no USB packet, macOS
    then hands ~250 ms of stale input) landed at the start of the next take"""

    def __init__(self, sr, ch, secs=120.0):
        self.sr, self.ch = int(sr), int(ch)
        self.N = int(secs * sr)
        self.buf = np.zeros((self.N, ch), np.float32)
        self.w = 0                                   # frames pushed so far (absolute)
        import threading
        from collections import deque
        self.lock = threading.Lock()
        # the block log: (absolute frame, ADC time of that frame) in two preallocated rings, nlog entries so far. Not
        # a deque turned into an array per cut: np.array() of the full deque (180,000 tuples at 48 kHz, 120 s) took
        # ~18 ms with the lock and the GIL held, so the PortAudio callback stalled and CoreAudio dropped USB input
        # (the 13-27 ms "gaps" right after a take's last event, while the take was polled every 10 ms; 2026-10-19).
        # A cut copies only the entries from its start (_snap): microseconds under the lock
        self.M = max(64, self.N // 32)
        self.lf = np.zeros(self.M, np.int64)
        self.la = np.zeros(self.M, np.float64)
        self.nlog = 0
        self.status = deque(maxlen=1000)             # (ADC time, PortAudio status flags)
        self.gaps = []

    def push(self, block, adc, status=None):
        """a callback's frames (n, ch) whose first was captured at adc (s, the stream clock)"""
        n = len(block)
        if not n:
            return
        if n > self.N:
            block, adc = block[-self.N:], adc + (n - self.N) / self.sr
            self.w += n - self.N
            n = self.N
        i = self.w % self.N
        k = min(n, self.N - i)
        self.buf[i:i + k] = block[:k]
        if k < n:
            self.buf[:n - k] = block[k:]
        with self.lock:
            j = self.nlog % self.M
            self.lf[j], self.la[j] = self.w, float(adc)
            self.nlog += 1
            if status:
                self.status.append((float(adc), str(status)))
            self.w += n

    def _snap(self, t0, back=64):
        """(L, w): the log from the last entry at or before stream time t0 (less `back` entries of margin: ADC times
        may jitter out of order) to the newest, as an (n, 2) float array [frame, ADC time], and the frames pushed;
        (None, w) while empty. O(log n) under the lock plus the copy of the entries after t0"""
        with self.lock:
            c, w = self.nlog, self.w
            lo = max(0, c - self.M)
            if c == lo:
                return None, w
            a, b = lo, c                             # the first logical index whose ADC time is > t0
            while a < b:
                m = (a + b) // 2
                if self.la[m % self.M] <= t0:
                    a = m + 1
                else:
                    b = m
            idx = np.arange(max(lo, a - 1 - back), c) % self.M
            L = np.empty((len(idx), 2), float)
            L[:, 0], L[:, 1] = self.lf[idx], self.la[idx]
        return L, w

    def frame_at(self, t):
        """the absolute frame captured at stream time t: the blocks' ADC times interpolated, extrapolated past the ends
        at the nominal rate"""
        L, _ = self._snap(t)
        if L is None:
            return None
        if t <= L[0, 1]:
            return L[0, 0] + (t - L[0, 1]) * self.sr
        if t >= L[-1, 1]:
            return L[-1, 0] + (t - L[-1, 1]) * self.sr
        return float(np.interp(t, L[:, 1], L[:, 0]))

    def rate(self, t0, dur):
        """the stream's true frames per second of the stream clock (the computer's) over [t0, t0 + dur], from the
        blocks' ADC times: the median slope across half the span (the ADC times jitter by ms; thousands of blocks), or
        None with fewer than 32 blocks. cut() lays the frames end to end at the nominal rate, so a take's audio runs
        rate / sr - 1 off the event clock (the FM-1's USB ~+540..760 ppm on the MOTU rig, 2026-10-24:
        tools/hil_crackle.py maps its guards with it)"""
        L, _ = self._snap(t0)
        if L is None:
            return None
        L = L[(L[:, 1] >= t0) & (L[:, 1] <= t0 + dur)]
        if len(L) < 32:
            return None
        h = len(L) // 2
        dt = L[h:2 * h, 1] - L[:h, 1]
        ok = dt > 0
        return float(np.median((L[h:2 * h, 0] - L[:h, 0])[ok] / dt[ok])) if ok.any() else None

    def end_time(self):
        """the stream time of the last frame held (None before the first block)"""
        with self.lock:
            if not self.nlog:
                return None
            j, w = (self.nlog - 1) % self.M, self.w
            f, a = int(self.lf[j]), float(self.la[j])
        return a + (w - 1 - f) / self.sr

    def cut(self, t0, dur, tol=0.006):
        """the frames from stream time t0 for dur s ((n, ch), n = dur * sr), or None while they are not all in yet.
        Placed by the blocks' ADC times: the first block anchors the take, the rest follow on contiguously; where a
        block's ADC time steps more than tol s off its predecessor's length (input the stream never delivered, or a
        block delivered twice) the take re-anchors there: the missing time stays zeros, a repeat is overwritten, so
        what comes after is still on the event clock (tol 6 ms: on the FM-1 the ADC times jitter up to ~3 ms from block
        to block, 2026-10-18; a lost USB block is >= 11.6 ms). self.gaps: [(stream time, s)] of the last cut. Frames
        that already left the ring (or came before the first block) read as zeros"""
        L, w = self._snap(t0)
        n = int(round(dur * self.sr))
        if L is None:
            return None
        if L[-1, 1] + (w - 1 - L[-1, 0]) / self.sr < t0 + (n - 1) / self.sr:
            return None
        ends = np.append(L[1:, 0], w)
        k0 = max(0, int(np.searchsorted(L[:, 1], t0, "right")) - 1)
        out = np.zeros((n, self.ch), np.float32)
        self.gaps = []
        pos = None
        for k in range(k0, len(L)):
            a, f0, f1 = L[k, 1], int(L[k, 0]), int(ends[k])
            if pos is None:
                pos = int(round((a - t0) * self.sr))
            else:
                step = a - L[k - 1, 1] - (f0 - L[k - 1, 0]) / self.sr
                if abs(step) > tol:
                    self.gaps.append((float(a), float(step)))
                    pos += int(round(step * self.sr))
            if pos >= n:
                break
            lo, hi = max(f0, w - self.N, f0 - pos), min(f1, f0 + n - pos)   # (frames still held, inside the take)
            if hi > lo:
                out[pos + lo - f0:pos + hi - f0] = self.buf[np.arange(lo, hi) % self.N]
            pos += f1 - f0
        return out

    def flags_between(self, t0, t1):
        with self.lock:
            st = list(self.status)
        return [s for a, s in st if t0 <= a <= t1]


class Capture:
    """the persistent recorder: one InputStream per device (the FM-1's USB at its own rate, stereo; the jack interface
    at its own, one channel kept), opened once and left running for the whole run; each take is a slice of it by the
    streams' ADC times (Ring.cut). The callbacks only copy; everything else runs in the caller's thread"""

    def __init__(self, sd):
        self.sd, self.rings, self.streams, self.keep = sd, {}, {}, {}

    def add(self, key, dev, ch_keep, n_in, secs=120.0, sr=None, blocksize=0, switch_rate=False):
        """sr: open the stream at this rate (tools/hil_crackle.py: the FM-1 at 44.1 kHz for exact samples), else the
        device's default; blocksize: the callback's frames (0: PortAudio's choice); switch_rate: the device itself
        switched to sr (Core Audio, as hil_cpu --usb): without it Core Audio keeps the device at its nominal rate and
        converts, and the FM-1 runs the other rate's path"""
        sd = self.sd
        sr = int(sr or sd.query_devices(dev)["default_samplerate"])
        ring = Ring(sr, len(ch_keep), secs)
        sel = list(ch_keep)

        def cb(indata, frames, ti, status):
            adc = ti.inputBufferAdcTime or (ti.currentTime - frames / sr)
            ring.push(indata[:, sel], adc, status)
        kw = {"blocksize": int(blocksize)} if blocksize else {}
        if switch_rate:
            kw["extra_settings"] = sd.CoreAudioSettings(change_device_parameters=True, fail_if_conversion_required=True)
        s = sd.InputStream(device=dev, samplerate=sr, channels=n_in, dtype="float32", callback=cb, **kw)
        s.start()
        self.rings[key], self.streams[key] = ring, s
        t0 = time.monotonic()
        while ring.end_time() is None and time.monotonic() - t0 < 5:
            time.sleep(0.01)
        return ring

    def now(self):
        """every stream's clock now (s), {key: stream.time}"""
        return {k: s.time for k, s in self.streams.items()}

    def cut(self, key, t0, dur, timeout=5.0):
        """the take's frames from stream time t0 (a now() stamp), waiting until they are in"""
        r = self.rings[key]
        lim = time.monotonic() + dur + timeout
        while True:
            x = r.cut(t0, dur)
            if x is not None:
                return x
            if time.monotonic() > lim:
                raise RuntimeError(f"{key}: the stream stopped delivering ({r.w} frames)")
            time.sleep(0.01)

    def close(self):
        for s in self.streams.values():
            try:
                s.stop()
                s.close()
            except Exception:
                pass
        self.streams = {}


class Rig(RigBase):
    """the FM-1 over USB-MIDI + its USB audio (and an interface input recording the headphone jack, add_jack()).
    persistent (default): one input stream per device for the whole run (Capture), every take and every silence poll
    a slice of it by the ADC time, so the FM-1 sees a computer reading its audio the whole time and keeps its
    automatic flash saves back (no save inside a take). persistent = False: a stream per take / poll, as before
    2026-10-18 (tools/hil_usb_onset.py measures exactly that case; rh_devcmp.py switches the device's rate)"""
    persistent = True

    def __init__(self):
        import mido
        import sounddevice as sd
        self.sd = sd
        names = [n for n in mido.get_output_names() if any(k in n.lower() for k in PORT_KEYS)]
        if not names:
            sys.exit("no FM-1 MIDI port")
        self.out = mido.open_output(names[0])
        self.mido = mido
        devs = [i for i, d in enumerate(sd.query_devices())
                if any(k in d["name"].lower() for k in PORT_KEYS[:2]) and d["max_input_channels"] >= 2]
        if not devs:
            sys.exit("no FM-1 audio input")
        self.dev = devs[0]
        self.cap, self.xruns = None, []

    def capture(self):
        """the persistent recorder, opened at the first use (the FM-1's USB, stereo)"""
        if self.cap is None:
            import atexit
            self.cap = Capture(self.sd)
            self.cap.add("usb", self.dev, (0, 1), 2)
            atexit.register(self.cap.close)
        return self.cap

    def add_jack(self, dev, ch0):
        """the jack's interface input ch0 (0-based) into the persistent recorder too"""
        cap = self.capture()
        if "jack" not in cap.rings:
            cap.add("jack", dev, (ch0,), int(self.sd.query_devices(dev)["max_input_channels"]))
        return cap

    def jack(self, name="M6", ch=5):
        """the headphone jack on the interface 'name' input ch (1-based; hil_jack's defaults) into the persistent
        recorder when it is connected: True if it is there"""
        if not hasattr(self, "_jack"):
            self._jack = False
            if self.persistent:
                devs = self.sd.query_devices()
                js = [i for i, d in enumerate(devs) if d["name"] == name and d["max_input_channels"] >= ch]
                if js:
                    self.add_jack(js[0], ch - 1)
                    self._jack = True
        return self._jack

    def _send(self, kind, a, b=0):
        M = self.mido.Message
        self.out.send(M("note_on", note=a, velocity=b) if kind == "on" else M("note_off", note=a) if kind == "off" else
                      M("program_change", program=a) if kind == "pc" else M("control_change", control=a, value=b))

    def wait_silence(self, limit=10.0):
        """until the output is exact zero (the gate), so no delay / reverb tail of the last test leaks in"""
        t0 = time.monotonic()
        while time.monotonic() - t0 < limit:
            if self.persistent:
                cap = self.capture()
                x = cap.cut("usb", cap.now()["usb"], 0.3)
            else:
                x = self.sd.rec(int(0.3 * FS), samplerate=FS, channels=2, device=self.dev, dtype="int16")
                self.sd.wait()
            if not np.any(x):
                return True
        return False

    def both(self, dur, events, jack=False):
        """dur s from now while the events go out on time: {"usb": (n, 2), "jack": (n, 1) with jack} at FS, full scale
        1.0 (the persistent recorder; each source cut from ITS stream's clock at the moment the event clock starts).
        self.xruns: PortAudio's status flags inside the take"""
        cap = self.capture()
        keys = ["usb"] + (["jack"] if jack else [])
        st = cap.now()
        t0 = time.monotonic()
        for t, kind, a, b in sorted(events, key=lambda e: e[0]):
            while time.monotonic() - t0 < t:
                time.sleep(0.0005)
            self._send(kind, a, b)
        out = {k: to_fs(cap.cut(k, st[k], dur).astype(float), cap.rings[k].sr, dur) for k in keys}
        self.xruns = [(k, f) for k in keys for f in cap.rings[k].flags_between(st[k], st[k] + dur)] + \
                     [(k, f"gap {1000 * g:+.0f} ms at {a - st[k]:.2f} s") for k in keys for a, g in cap.rings[k].gaps]
        return out

    def _rec(self, dur, events):
        if self.persistent:
            return self.both(dur, events)["usb"] * 32768
        rec = self.sd.rec(int(dur * FS), samplerate=FS, channels=2, device=self.dev, dtype="int16")
        t0 = time.monotonic()
        for t, kind, a, b in events:
            while time.monotonic() - t0 < t:
                time.sleep(0.0005)
            self._send(kind, a, b)
        self.sd.wait()
        return rec.astype(float)


class HostRig(RigBase):
    """the classic suites on the host build (tests/host.c through tests/py/hostrun.py), as the device would play them:
    each take is a fresh host run that replays the panel's state (every CC / program change sent so far, the last of
    each since the last program change), idles a random preroll (the LFOs run on, as on the FM-1 between takes; a
    take without the reset starts them at another phase) and plays the take lat ms late (MIDI + USB latency; +-1 ms
    of jitter per event), then resamples 44.1 -> 48 kHz. seed: the prerolls and latencies; reset=False: no CC 10"""

    def __init__(self, seed=0, reset=True, preroll=(300, 1300), lat=(3, 25), tag="hil"):
        self.rng = np.random.default_rng(seed)
        self.reset, self.preroll, self.latr, self.tag = reset, preroll, lat, f"{tag}-{seed}-{int(reset)}"
        self.hist, self.n, self.mido, self.out, self.dev = [], 0, _Mido, self, None

    def _send(self, kind, a, b=0):
        if kind == "pc":
            self.hist.append((0xC0, a, 0))
        elif kind == "cc" and a not in (RESET_CC, 120):     # (no note sounds outside a take)
            last_pc = max([k for k, h in enumerate(self.hist) if h[0] == 0xC0], default=-1)
            self.hist = [h for k, h in enumerate(self.hist) if k <= last_pc or h[:2] != (0xB0, a)] + [(0xB0, a, b)]

    def wait_silence(self, limit=10.0):
        return True

    def _rec(self, dur, events):
        from scipy.signal import resample_poly
        H = hr()
        lines = [f"{5 + k // 16} midi 0x{s:02X} {a} {b}" if s != 0xC0 else f"{5 + k // 16} midi 0xC0 {a}"
                 for k, (s, a, b) in enumerate(self.hist)]
        R = max(5 + len(self.hist) // 16 + 100, int(self.rng.integers(*self.preroll)))
        L = float(self.rng.uniform(*self.latr))
        for t, kind, a, b in events:
            ms = int(round(R + L + 1000 * t + self.rng.uniform(-1, 1)))
            if kind == "on":
                lines.append(f"{ms} midi 0x90 {a} {b}")
            elif kind == "off":
                lines.append(f"{ms} midi 0x80 {a} 0")
            elif kind == "cc":
                lines.append(f"{ms} midi 0xB0 {a} {b}")
                if a != RESET_CC:                          # (the take's CCs stay set, as on the device)
                    self._send("cc", a, b)
            elif kind == "pc":
                lines.append(f"{ms} midi 0xC0 {a}")
                self._send("pc", a)
        self.n += 1
        run = H.run(f"{self.tag}-{self.n}", lines, R + int(np.ceil(1000 * dur)) + 20)
        i0 = int(R * H.FS / 1000)
        x = np.stack([run.L[i0:], run.R[i0:]], 1)
        x = resample_poly(x, 160, 147, axis=0)             # 44.1 -> 48 kHz
        n = int(dur * FS)
        return np.round(np.pad(x, ((0, max(0, n - len(x))), (0, 0)))[:n])


def onset(x, thr=200, t=0.0):
    """the first sample over thr (LSB), searched from 5 ms before event time t (s): a release tail of the last take
    before its first note is not its onset"""
    s = max(0, int((t - 0.005) * FS))
    a = np.abs(x[s:]).max(axis=1) if x.ndim == 2 else np.abs(x[s:])
    if not len(a):
        return -1
    i = np.argmax(a > thr)
    return int(i) + s if a[i] > thr else -1


def f0_of(x, guess):
    """fundamental near guess (Hz): FFT peak, parabolic refinement"""
    n = len(x)
    w = np.hanning(n)
    sp = np.abs(np.fft.rfft(x * w, 8 * n))
    fr = np.fft.rfftfreq(8 * n, 1 / FS)
    m = (fr > guess * 0.94) & (fr < guess * 1.06)
    if not m.any():
        return 0.0
    i = np.flatnonzero(m)[np.argmax(sp[m])]
    if 0 < i < len(sp) - 1:
        a, b, c = np.log(sp[i - 1:i + 2] + 1e-12)
        p = 0.5 * (a - c) / (a - 2 * b + c)
        return (i + p) * FS / (8 * n)
    return fr[i]


def centroid(x):
    sp = np.abs(np.fft.rfft(x * np.hanning(len(x))))
    fr = np.fft.rfftfreq(len(x), 1 / FS)
    return float((sp * fr).sum() / (sp.sum() + 1e-12))


def bands_db(x):
    """third-octave band levels (dB) 100 Hz .. 10 kHz of a mono signal"""
    sp = np.abs(np.fft.rfft(x * np.hanning(len(x)))) ** 2
    f = np.fft.rfftfreq(len(x), 1 / FS)
    c = 100 * 2 ** (np.arange(0, 21) / 3)
    return np.array([10 * np.log10(sp[(f >= fc / 2 ** (1 / 6)) & (f < fc * 2 ** (1 / 6))].sum() + 1e-12) for fc in c])


def clicks(x):
    """dropout / click detector: second difference spikes far above the local level"""
    m = x.mean(axis=1) if x.ndim == 2 else x
    d2 = np.abs(np.diff(m, 2))
    lvl = np.convolve(np.abs(m), np.ones(480) / 480, "same")[1:-1] + 50
    return int((d2 > 6 * lvl + 2000).sum())


def zero_runs(x, lo=0.05, hi=None):
    """runs of exact zeros (>= 64 samples) inside a stretch that should be sounding"""
    m = np.abs(x).max(axis=1)
    z = (m == 0).astype(int)
    runs, n = 0, 0
    for v in z:
        n = n + 1 if v else 0
        if n == 64:
            runs += 1
    return runs


TESTS = {}


def test(fn):
    TESTS[fn.__name__] = fn
    return fn


@test
def silence(r, rep):
    r.dry()
    x = r.take(2.0, [])
    rep["silence"] = dict(peak=float(np.abs(x).max()), rms_db=db(x), nonzero=int((x != 0).sum()))
    ok = rep["silence"]["peak"] == 0
    return ok, f"peak {rep['silence']['peak']:.0f} LSB, {rep['silence']['nonzero']} non-zero samples"


LATENCY_BEFORE_MS = 12.5      # MANUAL's measured MIDI -> USB latency before the USB ring kept one block more (+5.8 ms)


@test
def latency(r, rep):
    r.dry()
    lat = []
    for k in range(5):
        x = r.take(0.8, [(0.2, "on", 72, 110), (0.5, "off", 72, 0)])
        i = onset(x, t=0.2)
        if i > 0:
            lat.append((i / FS - 0.2) * 1000)
    rep["latency_ms"] = lat
    if not lat:
        return False, "no onset"
    return True, (f"MIDI -> USB audio {np.median(lat):.1f} ms (min {min(lat):.1f}, max {max(lat):.1f}); includes USB + host "
                  f"buffers (the USB ring holds a block more since the click fix: ~{LATENCY_BEFORE_MS + 5.8:.0f} ms expected, "
                  f"{LATENCY_BEFORE_MS} before)")


@test
def keyboard(r, rep):
    r.dry()
    rows = []
    for n in range(28, 104):
        x = r.take(0.75, [(0.05, "on", n, 100), (0.6, "off", n, 0)])
        i = onset(x, 100, 0.05)
        if i < 0:
            rows.append(dict(note=n, level=-240.0, cents=None, peak=0.0))
            continue
        seg = x[i + int(0.05 * FS):i + int(0.45 * FS), 0]
        f = A4 * 2 ** ((n - 69) / 12)
        fm = f0_of(seg, f)
        rows.append(dict(note=n, level=db(x[i:i + int(0.3 * FS)]), cents=1200 * np.log2(fm / f) if fm else None,
                         peak=float(np.abs(x).max()), centroid=centroid(seg)))
    rep["keyboard"] = rows
    lv = np.array([q["level"] for q in rows])
    jumps = [(rows[k]["note"], round(lv[k] - (lv[k - 1] + lv[k + 1]) / 2, 1)) for k in range(1, len(rows) - 1)
             if abs(lv[k] - (lv[k - 1] + lv[k + 1]) / 2) > 3]
    cents = [abs(q["cents"]) for q in rows if q["cents"] is not None]
    dead = [q["note"] for q in rows if q["level"] < -70]
    rep["keyboard_summary"] = dict(jumps=jumps, worst_cents=max(cents) if cents else None, dead=dead,
                                   level_span=float(lv.max() - lv[lv > -100].min()))
    ok = not dead and not jumps and (max(cents) < 15 if cents else False)
    return ok, (f"76 keys: level span {rep['keyboard_summary']['level_span']:.1f} dB, "
                f"uneven keys {jumps or 'none'}, worst tuning {max(cents):.1f} cents, silent {dead or 'none'}")


@test
def velocity(r, rep):
    r.dry()
    out = {}
    for n in (45, 57, 72, 84):
        lv, ce = [], []
        for v in range(8, 128, 8):
            x = r.take(0.6, [(0.05, "on", n, v), (0.45, "off", n, 0)])
            i = onset(x, 20, 0.05)
            seg = x[max(i, 0):max(i, 0) + int(0.3 * FS), 0]
            lv.append(db(seg))
            ce.append(centroid(seg) if len(seg) else 0)
        out[n] = dict(level=lv, centroid=ce)
    rep["velocity"] = out
    bad = [n for n, d in out.items() if np.any(np.diff(d["level"]) < -0.5)]
    rng = {n: round(max(d["level"]) - min(d["level"]), 1) for n, d in out.items()}
    return not bad, f"level range per key {rng} dB; non-monotonic {bad or 'none'}"


@test
def types(r, rep):
    out = {}
    ev = [(0.05, "on", 57, 100), (0.7, "off", 57, 0), (0.8, "on", 69, 100), (1.4, "off", 69, 0)]

    def bdist(p, q, fl=None):
        if fl is not None:                            # (information: only the bands within fl dB of the loudest)
            p, q = np.maximum(p, p.max() - fl), np.maximum(q, q.max() - fl)
        return float(np.sqrt(np.mean(((p - p.mean()) - (q - q.mean())) ** 2)))
    for t in range(RHODES_TYPES):                   # the Rhodes TYPEs (the 200A's are another engine)
        r.dry(TYPE=type_cc(t))
        ks = []
        for k in range(reps("types")):
            if k:
                r.settle()
            x = r.take(1.6, ev)
            # each window from its note's own onset (align: the A4's too), never from the recording's start (it
            # starts 10-60 ms off the MIDI clock: the windows cut into the attack at another place each take)
            on = align(x[:, 0], [0.05, 0.8])[0]
            a = x[on[0] + int(0.05 * FS):on[0] + int(0.55 * FS), 0]
            b = x[on[1] + int(0.05 * FS):on[1] + int(0.5 * FS), 0]
            # the two notes' band powers summed (before: the bands of the two windows joined end to end, whose
            # junction - a step at a sample the onset sets - splashed 100-117 dB of broadband into the gaps between
            # the partials: it alone moved the distances by up to 0.8 dB from take to take)
            ks.append(dict(level=(db(a) + db(b)) / 2, centroid_a3=centroid(a), centroid_a4=centroid(b),
                           bands=10 * np.log10(10 ** (bands_db(a) / 10) + 10 ** (bands_db(b) / 10))))
        med = np.median([q["bands"] for q in ks], 0)          # per band, over the takes
        out[t] = dict(level=float(np.median([q["level"] for q in ks])),
                      centroid_a3=float(np.median([q["centroid_a3"] for q in ks])),
                      centroid_a4=float(np.median([q["centroid_a4"] for q in ks])), bands=med,
                      repeat_db=max([bdist(q["bands"], med) for q in ks] + [0.0]))   # the takes' own spread
    rep["types"] = {t: {k: v for k, v in d.items() if k != "bands"} for t, d in out.items()}
    lv = [out[t]["level"] for t in out]
    # distinct: every pair of types differs in its spectral envelope (third-octave bands 100 Hz - 10 kHz, levels
    # aligned), by more than 1.5 dB RMS. One centroid ratio (> 1.3, the old limit) never held: the types differ in
    # their bell, bark and decay more than in their mean brightness.
    dist = [bdist(out[i]["bands"], out[j]["bands"]) for i in range(RHODES_TYPES) for j in range(i + 1, RHODES_TYPES)]
    rep["types_distance_db"] = dist
    rep["types_distance_60db"] = [bdist(out[i]["bands"], out[j]["bands"], 60.0) for i in range(RHODES_TYPES)
                                  for j in range(i + 1, RHODES_TYPES)]   # (information: the bands 60 dB down floored)
    spread = max(out[t]["repeat_db"] for t in out)
    rep["types_repeat_db"] = spread
    return min(dist) > 1.5 and max(lv) - min(lv) < 3, ("levels " + ", ".join(f"{lv[t]:.1f}" for t in range(RHODES_TYPES)) +
                                                     " dB; spectral distance between types min " + f"{min(dist):.2f} dB (" +
                                                     ", ".join(f"{d:.2f}" for d in dist) + f"); median of {reps('types')}"
                                                     f" takes, their own spread <= {spread:.2f} dB")


# The classic chords hold no octave: two notes an octave apart sum their coinciding partials (A3's 2nd and A4's 1st,
# exactly harmonic in the pickup) at the phase their onsets set, and a note landing one audio block (5.8 ms) after
# the others moved the chord's loudness by up to 2.0 LU (host: F06 A3-E4-A4; C3-G3-C4-E4 1.7 LU); without an octave
# the coincidences are tempered fifths and thirds, which drift through every phase within the window: <= 0.2 LU
# (tests/py/test_hil_determinism.py). (Before 2026-10-18: A3 E4 A4 and C3 G3 C4 E4.)
CH_NOTES = (57, 60, 64)                   # A3 C4 E4 (v100)
FX_NOTES = (48, 55, 64)                   # C3 G3 E4 (v108: the FM-1's own keys)
CHORD = [(0.05, "on", n, 100) for n in CH_NOTES] + [(1.25, "off", n, 0) for n in CH_NOTES]
FX = [
    ("drive 64", dict(DRIVE=64)), ("drive 127", dict(DRIVE=127)),
    ("comp 64", dict(CMP_SW=127, CMP_AMT=64)), ("comp 127", dict(CMP_SW=127, CMP_AMT=127)),
    ("trem pan", dict(TW_SW=64, TW_DEPTH=100, TW_MODE=0)), ("trem am", dict(TW_SW=64, TW_DEPTH=100, TW_MODE=127)),
    ("wah", dict(TW_SW=127, TW_DEPTH=100)),
    ("phaser", dict(PH_SW=127, PH_DEPTH=100, PH_FDBK=64)), ("chorus", dict(CP_SW=64, CP_DEPTH=100)),
    ("delay dig", dict(DL_SW=48, DL_DEPTH=80, DL_TIME=64)), ("delay ana", dict(DL_SW=80, DL_DEPTH=80, DL_TIME=64)),
    ("reverb 64", dict(REV=64)), ("reverb 127", dict(REV=127)),
    ("everything", dict(DRIVE=40, CMP_SW=127, CMP_AMT=64, TW_SW=64, TW_DEPTH=64, PH_SW=127, PH_DEPTH=64,
                        CP_SW=64, CP_DEPTH=64, DL_SW=48, DL_DEPTH=64, REV=64)),
]


def lfo_rates(kv):
    """the free-running LFOs (Hz) a classic setting (CC values by CC name) turns on: the tremolo (TREM/WAH = TREM),
    the phaser (CC 12), the chorus (CC 85 43..85); rates at their defaults unless kv sets them"""
    P, out = vp(), []
    if (kv.get("TW_SW", 0) * 3) >> 7 == 1:
        out.append(lfo_hz(kv.get("TW_RATE", P["TW_RATE"]["de"])))
    if kv.get("PH_SW", 0) >= 64:
        out.append(lfo_hz(kv.get("PH_SPEED", P["PH_SPEED"]["de"]), True))
    if 43 <= kv.get("CP_SW", 0) <= 85:
        out.append(lfo_hz(kv.get("CP_SPEED", P["CP_SPEED"]["de"]), True))
    return out


def cyc_win(base, rates, min_frac=0.6):
    """a window (s) of whole cycles of the one LFO running, the most that fit in base s (and >= min_frac * base);
    else base (no LFO, several, or a cycle longer than base: the reset holds its phase, +-1 block)"""
    if len(rates) != 1:
        return base
    n = int(base * rates[0])
    return n / rates[0] if n >= 1 and n / rates[0] >= min_frac * base else base


@test
def effects(r, rep):
    r.dry()
    ref = r.take(1.5, CHORD)
    i0 = max(onset(ref, t=0.05), 0)
    base_direct = db(ref[i0:i0 + int(0.25 * FS)])
    rows, bad = [], []
    for name, kv in FX:
        r.dry(**kv)
        x = r.take(2.2, CHORD)
        i = max(onset(x, t=0.05), 0)
        timed = name.startswith(("delay", "reverb"))     # echoes / a tail add level by design: check the direct
        w = cyc_win(1.1, lfo_rates(kv))                  # (a modulated level: whole cycles of its LFO)
        base = db(ref[i0:i0 + int(w * FS)])
        lv = db(x[i:i + int(0.25 * FS)]) - base_direct + base if timed else db(x[i:i + int(w * FS)])
        lu = (lufs(x[i:i + int(0.25 * FS)] / 32768.0) - lufs(ref[i0:i0 + int(0.25 * FS)] / 32768.0) if timed else
              lufs(x[i:i + int(w * FS)] / 32768.0) - lufs(ref[i0:i0 + int(w * FS)] / 32768.0))
        clip = int((np.abs(x) >= 32700).sum())
        row = dict(name=name, delta_db=lv - base, delta_lu=lu, peak_db=20 * np.log10(np.abs(x).max() / 32767 + 1e-12), clipped=clip,
                   lr_corr=float(np.corrcoef(x[i:i + FS, 0], x[i:i + FS, 1])[0, 1]) if i + FS < len(x) else 1.0,
                   tail_db=db(x[i + int(1.3 * FS):i + int(1.9 * FS)]), clicks=clicks(x), window_s=w)
        rows.append(row)
        row["with_tail_db"] = db(x[i:i + int(w * FS)]) - base
        lim = 99 if name.startswith(("comp", "everything")) else 3   # a compressor's level depends on the playing
        # loudness, not RMS: the wah's resonance adds +4.5 dB RMS in the mids but +0.2 LU (fx_all) to the ear
        wide = row["lr_corr"] < 0.8
        target = -2.5 if wide and name.startswith("chorus") else 0.0       # the chorus' width compensation (0.8;
        # the phaser has none: FXALL_EXPECT's note)
        if abs(row["delta_lu"] - target) > lim or clip or row["clicks"]:
            bad.append(name)
    rep["effects"] = dict(dry_db=db(ref[i0:i0 + int(1.1 * FS)]), rows=rows)
    summary = "; ".join(f"{q['name']} {q['delta_lu']:+.1f}" for q in rows)
    return not bad, (f"loudness vs dry (LU; delay/reverb: the direct sound; compressor: information): {summary}; "
                     f"problems: {bad or 'none'}")


@test
def bypass(r, rep):
    """the FX button (CC 111): with typical effects on vs bypassed, and bypassed vs no effects at all"""
    r.dry()
    a = r.take(1.5, CHORD)
    r.dry(PH_SW=127, PH_DEPTH=80, CP_SW=64, CP_DEPTH=64, REV=50, DRIVE=30, CMP_SW=127, CMP_AMT=50, CMP_GAIN=15)
    b = r.take(2.2, CHORD)
    r.cc_raw(111, 127)
    r.settle()
    c = r.take(1.5, CHORD)
    r.cc_raw(111, 0)
    lv = []
    for x in (a, b, c):
        i = max(onset(x, t=0.05), 0)
        lv.append(lufs(x[i:i + int(1.1 * FS)] / 32768.0))
    rep["bypass"] = dict(no_fx=lv[0], fx_on=lv[1], bypassed=lv[2])
    # the compressor as the panel switches it on (THRESH up: +2.8 dB makeup); before, it ran with no makeup and the
    # effects-on level sat 5 dB under the bypass by construction
    ok = abs(lv[2] - lv[0]) < 0.5 and abs(lv[1] - lv[2]) < 3
    return ok, f"no effects {lv[0]:.1f}, effects on {lv[1]:.1f}, bypassed {lv[2]:.1f} LUFS"


FXALL = [  # (name, settings, expected: LU re clean within +-tol)
    ("drive 20", dict(DRIVE=20)), ("drive 64", dict(DRIVE=64)), ("drive 127", dict(DRIVE=127)),
    ("comp on (-20 dB 4:1 +4.3)", dict(CMP_SW=127, CMP_AMT=64, CMP_RAT=64, CMP_GAIN=23)),
    ("comp -40 dB 20:1 +4.3", dict(CMP_SW=127, CMP_AMT=127, CMP_RAT=127, CMP_GAIN=23)),
    ("trem pan 32", dict(TW_SW=64, TW_DEPTH=32)), ("trem pan 127", dict(TW_SW=64, TW_DEPTH=127)),
    ("trem am 64", dict(TW_SW=64, TW_DEPTH=64, TW_MODE=127)), ("trem am 127 square", dict(TW_SW=64, TW_DEPTH=127, TW_MODE=127, TW_SHAPE=64)),
    ("trem am 127 amp law", dict(TW_SW=64, TW_DEPTH=127, TW_MODE=127, TW_SHAPE=127)),
    ("wah 32", dict(TW_SW=127, TW_DEPTH=32)), ("wah 127", dict(TW_SW=127, TW_DEPTH=127)),
    ("phaser 32", dict(PH_SW=127, PH_DEPTH=32)), ("phaser 80", dict(PH_SW=127, PH_DEPTH=80)),
    ("phaser 127 fb127", dict(PH_SW=127, PH_DEPTH=127, PH_FDBK=127)),
    ("chorus 32", dict(CP_SW=64, CP_DEPTH=32)), ("chorus 64", dict(CP_SW=64, CP_DEPTH=64)), ("chorus 127", dict(CP_SW=64, CP_DEPTH=127)),
    ("delay dig 50", dict(DL_SW=48, DL_DEPTH=50)), ("delay ana 127", dict(DL_SW=80, DL_DEPTH=127)),
    ("reverb 32", dict(REV=32)), ("reverb 127", dict(REV=127)),
    ("all on", dict(DRIVE=30, CMP_SW=127, CMP_AMT=64, CMP_RAT=64, CMP_GAIN=23, TW_SW=64, TW_DEPTH=50, PH_SW=127, PH_DEPTH=60,
                    CP_SW=64, CP_DEPTH=50, DL_SW=48, DL_DEPTH=40, REV=40)),
]
# fx_all's judgement (LU re clean): 0 +- 2 LU; the chorus 2.5 LU under when wide (fx.c: its width compensation, 0.8);
# the delays +1.5 +- 3. The phaser has no width compensation (its sides are 30 degrees apart since 2026-10-17, they
# were 90): 'phaser 127 fb127' (L/R 0.71) was judged against the chorus' -2.5 and failed at +1.6 LU, which is the
# feedback's resonance (fx.c fx_phaser; +1.5 .. +1.6 on the FM-1 USB and jack in every report since 2026-10-18).
# The settings below sit off 0 by what the effect does, measured the same on the FM-1 (USB and jack) in every saved
# report since 2026-10-18 (build/hil/full/20261018-134700, -191741, build/marimba/p5, build/pr4test,
# build/release-0.9.4, build/final: their spread 0.2 LU) and on the host (tools/hil_scan.py --host --only fx_all);
# each is judged against that value +- 1 LU (the device's run-to-run +-0.3 LU and a margin), so a change shows:
#  - drive 127: -2.2 .. -2.4 LU. The drive's level trim (tools/gen_fx_tables.py DRV_RISE, d868d8a) is the mean of
#    single notes and chords: at AMOUNT 127 a single note +2.4, a chord -4.3 LU (an amp's compression on big chords);
#    this 3-note chord sits between them;
#  - (phaser 32 was -2.4 .. -2.5 LU until 0.9.6: 0.70 x (dry + allpasses) cut where the notches sat. The phaser's
#    level is now held at every DEPTH (mix-fix 0c0be11): the host reads +0.7 LU, judged at 0 +- 2 as every setting.)
# Not here: 'wah 32' (-2.1) and 'wah 127' (+2.1) failed until 2026-10-24: fx.c's depth law (wg, x0.80 .. x1.26,
# fitted on the FM-1 in dba3831 to cancel the wah's level swing) was still applied after the 0.9.3 make-up gain
# (ce5ccfb0) that already holds the level: 20 log10(0.80) = -1.9, 20 log10(1.26) = +2.0. The law is gone; the host
# reads -0.1 / +0.0 LU, so they are judged at 0 +- 2 as every other setting.
# (Agent E's reading of the same reports, 2026-10-24, proposed all five measured values as targets +- 1 LU; taken for
# drive 127 and phaser 32, not for the wah rows (the defect above) nor phaser 127 fb127 (its fault was the width
# target: at 0 +- 2 it passes without a target of its own).)
FXALL_EXPECT = {"drive 127": (-2.3, 1.0)}


def lufs(x):
    """ITU-R BS.1770 K-weighted loudness of a (n, 2) or (n,) float array at 48 kHz"""
    from scipy.signal import lfilter
    b1, a1 = [1.53512485958697, -2.69169618940638, 1.19839281085285], [1, -1.69065929318241, 0.73248077421585]
    b2, a2 = [1.0, -2.0, 1.0], [1, -1.99004745483398, 0.99007225036621]
    x = np.atleast_2d(np.asarray(x, float).T).T
    return -0.691 + 10 * np.log10(sum(np.mean(lfilter(b2, a2, lfilter(b1, a1, x[:, c])) ** 2) for c in range(x.shape[1])) + 1e-15)


@test
def fx_all(r, rep):
    """every effect at several settings: loudness (LUFS) re clean on USB (and the jack when an interface is there),
    stereo width, peak, clipping, clicks; the chord C3 G3 E4 at v108 (the FM-1's own keys' velocity). Each setting: the
    median of REPEATS['fx_all'] takes; a modulated one over whole cycles of its LFO (cyc_win of 1.8 s)"""
    jack = None
    try:
        if isinstance(r, Rig):                       # (the device only: HostRig opens no MIDI / audio)
            import hil_jack as hj
            jack = hj.Jack("M6", 5)
            jack.r = r
    except Exception:
        pass
    ch = [(0.1, "on", n, 108) for n in FX_NOTES] + [(2.1, "off", n, 0) for n in FX_NOTES]
    nrep = reps("fx_all")

    def seg(x, w=1.8):
        a = np.abs(x) if x.ndim == 1 else np.abs(x).max(axis=1)
        i = int(np.argmax(a > 0.3 * a.max()))
        return x[i:i + int(w * FS)]

    def takes(jk=True):
        """nrep takes: [(USB (n, 2) / 32768, the jack or None)], each after the reset"""
        out = []
        for k in range(nrep):
            if k:
                r.settle()
            u = r.take(2.6, ch) / 32768.0
            j = jack.both(2.6, with_reset(ch))[1] if jack and jk else None
            out.append((u, j))
        return out

    def med(f, ts):
        return float(np.median([f(u, j) for u, j in ts]))
    r.dry()
    t0 = takes()
    rows, bad = [], []
    for name, kv in FXALL:
        r.dry(**kv)
        ts = takes()
        w = cyc_win(1.8, lfo_rates(kv))
        base_u = med(lambda u, j: lufs(seg(u, w)), t0)
        lus = [lufs(seg(u, w)) - base_u for u, j in ts]
        row = dict(name=name, usb_lu=float(np.median(lus)), usb_lu_spread=float(max(lus) - min(lus)), window_s=w,
                   peak_db=float(max(20 * np.log10(np.abs(u).max() + 1e-12) for u, j in ts)),
                   clipped=int(max((np.abs(u) >= 0.9995).sum() for u, j in ts)),
                   clicks=int(max(clicks(u * 32768) for u, j in ts)),
                   corr=med(lambda u, j: float(np.corrcoef(seg(u, w)[:, 0], seg(u, w)[:, 1])[0, 1]), ts))
        if jack:
            base_j = med(lambda u, j: lufs(seg(j, w)), t0)
            base_l = med(lambda u, j: lufs(seg(u, w)[:, 0]), t0)
            row["jack_lu"] = med(lambda u, j: lufs(seg(j, w)), ts) - base_j
            row["usbL_lu"] = med(lambda u, j: lufs(seg(u, w)[:, 0]), ts) - base_l   # the jack cable carries the left channel
        wide = row["corr"] < 0.8
        tol = 99.0 if name.startswith(("comp -40", "all on")) else 2.0     # extreme comp: MAKEUP is the player's
        target = -2.5 if wide and name.startswith("chorus") else 0.0       # the chorus' width compensation (0.8)
        if name.startswith("delay"):                  # the echoes add loudness by design (full mix: about +3 LU)
            target, tol = 1.5, 3.0
        if name in FXALL_EXPECT:                      # a setting whose level the effect sets by design (FXALL_EXPECT)
            target, tol = FXALL_EXPECT[name]
        if abs(row["usb_lu"] - target) > tol or row["clipped"] or row["clicks"] or \
                ("jack_lu" in row and abs(row["jack_lu"] - row["usbL_lu"]) > 1.0):
            bad.append(name)
        rows.append(row)
        print(f"    {name:28s} USB {row['usb_lu']:+5.1f} LU" + (f"  jack {row['jack_lu']:+5.1f}" if "jack_lu" in row else "") +
              f"  L/R {row['corr']:.2f}  peak {row['peak_db']:6.1f}  clip {row['clipped']}  clicks {row['clicks']}"
              f"  (window {w:.2f} s, {nrep} takes within {row['usb_lu_spread']:.2f} LU)", flush=True)
    # the FX bypass (CC 111) with everything on: back to the clean level (clean measured again right before)
    r.dry()
    base_u = med(lambda u, j: lufs(seg(u)), takes(False))
    r.dry(**dict(FXALL[-1][1]))
    r.cc_raw(111, 127)
    r.settle()
    byp = med(lambda u, j: lufs(seg(u)), takes(False)) - base_u
    r.cc_raw(111, 0)
    print(f"    {'FX bypass (all on)':28s} USB {byp:+5.1f} LU", flush=True)
    if abs(byp) > 0.5:
        bad.append("bypass")
    rep["fx_all"] = dict(rows=rows, bypass_lu=byp, base_usb_lufs=base_u, repeats=nrep)
    spread = max(q["usb_lu_spread"] for q in rows)
    return not bad, (f"{len(FXALL)} settings + bypass, median of {nrep} takes (their own spread <= {spread:.2f} LU); "
                     f"out of tolerance: {bad or 'none'}")


TOGGLES = [("drive 64", "DRIVE", 64, 0), ("comp", "CMP_SW", 127, 0), ("trem pan", "TW_SW", 64, 0), ("wah", "TW_SW", 127, 0),
           ("phaser", "PH_SW", 127, 0), ("chorus", "CP_SW", 64, 0), ("delay", "DL_SW", 48, 0), ("reverb 50", "REV", 50, 0)]


@test
def fx_toggle(r, rep):
    """each effect switched on and off while a chord sounds (as the panel does, at its starting depth): the loudness
    step at each switch (before vs after, the chord's own decay taken out) and clicks around it. The windows: from
    the chord's measured onset; whole cycles of the effect's LFO in 0.38-0.6 s when they fit , else 0.4 s"""
    starts = dict(TW_DEPTH=64, PH_DEPTH=80, CP_DEPTH=64, DL_DEPTH=50, CMP_AMT=64, CMP_RAT=64, CMP_GAIN=15)
    rows, bad = [], []
    ch = [(0.1, "on", n, 108) for n in FX_NOTES] + [(3.4, "off", n, 0) for n in FX_NOTES]

    def st(x, g, a, b):
        return lufs(x[g + int(a * FS):g + int(b * FS)] / 32768.0)
    r.dry(**starts)
    ref = r.take(3.6, ch)
    gr = r.lag(ref, ch)
    for name, key, on, off in TOGGLES:
        r.dry(**starts)
        x = r.take(3.6, ch + [(1.2, "cc", CC[key], on), (2.4, "cc", CC[key], off)])
        g = r.lag(x, ch)
        rates = lfo_rates({key: on})
        w = next((k / rates[0] for k in range(1, 9) if rates and 0.38 <= k / rates[0] <= 0.6), 0.4)
        # the step at a switch, the chord's natural decay (ref) taken out
        steps = []
        for sw in (1.2, 2.4):
            aa, bb = (sw + 0.05, sw + 0.05 + w), (sw - 0.05 - w, sw - 0.05)
            steps.append((st(x, g, *aa) - st(x, g, *bb)) - (st(ref, gr, *aa) - st(ref, gr, *bb)))
        up, dn = steps
        ck = clicks(x[g + int(1.1 * FS):g + int(1.4 * FS)]) + clicks(x[g + int(2.3 * FS):g + int(2.6 * FS)])
        rows.append(dict(name=name, on_step=up, off_step=dn, clicks=ck, window_s=w))
        lim = 3.5 if name in ("phaser", "chorus", "wah") else 4.0 if name == "comp" else 2.0   # wah: a moving
        # resonance (0.4 s windows catch its sweep); comp: the +2.8 dB makeup plus its attack / release
        if abs(up) > lim or abs(dn) > lim or ck:
            bad.append(name)
        print(f"    {name:10s} on {up:+5.1f} LU   off {dn:+5.1f} LU   clicks {ck}   (windows {w:.2f} s)", flush=True)
    rep["fx_toggle"] = rows
    return not bad, f"{len(TOGGLES)} effects switched on/off under a chord; out of tolerance: {bad or 'none'}"


@test
def delay_time(r, rep):
    r.dry(DL_SW=48, DL_DEPTH=100, DL_TIME=64, DL_FDBK=60)
    x = r.take(1.6, [(0.05, "on", 72, 110), (0.1, "off", 72, 0)])
    from scipy.signal import find_peaks
    env = np.sqrt(np.convolve(x[:, 0] ** 2, np.ones(480) / 480, "same"))
    i = max(onset(x, t=0.05), 0)
    # echoes: envelope peaks at least 100 ms apart, standing out of the decay between them
    pk, _ = find_peaks(env[i:], distance=int(0.1 * FS), prominence=max(10.0, 0.05 * env[i:].max()))
    pk = [i + k for k in pk if k > int(0.05 * FS)]
    exp_ms = 20 * 74 ** (64 / 127)
    gaps = np.diff([i] + pk) / FS * 1000 if pk else []
    rep["delay"] = dict(expected_ms=exp_ms, echoes_ms=list(map(float, gaps)))
    ok = len(gaps) >= 2 and abs(gaps[0] - exp_ms) < 15
    return ok, f"expected {exp_ms:.0f} ms, echoes at {[round(g) for g in gaps[:4]]} ms"


@test
def tremolo_rate(r, rep):
    r.dry(TW_SW=64, TW_DEPTH=127, TW_MODE=127, TW_RATE=64)
    ev = [(0.05, "on", 60, 100), (2.9, "off", 60, 0)]
    x = r.take(3.0, ev)
    g = r.lag(x, ev)                                  # 0.3 .. 2.8 s on the event clock, from the note's onset
    env = np.sqrt(np.convolve(x[:, 0] ** 2, np.ones(480) / 480, "same"))[g + int(0.3 * FS):g + int(2.8 * FS)]
    env = env / (np.convolve(env, np.ones(9600) / 9600, "same") + 1e-9) - 1
    sp = np.abs(np.fft.rfft(env * np.hanning(len(env)), 1 << 18))
    fr = np.fft.rfftfreq(1 << 18, 1 / FS)
    m = (fr > 0.3) & (fr < 15)
    f = float(fr[m][np.argmax(sp[m])])
    exp = 0.5 * 24 ** (64 / 127)
    rep["tremolo"] = dict(expected_hz=exp, measured_hz=f)
    return abs(f - exp) / exp < 0.15, f"expected {exp:.2f} Hz, measured {f:.2f} Hz"


@test
def sustain(r, rep):
    r.dry()
    ea = [(0.05, "on", 60, 100), (0.5, "off", 60, 0)]
    a = r.take(2.0, ea)
    r.dry()
    eb = [(0.02, "cc", 64, 127), (0.05, "on", 60, 100), (0.5, "off", 60, 0)]
    b = r.take(2.0, eb)
    r.setup(SUS=0)
    ga, gb = r.lag(a, ea), r.lag(b, eb)               # 1.0 .. 1.5 s on the event clock
    la, lb = db(a[ga + int(1.0 * FS):ga + int(1.5 * FS)]), db(b[gb + int(1.0 * FS):gb + int(1.5 * FS)])
    rep["sustain"] = dict(released_db=la, pedal_db=lb)
    return lb - la > 15, f"0.5 s after the key: damped {la:.1f} dB, with the pedal {lb:.1f} dB"


@test
def held_tail(r, rep):
    r.dry(REV=40)
    ev = [(0.05, "on", 57, 100)]
    x = r.take(16.0, ev)
    r.note_off(57)
    g = r.lag(x, ev)                                  # times on the event clock (the note-on at 0.05 s)
    env = [db(x[g + int(t * FS):g + int((t + 0.5) * FS)]) for t in np.arange(0, 15.5, 0.5)]
    nz = np.flatnonzero(np.abs(x).max(axis=1) > 0)
    rep["held_tail"] = dict(env_db=env, last_nonzero_s=float((nz[-1] - g) / FS) if len(nz) else 0.0,
                            hiss_8k_db=None)
    last = rep["held_tail"]["last_nonzero_s"]
    return last < 15.0, f"held A3 with reverb: output reaches exact zero at {last:.1f} s (gate)"


@test
def stress(r, rep):
    """8+ voices, fast repeats, pedal, all effects: dropouts show as clicks / zero runs"""
    r.dry(**dict(FX[-1][1]))
    ev = [(0.02, "cc", 64, 127)]
    t = 0.1
    chords = [(36, 48, 55, 60, 64, 67, 72, 76, 79, 84), (38, 50, 57, 62, 65, 69, 74, 77, 81, 86)]
    for k in range(16):
        for n in chords[k % 2]:
            ev.append((t, "on", n, 127))
        t += 0.25
        for n in chords[k % 2]:
            ev.append((t - 0.01, "off", n, 0))
    ev.append((t + 0.1, "cc", 64, 0))
    x = r.take(t + 1.5, ev)
    g = r.lag(x, ev)
    c, z = clicks(x[g + int(0.1 * FS):g + int(t * FS)]), zero_runs(x[g + int(0.2 * FS):g + int(t * FS)])
    rep["stress"] = dict(clicks=c, zero_runs=z, peak_db=20 * np.log10(np.abs(x).max() / 32767 + 1e-12),
                         clipped=int((np.abs(x) >= 32700).sum()))
    return c == 0 and z == 0, f"clicks {c}, zero runs {z}, peak {rep['stress']['peak_db']:.1f} dBFS, clipped {rep['stress']['clipped']}"


PR_COUNT = 17          # F01..F17 (presets.c PR_NF): F01..F12 Rhodes, F13..F15 Rhodes (0.9.5), F16 / F17 the 200A
# each factory preset's loudness re F01 (LU) and its tolerance, the chord A3 C4 E4 v100 (CHORD): the Rhodes presets
# 0 +- 1.5 (AGENTS.md), the 200A's F16 WURLI 200A / F17 200A VIB too.
# The 0.9.5 presets (host): F02..F12 -0.7..+0.8 (F11 -1.6 since its LEVEL 10: HOST_SHIFT)
# F12 AUTO WAH (B, the user's pick): its deep 1/4 band-pass sweep reads +1.6 LU on the chord and -2.3 on a phrase at
# LEVEL 127 (the maximum): a documented exception, 0 +- 2.0
# F13..F15 (TAPE ECHO, DREAM, RADIO: the starters U10..U12 until 0.9.5's RC) keep the starters' 1 LU
PRESET_TARGET = {12: (0.0, 2.0), 13: (0.0, 1.0), 14: (0.0, 1.0), 15: (0.0, 1.0)}
# the user bank's starters (presets.c PR_NS: U01..U09 in a bank never saved): judged on the host only (a device's bank
# may hold the player's own presets), each within 1 LU of F01 on the chord; presets 18.. in the rows = U01..
PR_STARTERS = 9


def preset_pc(p):
    """the program change of row p (0-based: F01..F17, then U01..): presets.c preset_of_pc, PC 0..15 F01..F16,
    16..31 U01..U16, 32 F17"""
    return p if p < 16 else p + 16 if p < PR_COUNT else p - PR_COUNT + 16


# a preset the host reads at another level than the FM-1 (re F01, LU), its host target shifted by that much: F11 RESO
# SWEEP's OHCD drive into the resonant LP12 reads 1.9 LU louder on the FM-1 (2026-10-25, LEVEL 18: the FM-1 +1.8 on
# USB and the jack, the host -0.1); at LEVEL 10 the FM-1 ~+0.3, the host -1.6
HOST_SHIFT = {11: -1.9}


def preset_target(p, host=False):
    t, tol = PRESET_TARGET.get(p, (0.0, 1.0) if p > PR_COUNT else (0.0, 1.5))
    return (t + HOST_SHIFT.get(p, 0.0) if host else t), tol


def preset_label(p):
    return f"F{p:02d}" if p <= PR_COUNT else f"U{p - PR_COUNT:02d}"


@test
def presets(r, rep):
    """F01..F17 (and on the host the user bank's starters U01..U09: PR_STARTERS): the chord's loudness, the median of REPEATS['presets'] takes each (every take after the reset), on USB
    and on the headphone jack when the interface is there (Rig.jack(): the M6 input 5, recorded at once; the jack is
    the reference). The takes go to build/hil/presets/ (F01-1.npz ...: usb, jack)"""
    jk = isinstance(r, Rig) and r.jack()
    d = os.path.join(OUT, "presets")
    os.makedirs(d, exist_ok=True)
    rows = []

    def level(x, thr, t=0.05):
        i = max(onset(x, thr, t), 0)
        return lufs(x[i:i + int(1.1 * FS)]), i
    for p in range(PR_COUNT + (PR_STARTERS if isinstance(r, HostRig) else 0)):
        r.cc_raw(120, 0)
        r.pc(preset_pc(p))
        ks = []
        for k in range(reps("presets")):
            quiet = r.settle()                         # (False: no exact silence in 10 s, a note still sounds)
            if jk:
                dd = r.both(2.0, with_reset(CHORD), jack=True)
                x, j = dd["usb"] * 32768, dd["jack"][:, 0]
            else:
                x, j = r.take(2.0, CHORD), None
            lv, i = level(x / 32768.0, 200 / 32768)
            q = dict(level=lv, onset_ms=1000 * i / FS, rms_db=db(x[i:i + int(1.1 * FS)]),
                     peak_db=20 * np.log10(np.abs(x).max() / 32767 + 1e-12),
                     clipped=int((np.abs(x) >= 32700).sum()), clicks=clicks(x), quiet=bool(quiet),
                     gaps=[str(g) for g in getattr(r, "xruns", []) if "gap" in str(g)])
            if j is not None:
                floor = float(np.abs(j[:int(0.04 * FS)]).max()) if len(j) else 0.0
                q["jack"], ij = level(j, max(4 * floor, 0.01 * float(np.abs(j).max())))
                q["jack_onset_ms"] = 1000 * ij / FS
            ks.append(q)
            np.savez_compressed(os.path.join(d, f"{preset_label(p + 1)}-{k + 1}.npz"), usb=(x / 32768).astype(np.float32),
                                jack=(j if j is not None else np.zeros(0)).astype(np.float32))
        lv = [q["level"] for q in ks]
        row = dict(preset=p + 1, level=float(np.median(lv)), spread_lu=float(max(lv) - min(lv)),
                   rms_db=float(np.median([q["rms_db"] for q in ks])), peak_db=max(q["peak_db"] for q in ks),
                   clipped=max(q["clipped"] for q in ks), clicks=max(q["clicks"] for q in ks),
                   onset_ms=[round(q["onset_ms"], 1) for q in ks], not_quiet=sum(not q["quiet"] for q in ks),
                   gaps=sum(len(q["gaps"]) for q in ks))
        if jk:
            jl = [q["jack"] for q in ks]
            row.update(jack=float(np.median(jl)), jack_spread_lu=float(max(jl) - min(jl)),
                       jack_onset_ms=[round(q["jack_onset_ms"], 1) for q in ks])
        rows.append(row)
    rep["presets"] = rows
    lv = [q["level"] for q in rows]
    bad = [q["preset"] for q in rows if q["clipped"] or q["clicks"]]
    loud = [q["preset"] for q in rows if q["not_quiet"]]      # a take started over a sound that never ended
    gapped = [q["preset"] for q in rows if q["gaps"]]
    r.pc(0)
    spread = max(q["spread_lu"] for q in rows)
    ok = max(lv) - min(lv) < 8 and not bad and not loud and not gapped
    msg = (f"F01..{preset_label(len(rows))} loudness " + ", ".join(f"{v:.1f}" for v in lv) + f" LUFS (span {max(lv) - min(lv):.2f}; median of"
           f" {reps('presets')} takes, their own spread <= {spread:.2f} LU); clipping/clicks {bad or 'none'}")
    if loud:
        msg += f"; no exact silence before a take at {[preset_label(q) for q in loud]} (a note still sounding: those levels are not the preset)"
    if gapped:
        msg += f"; a recording gap at F{gapped}"
    # each preset re F01 against its target (PRESET_TARGET): the Rhodes F01..F12 within 1.5 LU (AGENTS.md's rule; F07
    # -1.9, F08 -1.7, F10 -4.0 LU until their LEVEL was set, 2026-10-24)
    host = isinstance(r, HostRig)
    off = [(q["preset"], v - lv[0]) for q, v in zip(rows, lv)
           if abs(v - lv[0] - preset_target(q["preset"], host)[0]) > preset_target(q["preset"], host)[1]]
    rep["presets_off_target"] = [dict(preset=p, lu=float(d)) for p, d in off]
    ok = ok and not off
    msg += ("; re F01 " + ", ".join(f"{preset_label(q['preset'])} {v - lv[0]:+.1f}" for q, v in zip(rows, lv) if q["preset"] > 1) +
            " LU; off target: " + (", ".join(f"{preset_label(p)} {d:+.1f}" for p, d in off) or "none"))
    if jk:                                             # the jack: the reference (what the player hears)
        jl = [q["jack"] for q in rows]
        rel = [(a - lv[0]) - (b - jl[0]) for a, b in zip(lv, jl)]      # USB vs jack, each re its own F01
        mis = [q["preset"] for q, m in zip(rows, rel) if abs(m) > MISMATCH_DB]
        ok = ok and max(jl) - min(jl) < 8
        msg += (f"; jack (re its own scale) {', '.join(f'{v - jl[0]:+.1f}' for v in jl)} LU re F01 (span"
                f" {max(jl) - min(jl):.2f}, spread <= {max(q['jack_spread_lu'] for q in rows):.2f}); USB-jack"
                f" disagreement <= {max(abs(m) for m in rel):.2f} LU{' at F' + str(mis) if mis else ''}")
    return ok, msg


# ==================================================================================================================
# The measures library: USB and the jack recorded at once, the parameters and CCs read from params.c, the expected
# rates and times from firmware/gen/fx_tables.h (used by the host tests and the other HIL tools; the device suites that
# used them, VIBES / MARIMBA, are gone with the bar engine). Levels and spectra are judged on the USB's LEFT channel
# (what the jack cable carries) and on the jack, side by side; a level difference the two disagree on by more than
# MISMATCH_DB is flagged.
# ==================================================================================================================
MISMATCH_DB = 1.0              # USB (left) vs jack: a level change both must agree on
_VP, _FXT = None, {}


def vp():
    """every parameter in params.c PARAMS[]: name -> dict(cc, hi, de, fmt); the CCs are read here, never typed in"""
    global _VP
    if _VP is None:
        import re
        src = open(os.path.join(ROOT, "firmware", "src", "params.c")).read()
        _VP = {n: dict(cc=int(c), hi=int(h), de=int(d), fmt=f)
               for n, h, d, f, c in re.findall(r'\[P_(\w+)\] = \{"[^"]*", (\d+), (\d+), (F_\w+), (\d+)', src)}
        names = re.search(r'N_SYNC\[\] = \{([^}]*)\}', src).group(1)
        _VP["_SYNC"] = re.findall(r'"([^"]*)"', names)
    return _VP


def sync_idx(name):
    return vp()["_SYNC"].index(name)


def fx_table(name):
    if name not in _FXT:
        import re
        src = open(os.path.join(ROOT, "firmware", "gen", "fx_tables.h")).read()
        m = re.search(r'%s\[\d+\] = \{([^}]*)\}' % name, src)
        _FXT[name] = [int(v) for v in re.findall(r'-?\d+', m.group(1))]
    return _FXT[name]


def lfo_hz(v, half=False):
    """F_HZ (tremolo, filter LFO, motor) and F_HZH (chorus, phaser: half): FX_LFO_INC[v] * 44100 / 2^32"""
    hz = fx_table("FX_LFO_INC")[int(v)] * 44100 / 2 ** 32
    return hz / 2 if half else hz


def dly_ms(v):
    return fx_table("FX_DLY_T")[int(v)] * 1000 / 44100


def ccv(name, x):
    """the CC value that sets parameter name to x (its index for an enum / switch, else 0..127), as midi_cc() reads it"""
    p = vp()[name]
    if name == "CP_SW":                        # CC 85, the combined CHO/PHA switch: 43..85 chorus (86..: the phaser)
        return 64 if x else 0
    if name == "DRV_SW":                       # CC 84, the drive TYPE's band table
        return drv_cc(int(x))
    if p["hi"] >= 127:
        return int(x)
    if p["hi"] == 2:
        return (0, 64, 127)[int(x)]
    return min(127, (int(x) * 128 + 64) // (p["hi"] + 1))


def ccs(settings):
    return [(vp()[n]["cc"], ccv(n, x)) for n, x in settings]


def with_(base, *kv):
    """base settings with some replaced in place (order kept: CC 85 before the chorus' 86 / 87), others appended"""
    out = list(base)
    for k, v in kv:
        for i, (n, _) in enumerate(out):
            if n == k:
                out[i] = (k, v)
                break
        else:
            out.append((k, v))
    return out


# ---------------------------------------------------------------- a take: USB and the jack at once
class Take:
    def __init__(self, tag, dur, ev, usb, jack):
        self.tag, self.dur, self.ev, self.usb, self.jack = tag, dur, ev, usb, jack
        self.sched = [t for t, k, a, b in ev if k == "on"]
        self._on = {}

    def srcs(self):
        return [s for s, x in (("usb", self.usb), ("jack", self.jack)) if x is not None]

    def m(self, src):
        return self.usb[:, 0] if src == "usb" else self.jack

    def on(self, src):
        """the onsets (samples) of the note-ons, in event order"""
        if src not in self._on:
            self._on[src] = align(self.m(src), self.sched)
        return self._on[src][0]

    def lat(self, src):
        self.on(src)
        return self._on[src][1]

    def lat_at(self, src, t):
        """the latency (s) in force at event time t: the last note-on's at or before it (the USB stream can slip)"""
        self.on(src)
        ls = [l for s, l in zip(self.sched, self._on[src][2]) if s <= t + 1e-9]
        return ls[-1] if ls else self._on[src][1]

    def slip_ms(self, src):
        """the largest change of the latency between the note-ons (ms): a stream that slipped"""
        self.on(src)
        ls = self._on[src][2]
        return float(1000 * (max(ls) - min(ls))) if ls else 0.0

    def at(self, src, t):
        """a sample index for event time t (s) in this source"""
        return int(round((t + self.lat_at(src, t)) * FS))

    def pre(self, src):
        """the pre-roll: the source before the first note-on reaches it (5 ms margin). USB: exact zero (the gate);
        the jack: the interface's and the FM-1's analogue hiss, the floor its measures stand on"""
        on = [t for t, k, a, b in self.ev if k == "on"]
        if not on:
            return self.m(src)[:0]
        return self.m(src)[:max(0, self.at(src, min(on)) - FS // 200)]


ALIGN_DENSE_S = 0.25  # align(): a note-on this soon after the previous one (a roll) is not searched for a slip: a
                       # re-strike of a ringing bar need not rise 3 dB in 1 ms frames, and the search then took a
                       # later strike for it (the 12 Hz rolls read 'the USB stream slipped' 217-338 ms)


def align(m, sched, lat_max=0.3, slip_max=0.35):
    """onsets of the scheduled note-ons: the first sound (5 % of the take's peak, never before the first event: a
    residue in the pre-roll is not a note) belongs to the last strike due before it (within lat_max): that gives the
    latency; each onset is then refined to a 6 dB rise within +-8 ms of its prediction. The USB stream can slip
    (round 1: +232 / -25 ms between strikes in the silent gaps of the VOICE takes; the jack, recorded at once, shows
    no slip): when the prediction holds no rise, the onset is searched +-slip_max around it (after the previous one)
    for the first rise of 10 dB, and the next prediction follows the latency found. Returns the onsets, the first
    latency (s) and the latency at each onset (s)."""
    if not sched:
        return [], 0.0, []
    a = np.abs(m)
    i0 = max(0, int((sched[0] - 0.005) * FS))
    mx = float(a[i0:].max()) if len(a) > i0 else 0.0
    if mx <= 0:
        return [int(s * FS) for s in sched], 0.0, [0.0] * len(sched)
    i = i0 + int(np.argmax(a[i0:] > 0.05 * mx))
    t = i / FS
    ks = [k for k, s in enumerate(sched) if s - 0.01 <= t <= s + lat_max]
    k = ks[-1] if ks else int(np.argmin([abs(t - s) for s in sched]))
    lat = t - sched[k]
    w = FS // 1000
    nf = len(m) // w
    e = 10 * np.log10(np.mean(np.asarray(m[:nf * w], float).reshape(nf, w) ** 2, axis=1) + 1e-20)   # 1 ms frames

    def rise(f, before=12):
        if f < 3 or f + 3 > nf:
            return 0.0
        return float(e[f:f + 3].max() - e[max(0, f - before):f - 1].max())
    out, lats, prev, prev_s = [], [], -1, None
    for s in sched:
        dense = prev_s is not None and 0 < s - prev_s < ALIGN_DENSE_S
        prev_s = s
        j = int(round((s + lat) * FS))
        a0, a1 = max(0, j - 10 * w), min(len(m), j + 8 * w)
        x = m[a0:a1]
        n = len(x) // w
        if n > 3:
            en = np.sqrt(np.mean(x[:n * w].reshape(n, w) ** 2, axis=1)) + 1e-9
            r = np.flatnonzero(en[2:] > 2 * en[:-2])
            if len(r):
                j = a0 + (int(r[0]) + 1) * w
        f = j // w
        r0 = max(rise(g) for g in range(max(1, f - 8), f + 9))
        if r0 < 3.0 and not dense:                       # nothing starts where it was due: the stream slipped?
            lo = max(prev // w + 30 if prev >= 0 else 0, f - int(slip_max * 1000))
            hi = min(nf - 3, f + int(slip_max * 1000))
            cand = [g for g in range(lo, hi) if rise(g) >= 15.0 and e[g:g + 3].max() > e[lo:hi + 3].max() - 30]
            if cand:
                j = min(cand, key=lambda c: abs(c - f)) * w
                lat = j / FS - s
        out.append(j)
        lats.append(j / FS - s)
        prev = j
    return out, lats[0], lats


def to_fs(x, sr, dur):
    """a capture at a device's own rate -> FS (polyphase), dur s long; (n,) or (n, ch)"""
    if sr != FS:
        from math import gcd
        from scipy.signal import resample_poly
        g = gcd(FS, sr)
        x = resample_poly(x, FS // g, sr // g, axis=0)
    n = int(dur * FS)
    return np.pad(x, [(0, max(0, n - len(x)))] + [(0, 0)] * (np.ndim(x) - 1))[:n]


def clicks_guarded(m16, guards, before_ms=3, after_ms=17):
    """hil_scan's clicks() detector (the same formula) with guards around the note events (3 ms before, 17 after)"""
    d2 = np.abs(np.diff(m16, 2))
    lvl = np.convolve(np.abs(m16), np.ones(480) / 480, "same")[1:-1] + 50
    hit = np.flatnonzero(d2 > 6 * lvl + 2000) + 1
    ok = np.ones(len(m16), bool)
    for i in guards:
        ok[max(0, i - before_ms * FS // 1000):max(0, i + after_ms * FS // 1000)] = False
    return int(ok[hit].sum())


# ---------------------------------------------------------------- measures (FS 48 kHz; mono unless named)
def rdb(x):
    x = np.asarray(x, float)
    return float(20 * np.log10(np.sqrt(np.mean(x * x)) + 1e-12)) if x.size else float("nan")


def cut(m, i, a, b):
    return m[max(0, i + int(a * FS)):max(0, i + int(b * FS))]


def lu(x):
    return float(lufs(x))


def third(x):
    """1/3-octave band levels (dB), 50 Hz .. 15.9 kHz"""
    S = np.abs(np.fft.rfft(x * np.hanning(len(x)))) ** 2
    fr = np.fft.rfftfreq(len(x), 1 / FS)
    return np.array([10 * np.log10(S[(fr >= c * 2 ** (-1 / 6)) & (fr < c * 2 ** (1 / 6))].sum() + 1e-30)
                     for c in [50 * 2 ** (i / 3) for i in range(26)]])


def sdist_bands(b1, b2):
    """the bands sdist averages over: those within 60 dB of the louder take's strongest band"""
    b1, b2 = np.asarray(b1), np.asarray(b2)
    r1, r2 = b1 - 10 * np.log10(np.sum(10 ** (b1 / 10))), b2 - 10 * np.log10(np.sum(10 ** (b2 / 10)))
    fl = max(r1.max(), r2.max()) - 60.0
    return (r1 > fl) | (r2 > fl)


def sdist(b1, b2, use=None):
    """level-aligned spectral distance, dB RMS (3 dB = a clear timbre change). use: the
    bands to average over (default sdist_bands of these two; the jack: USB's)"""
    b1, b2 = np.asarray(b1), np.asarray(b2)
    t1, t2 = 10 * np.log10(np.sum(10 ** (b1 / 10))), 10 * np.log10(np.sum(10 ** (b2 / 10)))
    r1, r2 = b1 - t1, b2 - t2
    fl = max(r1.max(), r2.max()) - 60.0
    use = (r1 > fl) | (r2 > fl) if use is None else np.asarray(use, bool)
    if not use.any():
        return 0.0
    d = np.maximum(r1, fl) - np.maximum(r2, fl)
    return float(np.sqrt(np.mean(d[use] ** 2)))


def frame_dist(x1, x2, n=2048):
    """the spectral distance (sdist) of two takes frame by frame (n samples, 43 ms), the median over the frames: for
    two settings of a swept effect whose sweeps run in step (CC 10 restarts the LFO at the take, +-1 block), where
    the whole window's spectrum averages the sweep away. A PHASER FDBK resonance moves with the notches; over the
    5 s window the FM-1's jack read FDBK 0 vs 127 1.8 dB apart (the old measure), frame by frame 3.9 dB (2026-10-18)"""
    k = min(len(x1), len(x2)) // n
    if k < 1:
        return float("nan")
    return float(np.median([sdist(third(x1[i * n:(i + 1) * n]), third(x2[i * n:(i + 1) * n])) for i in range(k)]))


def crest_db(m, i):
    """the strike's crest: the peak in 0..30 ms from onset i re the rms of 0.1..0.4 s (dB); information since
    2026-10-24 (strike_db)"""
    return 20 * np.log10(np.abs(cut(m, i, 0, 0.03)).max() + 1e-12) - rdb(cut(m, i, 0.1, 0.4))


def strike_db(m, i):
    """the strike's level: rms 0..20 ms from onset i re 0.1..0.4 s (dB), COMP ATTACK's measure since 2026-10-24. The
    crest (crest_db) read the vibraphone's first peak, 2 ms after the onset, which no ATTACK holds (the compressor's
    gain moves once per 16 samples), so VIBES read crest 18.6 / 20.8 / 20.5 dB at ATTACK 0 / 40 / 127 (+2.0, at the
    limit, on the FM-1 in every report since 2026-10-18 and on the host alike); the strike's energy shows the attack:
    the VIBES host 1.3 / 4.2 / 10.9 dB (+9.7), the FM-1's VIBES takes of 2026-10-17 0.2 / 3.1 / 10.7, MARIMBA (FM-1,
    2026-10-19) 0.3 / 5.5 / 14.3 (tests/py/test_hil_limits.py). Agent E proposed max(crest 40, 127) - crest 0 >= +1.5
    dB (2026-10-24): the same blind measure with a lower limit; the strike's level keeps the 2 dB with a 7 dB margin"""
    return rdb(cut(m, i, 0, 0.02)) - rdb(cut(m, i, 0.1, 0.4))


def hr():
    sys.path.insert(0, os.path.join(ROOT, "tests", "py"))
    import hostrun
    return hostrun


def env_db(x, hop=240):
    n = len(x) // hop
    return 20 * np.log10(np.sqrt(np.mean(np.asarray(x[:n * hop], float).reshape(n, hop) ** 2, axis=1)) + 1e-9)


def rate_of(tr, fps, guess, lo=0.4, hi=2.6):
    return float(hr().rate_of(np.asarray(tr, float), fps, guess, lo=lo, hi=hi))


def lfo_rate(tr, fps, lo_hz, hi_hz, H=6, pearson=False, octave=False):
    """the repetition rate (Hz) of a periodic trace whose harmonics may outweigh its fundamental (a band's level as a
    triangle LFO sweeps notches up and down: the jack's 1/4 at 69 BPM had harmonics 3 and 4 at 2.5x the fundamental's
    amplitude, and rate_of's harmonic sum picked the 3rd: PHASER SYNC read 1.72 Hz for 1.15). The period is the
    shortest lag at which the trace repeats itself: the autocorrelation's (over the whole trace's energy, so a long
    lag's few overlapping frames do not win) first peak of at least 0.85 of its best one between 1 / hi_hz and
    1 / lo_hz (at most 80 % of the trace), refined by a least-squares fit of H harmonics on a quadratic trend
    (+-8 %). A trace shorter than a period returns about the longest lag (tests/py/test_phaser_measures.py).
    pearson: each lag's correlation over its own overlap (lags up to 0.6 of the trace), not over the whole trace's
    energy, which shrinks a long lag's peak by its overlap: MARIMBA's PHASER SPEED 0 (a 4 s period, 3 dips a cycle
    on the roll) correlates 0.63 at 2 s and 0.99 at 4 s, but 0.50 / 0.58 by energy, and the 2 s peak passed the 0.85
    rule: 0.50 Hz on the host and the FM-1.
    octave: the octave check: a rate f whose fit (2H harmonics of f) holds under 10 % of its even harmonics' power in
    its odd ones is a wave at 2f read at two periods; then 2f (refined the same way) when 2f <= hi_hz"""
    d = detrend2(np.asarray(tr, float))
    n = len(d)
    lmin = max(2, int(fps / hi_hz))
    lmax = max(lmin + 2, min(int((0.6 if pearson else 0.8) * n), int(np.ceil(fps / lo_hz))))
    r = np.full(lmax + 2, -1.0)
    e = np.dot(d, d) + 1e-30
    with np.errstate(invalid="ignore", divide="ignore"):  # (pearson on a flat stretch: nan, never a peak)
        for lag in range(lmin, min(lmax + 2, n - 1)):
            r[lag] = np.corrcoef(d[:-lag], d[lag:])[0, 1] if pearson else np.dot(d[:-lag], d[lag:]) / e
    pk = [lag for lag in range(lmin + 1, lmax + 1) if r[lag] >= r[lag - 1] and r[lag] >= r[lag + 1] and r[lag] > 0]
    best = max((r[lag] for lag in pk), default=None)
    l0 = min(lag for lag in pk if r[lag] >= 0.85 * best) if pk else lmax
    t = np.arange(n) / fps
    tn = (t - t.mean()) / (t.max() - t.min() + 1e-9)
    trend = [np.ones_like(t), tn, tn * tn]

    def resid(f):
        A = np.column_stack(trend + [g(2 * np.pi * h * f * t) for h in range(1, H + 1) for g in (np.cos, np.sin)])
        return np.sum((d - A @ np.linalg.lstsq(A, d, rcond=None)[0]) ** 2)
    fine = fps / l0 * np.linspace(0.92, 1.08, 161)
    f0 = float(fine[int(np.argmin([resid(f) for f in fine]))])
    if octave and 2 * f0 <= hi_hz:
        A = np.column_stack(trend + [g(2 * np.pi * h * f0 * t) for h in range(1, 2 * H + 1) for g in (np.cos, np.sin)])
        c = np.linalg.lstsq(A, d, rcond=None)[0][3:]
        p = c[0::2] ** 2 + c[1::2] ** 2                 # (the power of harmonics 1 .. 2H)
        if p[0::2].sum() < 0.1 * p[1::2].sum():
            fine = 2 * f0 * np.linspace(0.96, 1.04, 81)
            f0 = float(fine[int(np.argmin([resid(f) for f in fine]))])
    return f0


def band_trace(x, lo, hi, n=2048, hop=240):
    """a band's power per frame (5 ms hop)"""
    fr = np.fft.rfftfreq(n, 1 / FS)
    m = (fr >= lo) & (fr < hi)
    w = np.hanning(n)
    idx = np.arange(0, len(x) - n, hop)
    return np.array([np.sum(np.abs(np.fft.rfft(x[i:i + n] * w))[m] ** 2) for i in idx]), FS / hop


def rel_trace(t, r, q, a, b, lo, hi):
    """a band's level (dB, 5 ms frames) of take t re the reference take r (the same stimulus, the effect at 0), from
    each one's onset: what the control moves, not the chord's decay and beating; and its distance over t's floor"""
    x, y = cut(t.m(q), t.on(q)[0], a, b), cut(r.m(q), r.on(q)[0], a, b)
    n = min(len(x), len(y))
    A, fps = band_trace(x[:n], lo, hi)
    B, _ = band_trace(y[:n], lo, hi)
    fl = t.pre(q)
    F = band_trace(fl, lo, hi)[0].mean() if len(fl) > 2400 and np.any(fl) else 0.0
    return 10 * np.log10((A + 1e-30) / (B + 1e-30)), 10 * np.log10(A / (F + 1e-30) + 1e-30), fps


def detrend2(d):
    k = np.arange(len(d))
    return d - np.polyval(np.polyfit(k, d, 2), k)


def edb1(x, w=96, hop=48):
    """the power envelope in dB, 1 ms frames (a 2 ms window)"""
    return 10 * np.log10(np.convolve(np.asarray(x, float) ** 2, np.ones(w) / w, "same")[::hop] + 1e-20)


def echo_cep_ms(x, y, b_s, lo, hi):
    """the delay as the peak of the cepstrum of x minus y's (y: the same stimulus without the echo): an echo is a
    ripple of the log spectrum, the notes' own pitch cancels; (ms, the peak's z-score)"""
    a, b = int(0.005 * FS), int(b_s * FS)
    n = 1 << int(np.ceil(np.log2(2 * b)))

    def cep(s):
        s = s[a:b]
        return np.fft.irfft(np.log(np.abs(np.fft.rfft(s * np.hanning(len(s)), n)) + 1e-9))
    m = min(len(x), len(y))
    c = cep(x[:m]) - cep(y[:m])
    qs = np.arange(int(lo * FS / 1000), int(hi * FS / 1000))
    k = qs[int(np.argmax(c[qs]))]
    return k * 1000.0 / FS, float(c[k] / (np.std(c[qs]) + 1e-12))


def echo_ms(x, y, exp_ms=None, flr_db=-120.0, t_min=30, rel=-20.0):
    """the first echo's arrival (ms; x and y from their onsets; y: the delay at DEPTH 0, the same stimulus). Round 1
    took the first envelope peak after the direct sound's: a vibraphone note keeps rising 60-90 ms (the tube), so
    it read 33-42 ms for every TIME. Here: a short delay (exp_ms <= 60: a comb on the note itself) by the cepstrum
    against y; else the first frame from t_min where x stands 6 dB over y (the direct sound with the delay's dry
    share) and over its floor, for 3 ms, within rel dB of the direct's peak (a residue of an earlier take is not an
    echo)"""
    if exp_ms is not None and exp_ms <= 60:
        k, z = echo_cep_ms(x, y, 0.3, 5, 60)
        return k if z >= 6 else float("nan")
    n = min(len(x), len(y))
    ex, ey = edb1(x[:n]), edb1(y[:n])

    def mis(k):
        d = (ex - np.roll(ey, k))[10:80]
        return np.sum(np.abs(d - np.median(d)))
    ey = np.roll(ey, min(range(-6, 7), key=mis))           # the two takes' onsets to the ms
    base = np.median((ex - ey)[5:15])
    top = ex[:150].max()
    exc = np.where(ex >= top + rel, ex - np.maximum(ey + base, flr_db + 6), -99.0)
    ok = np.flatnonzero((exc[t_min:-3] >= 6) & (exc[t_min + 1:-2] >= 6) & (exc[t_min + 2:-1] >= 6))
    if not len(ok):
        return float("nan")
    k = int(ok[0]) + t_min
    while k > t_min and ex[k - 1] - max(ey[k - 1] + base, flr_db + 6) >= 1.0:   # back to where it began to stand out
        k -= 1
    return float(k)


# ---------------------------------------------------------------- stimuli
def chord(notes, vel, on, off):
    return [(on, "on", n, vel) for n in notes] + [(off, "off", n, 0) for n in notes]


FXCH = (48, 55, 60, 64)                   # hil_scan fx_all's chord, v108 (the FM-1 keys' velocity)
ST_LONG = (6.0, chord(FXCH, 108, 0.1, 5.6))       # held 5.5 s: slow rates


def run_classic(names, r=None, report="report"):
    """the Rhodes-era tests: build/hil/report.txt / report.json (r: a HostRig -> host-report.*), ends on F01"""
    r = r or Rig()
    rep, lines, fails = {}, [], 0
    for n in names:
        t0 = time.time()
        try:
            ok, msg = TESTS[n](r, rep)
        except Exception as e:  # keep scanning
            ok, msg = False, f"error: {e!r}"
        fails += not ok
        line = f"{'ok  ' if ok else 'FAIL'}  {n:13s} {msg}  ({time.time() - t0:.0f} s)"
        print(line, flush=True)
        lines.append(line)
    r.dry()
    r.pc(0)
    json.dump(rep, open(os.path.join(OUT, report + ".json"), "w"), indent=1, default=float)
    open(os.path.join(OUT, report + ".txt"), "w").write("\n".join(lines) + "\n")
    print(f"{fails} failed; report: build/hil/{report}.txt, {report}.json")
    return fails


def stress_max(r, rep, rate=48000, seconds=None, quick=False, tier=None, engines=None):
    """the cracks test (tools/hil_crackle.py t1): T1's S1 calls it by name (tools/hil_must.py), per rate"""
    import hil_crackle
    return hil_crackle.t1(r, rep, rate=rate, seconds=seconds, quick=quick, tier=tier,
                          engines=tuple(engines) if engines else hil_crackle.ENGINES)


TESTS["stress_max"] = stress_max                       # (not in the default run: main leaves it out)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    if "--tier" in argv or any(x.startswith("--tier=") for x in argv):   # the release tiers (tools/hil_must.py):
        import hil_must                                                # must = T1's scenarios, full = T2
        return hil_must.tier_main(argv)
    ap = argparse.ArgumentParser()
    ap.add_argument("--only")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--source", default="both", choices=("usb", "jack", "both"), help="stress_max: what to record")
    ap.add_argument("--jack-dev", default="M6", help="stress_max: the interface recording the headphone jack")
    ap.add_argument("--jack-ch", type=int, default=5, help="stress_max: its input (1-based)")
    ap.add_argument("--dry-run", action="store_true", help="stress_max: print the planned events, open no MIDI / audio")
    ap.add_argument("--repeats", type=int, help="takes per measure for every repeated one (default: REPEATS)")
    ap.add_argument("--host", nargs="?", type=int, const=1, metavar="SEED",
                    help="the classic suites on the host build (tests/host.c), no FM-1: prerolls / latencies from SEED;"
                         " report build/hil/host-report.txt")
    ap.add_argument("--no-reset", action="store_true", help="no CC 10 before the takes (to see what it changes)")
    ap.add_argument("--rate", type=int, choices=(44100, 48000), help="stress_max: the USB rate (default 44100)")
    a = ap.parse_args(argv)
    if a.only and "stress_max" in a.only.split(","):   # the cracks test: tools/hil_crackle.py (its own recorder, rates)
        import hil_crackle
        rest = [n for n in a.only.split(",") if n != "stress_max"]
        argv = (["--host"] if a.host is not None else []) + (["--dry-run"] if a.dry_run else []) + \
            ["--source", "usb" if a.source == "usb" else "both", "--jack-dev", a.jack_dev, "--jack-ch", str(a.jack_ch)] + \
            (["--rate", str(a.rate)] if a.rate else [])
        code = hil_crackle.main(argv)
        if not rest:
            return code
        a.only = ",".join(rest)
        if code:
            print("stress_max: FAIL (the other tests run on)")
    if a.list:
        print(" ".join(TESTS))
        return 0
    names = a.only.split(",") if a.only else [n for n in TESTS if n != "stress_max"]
    bad = [n for n in names if n not in TESTS]
    if bad:
        sys.exit(f"unknown test(s) {bad}; --list")
    os.makedirs(OUT, exist_ok=True)
    if a.repeats:
        for k in REPEATS:
            REPEATS[k] = a.repeats
    if a.no_reset:
        RigBase.reset = False
        globals()["with_reset"] = lambda events, lead=RESET_LEAD: sorted(events, key=lambda e: e[0])
    if a.host is not None:
        return 1 if run_classic(names, HostRig(a.host, reset=not a.no_reset), "host-report") else 0
    if a.dry_run:
        print(f"--dry-run covers stress_max only: not running {', '.join(names)}")
        return 0
    return 1 if run_classic(names) else 0


if __name__ == "__main__":
    sys.exit(main())
