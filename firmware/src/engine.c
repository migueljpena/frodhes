/* SPDX-License-Identifier: GPL-3.0-only */
/* Frodhes voice engines: the dispatcher. One engine runs at a time; the TYPE picks it.
 *
 *   TYPE 0..3  ST77 SUIT FELT DYNO   the Rhodes engine (rhodes.c, unchanged)
 *   TYPE 4     200A                  the reed engine (reed.c, the Wurlitzer 200A): its line
 *   TYPE 5     200A AMP              the same through its amp and speaker
 *   TYPE 6     CP-80                 the string engine (strings.c, the CP-80)
 * (Frodhes' bar engine, VIBES and MARIMBA, is not in this firmware: its TYPEs 4 and 5 were those two; params.c
 * converts records that name them)
 *
 * The app calls only these entry points: eng_note_on / eng_note_off / eng_pedal_level / eng_all_off post events
 * into this file's ring (main loop -> ISR, single producer, single consumer); eng_set_type(TYPE); eng_quiet(),
 * eng_note_sounding(); the ISR calls eng_render(mono, n). eng_render drains the ring into the running engine: for
 * the Rhodes it re-posts each event into rhodes.c's own ring right before rh_render (in the same block, in order),
 * so the Rhodes engine sees exactly what it saw when the main loop posted to it directly: its output is unchanged.
 * rhodes.c only ever gets TYPE 0..3 in rh_type. params.c (param_apply) calls eng_set_type and eng_vib_knob.
 *
 * TYPE change across engines: the running engine keeps playing for one more block with its output faded to zero over
 * those 256 samples, then all its voices stop and the other engine takes over; events wait in the ring during that
 * block (they are played by the new engine one block later). With no voice sounding the change is immediate.
 * 200A <-> 200A AMP is not a change of engine: the reed engine glides its speaker in (reed.c re_amp).
 *
 * DUAL (two sounds, eng_set_dual): PART 1 is the TYPE (P_TYPE), PART 2 set.type2, one of them a Rhodes and the other
 * the 200A / 200A AMP / CP-80. The other engine is the running one (eng_cur); the Rhodes renders beside it and is
 * summed in. Each note event carries its part (EV_PART2 in val); eng_rh_part says which part is the Rhodes. Pedal and
 * all-off go to both. Each engine strikes at most eng_nv_cap voices (5: the CPU of 10 voices, measured on the FM-1,
 * 2026-11-11: Rhodes + 200A 4260 us, Rhodes + CP-80 4216 us). Out of DUAL the part is ignored and the Rhodes still
 * renders while its voices ring out. */
#ifdef DINGHY_HOST
#define ENG_CAP_SECTION
#else
#define ENG_CAP_SECTION __attribute__((section(".bss.eng_cap")))   /* (out of the global merge: see eq below) */
#endif
static volatile uint8_t eng_nv_cap ENG_CAP_SECTION;   /* 0: every voice; DUAL: 5 an engine */
#define ENG_CAP(nv) (eng_nv_cap && eng_nv_cap < (nv) ? (uint32_t)eng_nv_cap : (uint32_t)(nv))
#include "rhodes.c"
#include "reed.c"     /* the reed engine (self-contained: its tables, voices and render) */
#include "strings.c"  /* the string engine (its tables, voices and render; reed.c's integer helpers) */

#define ENG_RHODES 0u
#define ENG_REED 1u
#define ENG_CP 2u
#define ENG_N 3u
#define ENG_REED_TYPE 4u                       /* params.c: the 200A (TYPE 5: 200A AMP) */
#define ENG_CP_TYPE 6u                         /* params.c: the CP-80 */
#define ENG_RH_NTYPE 4u                        /* TYPE 0..3 the Rhodes, as params.c TYPE_ENGINE / TYPE_SUB */

static volatile uint8_t eng_want;              /* main loop: the engine of the TYPE last set */
static volatile uint8_t eng_cur;               /* ISR: the engine running */
static uint8_t eng_swapping;                   /* ISR: this block fades the old engine out */
static uint8_t eng_shed_req;                   /* ISR (frodhes.c app_audio): shed a voice of the running engine */
static uint8_t eng_pedal_cc;                   /* ISR: the last pedal level (handed to the new engine at a change) */
static volatile uint8_t eng_dual;              /* main loop: two sounds (DUAL) */
static volatile uint8_t eng_rh_part;           /* main loop: in DUAL, the part (0 / 1) the Rhodes plays */
static uint8_t eng_t1, eng_t2 = ENG_REED_TYPE; /* main loop: PART 1's TYPE, PART 2's (DUAL) */
#define EV_PART2 0x8000u                       /* in an event's val: a note of PART 2 */

/* the ring has a section of its own (firmware only) so the compiler's global merge leaves it out: merged in, it pushed
 * rhodes.c's rh_F and rh_x into two merged blocks, and the Rhodes mode loop needed a second base register and spilled
 * (about 35 us per block with 8 voices, 2026-10-17; tools/build.py checks that loop) */
#ifdef DINGHY_HOST
#define ENG_OWN_SECTION
#else
#define ENG_OWN_SECTION __attribute__((section(".bss.eng_eq")))
#endif
static volatile uint32_t eq[32] ENG_OWN_SECTION;
static volatile uint32_t eq_w, eq_r, eq_drop;

static void eng_post(uint32_t type, uint32_t note, uint32_t val)
{
    uint32_t w = eq_w;
    if (w - eq_r >= 32u) { eq_drop++; return; }
    eq[w & 31u] = type << 28 | (note & 127u) << 16 | (val & 0xFFFFu);
    RING_PUBLISH();
    eq_w = w + 1u;
}

/* ---- main loop */
/* part: 0 PART 1, 1 PART 2 (DUAL; ignored otherwise) */
static void eng_note_on_p(uint32_t note, uint32_t vel, uint32_t part)
{
    if (note < 128u && vel && vel < 128u) eng_post(EV_NOTE_ON, note, vel | (part ? EV_PART2 : 0u));
}
/* rv: the release velocity, 1..127 (MIDI note-off velocity); 0 = none given (a note-on at velocity 0, a note-off at 0,
 * the FM-1's keys: their domes give no release speed): the neutral release (rhodes.c rh_events, reed.c re_note_off) */
static void eng_note_off_p(uint32_t note, uint32_t rv, uint32_t part)
{
    if (note < 128u) eng_post(EV_NOTE_OFF, note, (rv < 128u ? rv : 0u) | (part ? EV_PART2 : 0u));
}
static void eng_note_on(uint32_t note, uint32_t vel) { eng_note_on_p(note, vel, 0); }
static void eng_note_off(uint32_t note, uint32_t rv) { eng_note_off_p(note, rv, 0); }
static void eng_pedal_level(uint32_t cc) { rh_hold = (uint8_t)(cc >= 64u); eng_post(EV_PEDAL, 0, cc > 127u ? 127u : cc); }
static void eng_all_off(void) { eng_post(EV_ALL_OFF, 0, 0); }

/* params.c param_apply(P_TYPE): rhodes.c only ever gets 0..3 in rh_type (a 200A TYPE leaves it at the last Rhodes
 * one, so a Rhodes note still fading out at the change keeps its own voicing) */
static uint16_t eng_epoch;                     /* main loop: counts the TYPE changes that stop the sounding voices (to
                                                * another engine); notes.c re-sounds a pitch still held across one when
                                                * it is pressed again (out_resound) */
static void eng_apply_types(void)
{
    uint8_t w = eng_want;
    uint32_t t = eng_t1, o;
    if (eng_dual) {                                 /* the Rhodes part and the other one */
        uint32_t p = eng_t1 < ENG_RH_NTYPE ? 0u : 1u;
        eng_rh_part = (uint8_t)p;
        rh_type = (uint8_t)(p ? eng_t2 : eng_t1);
        t = p ? eng_t1 : eng_t2;
    }
    o = t;
    if (o == ENG_REED_TYPE || o == ENG_REED_TYPE + 1u) { eng_want = ENG_REED; re_amp = (uint8_t)(o != ENG_REED_TYPE); }
    else if (o == ENG_CP_TYPE) eng_want = ENG_CP;
    else { rh_type = (uint8_t)(o < ENG_RH_NTYPE ? o : 0u); eng_want = ENG_RHODES; }
    eng_nv_cap = (uint8_t)(eng_dual ? 5u : 0u);
    if (eng_want != w) eng_epoch++;
}
static void eng_set_type(uint32_t t) { eng_t1 = (uint8_t)t; eng_apply_types(); }
/* DUAL on / off, PART 2's TYPE (params.c dual_apply: the two TYPEs are always one Rhodes and one other) */
static void eng_set_dual(uint32_t on, uint32_t t2)
{
    eng_t2 = (uint8_t)t2;
    eng_dual = (uint8_t)(on != 0);
    eng_apply_types();
}

/* ---- the pitch bend (main loop): rhodes.c rh_retune retunes the sounding tines in the next block where the bend
 * changed, a new note starts at it; reed.c re_render reads rh_bend_t the same way. +-2 semitones. v14: the 14-bit MIDI
 * value (8192 = centre; for an SMK-37 pitch wheel, 8192 + its bend in 1/4096 semitone). The table and rh_retune are
 * genkigenki's (PR #3, the SMK-37 pitch wheel) */
static const int32_t ENG_BEND_T[17] = {   /* Q30: 2^(k/48) - 1, k = -8..8 (quarter semitones, +-2 semitones) */
    -117146609, -103232612, -89116230, -74794521, -60264498, -45523131, -30567345, -15394023, 0,
    15617934, 31463037, 47538612, 63848011, 80394637, 97181938, 114213416, 131492623};
static uint16_t eng_bend14 = 8192u;            /* the bend last set (the host tests read it) */
static void eng_bend_set(int32_t v14)
{
    int32_t q = v14 < 0 ? 0 : v14 > 16383 ? 16383 : v14, i = q >> 10, f = q & 1023;
    eng_bend14 = (uint16_t)q;
    rh_bend_t = ENG_BEND_T[i] + (int32_t)(((int64_t)(ENG_BEND_T[i + 1] - ENG_BEND_T[i]) * f) >> 10);
}
static void eng_bend_reset(void) { eng_bend_set(8192); }

/* params.c / tempo.c: the 200A's vibrato, on the P_MOT_* parameters (on, depth, shape 0..127; INC: a Q32 step) */
enum { VK_ON, VK_DEPTH, VK_SHAPE, VK_INC };
static void eng_vib_knob(uint32_t k, uint32_t v)
{
    uint32_t c = v > 127u ? 127u : v;
    switch (k) {
    case VK_ON: re_vib_on = (uint8_t)(v != 0); break;
    case VK_DEPTH: re_vib_depth = (uint8_t)c; break;
    case VK_SHAPE: re_vib_shape = (uint8_t)c; break;
    case VK_INC: re_vib_inc = v; break;
    default: break;
    }
}

static int eng_quiet(void)
{
    if (eq_w != eq_r || eng_swapping || eng_want != eng_cur) return 0;
    if (eng_cur != ENG_RHODES && !rh_quiet()) return 0;            /* (DUAL's Rhodes, or one ringing out) */
    return eng_cur == ENG_REED ? re_quiet() : eng_cur == ENG_CP ? cp_quiet() : rh_quiet();
}


static int eng_note_sounding(uint32_t n)
{
    uint32_t v;
    if (eng_cur == ENG_REED) {
        uint32_t act = re_active_bits;
        for (v = 0; v < RE_NV; v++) if (act >> v & 1u && re_notes[v] == n) return 1;
        if (!eng_dual) return 0;
    }
    else if (eng_cur == ENG_CP) {
        uint32_t act = cp_active_bits;
        for (v = 0; v < CP_NV; v++) if (act >> v & 1u && cp_notes[v] == n) return 1;
        if (!eng_dual) return 0;
    }
    {
        uint32_t act = rh_active_bits;
        for (v = 0; v < RH_NV; v++) if (act >> v & 1u && rh_notes[v] == n) return 1;
    }
    return 0;
}

static uint32_t eng_shed_count(void) { return rh_shed_count + re_shed_count + cp_shed_count; }
/* the running engine's voices sounding (a bit a voice) */
static uint32_t eng_active_bits(void) { return eng_cur == ENG_REED ? re_active_bits : eng_cur == ENG_CP ? cp_active_bits : rh_active_bits; }

/* ---- ISR */
static void eng_rh_kill_all(void)               /* the Rhodes stops at once (after the fade block) */
{
    uint32_t i;
    for (i = 0; i < RH_NV; i++) { rv[i].alive = 0; rv[i].pend = 0; }
    memset(rh_st, 0, sizeof rh_st);
    rh_active_bits = rh_held_bits = 0;
}

static void eng_switch(uint32_t want)
{
    if (eng_cur == ENG_REED) re_kill_all(); else if (eng_cur == ENG_CP) cp_kill_all(); else eng_rh_kill_all();
    eng_cur = (uint8_t)want;
    if (want == ENG_REED) re_pedal(eng_pedal_cc);                   /* the pedal is global */
    else if (want == ENG_CP) cp_pedal(eng_pedal_cc);
    else rh_post(EV_PEDAL, 0, eng_pedal_cc);
    eng_swapping = 0;
}

#if defined(DINGHY_HOST) && !defined(DINGHY_WEB)
static uint64_t eng_h_ms, eng_h_mul, eng_h_blocks;
static uint32_t eng_h_max_ms, eng_h_max_mul, eng_h_max_us[ENG_N];   /* us: per engine (ENG_RHODES, ENG_REED, ENG_CP) */
static uint32_t eng_h_max_nv[ENG_N];                                /* the most voices alive at once, per engine */
static FILE *eng_h_log;
static int eng_h_log_open;
#endif

static void eng_render(int32_t *mono, uint32_t n)
{
    uint32_t want = eng_want < ENG_N ? eng_want : ENG_RHODES, j, dual = eng_dual;
    if (want != eng_cur && !eng_swapping) {                         /* another engine */
        uint32_t busy = eng_active_bits();
        if (dual && eng_cur == ENG_RHODES) {                        /* DUAL on: the Rhodes goes on beside the other */
            eng_cur = (uint8_t)want;
            if (want == ENG_REED) re_pedal(eng_pedal_cc); else if (want == ENG_CP) cp_pedal(eng_pedal_cc);
        }
        else if (busy) eng_swapping = 1;                            /* fade the old engine out this block */
        else eng_switch(want);                                      /* nothing sounding: change now */
    }
    if (!eng_swapping)                                              /* events wait while the old engine fades */
        while (eq_r != eq_w) {
            uint32_t e = eq[eq_r & 31u], type = e >> 28, note = e >> 16 & 127u, val = e & 0xFFFFu;
            if (eng_cur != ENG_RHODES) {
                if (dual && (type == EV_NOTE_ON || type == EV_NOTE_OFF)
                    && (uint32_t)((val & EV_PART2) != 0u) == eng_rh_part) {   /* DUAL: the Rhodes part's note */
                    if (ev_w - ev_r >= 32u) break;                  /* (its ring full: next block, in order) */
                    rh_post(type, note, val & 0x7FFFu); eq_r = eq_r + 1u; continue;
                }
                if ((type == EV_PEDAL || type == EV_ALL_OFF) && (dual || rh_active_bits)) {   /* both (a Rhodes
                                                                    * ringing out too) */
                    if (ev_w - ev_r >= 32u) break;
                    rh_post(type, note, val);
                }
            }
            val &= 0x7FFFu;
            if (eng_cur == ENG_REED ? type == EV_NOTE_ON && !re_strike(note, val)
                : eng_cur == ENG_CP ? type == EV_NOTE_ON && !cp_strike(note, val) : ev_w - ev_r >= 32u) break;
                                                                    /* every voice waits for a note (BUGHUNT #21):
                                                                    * this one next block, in order; the Rhodes'
                                                                    * ring full (its note-ons can wait the same way) */
            eq_r = eq_r + 1u;
            if (type == EV_PEDAL) eng_pedal_cc = (uint8_t)val;
            if (eng_cur == ENG_RHODES) rh_post(type, note, val);    /* drained by rh_render below, in order */
            else if (eng_cur == ENG_CP) {                           /* the strings (struck above) */
                if (type == EV_NOTE_OFF) cp_note_off(note, val);
                else if (type == EV_PEDAL) cp_pedal(val);
                else if (type == EV_ALL_OFF) cp_all_off();
            }
            else if (type == EV_NOTE_OFF) re_note_off(note, val);   /* the reed (struck above) */
            else if (type == EV_PEDAL) re_pedal(val);
            else if (type == EV_ALL_OFF) re_all_off();
        }
    PROF_STAMP(2);                                                  /* (developer build: the events, the strikes) */
#if defined(RH_PROF_STAGES) && !defined(DINGHY_HOST)
    {   /* the CP's strike split (strings.c cp_pf) as stages 5 (decays), 6 (amplitudes), 7 (the strikes in all) */
        uint32_t k_;
        for (k_ = 0; k_ < 3u; k_++) { if (cp_pf[k_] > prof_st_max[5u + k_]) prof_st_max[5u + k_] = cp_pf[k_]; cp_pf[k_] = 0; }
    }
#endif
    if (eng_shed_req) {
        eng_shed_req = 0;
        if (dual && eng_cur != ENG_RHODES) {                        /* DUAL: the engine with more voices sounding */
            uint32_t a = rh_active_bits, b = eng_active_bits(), ca = 0, cb = 0;
            for (; a; a &= a - 1u) ca++;
            for (; b; b &= b - 1u) cb++;
            if (ca >= cb || eng_cur == ENG_REED) rh_shed_req = 1;   /* (the reed sheds by its steal only) */
            else cp_shed_req = 1;
        }
        else if (eng_cur == ENG_RHODES) rh_shed_req = 1;            /* (the reed: its steal, re_strike) */
        else if (eng_cur == ENG_CP) cp_shed_req = 1;
    }
    if (eng_cur == ENG_REED) re_render(mono, n);                    /* (each sets rh_side_on: SPREAD, the CP's tremolo) */
    else if (eng_cur == ENG_CP) cp_render(mono, n);
    else rh_render(mono, n);
#if defined(DINGHY_HOST) && !defined(DINGHY_WEB)
    {   /* host counters: mode-samples and int64 multiplies this block (Rhodes: estimated from the live modes) */
        uint32_t ms = 0, mul = 0, ns = 0, i, m;
        if (eng_cur == ENG_REED) {                         /* (an estimate until the FM-1 measures it) */
            for (i = 0; i < RE_NV; i++)
                if (re_v[i].alive) {
                    for (m = 0; m < RE_NM; m++) if (re_v[i].m[m].on) { ms += n; mul += 2u * n; ns += 170u * n; }
                    mul += 4u * n; ns += 900u * n;
                }
        }
        else if (eng_cur == ENG_CP) {                      /* each partial 2^-band of a full-rate one (an estimate) */
            for (i = 0; i < CP_NV; i++)
                if (cp_v[i].alive) {
                    const cp_key_t *K = &CP_KEYS[cp_v[i].note - CP_KEY_LO];
                    for (m = 0; m < CP_NB; m++) { ms += K->nb[m] * (n >> m); mul += 2u * K->nb[m] * (n >> m); ns += 75u * K->nb[m] * (n >> m); }
                    ns += 170u * cp_v[i].np;                        /* the words a block */
                }
        }
        else {
            for (i = 0; i < RH_NV; i++)
                if (rv[i].alive) {
                    for (m = 0; m < RH_NM; m++) if (rv[i].m[m].live) { ms += n; mul += 3u * n; ns += 170u * n; }
                    mul += 5u * n; ns += 870u * n;                  /* pickup: square, 3 Hermite, gain; P0: 0.54 + 0.33 us */
                }
        }
        eng_h_ms += ms; eng_h_mul += mul; eng_h_blocks++;
        if (ms > eng_h_max_ms) eng_h_max_ms = ms;
        if (mul > eng_h_max_mul) eng_h_max_mul = mul;
        if (ns / 1000u > eng_h_max_us[eng_cur]) eng_h_max_us[eng_cur] = ns / 1000u;
        {
            uint32_t a = eng_active_bits(), c = 0;
            for (; a; a &= a - 1u) c++;
            if (c > eng_h_max_nv[eng_cur]) eng_h_max_nv[eng_cur] = c;
        }
        if (!eng_h_log_open) {
            const char *p = getenv("FRODHES_MSLOG");
            eng_h_log = p ? fopen(p, "w") : 0; eng_h_log_open = 1;
        }
        if (eng_h_log) fprintf(eng_h_log, "%u %u %u %u\n", (unsigned)eng_cur, ms, mul, ns / 1000u);
    }
#endif
    {
    uint32_t extra = eng_cur != ENG_RHODES && (dual || rh_active_bits || ev_w != ev_r);   /* DUAL's Rhodes (or one
                                                                    * ringing out after it), summed in (decided before
                                                                    * a change to the Rhodes: it renders once a block) */
    if (eng_swapping) {                                             /* the fade: 32768 -> 0 over the block */
        for (j = 0; j < n; j++) {
            int32_t g = 32768 - (int32_t)(((j + 1u) << 15) / n);
            mono[j] = (int32_t)(((int64_t)mono[j] * g) >> 15);
        }
        eng_switch(want);
    }
    if (extra) {
        static int32_t rx[256] ENG_OWN_SECTION;
        uint32_t i;
        rh_render(rx, n);
        for (i = 0; i < n; i++) mono[i] += rx[i];
    }
    }
}

#if defined(DINGHY_HOST) && !defined(DINGHY_WEB)
static void eng_host_report(void)                /* tests/host.c, at the end of a run */
{
    printf("mode-samples %llu (max per block %u, blocks %llu)\n", (unsigned long long)eng_h_ms, eng_h_max_ms,
           (unsigned long long)eng_h_blocks);
    printf("mul64 %llu (max per block %u)\n", (unsigned long long)eng_h_mul, eng_h_max_mul);
    printf("cpu-proxy max %u us per block (voices only, P0 costs; + 312 idle, + 1388 with every effect)\n",
           eng_h_max_us[eng_cur]);
    printf("cpu-proxy-each rhodes %u reed %u cp %u\n", eng_h_max_us[ENG_RHODES], eng_h_max_us[ENG_REED], eng_h_max_us[ENG_CP]);   /* a run on several */
    printf("voices max rhodes %u of %u reed %u of %u cp %u of %u\n", eng_h_max_nv[ENG_RHODES], (unsigned)RH_NV,
           eng_h_max_nv[ENG_REED], (unsigned)RE_NV, eng_h_max_nv[ENG_CP], (unsigned)CP_NV);                /* tools/hil_crackle.py: every voice taken */
    printf("ev_drop %u\n", (unsigned)(eq_drop + ev_drop));
    printf("rhodes steals %u\n", (unsigned)rh_h_steals);          /* tools/engine_ab.py */
    printf("cpu-proxy-reed %u voices max %u of %u\n", eng_h_max_us[ENG_REED], eng_h_max_nv[ENG_REED], (unsigned)RE_NV);
    printf("type %u\n", eng_cur == ENG_REED ? ENG_REED_TYPE + re_amp : eng_cur == ENG_CP ? ENG_CP_TYPE : (unsigned)rh_type);   /* the TYPE last rendered (C11) */
    if (eng_h_log) fclose(eng_h_log);
}
#endif
