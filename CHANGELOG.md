# Changelog

Every config or prompt version bump gets an entry, with the reason and what was regenerated.

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
