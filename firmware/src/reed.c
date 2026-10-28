/* SPDX-License-Identifier: GPL-3.0-only */
/* Frodhes reed engine: the Wurlitzer 200A (the float model tools/reed_model.py, the play page's v4.3; tables from
 * tools/gen_reed.py -> firmware/gen/reed_tables.h).
 *
 * Per voice: a half-sine hammer pulse drives three modes of one reed (its first on f0, its second at r2 f0, the
 * strike's knock: a share of the first that dies in ~20 ms, keys A1..A4), and with BELL over its middle a fourth,
 * the second mode struck again by a short pulse of its own (the ping). Each mode is the two-multiply resonator of
 * bars.c (x' = a1 x - a2 xp with first-order error feedback, the Q32 form while 2 - a1 < 1/2, else Q30). Their sum is
 * the tip's swing u (Q27, 1 = a full strike's first-mode swing x the key's u). The pickup: the bell (1 + z^2)^-p at
 * z = u + d (log2 and exp2 tables), (b0 - bell) x the key's gain. The voices sum; the input is a first difference
 * (the 200A's differentiating pickup input, reed_model fc 1 MHz: y = (s - s1) / 2); the preamp tanh(drive y); the
 * strikes' action noise after it (picked up: in the line).
 *
 * The VOICE knobs (reed_model.apply_knobs; 0..127, 64 the set as it is, REG 127 the measured regulation): re_knob()
 * from the main loop; re_poll() (main loop) works out every key's words from the tables (re_rk_t) when one moved,
 * into the buffer the ISR does not read, then publishes it; a note-on copies its key's, so a sounding note keeps
 * the words it started with. The strike's level is the set's (its normalisation at the knobs' centre).
 *
 * Interface (engine.c): re_strike(note, vel) (0: no voice free yet, try next block), re_note_off(note),
 * re_pedal(cc), re_all_off(), re_render(mono, n) (Q15, the effects chain's level), re_quiet(), re_kill_all(); the
 * summaries re_active_bits, re_notes[]. The ISR owns every voice. */
#ifndef RE_VOICES
#ifdef FR_VOICES
#define RE_VOICES FR_VOICES
#else
#define RE_VOICES 10u
#endif
#endif
#define RE_NV RE_VOICES
#ifndef RING_PUBLISH
#define RING_PUBLISH() do { } while (0)     /* host mode: one thread, no barrier needed */
#endif
#define RE_NM 4u                              /* modes a voice: first, second, knock, ping */
#define RE_PBUF 160u                          /* the strike's force buffer (a re-strike joins the pulse left) */
#define RE_BBUF 16u                           /* the ping's */
#define RE_FADE 220u                          /* a stolen voice's fade (5 ms) */
#define RE_NSN RE_NV                          /* action noises at once: one a voice */

typedef struct {
    int32_t lt60, ld, wa, lgk, lt1, lt127, n0;
    int32_t lvl[4], lvl0[4];                   /* Q24: with the second mode's share, without it (BELL 0) */
} re_key_t;
typedef struct { int32_t ld, lg, lt, cents, sn; } re_reg_t;
#include "reed_tables.h"
_Static_assert(RE_NLV == 4u, "reed.c: four strike levels a key");
_Static_assert(RE_NV >= 1u && RE_NV <= 32u, "summaries are 32-bit masks");

/* ---- the knobs and each key's words (main loop -> ISR) */
enum { RK_TONE, RK_BARK, RK_BELL, RK_HAMMER, RK_DECAY, RK_RELEASE, RK_NOISE, RK_TUNE, RK_REG, RK_N };
static volatile int32_t re_kv[RK_N] = {64, 64, 64, 64, 64, 64, 64, 0, 127};   /* TUNE: cents, Q16 */
static volatile uint32_t re_kgen = 1u;        /* bumped by re_knob */

typedef struct {
    int32_t k[3][2];                          /* the words: sustain, the damper's fast stage, its slow one */
    int32_t b;                                /* Q31 input weight (w r sin w) */
    uint8_t lo, on;                           /* lo: bit s = stage s in the Q32 form */
} re_mw_t;
typedef struct {
    re_mw_t m[RE_NM];
    int32_t d;                                /* Q27 rest point */
    int32_t gout;                             /* Q24 output gain: the bell's slope-1 norm x g x g_key */
    int32_t ltcap;                            /* log2 of the contact's cap (tc_per periods), samples, Q26 */
    int32_t sn;                               /* Q30 the action noise's gain (x NOISE) */
} re_rk_t;
typedef struct {
    re_rk_t k[RE_NKEY];
    int32_t p16;                              /* the bell's p (TONE), Q16 */
    int32_t bark;                             /* the swing's factor (BARK), Q16 */
    int32_t lhx;                              /* log2 of HAMMER's contact factor, Q26 */
    int32_t w2f;                              /* the second mode's share over the set's (BELL up to 64), Q15 */
    int32_t r_rel, r_knee, r_fast, r_slow;    /* the damper's step ratios (Q30): 1 / fast, fast / slow, fast, slow */
    uint32_t knee;                            /* the fast stage's samples */
    uint32_t gen;
} re_rks_t;
static re_rks_t re_rks[2];
static volatile uint32_t re_pub;              /* the published buffer + 1 (0: none yet) */

typedef struct {
    int32_t x1, x2, err;                      /* x[n], x[n-1] (Q27), the error-feedback remainder */
    int32_t g;                                /* the input weight x the strike level (Q31) */
    uint8_t lo, on;
} re_ms_t;

typedef struct {
    re_ms_t m[RE_NM];
    re_rk_t rk;                               /* the key's words at the note-on */
    int32_t r_rel, r_knee, r_fast, r_slow, p16;
    uint32_t knee;
    int32_t pbuf[RE_PBUF];                    /* the force (Q30 x the strike level ratio), delayed one sample */
    int32_t bbuf[RE_BBUF];                    /* the ping's */
    int32_t b0;                               /* Q30 the bell at rest (so u = 0 gives 0 out) */
    int32_t lvl;                              /* Q24 the first strike's level (a re-strike's ratio) */
    uint32_t pn, pi, bn, bi, blen;            /* force samples, next one; the ping's, its pulse's length */
    uint32_t rel;                             /* samples since the damper fell (0: not released) */
    uint32_t quiet;                           /* blocks under the floor */
    uint32_t age;
    int32_t fade;                             /* Q15, 32768 = not fading */
    uint8_t note, vel, alive, held, stage, pend, pend_note, pend_vel;   /* stage: 0 sustain, 1 fast, 2 slow */
    uint8_t rpend;                            /* released: the stage changes after the next sample */
} re_voice_t;

typedef struct {
    uint32_t s, i;                            /* the LCG, samples done */
    int32_t g;                                /* Q28 gain x drive */
    int32_t env;                              /* Q30 */
    int32_t f[2][4];                          /* the band-passes' x1 x2 y1 y2 (Q30) */
    uint8_t on;
} re_sn_t;

static re_voice_t re_v[RE_NV];
static re_sn_t re_sn[RE_NSN];
static uint32_t re_active_bits;
static uint8_t re_notes[RE_NV];
static uint32_t re_ages;
static uint8_t re_hold;                       /* the sustain pedal down */
static int32_t re_s1;                         /* the first difference's last input (Q22) */
static uint32_t re_quiet_blocks;
static uint32_t re_shed_count;
#ifdef RE_TAP
static int32_t re_tap[256];                   /* host: the preamp's output before the Q15 rounding (Q30) */
#endif

/* ---- integer maths (tables: reed_tables.h) */
static inline uint32_t re_clz64(uint64_t v) { return v ? (uint32_t)__builtin_clzll(v) : 64u; }

static inline int32_t re_tab(const int32_t *t, uint32_t i, uint32_t fr16)   /* t[i] .. t[i + 1], fr16 / 65536 */
{
    return t[i] + (int32_t)(((int64_t)(t[i + 1] - t[i]) * fr16) >> 16);
}

/* log2 of v / 2^qin, Q26 (v > 0) */
static int32_t re_log2(uint64_t v, int32_t qin)
{
    uint32_t e = 63u - re_clz64(v);
    uint64_t m = v << (63u - e);
    int32_t l30 = re_tab(RE_LOG2, (uint32_t)(m >> 53) & 1023u, (uint32_t)(m >> 37) & 0xFFFFu);
    return (((int32_t)e - qin) << 26) + (l30 >> 4);
}

/* 2^(l / 2^26) x 2^qout, an int64 */
static int64_t re_exp2(int32_t l26, int32_t qout)
{
    int64_t l = (int64_t)l26 + ((int64_t)qout << 26);
    int32_t n = (int32_t)(l >> 26), f = (int32_t)(l & 0x3FFFFFF), m30;
    if (f == 0) m30 = 1 << 30;
    else { uint32_t y = (uint32_t)((1 << 26) - f); m30 = re_tab(RE_EXP2, y >> 16, y & 0xFFFFu) << 1; }   /* 2^f = 2 2^-(1-f) */
    if (n >= 30) return (int64_t)m30 << (n - 30);
    if (n <= -2) return 0;
    return ((int64_t)m30 + ((int64_t)1 << (29 - n))) >> (30 - n);
}

/* sin and cos of x (Q30 radians, 0 <= x < 1.6), Q30: Taylor to x^15 / x^14 (the reciprocals Q32) */
static void re_sincos(int32_t x, int32_t *s, int32_t *c)
{
    static const uint32_t RK[7] = {0x2AAAAAABu, 0x0CCCCCCDu, 0x06186186u, 0x038E38E4u, 0x0253C825u, 0x01A41A42u,
                                    0x01381381u};   /* 1 / (2k (2k + 1)), k = 1..7: 1/6, 1/20, 1/42 ... */
    static const uint32_t RC[7] = {0x80000000u, 0x15555555u, 0x08888889u, 0x04924925u, 0x02D82D83u, 0x01F07C1Fu,
                                    0x01681681u};   /* 1 / ((2k - 1) 2k): 1/2, 1/12, 1/30 ... */
    int64_t x2 = ((int64_t)x * x) >> 30, ts = x, tc = 1 << 30, as = x, ac = 1 << 30;
    int k;
    for (k = 0; k < 7; k++) {
        ts = -((((ts * x2) >> 30) * (int64_t)RK[k]) >> 32);
        tc = -((((tc * x2) >> 30) * (int64_t)RC[k]) >> 32);
        as += ts; ac += tc;
    }
    *s = (int32_t)as; *c = (int32_t)ac;
}

/* sin(pi / 2 ph / 2^26), ph Q16 of the quarter table's steps (0 .. 2 x 1024 x 65536: 0 .. pi), Q30 */
static inline int32_t re_sin_ph(uint32_t ph)
{
    if (ph > (1024u << 16)) ph = (2048u << 16) - ph;
    return ph >= (1024u << 16) ? RE_SIN[1024] : re_tab(RE_SIN, ph >> 16, ph & 0xFFFFu);
}

/* (1 + s)^-p, s = z^2: zz = (1 + z^2) in Q54 (z Q27), p Q16 -> Q30 */
static inline int32_t re_bell(uint64_t zz, int32_t p16)
{
    uint32_t e = 63u - re_clz64(zz), fr;
    uint64_t m = zz << (63u - e);                                 /* [2^63, 2^64) */
    int32_t l30 = re_tab(RE_LOG2, (uint32_t)(m >> 53) & 1023u, (uint32_t)(m >> 37) & 0xFFFFu), n;
    int64_t L = ((int64_t)((int32_t)e - 54) << 30) + l30;        /* log2(1 + s), Q30 */
    int64_t pl = (L * p16) >> 16;                                 /* p log2(1 + s), Q30 (>= 0) */
    n = (int32_t)(pl >> 30);
    if (n > 30) return 0;
    fr = (uint32_t)pl & 0x3FFFFFFFu;
    return re_tab(RE_EXP2, fr >> 20, (fr >> 4) & 0xFFFFu) >> n;
}

static inline int32_t re_tanh(int32_t x24)                        /* Q24 in, Q30 out */
{
    int32_t a = x24 < 0 ? -x24 : x24, r;
    uint32_t i, fr;
    if (a >= (8 << 24)) r = RE_TANH[RE_TANH_N];
    else {
        i = (uint32_t)a >> 17;                                     /* 1/128 steps */
        fr = (uint32_t)a & 0x1FFFFu;
        r = RE_TANH[i] + (int32_t)(((int64_t)(RE_TANH[i + 1] - RE_TANH[i]) * fr) >> 17);
    }
    return x24 < 0 ? -r : r;
}

/* ---- the words (main loop) */
#define RE_HALF_MAX 1517971300                /* 0.45 pi (Q30): a mode over 0.45 fs is left out */

/* the resonator for the half angle h (Q30 radians) and decay a (per sample, Q40): lo, the two words, r sin om (Q30) */
static void re_words(int32_t h, int64_t a40, uint8_t *lo, int32_t *k1, int32_t *k2, int32_t *rs)
{
    int32_t sh, ch;
    int64_t a2 = ((a40 >> 8) * (a40 >> 8)) >> 24, a3 = ((a2 >> 8) * (a40 >> 8)) >> 24;   /* (a < 2^-4: no overflow) */
    int64_t omr = a40 - (a2 >> 1) + ((a3 * 0x2AAAAAABLL) >> 32);  /* 1 - r = a - a^2/2 + a^3/6, Q40 */
    int64_t omr2 = 2 * a40 - 2 * a2 + ((a3 * 0x55555555LL) >> 30);   /* 1 - r^2 = 2a - 2a^2 + 4a^3/3 */
    int64_t r30 = ((int64_t)1 << 30) - (omr >> 10), s2, cosw, c1;
    re_sincos(h, &sh, &ch);
    s2 = ((int64_t)sh * sh) >> 30;                                /* sin^2(om / 2), Q30 */
    cosw = ((int64_t)1 << 30) - 2 * s2;
    c1 = (((int64_t)sh * sh) >> 26) + ((2 * cosw * (omr >> 8)) >> 30);   /* 2 - a1 = 4 sin^2 + 2 cos (1 - r), Q32 */
    if (c1 < 0x7FFFFFFF) { *lo = 1; *k1 = (int32_t)c1; *k2 = (int32_t)(omr2 >> 8); }
    else { *lo = 0; *k1 = (int32_t)((2 * r30 * cosw) >> 30); *k2 = (int32_t)((r30 * r30) >> 30); }
    *rs = (int32_t)(((((2 * (int64_t)sh * ch) >> 30)) * r30) >> 30);
}

/* REG: linear between its anchors (the logs: so log-linear), f the field */
static int32_t re_reg(int32_t a15, uint32_t key, uint32_t f)
{
    uint32_t i = 0;
    int32_t x, y, t;
    const re_reg_t *A, *B;
    while (i + 2u < RE_NREG && a15 > (int32_t)RE_REG_AMT[i + 1]) i++;
    A = &RE_REG[i][key]; B = &RE_REG[i + 1u][key];
    x = f == 0 ? A->ld : f == 1 ? A->lg : f == 2 ? A->lt : f == 3 ? A->cents : A->sn;
    y = f == 0 ? B->ld : f == 1 ? B->lg : f == 2 ? B->lt : f == 3 ? B->cents : B->sn;
    t = ((a15 - (int32_t)RE_REG_AMT[i]) << 15) / (int32_t)(RE_REG_AMT[i + 1] - RE_REG_AMT[i]);
    t = t < 0 ? 0 : t > 32768 ? 32768 : t;
    return x + (int32_t)(((int64_t)(y - x) * t) >> 15);
}

static inline int32_t re_x16(int32_t v) { return (v - 64) << 10; }   /* x = (knob - 64) / 64, Q16 */

static void re_build(re_rks_t *R)
{
    int32_t kv[RK_N], x, n15, lfast, lslow, a15;
    int64_t af, as, w2, dd;
    uint32_t key, j, st;
    uint8_t lo;
    for (j = 0; j < RK_N; j++) kv[j] = re_kv[j];
    R->p16 = (int32_t)re_exp2(re_log2((uint64_t)RE_BELL_P, 16) + (re_x16(kv[RK_TONE]) << 10), 16);   /* p 2^x */
    R->bark = (int32_t)re_exp2((int32_t)(((int64_t)re_x16(kv[RK_BARK]) * 3) << 8), 16);             /* 2^(0.75 x) */
    R->lhx = -(re_x16(kv[RK_HAMMER]) << 10);                                                        /* 2^-x */
    x = (int32_t)(((int64_t)re_x16(kv[RK_RELEASE]) * 106344155) >> 16);                             /* x log2 3 */
    lfast = RE_L_FAST + x; lslow = RE_L_SLOW + x;                                                   /* x 3^x */
    af = re_exp2(RE_L_T60K - lfast, 40); as = re_exp2(RE_L_T60K - lslow, 40);   /* the per-sample decays, Q40 */
#define RE_SQ2(a) ((((a) >> 8) * ((a) >> 8)) >> 25)              /* a^2 / 2, Q40 (a < 2^-4) */
    R->r_fast = (int32_t)(((int64_t)1 << 30) - ((af - RE_SQ2(af)) >> 10));                          /* e^-a, Q30 */
    R->r_slow = (int32_t)(((int64_t)1 << 30) - ((as - RE_SQ2(as)) >> 10));
    R->r_rel = (int32_t)(((int64_t)1 << 30) + ((af + RE_SQ2(af)) >> 10));                           /* e^a */
    dd = af - as;
    R->r_knee = (int32_t)(((int64_t)1 << 30) - ((dd - RE_SQ2(dd)) >> 10));                          /* e^-(af - as) */
#undef RE_SQ2
    R->knee = (uint32_t)((re_exp2(lfast, 16) * RE_KNEE_K + ((int64_t)1 << 31)) >> 32);
    a15 = (kv[RK_REG] * 32768 + 63) / 127;
    /* NOISE: (knob / 64)^1.5; BELL: up to 64 the second mode's share x (knob / 64)^2, over it the ping */
    n15 = kv[RK_NOISE] ? (int32_t)re_exp2((int32_t)(((int64_t)re_log2((uint64_t)kv[RK_NOISE], 6) * 3) >> 1), 15) : 0;
    w2 = kv[RK_BELL] <= 64 ? ((int64_t)RE_W2 * kv[RK_BELL] * kv[RK_BELL]) >> 12 : RE_W2;
    R->w2f = kv[RK_BELL] <= 64 ? (kv[RK_BELL] * kv[RK_BELL]) << 3 : 32768;
    for (key = 0; key < RE_NKEY; key++) {
        const re_key_t *K = &RE_KEYS[key];
        re_rk_t *r = &R->k[key];
        int32_t cents = re_reg(a15, key, 3) + kv[RK_TUNE];
        int32_t lom = RE_L_OM69 + ((int32_t)key + 33 - 69) * 5592405 + (int32_t)(((int64_t)cents * 55924) >> 16);
        int32_t lt60 = K->lt60 + re_reg(a15, key, 2) + (re_x16(kv[RK_DECAY]) << 10);
        int32_t ld = K->ld + re_reg(a15, key, 0), lg = re_reg(a15, key, 1) + K->lgk, lzz, lp;
        struct { int32_t lom, lt60; int64_t w; } md[RE_NM] = {
            {lom, lt60, (int64_t)1 << 30}, {lom + RE_L_R2, lt60 - RE_L_Q2, w2},
            {lom, RE_L_ATT_T60, K->wa}, {lom + RE_L_R2, lt60 - RE_L_Q2, 0}};
        if (kv[RK_BELL] > 64) {                                   /* the ping: BELL_MAX x^2 */
            int64_t xb = re_x16(kv[RK_BELL]);
            md[3].w = ((int64_t)RE_BELL_MAX * ((xb * xb) >> 16)) >> 16;
        }
        for (j = 0; j < RE_NM; j++) {
            re_mw_t *m = &r->m[j];
            int64_t h = re_exp2(md[j].lom - (1 << 26), 30), a = re_exp2(RE_L_T60K - md[j].lt60, 40);
            int32_t k1, k2, rs;
            m->on = (uint8_t)(md[j].w > 0 && h < RE_HALF_MAX);
            m->lo = 0; m->b = 0;
            memset(m->k, 0, sizeof m->k);
            if (!m->on) continue;
            for (st = 0; st < 3u; st++) {
                re_words((int32_t)h, a + (st == 1u ? af : st == 2u ? as : 0), &lo, &k1, &k2, &rs);
                m->k[st][0] = k1; m->k[st][1] = k2;
                m->lo |= (uint8_t)(lo << st);
                if (!st) m->b = (int32_t)((md[j].w * rs) >> 29);                                    /* Q31 */
            }
        }
        r->d = (int32_t)re_exp2(ld, 27);
        /* the gain: norm = (1 + d^2)^(p + 1) / (2 p d), x g x g_key */
        lzz = re_log2((uint64_t)((int64_t)r->d * r->d) + ((uint64_t)1 << 54), 54);
        lp = re_log2((uint64_t)R->p16, 16);
        r->gout = (int32_t)re_exp2((int32_t)(((int64_t)lzz * (R->p16 + 65536)) >> 16) - (1 << 26) - lp - ld + lg, 24);
        r->ltcap = RE_L_TCPER + RE_L_2PI - lom;
        r->sn = (int32_t)(((int64_t)re_reg(a15, key, 4) * n15) >> 15);
    }
}

static void re_knob(uint32_t k, int32_t v)                       /* main loop: a VOICE knob (TUNE: cents, Q16) */
{
    if (k < RK_N && re_kv[k] != v) { re_kv[k] = v; re_kgen++; }
}

static void re_poll(void)                                        /* main loop: the words for the knobs as they are */
{
    uint32_t p = re_pub, b = p == 1u ? 1u : 0u, g = re_kgen;
    if (p && re_rks[p - 1u].gen == g) return;
    re_build(&re_rks[b]);
    re_rks[b].gen = g;
    RING_PUBLISH();
    re_pub = b + 1u;
}

/* ---- the voices (ISR) */
/* the damper's stage (0 sustain, 1 fast, 2 slow): the older state rescaled so the motion goes on as the float
 * model's x[k] env[k] does (reed-worklet.js: the states scaled by the step each sample). r30: the step before over
 * the step after (Q30) */
static void re_set_stage(re_voice_t *v, uint32_t st, int32_t r30)
{
    uint32_t j;
    for (j = 0; j < RE_NM; j++) {
        re_ms_t *s = &v->m[j];
        uint8_t lo = (uint8_t)(v->rk.m[j].lo >> st & 1u);
        if (!s->on) continue;
        if (lo != s->lo) s->err = lo ? (int32_t)((uint32_t)s->err << 2) : (int32_t)((uint32_t)s->err >> 2);
        s->lo = lo;
        s->x2 = (int32_t)(((int64_t)s->x2 * r30) >> 30);
    }
    v->stage = (uint8_t)st;
}

/* the contact (samples, rounded) at vel: the reference hammer's (ref), or with HAMMER's factor; capped at tc_per periods */
static uint32_t re_contact(const re_key_t *K, const re_rk_t *r, uint32_t vel, int32_t lhx, int ref)
{
    int32_t l = K->lt1 + (K->lt127 - K->lt1) * (int32_t)(vel - 1u) / 126;
    int64_t n;
    if (!ref) l += lhx;
    if (l > r->ltcap) l = r->ltcap;
    n = (re_exp2(l, 16) + 32768) >> 16;
    return n < 2 ? 2u : n > (int64_t)(RE_PBUF - 2u) ? RE_PBUF - 2u : (uint32_t)n;
}

/* the strike level at vel: the key's level for the reference contact's length (between the set's second-mode share
 * and none, as BELL has it: the float model's normalisation sees it) x BARK x (vel / 127)^gamma (Q24) */
static int32_t re_level(const re_key_t *K, const re_rk_t *r, uint32_t vel, int32_t bark, int32_t w2f)
{
    int32_t i = (int32_t)re_contact(K, r, vel, 0, 1) - K->n0, l;
    i = i < 0 ? 0 : i >= (int32_t)RE_NLV ? (int32_t)RE_NLV - 1 : i;
    l = K->lvl0[i] + (int32_t)(((int64_t)(K->lvl[i] - K->lvl0[i]) * w2f) >> 15);
    return (int32_t)(((((int64_t)l * RE_VPOW[vel]) >> 30) * bark) >> 16);
}

/* a half-sine of n samples, area 1 (Q30: sin((i + .5) pi / n) sin(pi / 2n)), x r15, added to what is left of buf from
 * at (of had), delayed one sample (reed-worklet.js Mode.step's x1); the new length */
static uint32_t re_join(int32_t *buf, uint32_t at, uint32_t had, uint32_t n, int32_t r15, uint32_t cap)
{
    uint32_t i, rest = had > at ? had - at : 0u, len = rest > n + 1u ? rest : n + 1u, step = 67108864u / n;
    int32_t sc = re_sin_ph(step);                                 /* sin(pi / 2n) */
    if (len > cap) len = cap;
    for (i = 0; i < len; i++) {
        int32_t a = i < rest ? buf[at + i] : 0;
        if (i >= 1u && i - 1u < n)
            a += (int32_t)(((((int64_t)re_sin_ph((2u * (i - 1u) + 1u) * step) * sc) >> 30) * r15) >> 15);
        buf[i] = a;
    }
    return len;
}

static const re_rks_t *re_now(void) { return &re_rks[re_pub ? re_pub - 1u : 0u]; }

static void re_start(re_voice_t *v, uint32_t note, uint32_t vel)
{
    const re_rks_t *R = re_now();
    const re_key_t *K = &RE_KEYS[note - RE_KEY_LO];
    uint32_t j;
    v->rk = R->k[note - RE_KEY_LO];
    v->r_rel = R->r_rel; v->r_knee = R->r_knee; v->r_fast = R->r_fast; v->r_slow = R->r_slow; v->knee = R->knee;
    v->p16 = R->p16;
    v->note = (uint8_t)note; v->vel = (uint8_t)vel;
    v->lvl = re_level(K, &v->rk, vel, R->bark, R->w2f);
    for (j = 0; j < RE_NM; j++) {
        re_ms_t *s = &v->m[j];
        s->x1 = s->x2 = s->err = 0;
        s->on = v->rk.m[j].on;
        s->lo = (uint8_t)(v->rk.m[j].lo & 1u);
        s->g = (int32_t)(((int64_t)v->rk.m[j].b * v->lvl) >> 24);   /* the input weight x the level, Q31 */
    }
    v->stage = 0;
    v->pn = re_join(v->pbuf, 0, 0, re_contact(K, &v->rk, vel, R->lhx, 0), 32768, RE_PBUF); v->pi = 0;
    v->bn = v->bi = v->blen = 0;
    if (v->m[3].on) {                                             /* the ping's pulse: min(0.25 ms, half its period) */
        int32_t l = RE_L_PING, lh = v->rk.ltcap - RE_L_TCPER - RE_L_R2 - (1 << 26);
        int64_t nb;
        if (lh < l) l = lh;
        nb = (re_exp2(l, 16) + 32768) >> 16;
        v->blen = nb < 2 ? 2u : nb > (int64_t)(RE_BBUF - 2u) ? RE_BBUF - 2u : (uint32_t)nb;
        v->bn = re_join(v->bbuf, 0, 0, v->blen, 32768, RE_BBUF);
    }
    v->b0 = re_bell((uint64_t)((int64_t)v->rk.d * v->rk.d) + ((uint64_t)1 << 54), v->p16);
    v->rel = 0; v->rpend = 0; v->quiet = 0; v->fade = 32768; v->held = 1; v->alive = 1; v->pend = 0;
    v->age = ++re_ages;
}

/* the strike's action noise (reed_model.snoise): from vel0 up, all of it from vel1 */
static void re_noise(uint32_t note, uint32_t vel, const re_rk_t *r)
{
    uint32_t i, best = 0;
    int32_t share;
    re_sn_t *q;
    if (vel <= RE_SN_VEL0 || !r->sn) return;
    share = vel >= RE_SN_VEL1 ? 32768 : (int32_t)(((vel - RE_SN_VEL0) << 15) / (RE_SN_VEL1 - RE_SN_VEL0));
    for (i = 0; i < RE_NSN; i++) {
        if (!re_sn[i].on) { best = i; break; }
        if (re_sn[i].i > re_sn[best].i) best = i;                 /* all busy: the oldest */
    }
    q = &re_sn[best];
    memset(q, 0, sizeof *q);
    q->s = (note * 2654435761u) ^ 0x5BD1E995u;
    q->g = (int32_t)(((((int64_t)r->sn * share) >> 15) * RE_DRIVE) >> 18);
    q->env = 1 << 30; q->on = 1;
}

static int re_strike(uint32_t note, uint32_t vel)
{
    uint32_t i, best = RE_NV, old = 0xFFFFFFFFu;
    const re_rks_t *R = re_now();
    if (note < RE_KEY_LO || note >= RE_KEY_LO + RE_NKEY || !vel) return 1;   /* outside the 200A's 64 keys */
    if (!re_pub) return 1;                                        /* (no words yet: the main loop's first pass) */
    if (vel > 127u) vel = 127u;
    for (i = 0; i < RE_NV; i++)                                   /* the same reed still sounding: strike it again */
        if (re_v[i].alive && re_v[i].fade == 32768 && re_v[i].note == note) {
            re_voice_t *v = &re_v[i];
            const re_key_t *K = &RE_KEYS[note - RE_KEY_LO];
            int32_t lv = re_level(K, &v->rk, vel, R->bark, R->w2f);
            int32_t r15 = (int32_t)re_exp2(re_log2((uint64_t)(lv > 0 ? lv : 1), 0) - re_log2((uint64_t)(v->lvl > 0 ? v->lvl : 1), 0), 15);
            re_noise(note, vel, &v->rk);
            v->pn = re_join(v->pbuf, v->pi, v->pn, re_contact(K, &v->rk, vel, R->lhx, 0), r15, RE_PBUF); v->pi = 0;
            if (v->m[3].on) { v->bn = re_join(v->bbuf, v->bi, v->bn, v->blen, r15, RE_BBUF); v->bi = 0; }
            if (v->stage) re_set_stage(v, 0, v->stage == 1u ? v->r_fast : v->r_slow);
            v->rel = 0; v->rpend = 0; v->quiet = 0; v->held = 1; v->age = ++re_ages;
            return 1;
        }
    for (i = 0; i < RE_NV; i++) if (!re_v[i].alive) { best = i; break; }
    if (best == RE_NV) {                                          /* every voice taken: the oldest fades in 5 ms */
        for (i = 0; i < RE_NV; i++) if (re_v[i].pend) return 0;   /* one steal at a time */
        for (i = 0; i < RE_NV; i++) if (re_v[i].age < old) { old = re_v[i].age; best = i; }
        re_v[best].pend = 1; re_v[best].pend_note = (uint8_t)note; re_v[best].pend_vel = (uint8_t)vel;
        if (re_v[best].fade == 32768) re_v[best].fade = 32767;
        re_shed_count++;
        re_noise(note, vel, &R->k[note - RE_KEY_LO]);
        return 1;
    }
    re_noise(note, vel, &R->k[note - RE_KEY_LO]);
    re_start(&re_v[best], note, vel);
    return 1;
}

/* the damper falls: the next sample is the release's first, still undamped (reed_model: env[0] = 1) */
static void re_release(re_voice_t *v) { if (!v->rel && !v->rpend && v->alive && !v->stage) v->rpend = 1; }

static void re_note_off(uint32_t note)
{
    uint32_t i;
    for (i = 0; i < RE_NV; i++)
        if (re_v[i].alive && re_v[i].note == note) {
            re_v[i].held = 0;
            if (!re_hold) re_release(&re_v[i]);
        }
}

static void re_pedal(uint32_t cc)
{
    uint32_t i;
    re_hold = (uint8_t)(cc >= 64u);
    if (!re_hold) for (i = 0; i < RE_NV; i++) if (re_v[i].alive && !re_v[i].held) re_release(&re_v[i]);
}

static void re_all_off(void) { uint32_t i; re_hold = 0; for (i = 0; i < RE_NV; i++) { re_v[i].held = 0; re_release(&re_v[i]); } }

static void re_kill_all(void)
{
    uint32_t i;
    for (i = 0; i < RE_NV; i++) { re_v[i].alive = 0; re_v[i].pend = 0; }
    for (i = 0; i < RE_NSN; i++) re_sn[i].on = 0;
    re_active_bits = 0; re_s1 = 0;
}

static int re_quiet(void) { return re_active_bits == 0 && re_quiet_blocks >= 2u; }

/* one mode, n samples, its sum into u (Q27); the force f (or none) */
static void re_run(re_ms_t *s, const re_mw_t *w, uint32_t st, const int32_t *f, int32_t *u, uint32_t n)
{
    int32_t x1 = s->x1, x2 = s->x2, err = s->err, k1 = w->k[st][0], k2 = w->k[st][1], g = s->g;
    uint32_t i;
    if (s->lo)
        for (i = 0; i < n; i++) {
            int64_t a = (int64_t)(uint32_t)err - (int64_t)k1 * x1 + (int64_t)k2 * x2;
            if (f) a += ((int64_t)g * f[i]) >> 2;             /* Q31 x Q30 >> 2 = Q59: x 2^32 of Q27 */
            x2 = 2 * x1 - x2 + (int32_t)(a >> 32); err = (int32_t)a;
            { int32_t t = x1; x1 = x2; x2 = t; }
            u[i] += x1;
        }
    else
        for (i = 0; i < n; i++) {
            int64_t a = (int64_t)(uint32_t)err + (int64_t)k1 * x1 - (int64_t)k2 * x2;
            if (f) a += ((int64_t)g * f[i]) >> 4;             /* Q31 x Q30 >> 4 = Q57: x 2^30 of Q27 */
            x2 = (int32_t)(a >> 30); err = (int32_t)a & 0x3FFFFFFF;
            { int32_t t = x1; x1 = x2; x2 = t; }
            u[i] += x1;
        }
    s->x1 = x1; s->x2 = x2; s->err = err;
}

static int32_t re_sn_next(re_sn_t *q)                            /* one sample of an action noise, Q30 x drive */
{
    int32_t x, j, e;
    q->s = 1664525u * q->s + 1013904223u;
    x = (int32_t)((q->s >> 8) << 7) - (1 << 30);                  /* [-1, 1), Q30 */
    for (j = 0; j < 2; j++) {
        int32_t *f = q->f[j];
        int64_t y = (int64_t)RE_SN_B0 * x - (int64_t)RE_SN_B0 * f[1] - (int64_t)RE_SN_A1 * f[2] - (int64_t)RE_SN_A2 * f[3];
        f[1] = f[0]; f[0] = x; f[3] = f[2]; f[2] = (int32_t)(y >> 30); x = f[2];
    }
    e = q->i < RE_SN_RISE ? q->env / (int32_t)RE_SN_RISE * (int32_t)q->i : q->env;
    q->env = (int32_t)(((int64_t)q->env * RE_SN_DEC) >> 30);
    if (++q->i >= RE_SN_LEN) q->on = 0;
    return (int32_t)(((((int64_t)x * e) >> 30) * q->g) >> 28);
}

static void re_render(int32_t *mono, uint32_t n)
{
    static int32_t s[256], u[256], ns[256];
    uint32_t i, j, act = 0;
    for (i = 0; i < n; i++) { s[i] = 0; ns[i] = 0; }
    for (i = 0; i < RE_NV; i++) {
        re_voice_t *v = &re_v[i];
        int32_t peak = 0, b0 = v->b0, d = v->rk.d, gout = v->rk.gout, p16 = v->p16;
        if (!v->alive) continue;
        for (j = 0; j < n; j++) u[j] = 0;
        j = 0;
        while (j < n) {                                       /* segments: the forces' ends, the damper's stages */
            uint32_t seg = n - j, pl = v->pi < v->pn ? v->pn - v->pi : 0u, bl = v->bi < v->bn ? v->bn - v->bi : 0u, m;
            if (pl && pl < seg) seg = pl;
            if (bl && bl < seg) seg = bl;
            if (v->rpend) seg = 1u;
            else if (v->stage == 1u && v->rel + seg > v->knee + 1u) seg = v->knee + 1u - v->rel;
            for (m = 0; m < 3u; m++)
                if (v->m[m].on) re_run(&v->m[m], &v->rk.m[m], v->stage, pl ? &v->pbuf[v->pi] : 0, u + j, seg);
            if (v->m[3].on) re_run(&v->m[3], &v->rk.m[3], v->stage, bl ? &v->bbuf[v->bi] : 0, u + j, seg);
            if (pl) v->pi += seg;
            if (bl) v->bi += seg;
            if (v->rpend || v->rel) v->rel += seg;
            if (v->rpend) { v->rpend = 0; re_set_stage(v, 1, v->r_rel); }
            else if (v->stage == 1u && v->rel == v->knee + 1u) re_set_stage(v, 2, v->r_knee);
            j += seg;
        }
        for (j = 0; j < n; j++) {
            int64_t z = (int64_t)u[j] + d;
            int32_t o = (int32_t)(((int64_t)(b0 - re_bell((uint64_t)(z * z) + ((uint64_t)1 << 54), p16)) * gout) >> 30);
            int32_t a = u[j] < 0 ? -u[j] : u[j];
            if (a > peak) peak = a;
            if (v->fade != 32768) {
                o = (int32_t)(((int64_t)o * v->fade) >> 15);
                v->fade -= 32768 / (int32_t)RE_FADE + 1; if (v->fade < 0) v->fade = 0;
            }
            s[j] += o >> 2;                                   /* Q22: every voice's sum */
        }
        v->quiet = peak < 1342 && v->pi >= v->pn ? v->quiet + 1u : 0u;   /* |u| < 1e-5 (Q27) */
        if (v->quiet > 8u || (v->fade != 32768 && v->fade <= 0)) {
            v->alive = 0;
            if (v->pend) { v->pend = 0; re_start(v, v->pend_note, v->pend_vel); }
        }
        if (v->alive) { act |= 1u << i; re_notes[i] = v->note; }
    }
    for (i = 0; i < RE_NSN; i++)
        for (j = 0; j < n && re_sn[i].on; j++) ns[j] += re_sn_next(&re_sn[i]);
    re_active_bits = act;
    re_quiet_blocks = act ? 0u : re_quiet_blocks + 1u;
    for (i = 0; i < n; i++) {
        int32_t y = (s[i] - re_s1) >> 1;                              /* the differentiating input, Q22 */
        int32_t t = (int32_t)(((int64_t)y * RE_DRIVE) >> 14);         /* x drive, Q24 */
        int32_t o = re_tanh(t) + ns[i];                               /* Q30, full scale = tanh's ceiling */
        re_s1 = s[i];
#ifdef RE_TAP
        re_tap[i] = o;
#endif
        mono[i] = (int32_t)(((int64_t)o * RE_OUT_GAIN) >> 31);        /* Q15 */
    }
}
