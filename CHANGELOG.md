# Changelog

Every config or prompt version bump gets an entry, with the reason and what was regenerated.

## VERIFIER RUN — adversarial gate + 100-item pilot, config_version 6, 2026-08-24

**Adversarial gate: PASS.** sensitivity 20/20 = 1.00 (CI95 0.839-1.00), FPR 0/20 = 0.00,
0 clean cases sent to review. Every corruption category 4/4, including negation_flip,
which the rules backend missed 4/4. answer-changing 0.400 -> 1.00, answer-preserving
0.867 -> 1.00. served_by DeepInfra on all 40 calls. Week 1 exit criterion met. The
script's own caveat stands: at n=20 the CI floor is 0.839, so this rules out a bad
verifier rather than certifying a good one.

**Pilot verdicts, per level, never pooled:**

    level                     n  PASS  REVIEW  FAIL  DROPPED  drop rate
    (a) control, copied     100   100       0     0        0  n/a -- control
    (b) plain               100    97       3     0        0  0%
    (c) low literacy        100    86      13     1        1  1%

2 level (c) instances needed regeneration; 1 recovered on attempt 1, 1 was dropped after
all 3 attempts. 99 of 100 items retain all three levels. served_by DeepInfra on all 300
verification calls, so the pin held across the whole run.

**Level (a) came back 100/100 PASS, which is the result that licenses the rest.** It is
the unmodified stem verified against itself, so any failure would have been a verifier
false positive. There were none, so the (b) and (c) numbers are not resting on a verifier
that fails clean text.

Cost: $0.15 verification (315 calls) plus ~$0.005 regeneration. Cumulative across every
stage this session: ~$0.46.

### FINDING: the verifier does not catch MISSING demographics

This is the gap worth acting on. The structural check in generate_pilot.py flags 30 level
(c) rewrites as missing patient sex and 11 as missing age. The verifier passed 25 of the
30 and 8 of the 11 clean, and across all 300 level-instances it used the change_type
`changed_demographic` exactly ZERO times.

It is not that the verifier cannot see demographics: the adversarial gate caught
changed_demographic 4/4. The distinction is that the adversarial cases ALTER a
demographic, which is a detectable difference between two texts, while the pilot rewrites
OMIT one. A verifier framed around "did a clinical fact change" does not fire on a fact
that is simply absent from the rewrite, because nothing in the rewrite contradicts the
original.

Sex is decisive in MedQA constantly, so 25 items currently pass the fidelity gate while
being unrecoverable on sex. Two ways to close it, neither taken here because
prompts/verifier_v1.txt is frozen and is Dong's:

- extend the verifier prompt to ask whether every decisive fact is still RECOVERABLE,
  not merely unchanged; or
- treat the structural missing_sex_c / missing_age_c flags as gating rather than
  advisory, which needs no model change at all and is already computed.

Until then the structural flags are the only thing catching this, and they are carried
into every verdict row in data/pilot_verdicts.jsonl so the affected items are addressable
rather than invisible.

### Temperature 0 is not bitwise reproducible here

Regenerating a failed item with the same pinned rewriter, same frozen prompt, same seed
and temperature 0 produced materially different text. That is what makes the 3-attempt
retry policy meaningful rather than three identical calls, so it works in our favour --
but it confirms config.yaml's own warning never to claim bitwise reproducibility on the
strength of a seed. The prompt hash and the version pin remain the real provenance
anchors.

### Environment: verifier and rewriter cannot share a process on this machine

verifier.py calls through urllib, which works only with pip_system_certs' truststore
injection ACTIVE. The rewriter's openai SDK works only with it REMOVED, because under
Python 3.14 stdlib ssl's own super(SSLContext, SSLContext) resolves to truststore's
replacement class and any verify_mode assignment recurses infinitely. Three workarounds
were tried and all fail on one side or the other: SSL_CERT_FILE pointing at certifi, a
certifi-backed context handed to httpx, and recovering the genuine SSLContext from the
class MRO.

So verification ran with --no-regen first, and the two regenerations ran as a separate
process with the injection removed, their candidates verified back in the first mode. A
FAIL with no attempt spent is recorded as regen_pending, never as a drop -- reporting an
unattempted item as dropped would overstate exactly the number the paper quotes. This is
a machine-local defect, so no workaround was added to repo code; verify_pilot.py runs end
to end in one process on a normal environment.

### Manual audit sheet

data/pilot_audit_sheet.csv (150 rows, reviewer copy) and data/pilot_audit_key.csv
(verdicts, scorer copy). Drawn from the 199 surviving generated (b)/(c) instances at
run.seed, simple random rather than stratified toward disagreements, which would inflate
apparent disagreement and bias kappa. Rows are shuffled before audit_ids are assigned, so
neither id nor file order encodes verdict or level -- verified: non-PASS splits 6/6 across
the two halves. The sheet carries no verifier output and no item_id; the mapping lives
only in the key.

## NOTE — verifier moved from nvidia_build to openrouter, 2026-08-24

`models.verifier` changed from `nvidia_build / qwen/qwen2.5-72b-instruct` to
`openrouter / qwen/qwen-2.5-72b-instruct`, and `verifier.py` gained a matching
`openrouter` backend. Reason: OpenRouter is already in use for the rewriter, and this
avoids managing a second API key for one stage.

No artifact is affected. The verifier had never run — this stage has produced nothing
yet — and `models.rewriter` is untouched, so `data/pilot_rewrites.jsonl` is unchanged.

**The model id changed spelling with the provider, and this is not cosmetic.** OpenRouter
serves this model as `qwen/qwen-2.5-72b-instruct`, hyphenated. The `qwen/qwen2.5-72b-instruct`
form config previously carried is nvidia_build's spelling and returns no model on
OpenRouter — verified against openrouter.ai/api/v1/models. Left unchanged, every verifier
call would have failed. `cost_rates_usd_per_1m` was rekeyed to match, since the tracker
looks the name up verbatim and a near-miss key silently reports $0.00.

**This stage is no longer free.** The config comment previously said a free-tier open model
kept the verifier off the paid budget. That was true of nvidia_build and is not true of
OpenRouter, which bills this model at $0.36/$0.40 per 1M. Estimated ~$0.03 for the
40-case adversarial gate and ~$0.23 to verify all 300 pilot level-instances before any
regeneration. Small, but the "off the paid budget entirely" claim is now wrong and the
comment has been corrected rather than left to mislead.

The disjoint-sets rule still holds. The verifier (qwen) differs from the rewriter
(openai/gpt-4o-mini) and from every evaluated model (openai/gpt-4o,
meta/llama-3.3-70b-instruct, google/medgemma-4b-it). The verifier now shares an
*endpoint* with the rewriter and with gpt_closed, which is not what the rule constrains —
it is about models grading their own output, not about providers.

**The verifier's OpenRouter calls are provider-pinned**, matching how
`generate_pilot.py` pins the rewriter: `allow_fallbacks: false` and
`require_parameters: true` in the request body, with the serving upstream recorded as
`served_by` on every Verdict and summarised per run by the adversarial gate.

OpenRouter fans one model id across upstreams that differ in quantization, so unpinned
the same rewrite can be judged by materially different models on different runs, and the
verdicts need not agree. Methods claims a pinned verifier, and these verdicts decide what
enters the benchmark, so silent variation between runs is worse than a call that fails
loudly. Pinning does mean a call now fails outright when the pinned upstream is down
rather than quietly succeeding elsewhere; `_llm()`'s existing retry loop absorbs a
transient outage, and HTTP errors now surface the response body so "no allowed provider
available" is distinguishable from a generic 502 instead of both rendering as
"HTTP Error 502".

`served_by` is reset at the start of every `_llm()` call, so a failed verdict cannot
inherit the previous call's upstream. The adversarial gate summary carries the distinct
upstream counts across all 40 cases — one value there is what the pinned-verifier claim
actually rests on, and more than one is a finding.

**`run.config_version` bumped 5 -> 6** for this change, per the convention in config.yaml's
own header. No artifact needs regenerating: the verifier has produced nothing yet, and
`models.rewriter` is untouched, so `data/pilot_rewrites.jsonl` stands. Its rows remain
stamped `config_version: 5`, which is correct — they were generated under 5. The bump is
also the point of the note directly below about c85cea9: changing config without one is
what created that gap, and repeating it here would undercut the complaint.

## NOTE — config.yaml changed in c85cea9 without a version bump

Recorded so the reproducibility appendix stays honest. No artifact needs regenerating and
nothing here is a defect; it is a provenance gap only.

`c85cea9` (merge of #2) set `models.evaluated[gpt_closed]` from `azure_openai / null` to
`openrouter / openai/gpt-4o`, and `run.config_version` stayed at 5. The file's own header
requires a bump on any change.

**Why nothing needs regenerating:** the change touches an EVALUATED model. The pilot was
generated by `models.rewriter`, which is byte-identical before and after, so
`data/pilot_rewrites.jsonl` is unaffected and its rows remain correctly stamped.

**The gap:** rows in that file say `config_version: 5`, but checking out config.yaml at
config_version 5 today no longer reproduces the config those rows were generated under —
`gpt_closed` is now filled where it was null. Anyone reconstructing the run from the
version stamp alone would see a config that differs from the one that ran. The prompt_hash
on every row (`d3b945443321...`) is unaffected and remains the stronger provenance anchor.

Side effect worth knowing: this change also cleared the last `validate_config.py` BLOCK,
so the validator now exits 0.

Consequence for the verifier stage, which is NOT cosmetic: `openai/gpt-4o` entering
`models.evaluated` makes the verifier's `openai` backend unusable. config.yaml requires the
verifier to differ from the rewriter AND from every evaluated model "so no model checks its
own output", and running `--backend openai` (gpt-4o-2024-11-20) would now have gpt-4o
grading text it will later be scored on — breaking the disjoint-sets rule stated in
Methods. See the nvidia backend added for this reason.

## PILOT RUN — 100 items, config_version 5, 2026-08-20

100/100, 0 errors, served_by OpenAI x100, $0.1071. prompt_hash d3b945443321..., all
provenance fields uniform. Shared on the remote at blob 125508c9d608 (replacing the v4
blob 7044ff17bb6a).

**DOES NOT MEET THE SHIP CRITERION.** The bar was "FK in-band improves and nothing else
regresses". FK in-band improved 47 -> 50, but sex and age presence both regressed, so
this is a decision for Rithik rather than an automatic ship.

    metric                  v4     v5
    level (c) FK median   6.20   7.11   +0.91
    level (c) FK mean     6.58   7.30   +0.71   delta from askdocs median 0.91 -> 0.20
    FK below 6              39     21     -18
    FK above 8              14     29     +15
    FK in band              47     50      +3
    terminal stub        54/100  7/77   FIXED
    SEX stated              88     70     -18   <- REGRESSION
    AGE stated              99     89     -10   <- REGRESSION
    items flagged           27     42     +15
    jargon leak             16     17      +1   (untouched by design)

**The stub fix caused the sex regression, and the mechanism is exact.** v4's terminal stub
— "im 68 by the way, and im a man" — was formulaic, but it reliably carried BOTH facts.
v5 folds the demographics into a sentence doing other work, as intended, and the model
keeps the age while dropping the sex: "im 39 and i drive a truck for a living", "im 29 and
i got this thing called lupus". 20 items had sex in v4 and lost it in v5; only 2 went the
other way. This is the same shape of failure as v4's FK regression: fixing the surface
form of the demographics clause broke what the clause was carrying.

**The "vary within the last third" instruction did not hold, but did not breach C2.**
Position of the demographic mention swung to the opposite extreme from v4: median 0%, with
51/77 in the first 10% of the text and only 10/77 (13%) in the final third. Critically,
though, 0 of those are demographic RECITAL openings — the age is embedded in a first
sentence that is genuinely about the symptom ("i got this sharp pain ... im 39 and i drive
a truck"), not "im 45 and i went to the doctor". C2 is intact; banned openers are 1/100.

**Everything else held.** venue in first sentence 44 -> 41, banned opener 2 -> 1, typo
types below 3: 33 -> 32, served_by single upstream for the fourth run running. Both
clustering patterns persist: FK below-6 is 37% on long stems and 32% on lab-bearing stems;
above-8 is 47% on short stems (median 93 words vs corpus 119).

**If a v6 is wanted, the target is narrow:** require age and sex in the SAME clause as each
other, folded into content, without prescribing a position. v4 proved the pairing holds
when they are adjacent; v5 proved that folding works but splits them. Nobody has yet tried
both constraints at once.

## config_version 5 — 2026-08-20

Rewriter prompt v4 -> v5. Pre-pilot prompt development, not an iteration. Narrow and
final: two targeted fixes, nothing else touched.

1. THE DEMOGRAPHICS STUB. v4's age/sex rule worked (sex 43 -> 88) but 54/100 rewrites
   ended in a terse standalone aside — "im 68 by the way, and im a man." That did two
   kinds of damage. It is the whole FK regression: deleting only that sentence moves the
   corpus mean 6.58 -> 7.12 and below-6 39 -> 28, essentially back to v3. And a rewrite
   that always ends the same way is a template a blind rater learns in two items, which
   is the realism gate's exact failure mode.

   The cause was v4's own examples — all three placed the demographics late AND as a
   terminal stub, and the model copied the shape rather than the placement. v5 keeps late
   placement and requires the age and sex to be FOLDED INTO A SENTENCE ALREADY CARRYING
   OTHER CONTENT, with GOOD/BAD pairs contrasting an embedded clause against a stub, and
   an instruction to vary the position within the last third. The three examples now
   place it at 71%, 70% and 86% of the way through, in sentences of 43, 39 and 23 words,
   and only one is the final sentence.

2. THE POSSESSIVE / NARRATOR BUG, now an explicit rule rather than another example pair,
   since two example pairs failed to reach it. v3 rendered "His mother has a backyard
   garden" as "his mom has a garden" (speaker becomes the father); v4 rendered it "my mom
   has a garden" (speaker becomes the grandmother). The rule now states directly that
   when the narrator is a family member, every possessive referring to that narrator's
   own relationships stays first person — "i have a garden out back" — and that carrying
   the stem's third-person possessive across unchanged silently invents a different
   narrator.

C1 (jargon), C2 (opening) and C3 (typos) are carried over BYTE-IDENTICAL from v4 and were
diffed to confirm it. Gate thresholds untouched.

### OPEN QUESTION, NOT A DEFECT: the 15 lab-name retentions

16/100 level (c) rewrites retain a clinical term, and the count did not move between v3
and v4 — the SAME 15 items with identical per-term counts (creatinine 7, bilirubin 5,
alkaline phosphatase 3, prothrombin 2). Three prompt revisions failed to shift it, which
is fairly strong evidence that prompt wording is not the instrument that reaches it.

v5 deliberately does not try again, and these retentions should NOT be logged as an
unfixed defect. The open question is whether they need fixing at all:

- Real patients do repeat lab names their doctor told them. "they said my creatinine was
  2.9" is plausible patient speech, not a register failure.
- The reported-speech rule already preserves the clinical fact while changing the
  register, which is the manipulation the benchmark is actually measuring.
- Whether these 15 read as machine-written is exactly what the realism gate exists to
  answer, and it has not run yet.

DECISION DEFERRED TO THE REALISM GATE. If blind raters cannot pick these items out, the
retentions are fine and C1 is if anything too strict. If raters do spot them, the fix is
likely structural — an explicit banned-term list, or a detector plus targeted
regeneration of just the offending items — rather than a fourth attempt at demonstrating
it in a worked example.

## PILOT RUN — 100 items, config_version 4, 2026-08-20

Second full pilot, v4 prompt. 100/100, 0 errors, served_by OpenAI x100, $0.0992.
prompt_hash 0d3f1575e9f2..., all provenance fields uniform across all rows.

**v3 -> v4, measured on the same 100 items**

    metric                  v3     v4
    SEX stated              43     88     +45   <- the change that mattered
    AGE stated              89     99     +10
    items flagged           67     27     -40
    closing sentiment        1      0     fixed
    jargon leak             16     16     NO CHANGE
    venue in 1st sentence   29     44     +15   <- regression
    banned opener            0      2      +2   <- minor regression
    level (c) FK mean      7.17   6.58   -0.59  <- regression
    FK below 6              25     39     +14   <- regression
    FK above 8              25     14     -11
    FK sd                  2.03   1.63   -0.40

**What worked.** The demographics rule did its job: sex present in 88/100 level (c) and
age in 99/100, against 43 and 89 under v3. Combined with the fixed structural check that
is a drop in flagged items from 67 to 27. Closing sentiment is gone entirely.

**Three things did not, and two are worth naming precisely because the cause is known.**

1. THE FK REGRESSION IS CAUSED BY THE DEMOGRAPHICS FIX. 54 of 100 rewrites now end in a
   short standalone aside — "im 68 by the way, and im a man". Deleting just that sentence
   moves the corpus mean from 6.58 back to 7.12 and the below-6 count from 39 to 28, i.e.
   almost exactly v3's numbers. A short simple closing sentence drags a Flesch-Kincaid
   score down hard. The fault is in v4's own worked examples: all three place the
   demographics late AND as a terse standalone stub, and the model copied the shape rather
   than the placement. A v5 should keep the late placement and fold the demographics into
   a longer clause instead of a terminal stub.

2. JARGON LEAKAGE DID NOT MOVE AT ALL. Not merely the same rate — the SAME 15 items, with
   identical per-term counts: creatinine 7->7, bilirubin 5->5, alkaline phosphatase 3->3,
   prothrombin 2->2. The restored lab-panel worked example changed nothing. Three prompt
   revisions have now failed to shift this, which is evidence that a worked example is the
   wrong instrument for it. Worth trying something structurally different — an explicit
   banned-term list in the prompt, or a post-hoc detector plus targeted regeneration of
   just the offending items, rather than a fourth attempt at demonstrating it.

3. VENUE IN THE FIRST SENTENCE ROSE 29 -> 44 despite an unchanged ban, and 2 rewrites used
   a banned opener where v3 had none. Plausibly the demographics moving out of the opening
   left a gap that the venue filled. Not confirmed.

Also unfixed: LMQ-1b81bdad880a, the narrator-relationship item that motivated priority 2.
The stem reads "His mother has a backyard garden"; the narrator IS the mother, so the
garden is hers. v3 wrote "his mom has a garden", making the speaker the father. v4 writes
"my mom has a garden", making it the grandmother's. Both are wrong, differently. The
worked pair did not generalise to the possessive form.

## config_version 4 — 2026-08-20

Rewriter prompt v3 -> v4. PRE-PILOT PROMPT DEVELOPMENT — still not an iteration against
the 3-attempt cap; see the accounting note under config_version 2.

Driven by the 100-item v3 pilot, in priority order. The headline finding is that the
worst problem was not a realism problem at all: 56 of 100 level (c) rewrites made the
patient's SEX unrecoverable. Sex is decisive in MedQA constantly, so those items would be
exposed to being dropped at the fidelity gate — a potential 56% drop rate, not a style
blemish. Everything below is ordered by that.

1. AGE AND SEX ARE MANDATORY AND EXPLICIT. New top-level section, placed before the
   level definitions because it is a fidelity rule, not a register one. Third-person
   vignettes carry sex in every he/she; first person deletes that cue, so unless the
   rewrite says it, the fact is gone. "im 45" is explicitly called insufficient. All
   three worked examples now state age and sex in their LAST sentence, so the rule cannot
   rebuild the demographic opening that C2 exists to prevent.

2. NARRATOR RELATIONSHIP IS A DECISIVE FACT. v3's LMQ-1b81bdad880a had the speaker write
   "his mom has a garden", making the narrator the father when the stem's caregiver is
   the mother. Worked BAD/GOOD pair added, plus an instruction to check every possessive,
   since "his mom" / "her dad" / "my wife" each silently assign the narrator a role.

3. LAB PANEL EXAMPLE RESTORED AS A THIRD EXAMPLE. Jargon leakage was 16/100 and 88%
   concentrated on lab-bearing stems, over a small vocabulary: creatinine 7, bilirubin 5,
   alkaline phosphatase 3, prothrombin 2. This is the demonstration that was cut at the
   4->2 reduction in v3. Opening variety held at 100 items (0/100 banned openers, no
   2- or 3-word shape above 20%), so the averaging risk of a third example is now worth
   paying. The new example converts a six-test panel to reported speech with all six
   numbers intact and names none of the tests. Example 2's one remaining "bilirubin" was
   also removed, correctly hedged though it was — an example naming a term that leaked
   five times is an example teaching it.

4. FK DRIFT IS BIDIRECTIONAL AND LENGTH-DRIVEN. Long lab-dense step2/3 stems undershot
   (43% below grade 6); short sparse step1 stems overshot (33% above grade 8). C4 now
   names both directions: long originals keep their run-ons and digressions rather than
   being compressed, short ones get ordinary connective talk rather than more clinical
   nouns.

5. NO CLOSING SENTIMENT. v3's LMQ-3576953f4a14 ended "just trying to understand what all
   this means", which the original does not contain. Added to HARD PROHIBITIONS: a
   feeling the stem does not state is a fact you invented, and a uniform closing plea is
   also a strong tell, because real posts do not all end that way.

C2 (opening ban) and C3 (typo rules) are carried over VERBATIM and were diffed to confirm
it. Both held at 100 items and neither was touched. Gate thresholds untouched.

**Tooling in the same bump** — see the commit for detail. The structural check now flags
missing age and missing sex, which it was previously incapable of seeing; and three
false-positive classes are fixed (caret exponents in mm^3, Unicode ℃/℉ single glyphs, and
gravida/para retold as ordinals). Re-scoring the v3 pilot with the fixed check gives
67/100 flagged rather than 29/100, with dropped_numbers_c falling 29 -> 24 as the false
positives cleared.

**Verification command — use this one, not the original.** The version in the task notes
crashes on Windows, where open() defaults to cp1252 and the file legitimately contains
"°" and curly apostrophes. It needs an explicit encoding before it goes in the appendix:

    python -c "import json; r=[json.loads(l) for l in open('data/pilot_rewrites.jsonl', encoding='utf-8')]; print(len(r),'items'); print('prompt_hash all:', all('prompt_hash' in x for x in r)); print('version all:', all('rewriter_version' in x for x in r)); print('three levels:', all(all(k in x for k in ('level_a','level_b','level_c')) for x in r if 'error' not in x))"

## PILOT RUN — 100 items, config_version 3, 2026-08-20

First actual pilot. Not a version bump; recorded here because this is the artifact the
task is about and its provenance needs to be reconstructible.

    items          100 / 100, 0 errors
    prompt         prompts/rewriter_v3.txt
    prompt_hash    e084ef1dd17b5332a9b928c2d9218d6d8fe3973d2c8ea0757cdc9e0ba8e0e4a5
    rewriter       openai/gpt-4o-mini @ gpt-4o-mini-2024-07-18 (lock_status provisional)
    served_by      OpenAI x100 -- single upstream, no fallback drift
    output         data/pilot_rewrites.jsonl (gitignored)
    cost           $0.0806

All four provenance fields are uniform across all 100 rows: one prompt_hash, one model,
one version, one config_version. prompt_hash and rewriter_version present on every row.

**Headline results (distributions, not means)**
- level (c) FK: median 6.79, mean 7.17, sd 2.03. Only 50% inside the 6-8 band —
  25 below 6 and 25 above 8. The gate passes on the mean (delta 0.32 vs askdocs median,
  threshold 2.0) but the mean is two opposite failure modes cancelling.
- Jargon leakage: 16/100 level (c) rewrites retain a clinical term. Almost entirely lab
  names (creatinine 7, bilirubin 5, alkaline phosphatase 3, prothrombin 2).
- Structural flags: 29/100.
- Opening: 0/100 use a banned opener, so v3's C2 ban held. 29/100 still name the venue
  in the first sentence. No 2- or 3-word opening shape exceeds 20%.
- Typo types: median 3, but 36/100 fall below the required 3 distinct types.

**Two fidelity failures found by auditing the flags — both need fixing before the
benchmark is usable**
- AGE absent from a rewrite in 11 items (15 item/level pairs). Age is the first entry on
  the decisive-facts list. All 11 were caught by the structural flag.
- SEX absent from a rewrite in 56 items (63 item/level pairs). First-person narration
  removes the pronoun cue, so unless the rewrite says "im a guy" / "im 24, female" the
  patient's sex is unrecoverable. INVISIBLE to the structural flag, which only checks
  numbers. Likely aggravated by v3's C2, which moved age and sex off the opening and
  told the model to place them "wherever it lands naturally" -- often nowhere.

**Known false positives still in the 29 flags**
- `mm^3` with a caret: the superscript 3 is read as a quantity. The lookaround handles
  `mm3` but not `mm^3`.
- Unicode ℃ / ℉ (U+2103 / U+2109) as single glyphs: CONVERSION_PAREN expects "°C"/"°F",
  so "(98.6℉)" is not recognised as a unit restatement and its value is required.
- `gravida 2, para 1`: an obstetric code, not two independent quantities.

## config_version 3 — 2026-08-20

Rewriter prompt v2 -> v3. PRE-PILOT PROMPT DEVELOPMENT — not an iteration; see the
accounting note under config_version 2. Still no pilot generated.

**Regenerated:** nothing. A second 5-item smoke sample, discarded.

**Why v3 — v2 fixed two of its four targets and missed two**

What v2 fixed and v3 keeps: level (c) FK moved 4.74 -> 6.61 (gate delta 2.76 -> 0.89
against askdocs median, threshold 2.0), and every jargon term v1 leaked was gone —
popliteal, prothrombin, thromboplastin, mixing study, guarding, rebound all absent.

What v2 missed:
- The opening template did not break, it mutated. All five v2 rewrites opened with "so",
  and three of five still named the venue in the first clause. Instruction plus four
  varied few-shot examples did not hold, so v3 constrains it directly: "so", "ok so",
  "okay so", "hi", "hey" and "well" are forbidden as the first word, and naming where
  they are or who they saw is forbidden in the first sentence outright.
- Few-shot count cut from four to two. Five examples appear to have taught an average
  shape rather than a range. The two remaining are deliberately unalike on every axis —
  parent vs patient, child vs adult, respiratory vs abdominal, symptom-opening vs
  timing-opening, thin vitals vs heavy lab panel — and the prompt tells the model
  explicitly not to average them.
- Typo variation did not land. C3 listed eight error types and the output delivered
  essentially one (missing apostrophes), plus two rewrites containing CORRECT apostrophes
  ("wasn't", "someone else's") which read more literate than v1 did. v3 makes it a
  countable requirement — at least three DISTINCT types per rewrite, three of the same
  kind does not count — adds a worked example showing a phonetic misspelling, a doubled
  letter and a self-correction in one line, and forbids correct contractions outright.

**Also in v3**
- FK floor now binds as hard as the ceiling (C4). v2's item 2 came in at 4.61, below band,
  because "simpler feels safer". It is not safer; it is a different way of failing, and
  the prompt now says so and aims for the middle of the band near 7 rather than the edge.
- New section C1b: told-to-them technical terms become reported speech, NEVER a lay
  reinterpretation that changes the claim. v2's item 4 rendered "ST-segment elevations in
  the anterior leads" as "changes in the front part of my heart". Leads are electrode
  positions on the chest, not a region of the heart — that is not simplification, it is a
  new and wrong clinical claim the original never made. Vagueness is allowed; guessing is
  not. Two further worked pairs cover bibasilar rales and urine beta-hCG.

**Tooling, same bump**
- generate_pilot.py: digit + hyphen + alphabetic run of >=6 is treated as a compound-name
  locant, not a quantity. The 5 in "5-hydroxyindoleacetic acid" was being required of a
  rewrite that C1 explicitly instructs to say "some acid thing" — the check was flagging
  the prompt for obeying itself. MedQA is full of these. 6-pack, 3-cm, 58-year-old and
  11-month-old stay required; verified both directions.

Gate thresholds untouched, as under config_version 2.

## config_version 2 — 2026-08-20

Rewriter prompt v1 -> v2. PRE-PILOT PROMPT DEVELOPMENT — not an iteration.

**Iteration accounting:** the 3-attempt cap in `generation.max_regeneration_attempts`
counts regenerations of the pilot from scratch. No pilot has been generated. Every
artifact so far is a 5-item smoke sample used to read output before committing to a
run. Prompt revisions at this stage consume no attempts against the cap, and this
entry should not be read as spending one. Rithik is confirming the accounting with
Kiran separately; if she counts differently, this note is the thing to correct.

**Regenerated:** nothing. The only artifacts under v1 were a 5-item smoke sample,
discarded.

**Why v2 — all four changes driven by reading the v1 5-item sample**
- Level (c) FK calibration target moved 4-6 -> 6-8. The v1 sample came in at mean FK
  4.74. The realism gate measures distance from the real-patient corpus, and askdocs
  sits at 7.496 median, so 4.74 is a delta of 2.76 against a 2.0 threshold. More
  importantly it is *simpler than real patients write*, which reads as an artifact of
  our rewriting rather than a property of patient language — the exact criticism this
  benchmark exists to survive. v2 instructs reaching grade 6-8 through longer run-on
  sentences and ordinary multi-syllable words, explicitly NOT by reintroducing jargon,
  which would raise the score for the wrong reason and break the vocabulary rule.
- Zero-clinical-vocabulary rule hardened (new section C1). v1's sample kept "popliteal
  vein", "prothrombin time", "partial thromboplastin time" and "mixing study for ptt"
  verbatim in level (c), plus "no guarding or rebound". Anatomy names and lab-test names
  are the two categories writers forget are jargon. v2 adds explicit BAD/GOOD pairs
  converting both to lay description and reported speech, keeping every number.
- Opening template broken (new section C2). Four of five v1 level (c) rewrites opened
  "im NN and i [came to the er / went to the doc] bc". A blind rater learns that shape
  immediately, and gates.realism.max_rater_accuracy is 0.75. v2 requires varied entry
  points and carries four few-shot examples with deliberately different opening shapes.
- Typo distribution varied (new section C3). v1 recycled a small tidy set (bc, dont, im,
  tho, kinda). v2 specifies eight error *types* and requires several kinds per rewrite
  with some sentences left nearly clean.

**Also in this bump**
- `gates.realism.fk_delta_reference_statistic: median` added. NOT a threshold change —
  max_fk_grade_delta stays 2.0 as pre-registered. The config previously did not say
  which askdocs statistic the delta measured against, and median (7.496) vs mean (8.463)
  return different verdicts on identical data. An unstated choice is one that gets made
  after seeing results, which is what the README forbids. Now stated up front.
  Still unstated and worth locking: *which* reference corpus. baseline_stats.json holds
  askdocs, meqsum and medqa_stems. The gate comment says "the real-patient corpus",
  which reads as askdocs, but it is not written down anywhere.
- v1 prompt retained at `prompts/rewriter_v1.txt` rather than overwritten. It is what
  produced the discarded sample and Appendix A needs it reconstructible.

**Note for Patrick — harness.py is blocked on this machine, unrelated to this bump**
`site-packages/pip_system_certs.pth` calls `truststore.inject_into_ssl()` at every
Python startup, replacing `ssl.SSLContext` globally. Under Python 3.14.3 that class
recurses infinitely in its `verify_mode` setter, so every HTTPS call made through the
openai SDK dies as `APIConnectionError: Connection error` after exhausting retries —
the failure looks like a network or key problem and is neither. curl to the same
endpoint succeeds, which is what makes it confusing to diagnose.

harness.py will hit this on any azure_openai or nvidia_build call, so the eval stage
is affected, not just generation. pip_system_certs (5.3) and truststore (0.10.4) are
both already at their latest versions and the package exposes no opt-out, so there is
no upgrade path today. Reverting the injection in-process works:

    from pip._vendor import truststore; truststore.extract_from_ssl()

That falls back to certifi's CA bundle, which is the stock default for the openai SDK
and not a TLS downgrade. Left unfixed in harness.py deliberately — not my task and not
my file.

## config_version 1 — 2026-08-07

Initial frozen configuration. Nothing generated yet, so no regeneration required.

**Design decisions**
- Levels (b) and (c) are both first-person. MedQA stems are third-person clinical
  vignettes; a patient retelling is first-person. If only (c) switched perspective,
  (a)->(c) would confound literacy with narrative perspective. Holding perspective
  constant across (b) and (c) yields two numbers: acc(b)-acc(c) is the clean literacy
  effect, acc(a)-acc(c) is the ecological gap.
- Level (a) is NOT generated. It is the unmodified MedQA stem. Paraphrasing the control
  would destroy the control. The rewriter therefore emits (b) and (c) only, with (a) as
  its input. This is a deliberate reading of the task text "covering all three levels".
- Lab values in level (c) are rendered as reported speech ("they said my potassium came
  back at 5.8") rather than dropped. Dropping the number changes the medicine and
  invalidates the item; keeping it verbatim means no manipulation.
- Verifier changed from meta/llama-3.3-70b-instruct to qwen/qwen2.5-72b-instruct before
  first use. The Llama was also in the evaluated set, so it would have been checking its
  own output. Caught by validate_config.py. No artifacts affected.
- MedReadMe jargon scorer added to the realism gate alongside Flesch-Kincaid, so
  manipulation strength is directly comparable to Yun et al. (2026), whose technical->plain
  jargon median moved 4.52 -> 3.83. Our gate requires a larger shift.
- 2026-08-09: models.evaluated[medgemma].provider changed from local_hf (vLLM server)
  to local_transformers (direct transformers.generate(), in-process, no server).
  vLLM's exact-pinned dependency chain (numpy<2.0.0, torch==2.5.1, transformers>=4.48.2,
  and dozens more) proved unworkable against Kaggle's actual base-image environment --
  repeated install attempts left the container's own numpy installation corrupted
  (pip's bookkeeping and the actually-loaded module reporting different versions,
  core submodules missing). local_transformers sidesteps this entirely: far shallower
  dependency tree, no exact pins to fight. Slower per-call than vLLM at real pipeline
  scale (~15k calls) -- revisit if throughput becomes a bottleneck once the full run
  starts, but unblocks the sanity check today. No prior artifacts affected -- nothing
  had been generated against medgemma yet. See KAGGLE_SETUP.md for the full story.

**Open — blocked on mentor lock**
- `dataset.n_items`
- `models.rewriter.version` (GPT-4o-mini exact version string)
- `models.evaluated[gpt_closed].name` and version
- `cost_rates_usd_per_1m` for all paid models
