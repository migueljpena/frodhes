/* SPDX-License-Identifier: GPL-3.0-only */
/* The reed engine (firmware/src/reed.c) alone on the host: events from a file, the preamp's output (Q30, before the
 * Q15 rounding) to a raw int32 file. tests/py/test_reed_engine.py compares it with tools/reed_model.py.
 *   reed_ref EVENTS OUT.raw SAMPLES
 * EVENTS: one per line, "SAMPLE on NOTE VEL" / "SAMPLE off NOTE" / "SAMPLE ped CC", SAMPLE a multiple of 256 (the
 * firmware's block: events land between blocks), in order. */
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#define RE_TAP 1
#include "../../firmware/src/reed.c"

int main(int argc, char **argv)
{
    FILE *ev, *out;
    long total, t = 0, et = -1;
    char kind[8];
    int a = 0, b = 0, have = 0;
    int32_t mono[256];
    if (argc < 4) { fprintf(stderr, "usage: reed_ref EVENTS OUT.raw SAMPLES\n"); return 2; }
    ev = fopen(argv[1], "r"); out = fopen(argv[2], "wb"); total = atol(argv[3]);
    if (!ev || !out) return 1;
    while (t < total) {
        for (;;) {
            if (!have) {
                if (fscanf(ev, "%ld %7s %d", &et, kind, &a) != 3) { et = -1; break; }
                b = 0;
                if (!strcmp(kind, "on")) if (fscanf(ev, "%d", &b) != 1) return 1;
                have = 1;
            }
            if (et != t) break;
            if (!strcmp(kind, "on")) { if (!re_strike((uint32_t)a, (uint32_t)b)) return 3; }
            else if (!strcmp(kind, "off")) re_note_off((uint32_t)a);
            else if (!strcmp(kind, "ped")) re_pedal((uint32_t)a);
            have = 0;
        }
        re_render(mono, 256);
        fwrite(re_tap, sizeof re_tap, 1, out);
        t += 256;
    }
    fclose(out);
    return 0;
}
