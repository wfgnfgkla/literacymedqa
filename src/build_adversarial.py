#!/usr/bin/env python3
"""
Task 3, part 1 -- expand the authored pairs into the adversarial test set.

Each authored pair produces TWO cases from the same base item:
    <id>#clean    faithful low-literacy rewrite, expected verdict PASS
    <id>#corrupt  same text, one injected error,  expected verdict FAIL

Twenty corruptions on their own only measure whether the verifier fires. They
cannot measure whether it fires at the right times: a verifier that flags every
rewrite scores 20/20 and is useless. The clean twins are the control, and they
are byte-identical to the corrupt twins apart from the injected edit, so a flag
on a clean twin is unambiguously a reaction to the paraphrase.

Also writes data/adversarial/held_out_ids.txt so these items can be excluded
from the released benchmark -- hand-corrupted text should never leak into it,
and the verifier should not be tuned on items it will later be judging.

    python src/build_adversarial.py
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sample_and_freeze import SOURCE, canonical_key, item_id  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
PAIRS = ROOT / "src" / "adversarial_pairs.json"
OUT = ROOT / "data" / "adversarial" / "adversarial_set.jsonl"
HELD_OUT = ROOT / "data" / "adversarial" / "held_out_ids.txt"

REQUIRED_CATEGORIES = {
    "altered_number", "unit_change", "changed_timeline",
    "changed_demographic", "negation_flip",
}


def main() -> None:
    rows = [json.loads(l) for l in SOURCE.open(encoding="utf-8") if l.strip()]
    spec = json.loads(PAIRS.read_text(encoding="utf-8"))
    pairs = spec["pairs"]

    errors, cases, held = [], [], []
    for p in pairs:
        src = rows[p["source_index"]]
        iid = item_id(src)
        clean = p["clean_rewrite"]

        n = clean.count(p["find"])
        if n != 1:
            errors.append(f"{iid} idx={p['source_index']}: find string occurs {n}x, need exactly 1: {p['find']!r}")
            continue
        corrupt = clean.replace(p["find"], p["replace"])
        if corrupt == clean:
            errors.append(f"{iid}: corruption is a no-op")
            continue
        if p["category"] not in REQUIRED_CATEGORIES:
            errors.append(f"{iid}: unknown category {p['category']}")
            continue

        held.append(iid)
        common = {
            "base_item_id": iid,
            "source_index": p["source_index"],
            "original": src["question"],
            "options": src["options"],
            "gold_letter": src["answer_idx"],
            "corruption_category": p["category"],
            "changes_gold_answer": p["changes_gold_answer"],
            "note": p["note"],
        }
        cases.append({**common, "case_id": f"{iid}#clean", "condition": "clean",
                      "rewrite": clean, "expected_verdict": "PASS", "injected_edit": None})
        cases.append({**common, "case_id": f"{iid}#corrupt", "condition": "corrupt",
                      "rewrite": corrupt, "expected_verdict": "FAIL",
                      "injected_edit": {"find": p["find"], "replace": p["replace"]}})

    if errors:
        print("BUILD FAILED\n  " + "\n  ".join(errors))
        sys.exit(1)

    counts = Counter(p["category"] for p in pairs)
    missing = REQUIRED_CATEGORIES - set(counts)
    if missing:
        print(f"BUILD FAILED: no coverage for {sorted(missing)}")
        sys.exit(1)

    OUT.write_text("".join(json.dumps(c, ensure_ascii=False) + "\n" for c in cases), encoding="utf-8")
    HELD_OUT.write_text("\n".join(sorted(set(held))) + "\n", encoding="utf-8")

    print(f"Built {len(cases)} cases from {len(pairs)} base items -> {OUT.relative_to(ROOT)}")
    for c, k in sorted(counts.items()):
        flips = sum(1 for p in pairs if p["category"] == c and p["changes_gold_answer"])
        print(f"  {c:22s} {k} corruptions ({flips} move the correct letter)")
    print(f"  answer-preserving corruptions: {sum(1 for p in pairs if not p['changes_gold_answer'])}/{len(pairs)}")
    print(f"Held-out item ids -> {HELD_OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
