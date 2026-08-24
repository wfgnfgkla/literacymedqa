# Adversarial verifier gate -- `openrouter`

Run 2026-08-24T01:25:26+00:00 · model `qwen/qwen-2.5-72b-instruct` · prompt `2ab5896afe5a`

**Gate: PASS**  (requires sensitivity 1.00 and FPR <= 0.10)

| metric | value | 95% CI |
|---|---|---|
| sensitivity (corruptions caught) | 20/20 = 1.00 | 0.84–1.00 |
| false positive rate (clean failed) | 0/20 = 0.00 | 0.00–0.16 |
| clean sent to manual review | 0/20 | |

## By corruption category

| category | caught |
|---|---|
| altered_number | 4/4 |
| changed_demographic | 4/4 |
| changed_timeline | 4/4 |
| negation_flip | 4/4 |
| unit_change | 4/4 |

## By effect on the correct answer

| corruption | caught |
|---|---|
| moves the correct letter | 5/5 |
| leaves the letter intact | 15/15 |

## Reading this

At n=20 per arm a perfect score still leaves the true sensitivity as low as
~0.83 at 95% confidence. The gate rules out a broken verifier; it does not
certify a good one. Passing it is a precondition for generating the full
set, not evidence that the set is clean.
