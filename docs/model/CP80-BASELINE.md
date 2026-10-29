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
| Pair detune | 0.8 +- 0.5 cents between a pair's strings | [A] |
| Decay | each partial's loss 0.183 + 2.35e-4 f (1/s): T60 ~28 s at 280 Hz, ~15 s at 1.1 kHz, ~5.5 s at 4.5 kHz, fitted to 38 chord tails of a CP-70's DI (the grand-piano prior was 8x faster) | [M] |
| Dampers | keys 1-73 damped (T60 0.25 s), 74-88 undamped | [S] / [A] |
| Hammer | rubber under leather: F = K c^p, p 2.5, K 16x a grand's felt (fitted to the CP-70 DI's brightness [M]); masses 10-4 g; the contact simulated on the string point by point, the force solved implicitly; 13-163 N, 2-0.4 ms | [M] / [A], checked against grand-piano literature |
| Pickup | a piezo under the bridge reads the string's force, T dy/dx (partials weighted by n) | [S] |
| Voicing | every key at one level at vel 80 (the physics leaves the top octave ~18 dB low; the schematic's 0.022 uF on keys 83-88 says the real top was too loud: Yamaha voiced it even) | [A] |
| BASS / TREBLE / BRILLIANCE | shelves whose gains make the chain give the manual's calibration levels exactly (50 Hz, 5 kHz re 500 Hz); the corners assumed | [C] |
| MIDDLE | a 600 Hz peak, +-3 dB (VR3 is 1 k against 3.9 k: a small range); no calibration point | [A] |
| Low-pass | FET3's unity-gain Sallen-Key from its parts: 4013 Hz, Q 1.08 (CP-70B: 2751 Hz, Q 0.74) | [S] |
| Tremolo | sine soft-limited, 0.8-10 Hz, depth 15-45 %, two LDRs in antiphase (ch1 / ch2), 3 / 35 ms response | [S] / [A] |
| CP-70 | the CP-70B's low-pass and its own calibration levels (its treble boosts ~6 dB more to make up for the darker filter) | [S] / [C] |

## Measured so far

Wingate's CP-70 DI (YouTube "I Bought a Yamaha CP70!", 0:01-3:02, chords): the decays per octave band and the
long-term spectrum. Hollow Sun's and Amazound's library demos (SoundCloud) agree at 4-8 kHz; Hollow Sun's decays
come out ~2x faster (a busier part). Audio in frodhes-refs/audio/cp70-yt, for measuring only.

## What only single-note recordings can give

1. The decays per key and per partial (the chords give a band's median), and a pair's double decay.
2. A pair's detune.
3. The level over the keys (the voicing) and its change with velocity.
4. The hammer's contact (the attack's brightness per velocity).
5. The piezos' capacitance (keys 83-88's level through the 0.022 uF) and the tone network's exact curves between
   the calibration points.

Single notes at several velocities with their full decay, from the line or jack out (not a microphone), are the
most useful.
