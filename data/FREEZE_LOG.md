# Freeze log

Every change to `data/medqa_base.jsonl` after the initial freeze is recorded here with a reason.

- 2026-08-11T07:44:55+00:00 — initial freeze, N=500, seed=20260811, sha256=688d04543f51c312
- 2026-08-11T07:49:35+00:00 — REFREEZE N=500 seed=20260811: hold out the 20 verifier-QA items so hand-corrupted text cannot leak into the released benchmark
  - was `688d04543f51c312` now `0f83f2dee50dc7dd`
