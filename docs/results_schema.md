# LiteracyMedQA Results Schema

## Decision

Use one canonical results file for every model run:

`results.csv`

Each row is one scored prediction for exactly one:

`(item_id, model, level, prompt_condition)`

The required columns are:

| Column | Type | Required | Allowed values / format | Meaning |
|---|---:|---:|---|---|
| `item_id` | string | yes | Stable dataset ID, e.g. `medqa_000123` | The original MedQA item. The same `item_id` is reused across all literacy levels and prompt conditions. |
| `model` | string | yes | Canonical model label, e.g. `gpt-4o-mini-2024-07-18`, `llama-3.1-70b-instruct` | The evaluated model, not the rewrite or verifier model. Use one exact spelling everywhere. |
| `level` | string | yes | `doctor`, `plain`, `low_literacy` | Input phrasing condition. `doctor` is the original/control wording. |
| `prompt_condition` | string | yes | `standard`, `clarify_then_answer` | Evaluation prompt used. `clarify_then_answer` is only expected for the low-literacy fix run unless the team explicitly expands the design. |
| `predicted` | string | yes | `A`, `B`, `C`, `D`, `E`, or `INVALID` | The model's final multiple-choice answer after parsing. Use `INVALID` if no single answer can be extracted. |
| `gold` | string | yes | `A`, `B`, `C`, `D`, or `E` | Correct answer letter from MedQA, unchanged across levels for the same `item_id`. |
| `correct` | integer | yes | `1` or `0` | `1` if `predicted == gold`, else `0`. `INVALID` predictions are always `0`. |

No downstream analysis should read raw model outputs, rewritten item files, logs, or ad hoc spreadsheets for scored results. All accuracy, literacy gap, right-to-wrong flips, McNemar tests, and prompt-recovery numbers must read from `results.csv`.

## Primary Key

The unique key is:

`item_id, model, level, prompt_condition`

There must be no duplicate rows for this key. If a run is repeated because of an execution error, replace the row only after the rerun policy is documented in the run log.

## Controlled Values

### `level`

| Value | Definition |
|---|---|
| `doctor` | Original doctor/board-exam wording. |
| `plain` | Content-preserving plain-language rewrite. |
| `low_literacy` | Content-preserving low-literacy patient-style rewrite. |

### `prompt_condition`

| Value | Definition |
|---|---|
| `standard` | Normal answer-only multiple-choice prompt. |
| `clarify_then_answer` | Fix prompt that asks the model to clarify/reason before giving the final answer. |

## Derived Metrics

All formulas below read only from `results.csv`.

| Metric | Formula |
|---|---|
| Accuracy by model, level, prompt | `mean(correct)` grouped by `model`, `level`, `prompt_condition` |
| Literacy gap | `accuracy(model, doctor, standard) - accuracy(model, low_literacy, standard)` |
| Plain-language gap | `accuracy(model, doctor, standard) - accuracy(model, plain, standard)` |
| Fix recovery | `accuracy(model, low_literacy, clarify_then_answer) - accuracy(model, low_literacy, standard)` |
| Percent of gap recovered | `fix_recovery / literacy_gap` when `literacy_gap > 0` |
| Right-to-wrong flip rate | Among rows where `(doctor, standard)` is correct, fraction where `(low_literacy, standard)` is incorrect for the same `item_id` and `model` |
| McNemar test | Paired correctness table comparing `(doctor, standard)` vs `(low_literacy, standard)` for the same `item_id` and `model` |

## Validation Rules

Before analysis, run these checks:

1. Required columns are exactly present: `item_id`, `model`, `level`, `prompt_condition`, `predicted`, `gold`, `correct`.
2. No duplicate primary keys.
3. `level` and `prompt_condition` contain only controlled values.
4. `predicted` is one of `A/B/C/D/E/INVALID`.
5. `gold` is one of `A/B/C/D/E`.
6. `correct` is always `1` when `predicted == gold`, otherwise `0`.
7. For each `item_id`, `gold` is identical across all levels, models, and prompt conditions.
8. For headline analysis, each included `(item_id, model)` has all required paired rows:
   - `doctor + standard`
   - `plain + standard`
   - `low_literacy + standard`
   - `low_literacy + clarify_then_answer`

## Minimal Example

```csv
item_id,model,level,prompt_condition,predicted,gold,correct
medqa_000001,gpt-4o-mini-2024-07-18,doctor,standard,C,C,1
medqa_000001,gpt-4o-mini-2024-07-18,plain,standard,C,C,1
medqa_000001,gpt-4o-mini-2024-07-18,low_literacy,standard,A,C,0
medqa_000001,gpt-4o-mini-2024-07-18,low_literacy,clarify_then_answer,C,C,1
```

## What Does Not Belong In `results.csv`

Keep these in separate files so the results table stays stable:

| File | Contents |
|---|---|
| `items.csv` | `item_id`, original question, rewritten question text, answer choices, source split, rewrite/verifier status |
| `runs.csv` | run date, API/model version strings, decoding settings, prompt template version, raw-output file path |
| `raw_outputs.jsonl` | full model responses and parser notes |
| `exclusions.csv` | dropped item IDs and reasons |

The analysis scripts may join these files for audit tables, but reported performance numbers must be computed from `results.csv`.

## Agreement Checklist

Team members should approve these points before any full run:

- The row grain is one prediction per `(item_id, model, level, prompt_condition)`.
- The seven required columns are sufficient for every headline metric.
- Controlled values for `level` and `prompt_condition` are final.
- Invalid or unparsable model answers are encoded as `predicted = INVALID` and `correct = 0`.
- The fix run is represented as `prompt_condition = clarify_then_answer`, not as a separate model or level.
- Raw text, prompts, run metadata, and exclusions live outside `results.csv`.

