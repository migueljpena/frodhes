/* SPDX-License-Identifier: GPL-3.0-only */
/* Frodhes sequencer (0.9.1), main loop only: a looping pattern recorder. What is played (single notes, chords, chord
 * mode's chords) is recorded as notes with their own timing; QUANT snaps them as they play, so changing it reshapes
 * the pattern at once, and nothing is lost. Edits never stop the loop:
 *
 *   PLAY            start / stop (on the SEQUENCER page)
 *   SEQ             recording on / off: playing, every pass adds what you play (overdub); stopped, the notes go on
 *                   the cursor's step and it moves on (step recording); held 1 s: CLEAR
 *   BARS 1..16      the loop's length (notes past it stay, and come back when it grows)
 *   QUANT           OFF, 1/4, 1/8, 1/16, 1/8T, 1/16T        SWING 50..75 % (the off-beats of 1/8 and 1/16)
 *   EDIT            left: UNDO the last pass recorded; right: ERASE on / off (hold keys: their notes go as the loop
 *                   passes them)
 *   RECORDING page  START (STEP: armed and stopped = step recording, PLAY starts the loop; NOTE: the first note played
 *                   starts the loop on bar 1, beat 1; COUNT: PLAY counts a bar in, then the loop records; FREE: on an
 *                   empty pattern a free take, the loop's length sets the tempo, else as NOTE), CLICK (OFF,
 *                   REC: while recording, ON: while playing; a count-in always clicks), its LEVEL, and UNDO / REDO
 *
 *   PATTERN page   SHIFT (every note a 16th later / earlier, around the loop), LENGTH (right x2: the loop twice as
 *                   long, its notes copied after themselves, or, with notes kept past its end, those back; left 1/2:
 *                   half as long, the rest kept as BARS keeps it), TRANSPOSE (every note a semitone up / down)
 *
 *   PATTERN K4      SECTION A..D: four patterns sharing the 448 notes (stopped: at once; playing: on the next bar; an
 *                   empty one starts as a copy of the one playing); each keeps its own BARS
 *   SONG page       LOOP / SONG; a chain of 8 steps, each a section and how many times it plays (x1..x4); in SONG,
 *                   PLAY plays the chain from its first step, round and round
 *
 * The sections: one array. The section playing is sq_ev[0 .. sq_n) (every reader as before), the others right after it
 * (sq_ev[sq_n .. sq_n + sq_pk), their own section in each event's rsv byte), REDO's stack at the array's top. Saved
 * together, the flash format as before (a pattern saved before reads as section A).
 *
 * Live recording takes back the time between a key and its sound (SQ_LAT_MS): what is played on the beat as heard
 * lands on the beat. An UNDO keeps what it took on a stack at the array's top (sq_un), for REDO; any new edit drops it.
 *
 * Recording armed while stopped is step recording: the notes go on the cursor's step (live_note_on). The page (ui.c) shows NOW / NEXT and the
 * bar's 16 cells. The notes play through notes.c (the
 * arpeggiator takes them when it runs) and are kept in two flash sectors written in turn (0x99000 / 0x9A000). */

#define SQ_PPQ 96u                                  /* ticks per beat */
#define SQ_MAXEV 448u
#define SQ_MAXACT 32u
/* SQ_A, SQ_B: the pattern's two sectors, per board (frodhes_store.h) */
#define SQ_MAGIC 0x46535131u                        /* "FSQ1" */
typedef struct { uint16_t tick, len; uint8_t note, vel, take, rsv; } sq_ev_t;   /* rsv: the section (bits 0-1), PART 2
                                                    * (SQ_P2: DUAL's second sound; patterns before: 0, PART 1) */
#define SQ_SECM 3u
#define SQ_P2 0x10u
typedef struct { uint32_t magic, seq, n, crc; } sq_hdr_t;
static sq_ev_t sq_ev[SQ_MAXEV];
static uint32_t sq_n, sq_flash_seq;
static uint8_t seq_play, seq_rec, sq_erase, sq_take;
static uint32_t sq_pos, sq_last_ms, sq_prev;        /* sq_pos: ticks in Q16 */
static uint32_t sq_k0;                              /* CLOCK EXT: the song position (tempo.c clk_sp) of tick 0 */
static uint8_t sq_ext, sq_jump, sq_src;
static uint32_t sq_jseen;                           /* tempo.c clk_jumps, as seq_poll saw it */
static uint16_t sq_L;                               /* (the loop length seq_poll last saw) */                     /* sq_ext 1: sq_pos follows the clock's pulses (sq_k0 set); sq_jump:
                                                     * Start / Continue's first position (only from there on) */
static struct { uint8_t note, part; uint16_t off; } sq_act[SQ_MAXACT];
static uint32_t sq_nact;
static uint16_t rec_start[128];
static uint8_t rec_vel[128], live_held[128];
static uint8_t sq_dirty, sq_roll_dirty, sq_btn_down;
static uint8_t sq_pmute;                                 /* DUAL: bit p, PART p+1 muted in the playback (not kept) */
static int32_t sq_cur = -1;                              /* the page's cursor, in 16ths (ui.c moves it) */
static uint32_t sq_cur_t;
static uint32_t st_keys, st_start, st_len, st_n;          /* step recording: the notes held, where, how long (16ths) */
static uint16_t st_idx[CH_MAX * 2u];
static uint32_t sq_change_t, sq_btn_t;
static const uint8_t SQ_GRID[6] = {0, 96, 48, 24, 32, 16};
static const char *const SQ_QUANT_NAME[6] = {"OFF", "1/4", "1/8", "1/16", "1/8T", "1/16T"};
#define SQ_BARS (set.seq_bars)
#define SQ_QUANT (set.seq_quant)
#define SQ_SWING (set.seq_swing)
#define SQ_START (set.seq_rsv[0])                   /* 0.9.7: 0 STEP, 1 NOTE, 2 COUNT, 3 FREE (records before: 0) */
#define SQ_CLICK (set.seq_rsv[1])                   /* 0.9.7: 0 OFF, 1 REC, 2 ON */
#define SQ_CLVL (set.seq_rsv[2])                    /* 0.9.7: the click's LEVEL 1..10 (0: 5) */
#define SQ_SEC (set.seq_rsv[3])                     /* 0.9.7: the section playing, 0..3 (A..D) */
#define SQ_SBARS(k) (set.seq_rsv[4u + (k)])         /* 0.9.7: each section's BARS (0: not set yet) */
#define SQ_SONG (set.seq_rsv[8])                    /* 0.9.7: 0 LOOP, 1 SONG */
#define SQ_CHAIN(i) (set.seq_rsv[9u + (i)])         /* 0.9.7: the song's 8 steps: section + 1 (0: the end) | (times - 1) << 4 */
#define SQ_NCHAIN 8u
static uint32_t sq_pk;                              /* the other sections' notes, right after the playing one's */
static uint8_t sq_sec_next = 0xFFu;                 /* a section asked for while playing: from the next bar */
static uint8_t sq_song_i, sq_song_pass;             /* SONG: the step playing, its passes so far */
static uint8_t sq_fresh;                            /* the first pass since PLAY (its start looks like a loop's end) */
#define SQ_LAT_MS 12u                               /* a key's sound comes ~12 ms after it (the scan, the audio's blocks:
                                                     * as SLOOP measured on the same platform): live notes are recorded
                                                     * that much earlier */
static uint32_t sq_un;                              /* REDO's stack: the last sq_un of sq_ev[] (top down), each UNDO's
                                                     * notes pushed together (one take) */
static uint8_t sq_count;                            /* COUNT: beats of the count-in still to come (0: none) */
static uint32_t sq_count_t;                         /* its next beat (fm1_ms) */
static uint8_t sq_free;                             /* FREE: a free take runs (from its first note, sq_free_t0) */
static uint32_t sq_free_t0;
#define SQ_FREE_MAX 30000u                          /* a free take closes by itself after 30 s */
static volatile uint8_t sq_click_req;               /* the audio's click (frodhes.c app_click): 1 a beat, 2 a bar's first */
static void ui_mark(uint32_t e);

static uint32_t sq_len(void) { return (uint32_t)SQ_BARS * 4u * SQ_PPQ; }
static uint32_t sq_tick(void) { return sq_pos >> 16; }
static void seq_sanitize(void)
{
    if (SQ_BARS < 1u || SQ_BARS > 16u) SQ_BARS = 4;
    if (SQ_QUANT > 5u) SQ_QUANT = 3;
    if (SQ_SWING < 50u || SQ_SWING > 75u) SQ_SWING = 50;
    if (SQ_START > 3u) SQ_START = 0;
    if (SQ_CLICK > 2u) SQ_CLICK = 0;
    if (SQ_CLVL > 10u) SQ_CLVL = 0;
    if (SQ_SEC > 3u) SQ_SEC = 0;
    if (SQ_SONG > 1u) SQ_SONG = 0;
    {
        uint32_t k;
        for (k = 0; k < 4u; k++) if (SQ_SBARS(k) > 16u) SQ_SBARS(k) = 0;
        for (k = 0; k < SQ_NCHAIN; k++) if ((SQ_CHAIN(k) & 15u) > 4u) SQ_CHAIN(k) = 0;
    }
}
/* the other sections' notes follow the playing one's: room for k more at sq_n (they move up), or the gap closed
 * after the playing one's shrank from n0 (they move down) */
/* (no memmove in the SDK's libc: an overlapping move of n events, by direction) */
static void sq_move(uint32_t to, uint32_t from, uint32_t n)
{
    uint32_t i;
    if (to > from) for (i = n; i-- > 0;) sq_ev[to + i] = sq_ev[from + i];
    else if (to < from) for (i = 0; i < n; i++) sq_ev[to + i] = sq_ev[from + i];
}
static void sq_grow(uint32_t k) { if (sq_pk) sq_move(sq_n + k, sq_n, sq_pk); }
static void sq_shrunk(uint32_t n0) { if (sq_pk && n0 != sq_n) sq_move(sq_n, n0, sq_pk); }
/* the events of section t first: [0, the count) of [0, tot) (in place, the order otherwise free) */
static uint32_t sq_partition(uint32_t tot, uint32_t t)
{
    uint32_t i = 0, j = tot;
    while (i < j) {
        if ((sq_ev[i].rsv & SQ_SECM) == t) { i++; continue; }
        j--;
        { sq_ev_t e = sq_ev[i]; sq_ev[i] = sq_ev[j]; sq_ev[j] = e; }
    }
    return i;
}
static uint8_t sq_sv_fail;                          /* the pattern's saves that failed since the last edit */
static void sq_changed(void) { sq_dirty = 1; sq_roll_dirty = 1; sq_change_t = fm1_ms; sq_sv_fail = 0; }
static void sq_edited(void) { sq_un = 0; sq_changed(); }   /* an edit: REDO's stack goes */

/* where a note plays: its tick on the QUANT grid (with SWING), inside the loop */
static uint32_t sq_at(const sq_ev_t *e)
{
    uint32_t g = SQ_GRID[SQ_QUANT], t = e->tick, i, L = sq_len();
    if (g) {
        i = (t + g / 2u) / g;
        t = i * g;
        if ((g == 48u || g == 24u) && (i & 1u)) t += 2u * g * SQ_SWING / 100u - g;
    }
    return t % L;
}
static int sq_in(uint32_t t, uint32_t p0, uint32_t p1)   /* t in (p0, p1], around the loop's end */
{
    return p0 <= p1 ? t > p0 && t <= p1 : t > p0 || t <= p1;
}

static void sq_all_off(void)
{
    uint32_t i;
    for (i = 0; i < sq_nact; i++) { nt_part = sq_act[i].part; out_off(sq_act[i].note); }
    sq_nact = 0;
}
static void sq_add(uint32_t tick, uint32_t len, uint32_t note, uint32_t vel)
{
    sq_ev_t *e;
    sq_un = 0;                                          /* (a new note: what UNDO took cannot come back) */
    if (sq_n + sq_pk >= SQ_MAXEV) return;
    sq_grow(1);
    e = &sq_ev[sq_n++];
    e->tick = (uint16_t)tick; e->len = (uint16_t)len; e->note = (uint8_t)note; e->vel = (uint8_t)(vel & 127u);
    e->take = sq_take; e->rsv = (uint8_t)(SQ_SEC | (vel >> 7 & 1u ? SQ_P2 : 0u));   /* vel bit 7: PART 2 (the part played,
                                                                                        * notes.c nt_part) */
    sq_changed();
}
static uint32_t sq_rec_tick(void)                       /* the loop's tick SQ_LAT_MS ago: where a live note was heard */
{
    uint32_t L = sq_len(), lat = SQ_LAT_MS * ((SQ_PPQ << 16) / (beat_ms ? beat_ms : 500u));
    return ((sq_pos + (L << 16) - lat) % (L << 16)) >> 16;
}
static void sq_rec_close(uint32_t n)                    /* a recorded note ends: it becomes an event */
{
    uint32_t L = sq_len(), t = sq_rec_tick(), len;
    if (rec_start[n] == 0xFFFFu) return;
    len = (t + L - rec_start[n]) % L;
    if (len < 6u) len = 6u;
    sq_add(rec_start[n], len, n, rec_vel[n]);
    rec_start[n] = 0xFFFFu;
}
static void sq_rec_close_all(void) { uint32_t n; for (n = 0; n < 128u; n++) sq_rec_close(n); }

static uint32_t sq_sec_set(uint32_t t);
static uint32_t sq_song_from(uint32_t k);
static void seq_start(void)
{
    sq_sec_next = 0xFFu;
    sq_fresh = 1;
    if (SQ_SONG && (sq_song_i = (uint8_t)sq_song_from(0)) != 0xFFu) {   /* SONG: from the chain's first step */
        sq_song_pass = 0;
        sq_sec_set((SQ_CHAIN(sq_song_i) & 15u) - 1u);
    }
    seq_play = 1;
    sq_pos = 0; sq_prev = sq_len() - 1u; sq_last_ms = fm1_ms;
    sq_ext = 0;                                         /* (a clock: taken up from here, seq_poll) */
    if (!clk_live()) beat_t0 = fm1_ms, beat_k0 = 0;                  /* the tempo's beat (arpeggiator, LED) from bar 1 (a clock's
                                                         * beats stay the clock's) */
    ui_mark(6);
}
/* Start / Continue from the clock (frodhes.c, CLOCK EXT): tick 0 is song position 0, so Start plays bar 1 on the
 * first pulse and Continue goes on at the song position (Stop stopped the count), the pattern looping */
static void seq_stop(void);
static void seq_start_ext(void)
{
    if (seq_play) seq_stop();                           /* (Start again while playing: from bar 1) */
    seq_start();
    sq_k0 = 0;
    sq_ext = 1;
    sq_jump = 1;
    sq_L = (uint16_t)sq_len(); sq_src = clk_stop;
    sq_pos = (clk_sp % (sq_len() / 4u)) << 18;           /* (Continue: there, should no clock come; Start: 0) */
    sq_prev = ((sq_pos >> 16) + sq_len() - 1u) % sq_len();
}
static void seq_stop(void)                              /* stop: back to the start (the cursor too: bar 1, step 1) */
{
    sq_rec_close_all();
    seq_play = 0;
    sq_all_off();
    sq_cur = 0; sq_cur_t = fm1_ms;
    ui_mark(6); ui_mark(7);
}
static int seq_counting(void) { return sq_count != 0u; }
static void seq_clear(void);

/* FREE: the free take closes at t_end (SEQ's press): its length T is the loop. The bars (1, 2, 4, 8) whose tempo
 * (B bars in T) is nearest the tempo set, within 40..240 BPM; within 3 % the tempo set stays. The notes were kept in
 * ms from the take's first (tick / len); now in the loop's ticks. The loop plays at once, REC off */
static void sq_free_close(uint32_t t_end)
{
    static const uint8_t BB[4] = {1, 2, 4, 8};
    uint32_t T = t_end - sq_free_t0, n, i, best = 0, bd = 0xFFFFFFFFu, b10 = set.bpm10, L;
    sq_free = 0;
    if (T < 400u) { seq_clear(); return; }              /* (a slip: nothing kept) */
    for (n = 0; n < 128u; n++)                          /* the notes still held end with the take */
        if (rec_start[n] != 0xFFFFu) { uint32_t l = T - rec_start[n]; sq_add(rec_start[n], l < 6u ? 6u : l, n, rec_vel[n]); rec_start[n] = 0xFFFFu; }
    for (i = 0; i < 4u; i++) {
        uint32_t bpm = BB[i] * 4u * 600000u / T, dd = bpm > b10 ? bpm - b10 : b10 - bpm;
        if (bpm < 400u || bpm > 2400u) continue;
        if (dd < bd) { bd = dd; best = i; }
    }
    if (bd == 0xFFFFFFFFu) best = T > 4000u ? 3u : 0u;  /* (no tempo in range: the longest or the shortest) */
    {
        uint32_t bpm = BB[best] * 4u * 600000u / T;
        if (bpm < 400u) bpm = 400u;
        if (bpm > 2400u) bpm = 2400u;
        if (bd * 100u > b10 * 3u) tempo_set10(bpm);    /* (within 3 %: the tempo set stays) */
    }
    SQ_BARS = BB[best];
    L = sq_len();
    for (i = 0; i < sq_n; i++) {                        /* ms -> ticks (ms < 2^15, L <= 3072: 32-bit) */
        uint32_t t = sq_ev[i].tick * L / T, l = sq_ev[i].len * L / T;
        sq_ev[i].tick = (uint16_t)(t < L ? t : L - 1u);
        sq_ev[i].len = (uint16_t)(l < 6u ? 6u : l);
    }
    sq_changed();
    seq_rec = 0;
    seq_start();
    {   /* in phase with the press (the "1"), not the release: the loop already that far on */
        uint32_t late = fm1_ms - t_end;
        if (late < 1000u) sq_pos = late * ((SQ_PPQ << 16) / (beat_ms ? beat_ms : 500u));
    }
    ui_mark(10);
}
static void seq_toggle_play(void)
{
    if (sq_count) { sq_count = 0; ui_mark(6); return; }  /* PLAY during the count-in: called off */
    if (sq_free) { sq_free = 0; seq_clear(); ui_mark(6); return; }   /* PLAY during a free take: dropped */
    if (seq_play) { seq_stop(); return; }
    if (seq_rec && SQ_START == 2u && !clk_live()) {     /* COUNT: a bar of clicks first (an external clock: at once) */
        sq_count = 5; sq_count_t = fm1_ms; ui_mark(6);
        return;
    }
    seq_start();
}
static void seq_toggle_rec(void)
{
    if (sq_free) {                                      /* SEQ closes the free take: at its press (a release comes later) */
        sq_free_close(fm1_ms - sq_btn_t < 1000u ? sq_btn_t : fm1_ms);
        return;
    }
    if (seq_rec) { sq_rec_close_all(); seq_rec = 0; sq_count = 0; }
    else { seq_rec = 1; sq_erase = 0; sq_take++; }
    ui_mark(6); ui_mark(10);
}
static void seq_clear(void)
{
    uint32_t n;
    sq_all_off();
    for (n = 0; n < 128u; n++) rec_start[n] = 0xFFFFu;
    n = sq_n; sq_n = 0; sq_shrunk(n);                   /* (the section playing only: the others stay) */
    sq_free = 0;
    sq_edited();
}
static uint32_t seq_undo(void)                          /* the last pass recorded goes: how many notes */
{
    uint32_t i, j, got = 0, best = 256u;
    uint8_t hi = 0;
    if (!sq_n) return 0;
    for (i = 0; i < sq_n; i++) {                        /* the most recent pass: the take closest behind sq_take */
        uint32_t d = (uint8_t)(sq_take - sq_ev[i].take);
        if (d < best) { best = d; hi = sq_ev[i].take; }
    }
    for (i = sq_n; i-- > 0;) {                          /* its notes onto REDO's stack (the array's top, down): each
                                                         * taken out first (the rest moves down one), then pushed into
                                                         * the place that frees, so a full array loses nothing */
        sq_ev_t e = sq_ev[i];
        if (e.take != hi) continue;
        for (j = i; j + 1u < sq_n; j++) sq_ev[j] = sq_ev[j + 1u];
        sq_n--; got++;
        sq_shrunk(sq_n + 1u);                           /* (the other sections down one first: the stack's place) */
        sq_un++; sq_ev[SQ_MAXEV - sq_un] = e;
    }
    sq_all_off();
    sq_changed();
    return got;
}
static uint32_t seq_redo(void)                          /* the last UNDO's notes come back: how many */
{
    uint32_t got = 0;
    uint8_t tk;
    if (!sq_un) return 0;
    tk = sq_ev[SQ_MAXEV - sq_un].take;                  /* the last pushed: the lowest of the stack */
    while (sq_un && sq_ev[SQ_MAXEV - sq_un].take == tk) {
        sq_ev_t e = sq_ev[SQ_MAXEV - sq_un];
        sq_un--;
        sq_grow(1);                                     /* (into the slot the stack just freed, at most) */
        sq_ev[sq_n++] = e; got++;
    }
    sq_all_off();
    sq_changed();
    return got;
}

/* ---- the sections: t becomes the one playing (main loop). An empty one starts as a copy of the one playing (when the
 * 448 have room): 1 = copied. The BARS go with each section. REDO's stack goes */
static uint32_t sq_sec_set(uint32_t t)
{
    uint32_t i, tot = sq_n + sq_pk, c = 0, cp = 0;
    if (t > 3u || t == SQ_SEC) return 0;
    sq_rec_close_all();
    sq_all_off();
    sq_un = 0;
    for (i = 0; i < sq_n; i++) sq_ev[i].rsv = (uint8_t)((sq_ev[i].rsv & SQ_P2) | SQ_SEC);
    SQ_SBARS(SQ_SEC) = SQ_BARS;
    for (i = sq_n; i < tot; i++) if ((sq_ev[i].rsv & SQ_SECM) == t) c++;
    if (!c && sq_n && tot + sq_n <= SQ_MAXEV) {          /* empty: a copy of the one playing */
        for (i = 0; i < sq_n; i++) { sq_ev[tot + i] = sq_ev[i]; sq_ev[tot + i].rsv = (uint8_t)((sq_ev[i].rsv & SQ_P2) | t); }
        tot += sq_n; cp = 1;
        SQ_SBARS(t) = SQ_BARS;
    }
    sq_n = sq_partition(tot, t);
    sq_pk = tot - sq_n;
    SQ_SEC = (uint8_t)t;
    if (SQ_SBARS(t)) SQ_BARS = SQ_SBARS(t);
    sq_changed();
    ui_mark(6); ui_mark(7);
    return cp;
}
static uint32_t sq_sec_notes(uint32_t t)               /* a section's notes */
{
    uint32_t i, c = 0;
    if (t == SQ_SEC) return sq_n;
    for (i = sq_n; i < sq_n + sq_pk; i++) if ((sq_ev[i].rsv & SQ_SECM) == t) c++;
    return c;
}
/* SONG: the chain's step k (from k on, the first with a section; none: 0xFF) */
static uint32_t sq_song_from(uint32_t k)
{
    uint32_t i;
    for (i = 0; i < SQ_NCHAIN; i++) { uint32_t j = (k + i) % SQ_NCHAIN; if (SQ_CHAIN(j) & 15u) return j; }
    return 0xFFu;
}

/* ---- the PATTERN page: the whole pattern at once (main loop; the loop never stops). The cards show how far it moved
 * since the FM-1 started (not saved) */
static int32_t sq_shift_n, sq_trans_n;
static void seq_shift(int32_t d)                        /* every note a 16th (24 ticks) later (d > 0) or earlier */
{
    uint32_t L = sq_len(), i;
    for (i = 0; i < sq_n; i++)
        if (sq_ev[i].tick < L) sq_ev[i].tick = (uint16_t)((sq_ev[i].tick + L + (d > 0 ? 24u : L - 24u)) % L);
    sq_shift_n += d > 0 ? 1 : -1;
    sq_all_off();
    sq_edited();
}
static uint32_t seq_transpose(int32_t d)                /* every note a semitone up / down: how many could not move */
{
    uint32_t i, stuck = 0;
    for (i = 0; i < sq_n; i++) {
        int32_t n = (int32_t)sq_ev[i].note + (d > 0 ? 1 : -1);
        if (n < 0 || n > 127) { stuck++; continue; }
        sq_ev[i].note = (uint8_t)n;
    }
    sq_trans_n += d > 0 ? 1 : -1;
    sq_all_off();
    sq_edited();
    return stuck;
}
/* LENGTH: d > 0 doubles the loop (up to 16 bars): notes kept past its end (a shorter BARS hid them) come back as they
 * were, else every note is copied a loop later (those that do not fit the 448 stay out: the count); d < 0 halves it
 * (from 1 bar: nothing), the notes past the new end kept. Returns the notes that did not fit */
static uint32_t seq_length(int32_t d)
{
    uint32_t L = sq_len(), i, n0 = sq_n, past = 0, lost = 0;
    if (d < 0) {
        if (SQ_BARS < 2u) return 0;
        SQ_BARS = (uint8_t)(SQ_BARS / 2u);
    } else {
        if (SQ_BARS > 8u) return 0;
        for (i = 0; i < sq_n; i++) if (sq_ev[i].tick >= L) past++;
        SQ_BARS = (uint8_t)(SQ_BARS * 2u);
        if (!past)
            for (i = 0; i < n0; i++) {
                if (sq_n + sq_pk >= SQ_MAXEV) { lost++; continue; }
                sq_grow(1);
                sq_ev[sq_n] = sq_ev[i];
                sq_ev[sq_n].tick = (uint16_t)(sq_ev[i].tick + L);
                sq_n++;
            }
    }
    sq_all_off();
    sq_edited();
    return lost;
}

/* step recording (armed and stopped): the notes go on the cursor's step, as long as the QUANT grid (stretched by
 * ALGORITHM while they are held); let go, the cursor moves on by that much */
static uint32_t sq_grid16(void) { static const uint8_t G[6] = {1, 4, 2, 1, 1, 1}; return G[SQ_QUANT]; }
static uint32_t sq_steps(void) { return (uint32_t)SQ_BARS * 16u; }
static void st_note_on(uint32_t n, uint32_t vel)
{
    if (!st_keys) {                                     /* a new step: its own take (UNDO takes it back alone) */
        st_start = sq_cur >= 0 && (uint32_t)sq_cur < sq_steps() ? (uint32_t)sq_cur : 0u;
        st_len = sq_grid16(); st_n = 0; sq_take++;
    }
    st_keys++;
    if (sq_n < SQ_MAXEV && st_n < CH_MAX * 2u) { st_idx[st_n++] = (uint16_t)sq_n; sq_add(st_start * 24u, st_len * 24u - 2u, n, vel | (uint32_t)nt_part << 7); }
    sq_cur = (int32_t)st_start; sq_cur_t = fm1_ms;
}
static void st_note_off(void)
{
    if (!st_keys || --st_keys) return;
    sq_cur = (int32_t)((st_start + st_len) % sq_steps()); sq_cur_t = fm1_ms;
    sq_changed();
}
static int seq_step_held(void) { return seq_rec && !seq_play && st_keys != 0u; }
static void seq_step_stretch(int32_t d)                 /* ALGORITHM while the step's keys are held */
{
    uint32_t i;
    int32_t l = (int32_t)st_len + d;
    st_len = (uint32_t)(l < 1 ? 1 : l > (int32_t)sq_steps() ? (int32_t)sq_steps() : l);
    for (i = 0; i < st_n; i++) if (st_idx[i] < sq_n) sq_ev[st_idx[i]].len = (uint16_t)(st_len * 24u - 2u);
    sq_changed();
}

/* live notes (keys, USB-MIDI, chord mode's notes; not the sequencer's own): notes.c calls these */
static void live_note_on(uint32_t n, uint32_t vel)
{
    if (n > 127u) return;
    live_held[n]++;
    if (!seq_rec || sq_erase) return;
    if (!seq_play) {
        if (SQ_START == 0u) { st_note_on(n, vel); return; }   /* STEP, stopped: step recording */
        if (SQ_START == 2u || clk_live()) return;       /* COUNT: only PLAY starts it (a count-in: the notes only
                                                         * sound); an external clock starts it */
        if (SQ_START == 3u && (sq_free || !sq_n)) {     /* FREE on an empty pattern: the free take, in ms */
            if (!sq_free) { sq_free = 1; sq_free_t0 = fm1_ms; sq_take++; ui_mark(6); }
            if (rec_start[n] == 0xFFFFu && fm1_ms - sq_free_t0 < 32768u) { rec_start[n] = (uint16_t)(fm1_ms - sq_free_t0); rec_vel[n] = (uint8_t)(vel | (uint32_t)nt_part << 7); }
            return;
        }
        seq_start();                                    /* NOTE: this note starts the loop, on bar 1 beat 1 */
        rec_start[n] = 0; rec_vel[n] = (uint8_t)(vel | (uint32_t)nt_part << 7);
        return;
    }
    if (rec_start[n] == 0xFFFFu) { rec_start[n] = (uint16_t)sq_rec_tick(); rec_vel[n] = (uint8_t)(vel | (uint32_t)nt_part << 7); }
}
static void live_note_off(uint32_t n)
{
    if (n > 127u) return;
    if (live_held[n]) live_held[n]--;
    if (sq_free) {                                      /* the free take: the note's length in ms */
        if (!live_held[n] && rec_start[n] != 0xFFFFu) {
            uint32_t l = fm1_ms - sq_free_t0 - rec_start[n];
            sq_add(rec_start[n], l < 6u ? 6u : l > 32767u ? 32767u : l, n, rec_vel[n]);
            rec_start[n] = 0xFFFFu;
        }
        return;
    }
    if (seq_rec && !seq_play && !sq_erase && SQ_START == 0u) { st_note_off(); return; }
    if (!live_held[n]) sq_rec_close(n);
}

static void seq_poll(void)
{
    uint32_t L = sq_len(), dt, p0, p1, i;
    int noon = 0, jump = 0;
    if (sq_free && fm1_ms - sq_free_t0 >= SQ_FREE_MAX) sq_free_close(fm1_ms);   /* (30 s: it closes itself) */
    if (sq_count) {                                     /* the count-in: four clicks a beat apart (the first accented),
                                                         * the loop on the fifth beat (sq_count 5 .. 2 click, 1 starts) */
        if ((int32_t)(fm1_ms - sq_count_t) < 0) return;
        if (sq_count == 1u) { sq_count = 0; seq_start(); return; }
        sq_click_req = (uint8_t)(sq_count == 5u ? 2u : 1u);
        sq_count_t += beat_ms ? beat_ms : 500u;
        sq_count--;
        ui_mark(6);
        return;
    }
    if (!seq_play) return;
    dt = fm1_ms - sq_last_ms;
    sq_last_ms = fm1_ms;
    if (dt > 1000u) dt = 1000u;                         /* (32-bit: no 64-bit division on this chip) */
    while (sq_pos >= L << 16) sq_pos -= L << 16;
    if (clk_live()) {                                   /* CLOCK EXT, pulses coming: 4 ticks a pulse, from the pulses
                                                         * (the song position after Start / Continue, else the clock's
                                                         * own count) */
        uint32_t lp = L / 4u, at, d, c = clk_stop ? clk_k : clk_sp;
        if (sq_src != clk_stop) { sq_src = clk_stop; sq_ext = 0; }
        if (L != sq_L && sq_ext && sq_L) sq_k0 = c - 1u - (c - 1u - sq_k0) % (sq_L / 4u);   /* BARS changed: on from
                                                         * the same place */
        sq_L = (uint16_t)L;
        if (!sq_ext) { sq_k0 = c - 1u - sq_tick() / 4u; sq_ext = 1; jump = 1; }   /* (taken up where it is) */
        if (clk_jumps != sq_jseen) { sq_jseen = clk_jumps; jump = 1; }   /* (Start, Continue, Song Position, pulses
                                                         * missing: the count jumped) */
        if (clk_wait || c - sq_k0 == 0u || (int32_t)(c - sq_k0) < 0) return;   /* Start / Continue: wait for the
                                                         * first pulse */
        at = (((c - 1u - sq_k0) % lp) << 16 | clk_frac16()) * 4u;      /* Q16 ticks, within the loop */
        d = (at - sq_pos + (L << 16)) % (L << 16);
        if (sq_jump) { sq_jump = 0; jump = 0; }         /* (Start / Continue:
                                                         * from its own place, seq_start_ext's sq_pos / sq_prev; a main
                                                         * loop held after it plays what it missed, late, as any lag) */
        if (d > ((L - 16u) << 16)) return;              /* never backwards (the estimate between pulses: up to 4
                                                         * pulses behind; anything else is a jump forward) */
        noon = (jump && d > (16u << 16)) || d > (96u << 16);   /* the count jumped by over 4 pulses, or anything over
                                                         * a beat (the main loop held up that long): the notes in
                                                         * between are not all fired together, only their ends; a
                                                         * shorter lag plays them late */
        sq_pos = at;
    } else {
        if (!set.clock || clk_stop) sq_ext = 0;         /* (a gap in a running clock keeps the song's place) */
        sq_L = (uint16_t)L;
        sq_jump = 0;
        if (sq_ext && fm1_ms - clk_last_ms < 2000u) return;   /* a gap in a running clock: held (tempo.c counts its
                                                         * pulses when it comes back) */
        sq_pos += dt * ((SQ_PPQ << 16) / (beat_ms ? beat_ms : 500u));
        while (sq_pos >= L << 16) sq_pos -= L << 16;
    }
    p0 = sq_prev % L; p1 = sq_tick();
    if (p0 == p1) return;
    {   /* a section change: one asked for (from the next bar), or the SONG's next step (at the loop's end): the new
         * section from its start, as far into it as this pass went past that line */
        uint32_t to = 0xFFu, at = 0, b;
        if (sq_sec_next != 0xFFu)
            for (b = 0; b < L; b += 4u * SQ_PPQ) if (sq_in(b, p0, p1)) { to = sq_sec_next; at = b; break; }
        if (SQ_SONG && sq_song_i != 0xFFu && p1 < p0 && !sq_fresh && sq_sec_next == 0xFFu) {   /* the loop came round */
            uint32_t times = (SQ_CHAIN(sq_song_i) >> 4 & 3u) + 1u;
            if (++sq_song_pass >= times) {
                sq_song_pass = 0;
                sq_song_i = (uint8_t)sq_song_from((sq_song_i + 1u) % SQ_NCHAIN);
                if (sq_song_i != 0xFFu) { to = (SQ_CHAIN(sq_song_i) & 15u) - 1u; at = 0; }
            }
        }
        sq_fresh = 0;
        if (to != 0xFFu) {
            uint32_t past = (p1 + L - at) % L;
            sq_sec_next = 0xFFu;
            if (to != SQ_SEC) {
                sq_sec_set(to);
                L = sq_len();
                if (past >= L) past = 0;
                sq_pos = past << 16;
                sq_ext = 0;                             /* (a clock: taken up from here) */
                p0 = L - 1u; p1 = past;
            }
        }
    }
    if (SQ_CLICK == 2u || (SQ_CLICK == 1u && seq_rec)) {   /* the click: a beat passed (the bar's first accented) */
        uint32_t b;
        for (b = 0; b < L; b += SQ_PPQ) if (sq_in(b, p0, p1)) sq_click_req = (uint8_t)(b % (4u * SQ_PPQ) ? 1u : 2u);
    }
    for (i = 0; i < sq_nact;) {                         /* note offs first: a repeated note restarts cleanly */
        if (sq_in(sq_act[i].off, p0, p1)) { nt_part = sq_act[i].part; out_off(sq_act[i].note); sq_act[i] = sq_act[--sq_nact]; }
        else i++;
    }
    for (i = 0; i < sq_n;) {
        sq_ev_t *e = &sq_ev[i];
        uint32_t t;
        if (e->tick >= L) { i++; continue; }            /* past the loop (BARS made shorter): kept, silent */
        t = sq_at(e);
        if (!noon && sq_in(t, p0, p1)) {
            if (sq_erase && live_held[e->note]) { sq_ev[i] = sq_ev[--sq_n]; sq_shrunk(sq_n + 1u); sq_edited(); continue; }
            if (sq_nact < SQ_MAXACT) {
                nt_part = (uint8_t)((e->rsv & SQ_P2) != 0u);   /* (DUAL: its part's sound; else one sound) */
                if (sq_pmute >> nt_part & 1u) { i++; continue; }   /* a part muted */
                out_on(e->note, e->vel, 1);
                sq_act[sq_nact].note = e->note; sq_act[sq_nact].part = nt_part;
                sq_act[sq_nact].off = (uint16_t)((t + (e->len < L ? e->len : L - 1u)) % L);
                if (sq_in(sq_act[sq_nact].off, t, p1)) sq_act[sq_nact].off = (uint16_t)((p1 + 1u) % L);   /* (a late pass
                                                         * over its end too: it ends with the next, not a loop later) */
                sq_nact++;
            }
        }
        i++;
    }
    if (p1 < p0 && seq_rec) sq_take++;                  /* a new pass: UNDO takes it away on its own */
    sq_prev = p1;
}

/* ---- the pattern in flash (only when stopped and quiet: an erase stalls the audio) */
#ifdef DINGHY_HOST
static uint32_t crc32(const uint8_t *p, uint32_t n);   /* presets.c (host) */
static uint32_t sq_host_reads;
static int sq_fl_read(uint32_t at, void *p, uint32_t n)   /* at: a sector and an offset in it (sq_valid reads the
                                                              * events after the header) */
{
    sq_host_reads++;                                    /* (tests: no reads at save time) */
    if ((at & 0xFFFu) + n > 4096u) return -1;
    return st_read(at, p, n);                           /* (tests/host.c's flash) */
}
static int sq_fl_erase(uint32_t at)
{
    if (host_fl_check("erase pattern", at, 0x1000u)) return -1;   /* (the firmware's allow-list, tests/host.c) */
    if (getenv("FRODHES_SQ_FAIL")) { printf("%6u ms  pattern erase failed\n", fm1_ms); return -1; }   /* (tests) */
    printf("%6u ms  pattern %c erased\n", fm1_ms, at == SQ_B && SQ_B != SQ_A ? 'B' : 'A');   /* (tests: when it saves) */
    memset(host_mem + at, 0xFF, 4096);
    return 0;
}
static int sq_fl_write(uint32_t at, uint32_t off, const void *p, uint32_t n)
{
    if (host_fl_check("write pattern", at + off, n)) return -1;
    memcpy(host_mem + at + off, p, n);
    return 0;
}
#else
static int sq_fl_read(uint32_t at, void *p, uint32_t n) { return flash_ok ? st_read(at, p, n) : -1; }
#endif
/* the pattern's save beside the main loop (0.9.5, as the settings' since pc-stall): one erase or page program
 * started per main-loop pass and run in slices (fm1_flash.h fl_op_start / fl_op_poll), not ~100 ms in one call:
 * a MIDI note or a clock's Start 1.5 s after a pattern edit came that late (the FM-1, hil_clock's sequencer take: its
 * Start right after the step recording). 1: started / running, 0: done, < 0: failed */
#ifdef DINGHY_HOST
static uint32_t sq_op_t, sq_op_ms;                      /* (the host: the part's time, the main loop going on: an
                                                         * erase HOST_SAVE_MS, a page 1 ms, as tests/host.c models it) */
static int sq_op_start(uint32_t at, uint32_t off, const void *p, uint32_t n)
{
    int rc = p ? sq_fl_write(at, off, p, n) : sq_fl_erase(at);
    sq_op_t = fm1_ms; sq_op_ms = p ? 1u : HOST_SAVE_MS;
    return rc < 0 ? rc : 1;
}
static uint8_t sq_sync;                                 /* (the migration's save at power-on: no time goes by) */
static int sq_op_poll(void) { return !sq_sync && fm1_ms - sq_op_t < sq_op_ms ? 1 : 0; }
#define SQ_SYNC(on) (sq_sync = (on))
#else
#define SQ_SYNC(on) ((void)0)
static int sq_op_start(uint32_t at, uint32_t off, const void *p, uint32_t n)
{
    int rc = flash_ok ? fl_op_start(at + off, (const uint8_t *)p, p ? n : 0u) : -1;
    return rc < 0 ? rc : 1;
}
static int sq_op_poll(void) { return fl_op_poll(); }
#endif
static int sq_valid(uint32_t at, sq_hdr_t *h)
{
    if (sq_fl_read(at, h, sizeof *h) || h->magic != SQ_MAGIC || h->n > SQ_MAXEV) return 0;
    if (sq_fl_read(at + sizeof *h, sq_ev, h->n * sizeof(sq_ev_t))) return 0;
    return crc32((const uint8_t *)sq_ev, h->n * sizeof(sq_ev_t)) == h->crc;
}
static uint32_t sq_sv_tgt = SQ_A;                     /* the sector the next save writes: never the one that holds the
                                                         * last good pattern (no flash reads at save time) */
static uint8_t st_new, st_old_ok, sq_old;               /* the store's record found; the places before 0.9.6 ours (SMK:
                                                         * the owner tag); the pattern came from there */
static uint8_t st_mig;                                  /* what to take from the places before 0.9.6 (frodhes.c
                                                         * st_old_scan): 1 the settings, 2 the presets, 4 the pattern */
static uint32_t st_oid[3];                              /* the ids of what is there now (0: nothing) */
/* the newest valid pattern of a / b (equal seq: b, as the load picks); 0: none */
static uint32_t sq_newest(uint32_t pa, uint32_t pb, sq_hdr_t *h)
{
    sq_hdr_t a, b;
    int va = sq_valid(pa, &a), vb = sq_valid(pb, &b);
    if (!va && !vb) return 0;
    if (va && (!vb || (int32_t)(a.seq - b.seq) > 0)) { *h = a; return pa; }
    *h = b;
    return pb;
}
static uint32_t sq_old_id(void)                         /* the pattern at the place before 0.9.6: its id (0: none) */
{
    sq_hdr_t h;
    return sq_newest(OLD_SQ_A, OLD_SQ_B, &h) ? st_id(h.crc ^ h.n, h.seq) : 0u;
}
static void seq_load(void)                              /* the pattern: its own, or the one at the place before 0.9.6
                                                         * when the migration takes it (st_mig 4) */
{
    sq_hdr_t h, o;
    uint32_t n, q = 0, at = sq_newest(SQ_A, SQ_B, &h), own = at != 0u;
    for (n = 0; n < 128u; n++) rec_start[n] = 0xFFFFu;
    sq_n = 0; sq_un = 0; sq_pk = 0;
    if (own) { q = h.seq; sq_sv_tgt = at == SQ_A ? SQ_B : SQ_A; }   /* (the next save: the other sector than its own
                                                         * newest; none: A) */
    sq_old = 0;
    if ((st_mig & 4u) && (n = sq_newest(OLD_SQ_A, OLD_SQ_B, &o)) != 0u) { at = n; sq_old = 1; }
    if (!at) return;
    if (!sq_valid(at, &h)) { sq_n = 0; sq_pk = 0; return; }   /* (re-reads it into sq_ev) */
    sq_n = sq_partition(h.n, SQ_SEC);                   /* the section playing first, the others after it */
    sq_pk = h.n - sq_n;
    sq_flash_seq = own && (int32_t)(q - h.seq) > 0 ? q : h.seq;   /* (a pattern taken over its own: the next save
                                                         * newer than both) */
    for (n = 0; n < sq_n + sq_pk; n++) if (sq_ev[n].take > sq_take) sq_take = sq_ev[n].take;
}
static int set_save_busy(void);                         /* frodhes.c: the settings' auto save runs */
static sq_hdr_t sq_sv_h;                                /* the save's header (written last: a cut save stays invalid) */
static uint32_t sq_sv_at, sq_sv_off, sq_sv_end;
static uint8_t sq_sv_step;                              /* 0 none, 1 erasing, 2 the events, 3 the header */
static int seq_save_busy(void) { return sq_sv_step != 0; }   /* (frodhes.c settings_poll: not while it runs) */
static void sq_sv_failed(void)                         /* a step failed: again 1.5 s later, at most 3 times an edit */
{
    sq_sv_step = 0;
    if (++sq_sv_fail < 3u) { sq_dirty = 1; sq_change_t = fm1_ms; }
}
static void sq_sv_begin(void);
static void seq_save_poll(void)
{
    if (sq_sv_step) {                                   /* a save runs: its next step when the last one is done */
        int rc = sq_op_poll();
        if (rc == 1) return;
        if (rc < 0) { sq_sv_failed(); return; }
        if (sq_sv_step == 3u) {                         /* done: the next save the other sector, unless an edit came
                                                         * meanwhile (this one may be invalid: the other keeps the last) */
            sq_sv_step = 0;
            if (!sq_dirty) sq_sv_tgt = sq_sv_at == SQ_A ? SQ_B : SQ_A;
            return;
        }
        if (sq_sv_step == 1u && sq_sv_off >= sq_sv_end) sq_sv_step = 2u;
        if (sq_sv_off < sq_sv_end) {                    /* the events, a page at a time */
            uint32_t a = sq_sv_at + sq_sv_off, chunk = 256u - (a & 255u);
            if (chunk > sq_sv_end - sq_sv_off) chunk = sq_sv_end - sq_sv_off;
            rc = sq_op_start(sq_sv_at, sq_sv_off, (const uint8_t *)sq_ev + (sq_sv_off - sizeof(sq_hdr_t)), chunk);
            sq_sv_off += chunk;
            sq_sv_step = 2u;
        } else {
            rc = sq_op_start(sq_sv_at, 0, &sq_sv_h, sizeof sq_sv_h);
            sq_sv_step = 3u;
        }
        if (rc < 0) sq_sv_failed();
        return;
    }
    if (!sq_dirty || seq_play || fm1_ms - sq_change_t < 1500u || !eng_idle() || !fx_quiet() || !tempo_flash_ok() ||
        set_save_busy()) return;                        /* (one auto save at a time: they share the flash's one
                                                         * operation, and neither waits for the other in the loop) */
    sq_sv_begin();
}
static void sq_sv_begin(void)                           /* a save starts (seq_save_poll runs it; the migration too) */
{
    uint32_t at;
    sq_dirty = 0;
    at = sq_sv_tgt;
    {   /* every section (the playing one's events marked as its: they were recorded as it, a pattern from before is A) */
        uint32_t i;
        for (i = 0; i < sq_n; i++) sq_ev[i].rsv = (uint8_t)((sq_ev[i].rsv & SQ_P2) | SQ_SEC);
    }
    sq_sv_h.magic = SQ_MAGIC; sq_sv_h.seq = ++sq_flash_seq; sq_sv_h.n = sq_n + sq_pk;
    sq_sv_h.crc = crc32((const uint8_t *)sq_ev, (sq_n + sq_pk) * sizeof(sq_ev_t));   /* (an edit while it writes: the crc
                                                         * fails, the load takes the other sector, a new save follows) */
    sq_sv_at = at; sq_sv_off = sizeof(sq_hdr_t); sq_sv_end = sizeof(sq_hdr_t) + (sq_n + sq_pk) * sizeof(sq_ev_t);
    sq_sv_step = 1;
    if (sq_op_start(at, 0, 0, 0) < 0) sq_sv_failed();   /* the erase first */
}
static int seq_save_now(void)                           /* the migration's: the pattern to its place, holding the loop;
                                                         * 0 = written */
{
    uint8_t f = sq_sv_fail;
    sq_sv_begin();
    SQ_SYNC(1);
    while (sq_sv_step) seq_save_poll();
    SQ_SYNC(0);
    return sq_sv_fail != f ? -1 : 0;
}
