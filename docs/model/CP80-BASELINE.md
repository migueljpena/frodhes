# CP-80: the baseline before recordings

The Yamaha CP-80 model as the documents and physics give it, before any recording is measured. The values and
each one's source are in `data/cp80.json`; the float model is `tools/cp_model.py` (`--render DIR` for the demo
WAVs); its checks are `tests/py/test_cp_model.py`. Sources: [S] the service manual, schematic and parts list,
[C] fitted to the manual's calibration levels, [P] physics from [S] with a stated assumption, [A] assumed.

| Part | What the model does | Source |
|---|---|---|
| Keys and tuning | 88 keys A0-C8; every key's stretch from Yamaha's tuning table (-23 to +35 cents) | [S] |
| Strings | keys 1-22 one wound string, 23-42 two wound, 43-88 two plain (gauges #18-#13.5) | [S] |
| Inharmonicity | plain wire: B from the gauge, steel and a tension of 45 % of the wire's strength (A4: 7.3e-4); wound: from the tuning table's octave stretch | [P] |
| Pair detune | 4 +- 2 cents between a pair's strings (David's CP70: 2-10, median 4.3 on 12 keys; an instrument its owner calls slightly out of tune) | [M] |
| Decay | two stages per partial (fast, then slow), T60 by frequency from 157 single CP-70 notes: early 6.2 s / late 10.5 s at 430 Hz, 5.2 / 7.0 s at 1.2 kHz, 1.5 / 3.6 s at 4.9 kHz (the chord tails of Wingate's DI read 3x longer: the pedal and the other strings) | [M] |
| Dampers | keys 1-73 damped (T60 0.25 s), 74-88 undamped | [S] / [A] |
| Hammer | rubber under leather: F = K c^p, p 2.5, K 16x a grand's felt (fitted to the CP-70 DI's brightness [M]); masses 10-4 g; the hammer's speed 0.15-5 m/s over vel 1-127 (David's CP70's ~33 dB range and +9..+15 dB H2-H6 change [M]); the contact simulated on the string point by point, the force solved implicitly; 13-163 N, 2-0.4 ms | [M] / [A], checked against grand-piano literature |
| Pickup | a piezo under the bridge reads the string's force, T dy/dx (partials weighted by n) | [S] |
| Voicing | every key at one level at vel 80 (the physics leaves the top octave ~18 dB low; the schematic's 0.022 uF on keys 83-88 says the real top was too loud: Yamaha voiced it even) | [A] |
| BASS / TREBLE / BRILLIANCE | shelves whose gains make the chain give the manual's calibration levels exactly (50 Hz, 5 kHz re 500 Hz); the corners assumed | [C] |
| MIDDLE | a 600 Hz peak, +-3 dB (VR3 is 1 k against 3.9 k: a small range); no calibration point | [A] |
| Low-pass | FET3's unity-gain Sallen-Key from its parts: 4013 Hz, Q 1.08 (CP-70B: 2751 Hz, Q 0.74) | [S] |
| Tremolo | sine soft-limited, 0.8-10 Hz, depth 15-45 %, two LDRs in antiphase (ch1 / ch2), 3 / 35 ms response | [S] / [A] |
| CP-70 | the CP-70B's low-pass and its own calibration levels (its treble boosts ~6 dB more to make up for the darker filter) | [S] / [C] |

## Measured so far

Pianobook's "David's CP70" (Conner Sawchuk; 157 single notes F1-Eb7, 4-7 layers, the files' own levels): the
decays, the pairs' detune, B (it agrees with the physics: A4 7.3e-4 measured and computed), the dynamic range and
its brightness. Wingate's CP-70 DI (YouTube "I Bought a Yamaha CP70!", 0:01-3:02, chords): the long-term spectrum. Hollow Sun's and Amazound's library demos (SoundCloud) agree at 4-8 kHz; Hollow Sun's decays
come out ~2x faster (a busier part). Audio in frodhes-refs/audio/cp70-yt, for measuring only.

## What only single-note recordings can give

1. A CP-80's own (every measured instrument so far is a CP-70; the strings differ).
2. The level over the keys (the voicing).
3. The piezos' capacitance (keys 83-88's level through the 0.022 uF) and the tone network's exact curves between
   the calibration points.

Single notes at several velocities with their full decay, from the line or jack out (not a microphone), are the
most useful.
