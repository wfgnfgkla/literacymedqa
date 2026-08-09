"""
Merge per-person cost logs into one team total.

The task says a SHARED cost tracker. cost_tracker.py writes to a local JSONL, which
means five people running calls on five machines produce five partial logs and no
team number. This merges them.

Workflow:
  1. Each person runs their own calls. Their log lands at logs/api_calls.jsonl.
  2. Before each weekly meeting, everyone commits their log as
     logs/shared/api_calls_<name>.jsonl and pushes.
  3. Anyone runs:  python src/merge_costs.py
     -> writes logs/api_calls_merged.jsonl and prints the team total.

Deduplication is by (ts, stage, model, input_tokens, output_tokens, item_id), so if
two people accidentally commit overlapping logs the total does not double-count.

Note that logs/*.jsonl is gitignored by default. The shared/ subdirectory is force-added:

    git add -f logs/shared/api_calls_yourname.jsonl
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

SHARED_DIR = Path("logs/shared")
OUTPUT = Path("logs/api_calls_merged.jsonl")

DEDUP_KEYS = ("ts", "stage", "model", "input_tokens", "output_tokens", "item_id")


def load(path: Path) -> list[dict]:
    rows = []
    with open(path, encoding="utf-8") as fh:
        for i, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                print(f"  warning: {path.name} line {i} is malformed, skipped")
    return rows


def dedup_key(r: dict) -> tuple:
    return tuple(r.get(k) for k in DEDUP_KEYS)


def main() -> int:
    if not SHARED_DIR.exists():
        print(f"No {SHARED_DIR}/ yet. Create it and have each person commit their log:")
        print("  mkdir -p logs/shared")
        print("  cp logs/api_calls.jsonl logs/shared/api_calls_yourname.jsonl")
        print("  git add -f logs/shared/api_calls_yourname.jsonl && git commit && git push")
        return 1

    files = sorted(SHARED_DIR.glob("api_calls_*.jsonl"))
    if not files:
        print(f"No api_calls_*.jsonl files in {SHARED_DIR}/.")
        return 1

    seen: set[tuple] = set()
    merged: list[dict] = []
    per_person: dict[str, dict] = {}
    dupes = 0

    for f in files:
        person = f.stem.replace("api_calls_", "")
        rows = load(f)
        kept = 0
        person_usd = 0.0
        person_unpriced = 0
        for r in rows:
            k = dedup_key(r)
            if k in seen:
                dupes += 1
                continue
            seen.add(k)
            r["_logged_by"] = person
            merged.append(r)
            kept += 1
            if r.get("cost_usd") is None:
                person_unpriced += 1
            else:
                person_usd += r["cost_usd"]
        per_person[person] = {
            "calls": kept,
            "usd": person_usd,
            "unpriced": person_unpriced,
        }

    merged.sort(key=lambda r: r.get("ts", ""))
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT, "w", encoding="utf-8") as fh:
        for r in merged:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")

    # --- report ---
    by_stage: dict[str, dict] = defaultdict(lambda: {"calls": 0, "usd": 0.0})
    total_usd = 0.0
    total_unpriced = 0
    for r in merged:
        b = by_stage[r["stage"]]
        b["calls"] += 1
        if r.get("cost_usd") is None:
            total_unpriced += 1
        else:
            b["usd"] += r["cost_usd"]
            total_usd += r["cost_usd"]

    print(f"\nMerged {len(files)} log file(s) -> {OUTPUT}")
    if dupes:
        print(f"Skipped {dupes:,} duplicate record(s).")

    print(f"\n{'person':<16}{'calls':>10}{'USD':>10}")
    print("-" * 36)
    for p in sorted(per_person):
        d = per_person[p]
        print(f"{p:<16}{d['calls']:>10,}{d['usd']:>10.2f}")

    print(f"\n{'stage':<16}{'calls':>10}{'USD':>10}")
    print("-" * 36)
    for s in sorted(by_stage):
        b = by_stage[s]
        print(f"{s:<16}{b['calls']:>10,}{b['usd']:>10.2f}")
    print("-" * 36)
    print(f"{'TEAM TOTAL':<16}{len(merged):>10,}{total_usd:>10.2f}")

    if total_unpriced:
        print(
            f"\n  {total_unpriced:,} call(s) have no price in config.yaml. "
            "The total above is a LOWER BOUND."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
