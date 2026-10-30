/* SPDX-License-Identifier: GPL-3.0-only */
/* The reed engine (firmware/src/reed.c) alone on the host: events from a file, the preamp's output (Q30, before the
 * Q15 rounding) to a raw int32 file. tests/py/test_reed_engine.py compares it with tools/reed_model.py.
 * "SAMPLE vib DEPTH SHAPE INC" the vibrato (the motor's depth and shape 0..127, its phase step Q32).
 *   reed_ref EVENTS OUT.raw SAMPLES [SIDE.raw]   (SIDE: SPREAD's side signal, Q15, when it is on)
 * EVENTS: one per line, "SAMPLE on NOTE VEL" / "SAMPLE off NOTE" / "SAMPLE ped CC", SAMPLE a multiple of 256 (the
 * firmware's block: events land between blocks), in order; "SAMPLE knob ID VALUE" sets a VOICE knob (reed.c RK_*,
 * 0..127; TUNE in cents Q16) and works out the words (re_poll, as the main loop); "SAMPLE offv NOTE RV" a key-up with
 * its release velocity; "SAMPLE bend MILLISEMITONES" (as engine.c rh_bend_t); "SAMPLE spread V" (SPREAD, 0..127). */
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#define RE_TAP 1
/* what reed.c takes from the Rhodes engine (rhodes.c, before it in engine.c): the pedal's weights, the release
 * velocity's gain, SPREAD, the bend */
static const uint16_t PEDAL_W[128] = {
#define W8(x) x, x, x, x, x, x, x, x
    W8(32767), W8(32767), W8(32767), W8(32767), W8(32767), 32767, 32767, 29491, 28945, 28399, 27853, 27307, 26761,
    26214, 25668, 25122, 24576, 24030, 23484, 22938, 22528, 22119, 21709, 21300, 20890, 20480, 20071, 19661, 19661,
    W8(0), W8(0), W8(0), W8(0), W8(0), W8(0), W8(0), W8(0)};
static int32_t rh_rel_gain(uint32_t rv) { return 32768 + ((int32_t)(rv ? rv : 64u) - 64) * 13107 / 63; }
static volatile int32_t rh_spread, rh_bend_t;
static int32_t rh_side[256];
static uint8_t rh_side_on;
#define RH_OWN_SECTION(s)
#include "../../firmware/src/reed.c"

int main(int argc, char **argv)
{
    FILE *ev, *out, *sd = 0;
    long total, t = 0, et = -1;
    char kind[8];
    int a = 0, b = 0, have = 0;
    int32_t mono[256];
    if (argc < 4) { fprintf(stderr, "usage: reed_ref EVENTS OUT.raw SAMPLES\n"); return 2; }
    ev = fopen(argv[1], "r"); out = fopen(argv[2], "wb"); total = atol(argv[3]);
    if (argc > 4) sd = fopen(argv[4], "wb");
    if (!ev || !out) return 1;
    re_poll();
    while (t < total) {
        for (;;) {
            if (!have) {
                if (fscanf(ev, "%ld %7s %d", &et, kind, &a) != 3) { et = -1; break; }
                b = 0;
                if (!strcmp(kind, "on") || !strcmp(kind, "knob") || !strcmp(kind, "offv")) if (fscanf(ev, "%d", &b) != 1) return 1;
                have = 1;
            }
            if (et != t) break;
            if (!strcmp(kind, "on")) { if (!re_strike((uint32_t)a, (uint32_t)b)) return 3; }
            else if (!strcmp(kind, "off")) re_note_off((uint32_t)a, 0);
            else if (!strcmp(kind, "ped")) re_pedal((uint32_t)a);
            else if (!strcmp(kind, "knob")) { re_knob((uint32_t)a, b); re_poll(); }
            else if (!strcmp(kind, "offv")) re_note_off((uint32_t)a, (uint32_t)b);
            else if (!strcmp(kind, "bend")) rh_bend_t = (int32_t)llround((pow(2.0, a / 12000.0) - 1.0) * 1073741824.0);
            else if (!strcmp(kind, "spread")) rh_spread = a;
            else if (!strcmp(kind, "amp")) { re_amp = (uint8_t)(a != 0); if (a == 2) re_spk_n = RE_SPK_GLIDE; }   /* "SAMPLE amp 1": 200A AMP (2: no glide in) */
            else if (!strcmp(kind, "vib")) {                       /* "SAMPLE vib DEPTH SHAPE INC": on */
                unsigned sh = 0, inc = 0;
                if (fscanf(ev, "%u %u", &sh, &inc) != 2) return 1;
                re_vib_on = (uint8_t)(a != 0); re_vib_depth = (uint8_t)a; re_vib_shape = (uint8_t)sh; re_vib_inc = inc;
            }
            have = 0;
        }
        re_render(mono, 256);
        fwrite(re_tap, sizeof re_tap, 1, out);
        if (sd) { int32_t z[256] = {0}; fwrite(rh_side_on ? rh_side : z, sizeof z, 1, sd); }
        t += 256;
    }
    fclose(out);
    if (sd) fclose(sd);
    return 0;
}
