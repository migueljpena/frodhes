/* SPDX-License-Identifier: GPL-3.0-only */
/* The engines' controls on the panel (ui.c): one table per engine. The UI builds the VOICE pages from it (four
 * controls a page, in the table's order: VOICE 1/n .. n/n, SELECT walks them), the FRODHES page's K2 / K3 (the two
 * headline controls) and the PERFORM / ASSIGN names of the engine's parameters. A new engine adds its table here and
 * its row in ENG_UI, its TYPEs in TYPE_UI; the page code is not touched.
 *
 * A control: the slot (a parameter id < P_COUNT, or a ui.c slot such as S_TUNE), its name on the row, its kind, its
 * unit, the headline flag and, for named detents, the names and where they sit. The range and the number's format are
 * the parameter's own (PARAMS[id].hi / .fmt, or the slot's in ui.c slot_val / slot_text), so the table cannot disagree
 * with the CC map, the records or the presets. Included by ui.c after its S_* slot ids. */

enum { EK_AMT, EK_CHOICE, EK_DETENT };      /* an amount (a fill and a number), a choice (its name,
                                             * large), named detents (a fill and the detent's name, the number
                                             * between two of them) */
enum { EU_NONE, EU_HZ, EU_MS, EU_DB, EU_C };   /* the unit, small next to the number: Hz, ms, dB, c (cents) */
typedef struct {
    uint8_t id;                              /* the slot */
    const char *name;                        /* the row's label (short: the value is right of it) */
    uint8_t kind, unit;                      /* EK_*, EU_* */
    uint8_t head;                            /* 1 = the FRODHES page's K2, 2 = its K3 (0: not a headline) */
    uint8_t nn, tol;                         /* EK_DETENT: the names; how far from a detent its name still shows */
    const char *const *names;                /* EK_DETENT: one name per detent */
    const uint8_t *at;                       /* EK_DETENT: each name's value */
} ectl_t;
typedef struct {
    const ectl_t *c;
    uint8_t n;                               /* controls: VOICE pages = (n + 3) / 4 */
    uint8_t cmp_mkup;                        /* the compressor's make-up when the panel switches it on with none set
                                             * (-20 dB 4:1): level-neutral for this engine on the FM-1 (Rhodes 15 =
                                             * +4.3 dB) */
    const char *const *hint;                 /* the footer's hint, one per VOICE page */
} engui_t;

/* the Rhodes (TYPE 0..3): the tine's voicing, then how notes die away */
static const ectl_t ENG_RHODES_C[] = {
    {P_TONE, "TONE", EK_AMT, EU_NONE, 1},    /* FRODHES K2 (PLAN rev. 12: TONE + BELL; DRIVE / REVERB have pages) */
    {P_BARK, "BARK", EK_AMT, EU_NONE},
    {P_BELL, "BELL", EK_AMT, EU_NONE, 2},    /* FRODHES K3 */
    {P_HAMMER, "HAMMER", EK_AMT, EU_NONE},
    {P_DECAY, "DECAY", EK_AMT, EU_NONE},
    {P_RELEASE, "RELEASE", EK_AMT, EU_NONE},
    {P_NOISE, "NOISE", EK_AMT, EU_NONE},
    {S_TUNE, "TUNE", EK_AMT, EU_C},
};
static const char *const ENG_RHODES_HINT[] = {"HOW THE TINE IS STRUCK", "HOW NOTES DIE AWAY"};

/* the 200A (TYPE 4, the reed engine, reed.c): the Rhodes' cards, what they do on a reed (TONE the pickup plates'
 * shape, BARK the reed's swing, BELL its second mode / the ping, HAMMER the felt), then its vibrato on the P_MOT_*
 * parameters (Frodhes' VIBES motor's: VIBRATO on / off, DEPTH, SPEED, SHAPE: 0 the 200A's own cycle, 127 a smooth sine), and REG,
 * the keys' regulation (the settings' set.reg, as TUNE: the instrument's) */
static const ectl_t ENG_REED_C[] = {
    {P_TONE, "TONE", EK_AMT, EU_NONE},
    {P_BARK, "BARK", EK_AMT, EU_NONE, 1},    /* FRODHES K2: the 200A's character */
    {P_BELL, "BELL", EK_AMT, EU_NONE},
    {P_HAMMER, "HAMMER", EK_AMT, EU_NONE},
    {P_DECAY, "DECAY", EK_AMT, EU_NONE},
    {P_RELEASE, "RELEASE", EK_AMT, EU_NONE},
    {P_NOISE, "NOISE", EK_AMT, EU_NONE},
    {S_TUNE, "TUNE", EK_AMT, EU_C},
    {P_MOT_ON, "VIBRATO", EK_CHOICE, EU_NONE},
    {P_MOT_DEPTH, "DEPTH", EK_AMT, EU_NONE, 2},                                       /* FRODHES K3 */
    {P_MOT_RATE, "SPEED", EK_AMT, EU_HZ},
    {P_MOT_SHAPE, "SHAPE", EK_AMT, EU_NONE},
    {S_REG, "REG", EK_AMT, EU_NONE},         /* the keys' regulation: 10 as measured, 0 every key even (settings) */
};
static const char *const ENG_REED_HINT[] = {"HOW THE REED IS STRUCK", "HOW NOTES DIE AWAY", "THE 200A'S VIBRATO",
                                            "10 AS MEASURED, 0 EVEN"};

/* the CP-80 (TYPE 6, the string engine, strings.c): its panel's tone controls (BASS / MIDDLE / TREBLE: 5 the
 * service manual's centre; BRILLIANCE's three positions) on the Rhodes' cards (BARK, BELL, HAMMER, TONE), its
 * tremolo on the P_MOT_* parameters (on / off, DEPTH, SPEED), TUNE */
static const char *const ENG_CP_BRIL[] = {"LOW", "MEDIUM", "HIGH"};
static const uint8_t ENG_CP_BRIL_AT[] = {0, 64, 127};
static const ectl_t ENG_CP_C[] = {
    {P_BARK, "BASS", EK_AMT, EU_NONE, 1},    /* FRODHES K2 */
    {P_BELL, "MIDDLE", EK_AMT, EU_NONE},
    {P_HAMMER, "TREBLE", EK_AMT, EU_NONE, 2},   /* FRODHES K3 */
    {P_TONE, "BRILLIANCE", EK_DETENT, EU_NONE, 0, 3, 21, ENG_CP_BRIL, ENG_CP_BRIL_AT},
    {P_MOT_ON, "TREMOLO", EK_CHOICE, EU_NONE},
    {P_MOT_DEPTH, "DEPTH", EK_AMT, EU_NONE},
    {P_MOT_RATE, "SPEED", EK_AMT, EU_HZ},
    {S_TUNE, "TUNE", EK_AMT, EU_C},
};
static const char *const ENG_CP_HINT[] = {"THE CP-80'S TONE CONTROLS", "ITS TREMOLO, TUNE"};

#define ENG_UI_ROW(c, mk, h) {c, sizeof c / sizeof c[0], mk, h}
static const engui_t ENG_UI[] = {           /* by TYPE_UI */
    ENG_UI_ROW(ENG_RHODES_C, 15, ENG_RHODES_HINT),
    ENG_UI_ROW(ENG_REED_C, 15, ENG_REED_HINT),   /* make-up: the Rhodes' until the 200A is measured on the FM-1 */
    ENG_UI_ROW(ENG_CP_C, 15, ENG_CP_HINT),       /* (the same: the CP-80 not measured on the FM-1 yet) */
};
static const uint8_t TYPE_UI[N_TYPE_N] = {0, 0, 0, 0, 1, 1, 2};   /* TYPE -> its ENG_UI row (params.c N_TYPE) */
_Static_assert(sizeof ENG_RHODES_HINT / sizeof ENG_RHODES_HINT[0] == (sizeof ENG_RHODES_C / sizeof ENG_RHODES_C[0] + 3u) / 4u &&
               sizeof ENG_REED_HINT / sizeof ENG_REED_HINT[0] == (sizeof ENG_REED_C / sizeof ENG_REED_C[0] + 3u) / 4u &&
               sizeof ENG_CP_HINT / sizeof ENG_CP_HINT[0] == (sizeof ENG_CP_C / sizeof ENG_CP_C[0] + 3u) / 4u,
               "one hint per VOICE page");
#define ENG_VP_MAX 4u                        /* VOICE pages an engine may have (ui.c's page walk) */
