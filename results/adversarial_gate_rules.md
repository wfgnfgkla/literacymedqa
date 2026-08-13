# Adversarial verifier gate -- `rules`

Run 2026-08-13T22:36:01+00:00 · model `rules-only` · prompt `2ab5896afe5a`

**Gate: FAIL**  (requires sensitivity 1.00 and FPR <= 0.10)

| metric | value | 95% CI |
|---|---|---|
| sensitivity (corruptions caught) | 15/20 = 0.75 | 0.53–0.89 |
| false positive rate (clean failed) | 0/20 = 0.00 | 0.00–0.16 |
| clean sent to manual review | 0/20 | |

## By corruption category

| category | caught |
|---|---|
| altered_number | 4/4 |
| changed_demographic | 3/4 |
| changed_timeline | 4/4 |
| negation_flip | 0/4 |
| unit_change | 4/4 |

## By effect on the correct answer

| corruption | caught |
|---|---|
| moves the correct letter | 2/5 |
| leaves the letter intact | 13/15 |

## Missed corruptions

- `LMQ-668e70c0450a#corrupt` (changed_demographic) — ancestry is the answer-bearing fact in this item
- `LMQ-80f90c090fc8#corrupt` (negation_flip) — acyanotic to cyanotic
- `LMQ-4a27deb83dad#corrupt` (negation_flip) — flexible to rigid deformity; reassurance no longer correct
- `LMQ-e60047b5afaf#corrupt` (negation_flip) — compensated to decompensated cirrhosis
- `LMQ-bcaad07a6cc6#corrupt` (negation_flip) — single negation flip that shifts the differential without moving the letter

## Reading this

At n=20 per arm a perfect score still leaves the true sensitivity as low as
~0.83 at 95% confidence. The gate rules out a broken verifier; it does not
certify a good one. Passing it is a precondition for generating the full
set, not evidence that the set is clean.
