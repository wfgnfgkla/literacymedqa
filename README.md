# LiteracyMedQA

Benchmark for measuring whether medical LLMs get less accurate when the same question
is asked in low-literacy patient language instead of clinical language.

Built on MedQA (Jin et al., 2021). We rewrite each question into three literacy levels,
keep the answer options and clinical facts identical, and measure the accuracy difference.

Main number: `acc(a) - acc(c)`, the literacy gap.
Secondary: `acc(b) - acc(c)`, which controls for narrative perspective (see below).

## Setup

```bash
pip install -r requirements.txt
python src/validate_config.py config.yaml
```

The validator exits 1 if anything in `config.yaml` is still unlocked. It should currently
report 2 blocking issues (rewriter version string and the closed model name), both of
which need to be confirmed with Kiran.

## Run order

Steps 4 and 5 are gates. Step 6 doesn't start until both pass.

| # | Step | Owner | Output |
|---|------|-------|--------|
| 1 | Sample and freeze the item set from MedQA | Dong | `data/medqa_base.jsonl` |
| 2 | Reference corpora + real-patient readability baselines | Andrew | `data/reference/` |
| 3 | Generate the 100-item pilot, levels (b) and (c) | Rithik | `data/pilot_rewrites.jsonl` |
| 4 | Fidelity gate: verifier over pilot rewrites | Dong | drop rate, filter-human kappa |
| 5 | Realism gate: readability stats + blind raters | Andrew, Yuka | rater accuracy, FK delta |
| 6 | Full generation at locked N | Rithik | `data/literacymedqa_v1.jsonl` |
| 7 | Verifier over full set + 150-item manual audit | Dong | validation numbers |
| 8 | Main run: every model, all 3 levels | Patrick | `data/results.jsonl` |
| 9 | Intervention run: level (c) with clarify-first prompt | Patrick | appended to results |
| 10 | Scoring, McNemar, figures | Yuka | tables and plots |

Step 2 has to finish before step 3, since the real-corpus readability distributions are
what the level (c) rewrites are being targeted at.

## The three levels

- **(a)** Original MedQA stem, unchanged. Not generated, just copied.
- **(b)** First-person, plain language, well-formed, no jargon.
- **(c)** First-person, low literacy: casual wording, typos, vague symptom descriptions.

Both (b) and (c) are first person on purpose. MedQA stems are third-person clinical
vignettes, so if only (c) switched to first person we'd be changing perspective and
literacy at the same time and couldn't tell which one caused the drop. Keeping perspective
fixed between (b) and (c) means `acc(b) - acc(c)` isolates literacy, while `acc(a) - acc(c)`
still gives the full real-world gap. We report both.

Lab values in level (c) are kept as reported speech, e.g. "they said my potassium came back
at 5.8", rather than dropped. Dropping the number would change the medicine and break the
item; keeping it in clinical form would mean there's no manipulation.

## Gates

**Fidelity.** The verifier checks that the gold answer letter is unchanged and that no
clinical fact, number, dose, or timeline moved. It only ever checks whether the medicine
changed, never whether a model still got the answer right. Filtering on model correctness
would remove exactly the items we're trying to measure.

**Realism.** Blind raters have to identify our rewrites at 75% accuracy or below, and the
level (c) FK grade median has to sit within 2 grades of the real patient corpus.

## Repo layout

```
config.yaml              run configuration, single source of truth
prompts/
  rewriter_v1.txt        generation prompt for levels (b) and (c)
  eval_plain_v1.txt      standard eval prompt
  eval_clarify_v1.txt    intervention arm
  verifier_v1.txt        fidelity check prompt (Dong), pinned + hashed into
                          every verifier result
src/
  cost_tracker.py        logs every API call
  merge_costs.py         combines per-person logs into a team total
  validate_config.py     pre-flight check
  harness.py             run_model()/run_batch() -- calls Azure/NVIDIA/local vLLM
                          through one interface, resume-safe
  answer_parser.py       recovers A-D from raw model output; None (not a guess)
                          when ambiguous or unparseable
  sanity_check.py        50-item harness smoke test against published MedQA
                          numbers, before trusting the harness for anything real
  sample_and_freeze.py   Dong -- samples + freezes data/medqa_base.jsonl,
                          enforced (refuses to silently overwrite)
  verifier.py            Dong -- fidelity verifier: contract checks, deterministic
                          surface diff, LLM backend
  build_adversarial.py   Dong -- builds the 40-case adversarial gate set
  run_adversarial_gate.py Dong -- runs the verifier against it, gates on
                          sensitivity 1.00 / FPR <= 0.10
  power.py               Dong -- McNemar sample-size calculator; N=500 traces to
                          this, not to taste
  verifier_selftest.py   Dong -- 22-assertion self-test, no API key needed
tests/
  test_parser.py         answer_parser.py unit tests, no network needed
  test_harness.py        harness.py tests (resume-safety, crash-resilience,
                          credential handling), isolated from real data/logs
  test_score_results.py  score_results.py unit tests
data/
  medqa_base.jsonl        frozen 500-item base set (Dong)
  medqa_base.manifest.json  source hash, seed, strata -- see FREEZE_LOG.md for
                          every change after the initial freeze
  adversarial/            the 40-case gate set + held-out ids (excluded from
                          the released benchmark)
  medqa_source/           vendored MedQA test split + license
results/                 adversarial_gate_rules.{json,md} -- current gate: FAIL
                          on the rules-only backend (expected; see docs)
logs/                    api call logs
docs/
  results_schema.md       canonical results.csv interchange schema
  dataset_and_verifier.md full writeup: freeze design, verifier architecture,
                          adversarial gate methodology (Dong)
CHANGELOG.md             config and prompt version history
KAGGLE_SETUP.md          cell-by-cell notebook setup for the harness + sanity check
```

Run `pytest tests/` before trusting any change to `harness.py` or `answer_parser.py`.
`python src/sanity_check.py --mock` exercises the full harness code path with zero
API keys and zero GPU; drop `--mock` once real credentials and a running vLLM
server are in place.

## Dataset freeze & fidelity verifier

```bash
python src/sample_and_freeze.py verify        # confirm data/medqa_base.jsonl matches its manifest
python src/verifier_selftest.py               # 22 assertions, no API key needed
python src/run_adversarial_gate.py --backend rules       # no key
ANTHROPIC_API_KEY=... python src/run_adversarial_gate.py --backend anthropic  # the real gate
```

N=500 comes from `src/power.py`'s McNemar power calculation, not from taste. The
rules-only backend currently **fails** the adversarial gate (15/20 sensitivity,
misses all 4 negation-flip corruptions) -- expected, since regex can't see a
negation flip; the LLM backend is the real Week 1 exit criterion and hasn't been
run yet (no API key in this environment). Full design writeup, including why the
gate is built the way it is: [`docs/dataset_and_verifier.md`](docs/dataset_and_verifier.md).

## Scoring

```bash
python src/score_results.py data/results.jsonl --out-dir data/scoring
```

This writes:

- `data/scoring/accuracy.csv`: accuracy per model per level
- `data/scoring/literacy_gap.csv`: `acc(a) - acc(c)` plus right-to-wrong flip rate
- `data/scoring/mcnemar.csv`: paired McNemar contingency counts and exact p-values

By default, scoring uses `prompt_condition=plain`, `clinical_level=a`, and
`low_literacy_level=c`. Override those only if the locked config names change.

## Reproducibility

Nothing in `config.yaml` or `prompts/` should change once a run has started. If something
does have to change, bump the version, write down why in `CHANGELOG.md`, and regenerate
everything affected. Regenerating only part of the set leaves you with a dataset built from
two different prompt versions and no way to tell which item came from which.

Every generated item stores its `prompt_hash` and `config_version`.

The rewriter, the verifier, and the evaluated models are three separate sets with no
overlap, so no model is graded on text it wrote and no model checks its own output.

Temperature 0 everywhere. Version strings pinned; for HuggingFace models pin the commit
SHA rather than the branch name.

Failed rewrites get up to 3 regeneration attempts and are then dropped. Retrying
indefinitely would bias the set toward rewrites that happen to satisfy the filter.

## Cost tracking

```bash
python src/cost_tracker.py config.yaml   # your own spend
python src/merge_costs.py                # team total
```

Everyone logs locally. Before the weekly meeting, commit your log as
`logs/shared/api_calls_<yourname>.jsonl` and push. `merge_costs.py` combines them and
deduplicates, so overlapping commits don't double-count.

Fill in `cost_rates_usd_per_1m` in `config.yaml` from the provider pricing pages. Calls
with no rate still log their token counts, but the dollar total will be an underestimate.

Before we commit to a final N, run `tracker.project(scale=...)` on the pilot numbers and
bring the projection to Kiran.

## Related work we need to address

[Yun et al. (2026)](https://arxiv.org/abs/2604.05051) ran a technical vs. plain language
comparison in medical QA and found no significant effect of language style. We should cite
this directly in the intro rather than let a reviewer bring it up.

Four things make our setup different:

1. They measure consistency between paired responses. We measure accuracy against a known
   correct answer, so we can detect a model being consistently wrong.
2. Their models had the same retrieved documents in every condition. They note themselves
   that this may have washed out the style effect. We don't use retrieval.
3. Their plain language condition swapped treatment and condition terms inside fixed
   templates and required error-free output. Their jargon score moved from 4.52 to 3.83.
   We rewrite the whole stem and include typos and vagueness.
4. They don't test a mitigation. We do.

Point 3 needs to be shown with numbers, not asserted, so `config.yaml` includes their
readability scorer (`chaojiang06/medreadme_medical_sentence_readability_prediction_CWI`)
alongside Flesch-Kincaid, with a gate requiring our jargon shift to be larger than theirs.

Worth being realistic: if we also find no gap, that's a second null result on a similar
question and the paper gets harder to place. How strong the level (c) rewrites actually
are is the thing that determines whether this works.
