/* SPDX-License-Identifier: GPL-3.0-only */
/* Frodhes reed engine: the Wurlitzer 200A (the float model tools/reed_model.py, the play page's v4.3; tables from
 * tools/gen_reed.py -> firmware/gen/reed_tables.h).
 *
 * Per voice: a half-sine hammer pulse drives three modes of one reed (its first on f0, its second at r2 f0, the
 * strike's knock: a share of the first that dies in ~20 ms, keys A1..A4), each the two-multiply resonator of bars.c
 * (x' = a1 x - a2 xp with first-order error feedback, the Q32 form while 2 - a1 < 1/2, else Q30). Their sum is the
 * tip's swing u (Q27, 1 = a full strike's first-mode swing x the key's u). The pickup: the bell (1 + z^2)^-p at
 * z = u + d (log2 and exp2 tables), (b0 - bell) x the key's gain. The voices sum; the input is a first difference
 * (the 200A's differentiating pickup input, reed_model fc 1 MHz: y = (s - s1) / 2); the preamp tanh(drive y).
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
#define RE_PBUF 160u                          /* the strike's force buffer (a re-strike joins the pulse left) */
#define RE_FADE 220u                          /* a stolen voice's fade (5 ms) */

typedef struct {
    int32_t k1, k2, f1, f2, s1, s2;           /* the sustain's, the fast release's and the slow release's words */
    int32_t b;                                /* Q31 input weight (w r sin w) */
    uint8_t lo, on, pad0, pad1;               /* lo: bit0 sustain, bit1 fast, bit2 slow in the Q32 form */
} re_mode_t;
typedef struct {
    re_mode_t m[3];
    int32_t d;                                /* Q27 rest point */
    int32_t gout;                             /* Q24 output gain: the bell's slope-1 norm x g x g_key */
    int32_t n0;                               /* the shortest contact (samples) */
    int32_t lvl[4];                           /* Q24 strike level per contact length n0, n0 + 1 ... (x the key's swing) */
} re_key_t;
#include "reed_tables.h"
_Static_assert(RE_NM == 3u && RE_NLV == 4u, "reed.c: three modes, four strike levels");
_Static_assert(RE_NV >= 1u && RE_NV <= 32u, "summaries are 32-bit masks");

typedef struct {
    int32_t x1, x2, err;                      /* x[n], x[n-1] (Q27), the error-feedback remainder */
    int32_t k1, k2, g;                        /* the live words; g: the input weight x the strike level */
    uint8_t lo, on;
} re_ms_t;

typedef struct {
    re_ms_t m[RE_NM];
    const re_key_t *k;
    int32_t pbuf[RE_PBUF];                    /* the force (Q30 x the strike level ratio), delayed one sample */
    int32_t b0;                               /* Q30 the bell at rest (so u = 0 gives 0 out) */
    int32_t lvl;                              /* Q24 the first strike's level (a re-strike's ratio) */
    uint32_t pn, pi;                          /* force samples, next one */
    uint32_t rel;                             /* samples since the damper fell (0: not released) */
    uint32_t quiet;                           /* blocks under the floor */
    uint32_t age;
    int32_t fade;                             /* Q15, 32768 = not fading */
    uint8_t note, vel, alive, held, stage, pend, pend_note, pend_vel;   /* stage: 0 free, 1 fast, 2 slow */
    uint8_t rpend;                            /* released: the stage changes after the next sample */
} re_voice_t;

static re_voice_t re_v[RE_NV];
static uint32_t re_active_bits;
static uint8_t re_notes[RE_NV];
static uint32_t re_ages;
static uint8_t re_hold;                       /* the sustain pedal down */
static int32_t re_s1;                         /* the first difference's last input (Q24) */
static uint32_t re_quiet_blocks;
static uint32_t re_shed_count;
#ifdef RE_TAP
static int32_t re_tap[256];                   /* host: the preamp's output before the Q15 rounding (Q30) */
#endif

static inline uint32_t re_clz64(uint64_t v) { return v ? (uint32_t)__builtin_clzll(v) : 64u; }

/* (1 + s)^-p, s = z^2: zz = (1 + z^2) in Q54 (z Q27), p Q16 -> Q30 */
static inline int32_t re_bell(uint64_t zz, int32_t p16)
{
    uint32_t e = 63u - re_clz64(zz), idx, fr;
    uint64_t m = zz << (63u - e);                                 /* [2^63, 2^64) */
    int32_t l30, n;
    int64_t L, pl;
    idx = (uint32_t)(m >> 53) & 1023u;
    fr = (uint32_t)(m >> 37) & 0xFFFFu;
    l30 = RE_LOG2[idx] + (int32_t)(((int64_t)(RE_LOG2[idx + 1] - RE_LOG2[idx]) * fr) >> 16);
    L = ((int64_t)((int32_t)e - 54) << 30) + l30;                 /* log2(1 + s), Q30 */
    pl = (L * p16) >> 16;                                         /* p log2(1 + s), Q30 (>= 0) */
    n = (int32_t)(pl >> 30);
    if (n > 30) return 0;
    fr = (uint32_t)pl & 0x3FFFFFFFu;
    idx = fr >> 20;
    fr = (fr >> 4) & 0xFFFFu;
    return (RE_EXP2[idx] + (int32_t)(((int64_t)(RE_EXP2[idx + 1] - RE_EXP2[idx]) * fr) >> 16)) >> n;
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

/* the damper's stage (0 sustain, 1 fast, 2 slow): its words, and the older state rescaled so the motion goes on as
 * the float model's x[k] env[k] does (reed-worklet.js: the states scaled by the step each sample). r30: the step
 * before over the step after (Q30) */
static void re_set_stage(re_voice_t *v, uint32_t st, int32_t r30)
{
    uint32_t j;
    for (j = 0; j < RE_NM; j++) {
        const re_mode_t *km = &v->k->m[j];
        re_ms_t *s = &v->m[j];
        uint8_t lo = (uint8_t)(km->lo >> st & 1u);
        if (!s->on) continue;
        if (lo != s->lo) s->err = lo ? (int32_t)((uint32_t)s->err << 2) : (int32_t)((uint32_t)s->err >> 2);
        s->lo = lo;
        s->k1 = st == 0 ? km->k1 : st == 1 ? km->f1 : km->s1;
        s->k2 = st == 0 ? km->k2 : st == 1 ? km->f2 : km->s2;
        if (r30 != (1 << 30)) s->x2 = (int32_t)(((int64_t)s->x2 * r30) >> 30);
    }
    v->stage = (uint8_t)st;
}

/* the strike level at vel: the key's level for this contact's length (the float model's 1 / its own 50 ms peak x
 * the full strike's x the key's swing) x (vel / 127)^gamma (Q24) */
static int32_t re_level(const re_key_t *k, uint32_t note, uint32_t vel)
{
    int32_t i = (int32_t)RE_PLEN[note - RE_KEY_LO][vel - 1u] - k->n0;
    if (i < 0) i = 0;
    if (i >= (int32_t)RE_NLV) i = (int32_t)RE_NLV - 1;
    return (int32_t)(((int64_t)k->lvl[i] * RE_VPOW[vel]) >> 30);
}

/* the strike's force joined to what is left of the voice's (a re-strike: the same reed, one voice a key) */
static void re_pulse(re_voice_t *v, uint32_t vel, int32_t ratio15)
{
    uint32_t n = RE_PLEN[v->note - RE_KEY_LO][vel - 1u], i, rest = v->pn > v->pi ? v->pn - v->pi : 0u, len;
    const int32_t *h = &RE_PSH[RE_PSH_OFF[n - RE_PL_MIN]];
    int32_t tmp[RE_PBUF];
    len = (rest > n + 1u ? rest : n + 1u);
    if (len > RE_PBUF) len = RE_PBUF;
    for (i = 0; i < len; i++) {
        int32_t a = i < rest ? v->pbuf[v->pi + i] : 0;
        if (i >= 1u && i - 1u < n) a += (int32_t)(((int64_t)h[i - 1u] * ratio15) >> 15);   /* the one-sample delay */
        tmp[i] = a;
    }
    for (i = 0; i < len; i++) v->pbuf[i] = tmp[i];
    v->pn = len; v->pi = 0;
}

static void re_start(re_voice_t *v, uint32_t note, uint32_t vel)
{
    const re_key_t *k = &RE_KEYS[note - RE_KEY_LO];
    uint32_t j;
    int64_t zz;
    v->k = k; v->note = (uint8_t)note; v->vel = (uint8_t)vel;
    v->lvl = re_level(k, note, vel);
    for (j = 0; j < RE_NM; j++) {
        re_ms_t *s = &v->m[j];
        s->x1 = s->x2 = s->err = 0;
        s->on = k->m[j].on;
        s->lo = 1;
        s->g = (int32_t)(((int64_t)k->m[j].b * v->lvl) >> 24);   /* Q31 x level: the input weight, Q31 */
    }
    re_set_stage(v, 0, 1 << 30);
    v->pn = v->pi = 0;
    re_pulse(v, vel, 32768);
    zz = (int64_t)k->d * k->d + ((int64_t)1 << 54);
    v->b0 = re_bell((uint64_t)zz, RE_BELL_P);
    v->rel = 0; v->rpend = 0; v->quiet = 0; v->fade = 32768; v->held = 1; v->alive = 1; v->pend = 0;
    v->age = ++re_ages;
}

static int re_strike(uint32_t note, uint32_t vel)
{
    uint32_t i, best = RE_NV, old = 0xFFFFFFFFu;
    if (note < RE_KEY_LO || note >= RE_KEY_LO + RE_NKEY || !vel) return 1;   /* outside the 200A's 64 keys */
    if (vel > 127u) vel = 127u;
    for (i = 0; i < RE_NV; i++)                                   /* the same reed still sounding: strike it again */
        if (re_v[i].alive && re_v[i].fade == 32768 && re_v[i].note == note) {
            re_voice_t *v = &re_v[i];
            int32_t lv = re_level(v->k, note, vel);
            int32_t r15 = (int32_t)(((int64_t)lv << 15) / (v->lvl ? v->lvl : 1));
            re_pulse(v, vel, r15);
            if (v->stage) re_set_stage(v, 0, v->stage == 1u ? RE_D_FAST : RE_D_SLOW);
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
        return 1;
    }
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
    re_active_bits = 0; re_s1 = 0;
}

static int re_quiet(void) { return re_active_bits == 0 && re_quiet_blocks >= 2u; }

/* one mode, n samples, its sum into u (Q27); the force f (or none) */
static void re_run(re_ms_t *s, const int32_t *f, int32_t *u, uint32_t n)
{
    int32_t x1 = s->x1, x2 = s->x2, err = s->err, k1 = s->k1, k2 = s->k2, g = s->g;
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

static void re_render(int32_t *mono, uint32_t n)
{
    static int32_t s[256], u[256];
    uint32_t i, j, act = 0;
    for (i = 0; i < n; i++) s[i] = 0;
    for (i = 0; i < RE_NV; i++) {
        re_voice_t *v = &re_v[i];
        int32_t peak = 0, b0 = v->b0, d = v->k ? v->k->d : 0, gout = v->k ? v->k->gout : 0;
        if (!v->alive) continue;
        for (j = 0; j < n; j++) u[j] = 0;
        j = 0;
        while (j < n) {                                       /* segments: the force's end, the damper's stages */
            uint32_t seg = n - j, pl = v->pi < v->pn ? v->pn - v->pi : 0u, m;
            if (pl && pl < seg) seg = pl;
            if (v->rpend) seg = 1u;
            else if (v->stage == 1u && v->rel + seg > RE_KNEE + 1u) seg = RE_KNEE + 1u - v->rel;
            for (m = 0; m < RE_NM; m++)
                if (v->m[m].on) re_run(&v->m[m], pl ? &v->pbuf[v->pi] : 0, u + j, seg);
            if (pl) v->pi += seg;
            if (v->rpend || v->rel) v->rel += seg;
            if (v->rpend) { v->rpend = 0; re_set_stage(v, 1, RE_D_REL); }
            else if (v->stage == 1u && v->rel == RE_KNEE + 1u) re_set_stage(v, 2, RE_D_KNEE);
            j += seg;
        }
        for (j = 0; j < n; j++) {
            int64_t z = (int64_t)u[j] + d;
            int32_t o = (int32_t)(((int64_t)(b0 - re_bell((uint64_t)(z * z) + ((uint64_t)1 << 54), RE_BELL_P)) * gout) >> 30);
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
    re_active_bits = act;
    re_quiet_blocks = act ? 0u : re_quiet_blocks + 1u;
    for (i = 0; i < n; i++) {
        int32_t y = (s[i] - re_s1) >> 1;                              /* the differentiating input, Q22 */
        int32_t t = (int32_t)(((int64_t)y * RE_DRIVE) >> 14);         /* x drive, Q24 */
        int32_t o = re_tanh(t);                                       /* Q30, full scale = tanh's ceiling */
        re_s1 = s[i];
#ifdef RE_TAP
        re_tap[i] = o;
#endif
        mono[i] = (int32_t)(((int64_t)o * RE_OUT_GAIN) >> 31);        /* Q15 */
    }
}
