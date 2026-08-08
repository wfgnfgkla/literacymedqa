# Changelog

Every config or prompt version bump gets an entry, with the reason and what was regenerated.

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

**Open — blocked on mentor lock**
- `dataset.n_items`
- `models.rewriter.version` (GPT-4o-mini exact version string)
- `models.evaluated[gpt_closed].name` and version
- `cost_rates_usd_per_1m` for all paid models
