# Pilot Week 3 Headline Numbers

_Pilot only: 100 items x 3 levels for `openai/gpt-4o` with the plain prompt. These numbers show that the scoring path works as runs land, but they are not the final full-study results._

| Model | Prompt | n paired | Acc(a) clinical | Acc(b) plain | Acc(c) low-lit | Gap a-c | Right->wrong | McNemar p |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| `openai/gpt-4o` | `plain` | 100 | 90.0% | 77.0% | 76.0% | +14.0% | 16.7% | 0.000519 |

## What This Completes Now

- Scores the pilot run as it lands, writing:
  - `data/pilot_scoring/accuracy.csv`
  - `data/pilot_scoring/literacy_gap.csv`
  - `data/pilot_scoring/mcnemar.csv`
- Produces pilot headline numbers: accuracy per level, literacy gap, right-to-wrong flip rate, and paired McNemar p-value.
- Produces the draft table format in:
  - `data/pilot_draft_results_table.md`
  - `data/pilot_draft_results_table.csv`

## Still Needed For The Full Week 3 Task

- Run every planned model on the frozen full dataset, not just `openai/gpt-4o` on the 100-item pilot.
- Run the level-(c) clarify-first intervention.
- Rebuild the draft/final table once `clarify_first` rows exist so `Acc(c) clarify` and `Clarify recovery` are populated.
