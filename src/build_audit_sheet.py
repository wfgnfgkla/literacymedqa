#!/usr/bin/env python3
"""
Task 3, part 4 -- build the blind manual fidelity audit sheet for Week 3.

Writes TWO files, and the separation is the whole point:

    data/pilot_audit_sheet.csv   goes to the reviewer. Carries the original, the
                                 rewrite, the options and the gold letter, and two
                                 EMPTY columns for their judgement. It contains no
                                 verifier output of any kind.
    data/pilot_audit_key.csv     stays with whoever scores the audit. Carries the
                                 verifier's verdict per audit_id.

The verdict is in a separate FILE rather than a hidden column or a far-right column,
because a hidden column is one unhide away and a far-right column is one scroll away.
The point of the audit is to measure whether a human agrees with the verifier
(gates.fidelity.min_filter_human_kappa); a reviewer who has seen the verdict cannot
produce that number, and would not know their own judgement had been anchored.

SAMPLING

`gates.fidelity.manual_audit_n` rows are drawn from the GENERATED level-instances
only -- levels (b) and (c), 200 of them. Level (a) is the unmodified stem copied
verbatim, so auditing it would ask a reviewer to compare a string with itself and
would spend the audit budget measuring nothing.

The draw is a simple random sample at `run.seed`, not stratified toward the items
the verifier disliked. Oversampling disagreements would inflate apparent
disagreement and bias kappa, and at 150 of 200 the sample is 75% of the population
anyway, so it carries the non-PASS instances in proportion without help.

Rows are shuffled before audit_ids are assigned, so neither the id order nor the
file order encodes the verdict, the level, or the source item order.

    python src/build_audit_sheet.py
"""
from __future__ import annotations

import argparse
import csv
import json
import random
import sys
from pathlib import Path

try:
    import yaml
except ImportError as exc:  # pragma: no cover
    raise SystemExit("pyyaml is required: pip install pyyaml") from exc

ROOT = Path(__file__).resolve().parents[1]
GENERATED = ("b", "c")

SHEET_COLS = ["audit_id", "level", "original_vignette", "patient_rewrite",
              "answer_options", "correct_letter",
              "REVIEWER_clinical_fact_changed", "REVIEWER_notes"]
KEY_COLS = ["audit_id", "item_id", "level", "verifier_verdict", "verifier_change_type",
            "verifier_confidence", "verifier_what_changed", "structural_flags",
            "dropped", "regen_attempts"]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    ap.add_argument("--rewrites", default=str(ROOT / "data" / "pilot_rewrites.jsonl"))
    ap.add_argument("--verdicts", default=str(ROOT / "data" / "pilot_verdicts.jsonl"))
    ap.add_argument("--sheet", default=str(ROOT / "data" / "pilot_audit_sheet.csv"))
    ap.add_argument("--key", default=str(ROOT / "data" / "pilot_audit_key.csv"))
    args = ap.parse_args()

    cfg = yaml.safe_load(open(args.config, encoding="utf-8"))
    n_target = int(cfg["gates"]["fidelity"]["manual_audit_n"])
    seed = int(cfg["run"]["seed"])

    pilot = {r["item_id"]: r for r in
             (json.loads(l) for l in open(args.rewrites, encoding="utf-8") if l.strip())}
    verdicts = [json.loads(l) for l in open(args.verdicts, encoding="utf-8") if l.strip()]

    pool = [v for v in verdicts if v["level"] in GENERATED and not v.get("dropped")]
    if len(pool) < n_target:
        raise SystemExit(
            f"Only {len(pool)} auditable level-instances available, need {n_target}. "
            "Refusing to silently ship a smaller audit than the gate specifies."
        )

    rng = random.Random(seed)
    sample = rng.sample(pool, n_target)
    rng.shuffle(sample)          # id order must not encode verdict or level

    sheet_path, key_path = Path(args.sheet), Path(args.key)
    sheet_path.parent.mkdir(parents=True, exist_ok=True)

    with open(sheet_path, "w", encoding="utf-8", newline="") as fs, \
         open(key_path, "w", encoding="utf-8", newline="") as fk:
        ws, wk = csv.writer(fs), csv.writer(fk)
        ws.writerow(SHEET_COLS)
        wk.writerow(KEY_COLS)
        for i, v in enumerate(sample, 1):
            audit_id = f"AUD-{i:03d}"
            row = pilot[v["item_id"]]
            ws.writerow([
                audit_id, v["level"], row["level_a"], row[f"level_{v['level']}"],
                json.dumps(row.get("options") or {}, ensure_ascii=False),
                row.get("gold"), "", "",
            ])
            wk.writerow([
                audit_id, v["item_id"], v["level"], v["verdict"], v["change_type"],
                v["confidence"], v["what_changed"],
                ";".join(v.get("structural_flags") or []),
                v.get("dropped"), v.get("regen_attempts"),
            ])

    # Assert the blindness property rather than trusting the column list above.
    # The reviewer's own input columns are exempt by prefix -- REVIEWER_* is where
    # their judgement goes, and "REVIEWER" legitimately contains "review".
    import csv as _csv
    with open(sheet_path, encoding="utf-8", newline="") as fh:
        header = next(_csv.reader(fh))
    for col in header:
        if col.upper().startswith("REVIEWER_"):
            continue
        low = col.lower()
        for banned in ("verdict", "confidence", "change_type", "dropped", "regen",
                       "verifier", "flag"):
            if banned in low:
                raise SystemExit(f"audit sheet column {col!r} leaks verifier output")

    from collections import Counter
    print(f"  sheet   {sheet_path}   {len(sample)} rows, {len(SHEET_COLS)} cols, no verifier output")
    print(f"  key     {key_path}     verdicts only, keep away from the reviewer")
    print(f"  drawn   {len(sample)} of {len(pool)} auditable (b)/(c) instances, seed {seed}")
    print(f"  levels  {dict(Counter(v['level'] for v in sample))}")
    print(f"  hidden verdict mix: {dict(Counter(v['verdict'] for v in sample))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
