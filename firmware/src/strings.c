/* SPDX-License-Identifier: GPL-3.0-only */
/* Frodhes string engine: the Yamaha CP-80 (the float model tools/cp_model.py, its algorithm tools/cp_multirate.py;
 * tables from tools/gen_cp.py -> firmware/gen/cp_tables.h).
 *
 * A key is the partials of its string (keys 1-22) or its pair of strings (23-88): f_n = n f0 sqrt(1 + B n^2), the
 * ones within 50 dB of its strongest. Each is a two-multiply resonator (x' = k1 x - k2 xp, Q30 words, Q26 states)
 * run at the lowest rate its frequency allows, 44.1 kHz / 2^b (b = 0..4: a band); a pair's partial is one resonator
 * at the pair's mean frequency times cos(pi df t), the beating (linear over a block). The voices add each band's
 * partials into one buffer for the band; the bands come up to 44.1 kHz together through polyphase IIR half-bands
 * (cp_up). The strike starts each resonator where the reference's partial is after the hammer left it (its complex
 * amplitude, from the tables' velocity layers x the hammer's speed), divided by the interpolators' response at its
 * frequency: no latency. Each partial decays in two stages, (1 - w) e^(-se t) + w e^(-sl t): the resonator's loss is
 * set every block to that envelope's slope, sl + (se - sl) rho / (1 + rho), rho = (1 - w) / w e^(-(se - sl) t); the
 * damper adds its own (keys 1-73).
 *
 * After the voices, the preamp (the CP-80's TR board, linear): the input high-pass, BASS, MIDDLE, TREBLE,
 * BRILLIANCE, the Sallen-Key low-pass; then the tremolo, two LDR shunts in antiphase (ch1, ch2: the mono M and
 * the side S, as SPREAD's). The tone knobs' biquads come from the main loop (cp_poll, when one moved); the TREMOLO
 * controls are the P_MOT_* parameters (engine.c eng_vib_knob: re_vib_on, re_vib_depth, re_vib_inc).
 *
 * Interface (engine.c): cp_strike(note, vel) (0: every voice waits for a note, try next block), cp_note_off(note,
 * rv), cp_pedal(cc), cp_all_off(), cp_render(mono, n) (Q15), cp_quiet(), cp_kill_all(); the summaries
 * cp_active_bits, cp_notes[]. The ISR owns every voice. */
#ifndef CP_VOICES
#ifdef FR_VOICES
#define CP_VOICES FR_VOICES
#else
#define CP_VOICES 10u
#endif
#endif
#define CP_NV CP_VOICES
#include "cp_tables.h"
_Static_assert(CP_NV >= 1u && CP_NV <= 32u, "summaries are 32-bit masks");
_Static_assert(CP_NB == 5u, "strings.c: five bands");

typedef struct {
    int32_t x1, x2;                           /* the resonator's last two outputs (Q26; x1 the newer) */
    int32_t c;                                /* cos of its angle a band sample (Q30; the tuning and the bend in it) */
    int32_t rho;                              /* the stages' ratio at this block's middle (Q24) */
    int32_t sl, ds;                           /* the late stage's loss, the early's over it (Q40 a 44.1 kHz sample) */
    uint32_t bph, binc;                       /* the pair's beat: cos(pi df t)'s phase (Q32), its step a block */
} cp_part_t;

typedef struct {
    cp_part_t p[CP_NPMAX];
    int32_t damp;                             /* the damper's loss now (Q40 a sample; 0 held, the pedal down, undamped) */
    int32_t tf;                               /* the tuning and bend its angles were made with (Q30) */
    int32_t fade;                             /* Q15: 32768 not fading; a stolen voice fades out over one block */
    uint32_t quiet, age;
    int32_t peak;                             /* the largest state at the last block's end (Q26): the shed's pick */
    uint16_t rg;                              /* the release velocity's x (Q15) */
    uint8_t note, vel, alive, held, pair, np;
    uint8_t pend, pend_note, pend_vel;        /* a note waits for this voice's fade: 1 held, 2 let go, 3 all-off */
} cp_voice_t;

static cp_voice_t cp_v[CP_NV] RH_OWN_SECTION(".pool.cp_v");   /* ~19 KB: the pool (zeroed at boot) */
static uint32_t cp_active_bits, cp_ages, cp_quiet_blocks, cp_shed_count;
static uint8_t cp_notes[CP_NV];
static uint8_t cp_pedal_cc, cp_hold;

/* ---- the knobs (main loop -> ISR): BASS MIDDLE TREBLE 0..127 (64 = 5 on the panel), BRILLIANCE 0..2 (LOW MEDIUM
 * HIGH), TUNE (cents, Q16) */
enum { CK_BASS, CK_MIDDLE, CK_TREBLE, CK_BRIL, CK_TUNE, CK_N };
static volatile int32_t cp_kv[CK_N] = {64, 64, 64, 1, 0};
static volatile uint32_t cp_kgen = 1u;
typedef struct { int32_t bq[6][5]; uint32_t gen; } cp_coef_t;  /* HP, BASS, MIDDLE, TREBLE, BRILLIANCE, SK */
static cp_coef_t cp_cf[2];
static volatile uint32_t cp_pub;              /* the published coefficients + 1 (0: none yet) */
static volatile int32_t cp_tune_l;            /* log2 of TUNE's factor (Q26): the main loop's */

static void cp_knob(uint32_t k, int32_t v)                       /* main loop */
{
    if (k < CK_N && cp_kv[k] != v) { cp_kv[k] = v; cp_kgen++; }
}

/* a tone knob's biquad: the table's two positions around v (0..127), linear between them */
static void cp_knob_bq(const int32_t (*t)[5], int32_t v, int32_t *o)
{
    uint32_t i = v >= 124 ? CP_NKNOB - 2u : (uint32_t)v >> 2, j;
    int32_t a = (int32_t)(i << 2), b = i + 1u == CP_NKNOB - 1u ? 127 : a + 4, f;
    f = ((v - a) << 15) / (b - a);
    for (j = 0; j < 5u; j++) o[j] = t[i][j] + (int32_t)(((int64_t)(t[i + 1u][j] - t[i][j]) * f) >> 15);
}

static void cp_poll(void)                                        /* main loop: the preamp's words for the knobs */
{
    uint32_t p = cp_pub, b = p == 1u ? 1u : 0u, g = cp_kgen, j;
    cp_coef_t *C = &cp_cf[b];
    int32_t v;
    if (p && cp_cf[p - 1u].gen == g) return;
    for (j = 0; j < 5u; j++) { C->bq[0][j] = CP_HP[j]; C->bq[5][j] = CP_SK[j]; }
    v = cp_kv[CK_BASS];   cp_knob_bq(CP_BASS, v < 0 ? 0 : v > 127 ? 127 : v, C->bq[1]);
    v = cp_kv[CK_MIDDLE]; cp_knob_bq(CP_MIDDLE, v < 0 ? 0 : v > 127 ? 127 : v, C->bq[2]);
    v = cp_kv[CK_TREBLE]; cp_knob_bq(CP_TREBLE, v < 0 ? 0 : v > 127 ? 127 : v, C->bq[3]);
    v = cp_kv[CK_BRIL];   for (j = 0; j < 5u; j++) C->bq[4][j] = CP_BRIL[v < 0 ? 0 : v > 2 ? 2 : v][j];
    cp_tune_l = (int32_t)(((int64_t)cp_kv[CK_TUNE] * 55924) >> 16);   /* cents Q16 -> log2 Q26 (2^26 / 1200) */
    C->gen = g;
    RING_PUBLISH();
    cp_pub = b + 1u;
}

/* ---- integer maths (reed.c's tables and helpers: re_tab, re_log2, re_exp2, re_cos32) */

/* the tuning and the bend now (Q30): 2^(TUNE / 1200) x (1 + rh_bend_t) */
static int32_t cp_tf_now(void)
{
    int64_t t = re_exp2(cp_tune_l, 30);
    return (int32_t)((t * ((int64_t)(1 << 30) + rh_bend_t)) >> 30);
}

/* rho / (1 + rho), Q30 (rho Q24, > -0.9) */
static inline int32_t cp_s(int32_t rho)
{
    uint32_t d = (uint32_t)((1 << 24) + rho) >> 7;               /* Q17 */
    uint32_t inv = 0x80000000u / d;                              /* 1 / (1 + rho), Q14 */
    return (int32_t)((1u << 30) - (inv << 16));
}

/* the 2^-32-cycle angle inc (a band sample) at the factor tf (Q30) */
static inline uint32_t cp_inc(uint32_t inc, int32_t tf) { return (uint32_t)(((uint64_t)inc * (uint32_t)tf) >> 30); }

/* the decays at a partial of the band-b angle inc (the tables' by log2 f, linear between their points) */
static void cp_decay(uint32_t inc, uint32_t b, int32_t *sl, int32_t *ds, int32_t *r0)
{
    int32_t lf = re_log2(inc, 32) + CP_L_FS - (int32_t)(b << 26) - CP_LUT_L0;   /* log2(f / 20 Hz), Q26 */
    uint32_t i, fr;
    if (lf < 0) lf = 0;
    i = (uint32_t)lf >> 22; fr = ((uint32_t)lf >> 6) & 0xFFFFu;   /* 1/16 octave a point */
    if (i >= CP_LUT_N - 1u) { i = CP_LUT_N - 2u; fr = 65535u; }
    *sl = re_tab(CP_SL, i, fr); *ds = re_tab(CP_DS, i, fr); *r0 = re_tab(CP_R0, i, fr);
}

/* a layer's start of partial j of key K: Q26 (the 12-bit pair x 2^exponent) */
static void cp_amp(const cp_key_t *K, uint32_t layer, uint32_t j, uint32_t np, int64_t *re, int64_t *im)
{
    const uint8_t *a = &CP_AMP[K->a0 + (layer * np + j) * 3u];
    int32_t r = (int32_t)((uint32_t)(a[0] | (a[1] & 0x0Fu) << 8) << 20) >> 20;
    int32_t m = (int32_t)((uint32_t)(a[1] >> 4 | a[2] << 4) << 20) >> 20;
    int32_t ex = CP_EXP[K->e0 + layer];
    *re = ex >= 0 ? (int64_t)r << ex : (int64_t)r >> -ex;
    *im = ex >= 0 ? (int64_t)m << ex : (int64_t)m >> -ex;
}

/* e^u (u Q32, small) in Q30 */
static inline int64_t cp_eu(int64_t u32)
{
    int64_t u = u32 >> 2;
    return ((int64_t)1 << 30) + u + ((u * u) >> 31);
}

/* ---- the voices (ISR) */
static int32_t cp_tf = 1 << 30;               /* this block's tuning and bend (Q30) */

/* the strike of note at vel added to voice v's partials (fresh: their states zero), its stages from the start */
static void cp_hit(cp_voice_t *v, uint32_t note, uint32_t vel)
{
    const cp_key_t *K = &CP_KEYS[note - CP_KEY_LO];
    uint32_t np = v->np, j, b = 0, left = K->nb[0], dense = note - CP_KEY_LO + 1u >= CP_DENSE_FROM;
    uint32_t layer = dense ? CP_VL32_J[vel] : CP_VL9_J[vel], t = dense ? CP_VL32_T[vel] : CP_VL9_T[vel];
    int64_t vh = CP_VH[vel];
    for (j = 0; j < np; j++) {
        cp_part_t *p = &v->p[j];
        int64_t r0, i0, r1, i1, zr, zi, e1, e2, u;
        int32_t s, c2, s2, r0d;
        uint32_t inc;
        while (!left && b + 1u < CP_NB) left = K->nb[++b];
        left--;
        inc = cp_inc(CP_INC[K->p0 + j], v->tf);
        cp_decay(CP_INC[K->p0 + j], b, &p->sl, &p->ds, &r0d);
        p->rho = (int32_t)(((int64_t)r0d * cp_eu(-(int64_t)p->ds >> 1)) >> 30);   /* the first block's middle */
        cp_amp(K, layer, j, np, &r0, &i0);
        cp_amp(K, layer + 1u, j, np, &r1, &i1);
        zr = ((r0 + (((r1 - r0) * t) >> 16)) * vh) >> 16;          /* x the hammer's speed (Q16) */
        zi = ((i0 + (((i1 - i0) * t) >> 16)) * vh) >> 16;
        p->c = re_cos32(inc);
        s = re_cos32(inc - 0x40000000u);                           /* sin */
        c2 = (int32_t)((2 * (int64_t)p->c * p->c >> 30) - (1 << 30));
        s2 = (int32_t)(2 * (int64_t)s * p->c >> 30);
        u = (int64_t)(((uint32_t)(p->sl + (int32_t)(((int64_t)p->ds * cp_s(p->rho)) >> 30)) >> 8) << b);
        e1 = cp_eu(u); e2 = (e1 * e1) >> 30;
        /* the two samples before the first: x(-m) = Im(z w^-m), w = e^((-sigma + i om) T) */
        p->x1 += (int32_t)((((zi * p->c - zr * s) >> 30) * e1) >> 30);
        p->x2 += (int32_t)((((zi * c2 - zr * s2) >> 30) * e2) >> 30);
        p->binc = (uint32_t)((((uint64_t)inc * K->kd) >> 32) * (128u >> b));
    }
}

static void cp_start(cp_voice_t *v, uint32_t note, uint32_t vel)
{
    const cp_key_t *K = &CP_KEYS[note - CP_KEY_LO];
    uint32_t b;
    v->np = 0;
    for (b = 0; b < CP_NB; b++) v->np = (uint8_t)(v->np + K->nb[b]);
    memset(v->p, 0, sizeof v->p[0] * v->np);                     /* (the beats' phases 0: the strings in step) */
    v->tf = cp_tf = cp_tf_now();
    v->pair = (uint8_t)(K->kd != 0u);
    v->note = (uint8_t)note; v->vel = (uint8_t)vel;
    cp_hit(v, note, vel);
    v->damp = 0; v->rg = 32768; v->fade = 32768; v->quiet = 0; v->held = 1; v->alive = 1; v->pend = 0;
    v->age = ++cp_ages;
}

/* the damper of a key that is up: the pedal's weight (rhodes_tables.h PEDAL_W) x the release velocity's */
static void cp_damp(cp_voice_t *v)
{
    uint32_t w = PEDAL_W[cp_pedal_cc & 127u];
    w = w >= 32767u ? 32768u : w;
    w = (w * v->rg) >> 15;
    v->damp = v->held || !CP_KEYS[v->note - CP_KEY_LO].damped ? 0 : (int32_t)(((int64_t)CP_DAMP * w) >> 15);
}

static int cp_strike(uint32_t note, uint32_t vel)
{
    uint32_t i, best = CP_NV, old = 0xFFFFFFFFu;
    if (note < CP_KEY_LO || note >= CP_KEY_LO + CP_NKEY || !vel) return 1;
    if (vel > 127u) vel = 127u;
    for (i = 0; i < CP_NV; i++)                                   /* the strings still sounding: struck again */
        if (cp_v[i].alive && cp_v[i].fade == 32768 && cp_v[i].note == note) {
            cp_voice_t *v = &cp_v[i];
            cp_hit(v, note, vel);                                 /* (added: the strings move on as they were) */
            v->held = 1; v->damp = 0; v->quiet = 0; v->age = ++cp_ages;
            return 1;
        }
    for (i = 0; i < CP_NV; i++) if (!cp_v[i].alive) { best = i; break; }
    if (best == CP_NV) {                                          /* every voice taken: the oldest not already given
                                                                  * a note fades out over a block (as reed.c) */
        for (i = 0; i < CP_NV; i++) if (!cp_v[i].pend && cp_v[i].age < old) { old = cp_v[i].age; best = i; }
        if (best == CP_NV) return 0;
        cp_v[best].pend = 1; cp_v[best].pend_note = (uint8_t)note; cp_v[best].pend_vel = (uint8_t)vel;
        if (cp_v[best].fade == 32768) cp_v[best].fade = 32767;
        cp_shed_count++;
        return 1;
    }
    cp_start(&cp_v[best], note, vel);
    return 1;
}

static void cp_note_off(uint32_t note, uint32_t rv)
{
    uint32_t i;
    for (i = 0; i < CP_NV; i++)
        if (cp_v[i].alive && cp_v[i].note == note) {
            cp_v[i].held = 0;
            cp_v[i].rg = (uint16_t)rh_rel_gain(rv);
            cp_damp(&cp_v[i]);
        }
    for (i = 0; i < CP_NV; i++) if (cp_v[i].alive && cp_v[i].pend == 1u && cp_v[i].pend_note == note) cp_v[i].pend = 2;
}

static void cp_pedal(uint32_t cc)
{
    uint32_t i;
    cp_pedal_cc = (uint8_t)(cc > 127u ? 127u : cc);
    cp_hold = (uint8_t)(cp_pedal_cc >= 64u);
    for (i = 0; i < CP_NV; i++) if (cp_v[i].alive && !cp_v[i].held) cp_damp(&cp_v[i]);
}

static void cp_all_off(void)
{
    uint32_t i;
    cp_hold = 0; cp_pedal_cc = 0;
    for (i = 0; i < CP_NV; i++) {
        if (!cp_v[i].alive) continue;
        cp_v[i].held = 0; cp_v[i].rg = 32768; cp_damp(&cp_v[i]);
        if (cp_v[i].pend) cp_v[i].pend = 3;
    }
}

#ifdef CP_TAP
static int32_t cp_tap[256];
#endif
static int32_t cp_up_st[CP_NB - 1u][4][2];   /* the interpolators' states: per stage, its 4 all-pass sections (x1 y1) */
static int32_t cp_pre_st[6][4];               /* the preamp's biquads: x1 x2 y1 y2 */
static uint32_t cp_tph;                       /* the tremolo's phase (Q32) */
static int32_t cp_tl1, cp_tl2, cp_td;         /* its LDRs' light (Q30), its depth gliding to DEPTH's (Q30) */

static void cp_kill_all(void)
{
    uint32_t i;
    for (i = 0; i < CP_NV; i++) { cp_v[i].alive = 0; cp_v[i].pend = 0; }
    cp_active_bits = 0;
    memset(cp_up_st, 0, sizeof cp_up_st);
    memset(cp_pre_st, 0, sizeof cp_pre_st);
}

static int cp_quiet(void) { return cp_active_bits == 0 && cp_quiet_blocks >= 2u; }

/* one partial, n samples of its band into u: plain (gain 1) */
static void cp_run(cp_part_t *p, int32_t k1, int32_t k2, int32_t *u, uint32_t n)
{
    int32_t x1 = p->x1, x2 = p->x2;
    uint32_t i;
    for (i = 0; i < n; i++) {
        int32_t x0 = (int32_t)(((int64_t)k1 * x1 - (int64_t)k2 * x2) >> 30);
        x2 = x1; x1 = x0;
        u[i] += x0;
    }
    p->x1 = x1; p->x2 = x2;
}

/* with a gain g (Q30) moving by dg a sample: the beat, a fade */
static void cp_run_g(cp_part_t *p, int32_t k1, int32_t k2, int32_t g, int32_t dg, int32_t *u, uint32_t n)
{
    int32_t x1 = p->x1, x2 = p->x2;
    uint32_t i;
    for (i = 0; i < n; i++) {
        int32_t x0 = (int32_t)(((int64_t)k1 * x1 - (int64_t)k2 * x2) >> 30);
        x2 = x1; x1 = x0;
        u[i] += (int32_t)(((int64_t)x0 * g) >> 30);
        g += dg;
    }
    p->x1 = x1; p->x2 = x2;
}

/* one voice's block: every partial's words for the block, its samples into its band's buffer */
static void cp_voice(cp_voice_t *v, int32_t *const *buf)
{
    uint32_t j = 0, b, n, sh;
    int32_t f0 = v->fade, f1 = v->fade == 32768 ? 32768 : 0, peak = 0;
    const cp_key_t *K = &CP_KEYS[v->note - CP_KEY_LO];
    int retune = v->tf != cp_tf;
    if (retune) v->tf = cp_tf;
    for (b = 0; b < CP_NB; b++) {
        uint32_t e = j + K->nb[b];
        n = 256u >> b; sh = 8u - b;
        for (; j < e; j++) {
            cp_part_t *p = &v->p[j];
            int64_t v30 = p->ds >> 2, q = ((int64_t)1 << 30) - v30 + ((v30 * v30) >> 31);   /* e^-(se - sl) a block */
            int32_t sig = p->sl + (int32_t)(((int64_t)p->ds * cp_s(p->rho)) >> 30) + v->damp;   /* Q40 */
            int64_t u = (int64_t)((uint32_t)sig >> 8 << b) >> 2;     /* the loss a band sample, Q30 */
            int64_t r = ((int64_t)1 << 30) - u + ((u * u) >> 31);
            int32_t k2 = (int32_t)(((int64_t)1 << 30) - 2 * u + ((u * u) >> 29)), k1, a;
            if (retune) p->c = re_cos32(cp_inc(CP_INC[K->p0 + j], cp_tf));
            k1 = (int32_t)((2 * r * p->c) >> 30);
            p->rho = (int32_t)(((int64_t)p->rho * q) >> 30);
            if (v->pair) {                                       /* the beat (and the fade) */
                int32_t g0 = re_cos32(p->bph), g1 = re_cos32(p->bph + p->binc);
                p->bph += p->binc;
                if (f0 != 32768) { g0 = (int32_t)(((int64_t)g0 * f0) >> 15); g1 = (int32_t)(((int64_t)g1 * f1) >> 15); }
                cp_run_g(p, k1, k2, g0, (g1 - g0) >> sh, buf[b], n);
            }
            else if (f0 != 32768) cp_run_g(p, k1, k2, f0 << 15, ((f1 - f0) << 15) >> sh, buf[b], n);
            else cp_run(p, k1, k2, buf[b], n);
            a = p->x1 < 0 ? -p->x1 : p->x1;
            if (a > peak) peak = a;
            a = p->x2 < 0 ? -p->x2 : p->x2;
            if (a > peak) peak = a;
        }
    }
    if (f0 != 32768) v->fade = 0;
    v->peak = peak;
    v->quiet = peak < 671 ? v->quiet + 1u : 0u;                  /* |x| < 1e-5 (Q26) */
}

/* the interpolator: x (n samples) -> y (2n): the even samples branch A0's, the odd A1's, each two first-order
 * all-passes (c + z^-1) / (1 + c z^-1): y = c (x - y1) + x1 */
static void cp_up(const int32_t *x, int32_t *y, uint32_t n, int32_t (*st)[2])
{
    uint32_t i, k;
    for (i = 0; i < n; i++)
        for (k = 0; k < 2u; k++) {
            int32_t a = x[i], s;
            for (s = 0; s < 2; s++) {
                int32_t *z = st[2u * k + (uint32_t)s], c = CP_HB[2u * k + (uint32_t)s];
                int32_t o = (int32_t)(((int64_t)c * (a - z[1])) >> 30) + z[0];
                z[0] = a; z[1] = o; a = o;
            }
            y[2u * i + k] += a;
        }
}

static uint8_t cp_shed_req;                   /* ISR (engine.c): the audio ran late: fade out the quietest voice */
static void cp_shed(void)                     /* (as rhodes.c rh_shed: a released voice first) */
{
    uint32_t i, pick = CP_NV, best = 0xFFFFFFFFu;
    for (i = 0; i < CP_NV; i++) {
        uint32_t score;
        if (!cp_v[i].alive || cp_v[i].fade != 32768) continue;
        score = (cp_v[i].held ? 0x40000000u : 0u) + ((uint32_t)cp_v[i].peak >> 2);
        if (score < best) { best = score; pick = i; }
    }
    if (pick < CP_NV) { cp_v[pick].fade = 32767; cp_v[pick].pend = 0; cp_shed_count++; }
}

static void cp_render(int32_t *mono, uint32_t n)
{
    static int32_t b0[256], b1[128], b2[64], b3[32], b4[16];
    static int32_t *const buf[CP_NB] = {b0, b1, b2, b3, b4};
    uint32_t i, act = 0;
    const cp_coef_t *C = &cp_cf[cp_pub ? cp_pub - 1u : 0u];
    (void)n;                                                      /* (the engine's block: 256) */
    cp_tf = cp_tf_now();
    if (cp_shed_req) { cp_shed_req = 0; cp_shed(); }
    memset(b0, 0, sizeof b0); memset(b1, 0, sizeof b1); memset(b2, 0, sizeof b2);
    memset(b3, 0, sizeof b3); memset(b4, 0, sizeof b4);
    for (i = 0; i < CP_NV; i++) {
        cp_voice_t *v = &cp_v[i];
        if (!v->alive) continue;
        cp_voice(v, buf);
        if (v->quiet > 8u || v->fade == 0) {
            v->alive = 0;
            if (v->pend) {                                       /* the waiting note (let go meanwhile: damped) */
                uint32_t rel = v->pend;
                v->pend = 0; cp_start(v, v->pend_note, v->pend_vel);
                if (rel >= 2u) { v->held = 0; if (rel == 3u) v->rg = 32768; cp_damp(v); }
            }
        }
        if (v->alive) { act |= 1u << i; cp_notes[i] = v->note; }
    }
    cp_active_bits = act;
    cp_quiet_blocks = act ? 0u : cp_quiet_blocks + 1u;
    cp_up(b4, b3, 16u, cp_up_st[3]);                              /* the bands up: 2.76 -> 5.5 -> 11 -> 22 -> 44.1 kHz */
    cp_up(b3, b2, 32u, cp_up_st[2]);
    cp_up(b2, b1, 64u, cp_up_st[1]);
    cp_up(b1, b0, 128u, cp_up_st[0]);
#ifdef CP_TAP
    memcpy(cp_tap, b0, sizeof b0);                                /* host: the strings' bus (Q26), before the preamp */
#endif
    {   /* the preamp (Q26 in, Q26 out; Q28 words) and the tremolo */
        int32_t dt = re_vib_on ? CP_TR_D[re_vib_depth & 127u] : 0;
        uint32_t inc = re_vib_inc;
        int trem = dt || cp_td;
        for (i = 0; i < 256u; i++) {
            int32_t x = b0[i], k;
            for (k = 0; k < 6; k++) {
                const int32_t *w = C->bq[k];
                int32_t *s = cp_pre_st[k];
                int64_t a = (int64_t)w[0] * x + (int64_t)w[1] * s[0] + (int64_t)w[2] * s[1] - (int64_t)w[3] * s[2] - (int64_t)w[4] * s[3];
                int32_t y = (int32_t)(a >> 28);
                s[1] = s[0]; s[0] = x; s[3] = s[2]; s[2] = y; x = y;
            }
            if (trem) {                                          /* the oscillator, the LDRs, the antiphase shunts */
                int32_t o, l1, l2, g, sd;
                cp_tph += inc;
                o = re_cos32(cp_tph - 0x40000000u) - (int32_t)(((int64_t)CP_TR_H3 * re_cos32(3u * cp_tph - 0x40000000u)) >> 30);
                o = (int32_t)(((int64_t)o * CP_TR_NORM) >> 30);
                l1 = (1 << 29) + (o >> 1); l2 = (1 << 29) - (o >> 1);
                cp_tl1 += (int32_t)(((int64_t)(l1 - cp_tl1) * (l1 > cp_tl1 ? CP_TR_KON : CP_TR_KOFF)) >> 30);
                cp_tl2 += (int32_t)(((int64_t)(l2 - cp_tl2) * (l2 > cp_tl2 ? CP_TR_KON : CP_TR_KOFF)) >> 30);
                cp_td += (dt - cp_td + (dt > cp_td ? 255 : 0)) >> 8;
                g = (1 << 30) - (int32_t)(((int64_t)cp_td * ((cp_tl1 >> 1) + (cp_tl2 >> 1))) >> 30);
                sd = (int32_t)(((int64_t)cp_td * ((cp_tl2 - cp_tl1) >> 1)) >> 30);
                rh_side[i] = (int32_t)(((int64_t)x * sd) >> 41);  /* Q26 x Q30 -> Q15 */
                x = (int32_t)(((int64_t)x * g) >> 30);
            }
            else cp_tph += inc;
            mono[i] = (x + (1 << 10)) >> 11;                       /* Q26 -> Q15 */
        }
        rh_side_on = (uint8_t)(trem != 0);
    }
}
