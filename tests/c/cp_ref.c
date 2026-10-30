/* SPDX-License-Identifier: GPL-3.0-only */
/* The string engine (firmware/src/strings.c, the CP-80) alone on the host: events from a file; the strings' bus
 * (Q26, before the preamp) and the engine's output (Q15) to two raw int32 files. tests/py/test_cp_engine.py compares
 * them with tools/cp_multirate.py and tools/cp_model.py.
 *   cp_ref EVENTS BUS.raw OUT.raw SAMPLES [SIDE.raw]
 * EVENTS: one per line, SAMPLE a multiple of 256 (events land between blocks), in order: "SAMPLE on NOTE VEL",
 * "SAMPLE off NOTE", "SAMPLE offv NOTE RV", "SAMPLE ped CC", "SAMPLE knob ID VALUE" (strings.c CK_*; TUNE cents Q16),
 * "SAMPLE bend MILLISEMITONES", "SAMPLE trem DEPTH INC" (the tremolo on at DEPTH 0..127, its phase step Q32; DEPTH 0:
 * off). */
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#define CP_TAP 1
#define RH_OWN_SECTION(s)
static const uint16_t PEDAL_W[128] = {
#define W8(x) x, x, x, x, x, x, x, x
    W8(32767), W8(32767), W8(32767), W8(32767), W8(32767), 32767, 32767, 29491, 28945, 28399, 27853, 27307, 26761,
    26214, 25668, 25122, 24576, 24030, 23484, 22938, 22528, 22119, 21709, 21300, 20890, 20480, 20071, 19661, 19661,
    W8(0), W8(0), W8(0), W8(0), W8(0), W8(0), W8(0), W8(0)};
static int32_t rh_rel_gain(uint32_t rv) { return 32768 + ((int32_t)(rv ? rv : 64u) - 64) * 13107 / 63; }
static volatile int32_t rh_spread, rh_bend_t;
static int32_t rh_side[256];
static uint8_t rh_side_on;
#include "../../firmware/src/reed.c"
#include "../../firmware/src/strings.c"

int main(int argc, char **argv)
{
    FILE *ev, *bus, *out, *sd = 0;
    long total, t = 0, et = -1;
    char kind[8];
    int a = 0, b = 0, have = 0;
    int32_t mono[256];
    if (argc < 5) { fprintf(stderr, "usage: cp_ref EVENTS BUS.raw OUT.raw SAMPLES [SIDE.raw]\n"); return 2; }
    ev = fopen(argv[1], "r"); bus = fopen(argv[2], "wb"); out = fopen(argv[3], "wb"); total = atol(argv[4]);
    if (argc > 5) sd = fopen(argv[5], "wb");
    if (!ev || !bus || !out) return 1;
    cp_poll();
    while (t < total) {
        for (;;) {
            if (!have) {
                if (fscanf(ev, "%ld %7s %d", &et, kind, &a) != 3) { et = -1; break; }
                b = 0;
                if (!strcmp(kind, "on") || !strcmp(kind, "knob") || !strcmp(kind, "offv") || !strcmp(kind, "trem"))
                    if (fscanf(ev, "%d", &b) != 1) return 1;
                have = 1;
            }
            if (et != t) break;
            if (!strcmp(kind, "on")) { if (!cp_strike((uint32_t)a, (uint32_t)b)) return 3; }
            else if (!strcmp(kind, "off")) cp_note_off((uint32_t)a, 0);
            else if (!strcmp(kind, "offv")) cp_note_off((uint32_t)a, (uint32_t)b);
            else if (!strcmp(kind, "ped")) cp_pedal((uint32_t)a);
            else if (!strcmp(kind, "knob")) { cp_knob((uint32_t)a, b); cp_poll(); }
            else if (!strcmp(kind, "bend")) rh_bend_t = (int32_t)llround((pow(2.0, a / 12000.0) - 1.0) * 1073741824.0);
            else if (!strcmp(kind, "trem")) { re_vib_on = (uint8_t)(a != 0); re_vib_depth = (uint8_t)a; re_vib_inc = (uint32_t)b; }
            have = 0;
        }
        cp_render(mono, 256);
        fwrite(cp_tap, sizeof cp_tap, 1, bus);
        fwrite(mono, sizeof mono, 1, out);
        if (sd) { int32_t z[256] = {0}; fwrite(rh_side_on ? rh_side : z, sizeof z, 1, sd); }
        t += 256;
    }
    fclose(bus); fclose(out);
    if (sd) fclose(sd);
    return 0;
}
