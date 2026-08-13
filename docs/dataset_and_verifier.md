# Dataset freeze, fidelity verifier, and adversarial gate

Three tasks, all runnable end to end. Nothing here needs a GPU and only the LLM
verifier backend needs an API key.

```
python src/sample_and_freeze.py freeze --n 500 \
    --exclude-file data/adversarial/held_out_ids.txt --reason "..."
python src/sample_and_freeze.py verify
python src/build_adversarial.py
python src/run_adversarial_gate.py --backend rules
ANTHROPIC_API_KEY=... python src/run_adversarial_gate.py --backend anthropic
python src/verifier_selftest.py
python src/power.py
```

## 1. Frozen item set

`data/medqa_base.jsonl` — 500 items, `data/medqa_base.manifest.json`,
`data/FREEZE_LOG.md`.

Source is MedQA (Jin et al., 2021), US 4-option **test** split, 1,273 items,
MIT licensed, vendored under `data/medqa_source/` with its license and pinned by
SHA-256 in the manifest.

**N = 500** comes from `src/power.py`, not from taste. Under McNemar at α=.05
and 80% power, a 5-point literacy gap with 15% of items flipping in either
direction needs 471 items. A 3-point gap would need 1,309 — more than the split
holds, which is worth knowing now rather than in Week 5.

**Stratification** uses only fields MedQA actually ships. There are no specialty
labels in the release, so the axes are `exam_step` (step1 vs step2&3), `has_labs`
(a lab or vitals block is present), and `len_tercile` (cut on the population).
Twelve cells, proportional allocation with largest-remainder rounding; sample
shares match population shares to within 0.001 on every cell. These are also the
pre-registered subgroup axes for the gap analysis.

**IDs** are `LMQ-` plus the first 12 hex of a SHA-256 over the question and
options. Content-derived, so they survive a re-release, a reordering, or a
re-download; they are not row numbers.

**The freeze is enforced, not just documented.** `freeze` recomputes the file
and refuses to overwrite a differing one unless `--reason` is supplied, and
every such change is appended to `FREEZE_LOG.md`. `verify` re-checks the source
hash, the output hash, the line count, ID uniqueness, and that every gold letter
is among its options.

The 20 verifier-QA items are **held out** of the benchmark. Hand-corrupted text
should never leak into a released set, and the verifier should not be tuned on
items it will later judge.

## 2. Fidelity verifier

`prompts/verifier_v1.txt` (pinned, hashed into every result),
`src/verifier.py`.

Three layers:

1. **Contract checks, in code, no model call.** Answer options must be byte
   identical and the gold letter unchanged. These are contract violations, not
   judgement calls, and asking a model to rule on them just adds a failure mode.
2. **Deterministic surface diff.** Number+unit pairs, blood pressures, and
   demographic tokens are extracted and compared. Split into *hard* flags
   (a value was swapped or added, a unit changed) and *soft* flags (values
   quietly dropped) — because dropping units is what a low-literacy paraphrase
   is supposed to do. "K+: 3.3 mEq/L" → "my potassium was 3.3" is the rewrite
   working, not a corruption.
3. **LLM verifier.** Pinned prompt, temperature 0, structured JSON, retries with
   backoff, code-fence tolerant parsing.

Output keeps the three fields you specified — `same_answer_letter`,
`clinical_fact_changed`, `what_changed` — and adds:

- `change_type` from a fixed enum, so drop-rate reporting is analyzable instead
  of a pile of free text
- `evidence_original` / `evidence_rewrite`, required spans; an ungrounded flag is
  not a flag
- `jargon_removed` — the **manipulation check**. A rewrite that keeps "substernal
  chest pressure" passes fidelity while not being low-literacy at all. Your
  Flesch-Kincaid gate catches that in the distribution but not per item.
- `confidence`, and a **REVIEW** verdict, so "unsure" is never silently rounded
  to PASS. A verifier without an abstain option converts its own uncertainty
  into clean items.

Two design choices worth defending in the paper:

- The prompt never asks the model to answer the question. It asks whether the
  rewrite *alters or removes evidence the gold answer depends on*. Handing it
  the gold letter and asking "is this still right?" is leading, and asking it to
  answer independently measures verifier competence rather than rewrite fidelity.
- Code overrides the model in one direction only. If the deterministic layer sees
  a numeric change and the model says PASS, the verdict becomes REVIEW. Never the
  reverse.

## 3. Adversarial gate

`src/adversarial_pairs.json` (hand-authored), `src/build_adversarial.py`,
`data/adversarial/adversarial_set.jsonl`, `src/run_adversarial_gate.py`.

20 corruptions across five categories — altered_number, unit_change,
changed_timeline, changed_demographic, negation_flip — **plus 20 clean twins**.
"Confirm the verifier catches every one" is passable by a verifier that flags
everything, so the controls are the point. Each twin pair differs by exactly one
edit (`find`/`replace` on the same faithful rewrite, asserted unique at build
time), which means a flag on a clean twin is unambiguously a reaction to the
paraphrase rather than to the injected error.

15 of 20 corruptions leave the correct letter intact. Those are the hard ones and
they are reported separately: catching a corruption that flips the answer is easy
because the text stops cohering, while a dose unit change that leaves the answer
alone is where a released benchmark actually gets contaminated.

Gate passes only at sensitivity 1.00 with FPR ≤ 0.10, and exits non-zero
otherwise so it can sit in CI.

### Result on the deterministic backend

**Sensitivity 15/20 = 0.75, FPR 0/20 = 0.00 → GATE FAIL** (see
`results/adversarial_gate_rules.md`). Perfect on altered_number and
changed_timeline, 3/4 on unit_change and changed_demographic, **0/4 on
negation_flip**. Which is the correct and expected outcome: regex cannot see
"never turned blue" becoming "turns blue often", and it cannot see
"African-American" becoming "white". The rules layer is a free pre-filter with no
false positives, not a verifier. Its value is that it fails this gate honestly —
if it had passed, the gate would be broken.

**The LLM backend has not been run** — no API key in this environment. That run
is the real Week 1 exit criterion. `src/verifier_selftest.py` exercises the LLM code
path with a stubbed transport (22 assertions, all passing), so the first real run
should cost only tokens, not debugging.

## Caveat on the gate itself

At n=20 per arm, a perfect 20/20 still leaves true sensitivity as low as ~0.83 at
95% confidence. This gate rules out a broken verifier; it does not certify a good
one. Treat passing as a precondition for generating the full set, not as evidence
the set is clean — the 150-item human kappa audit in Week 3 is what actually
bounds the filter's reliability.
