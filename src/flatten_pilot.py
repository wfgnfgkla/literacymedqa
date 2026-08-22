"""
Flatten generate_pilot.py's wide pilot_rewrites.jsonl into the long-format
records harness.run_batch / render_prompt expect.

generate_pilot.py writes ONE ROW PER ITEM, with level_a/level_b/level_c as
columns on that row (100 items -> 100 rows). harness.run_model reads
item["stem"], item["options"], item["gold"], item["item_id"], item.get("level")
-- there is no level_a/level_b/level_c concept in the harness at all. Nothing
in the pipeline previously converted between the two, so nothing could run a
pilot item through a model until this existed.

Usage:
    python src/flatten_pilot.py
    python src/flatten_pilot.py --input data/pilot_rewrites.jsonl \
        --output data/pilot_items_flat.jsonl
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

LEVELS = ("a", "b", "c")


def flatten_pilot_rewrites(rows: list[dict]) -> list[dict]:
    """
    Turn wide pilot rows (one per item, with level_a/level_b/level_c columns)
    into long harness-ready records (one per item x level): item_id, level,
    stem, options, gold.

    item_id is IDENTICAL across the three records emitted for a given input
    row -- only "level" distinguishes them. That's deliberate, not an
    oversight:
      - run_batch's resume-safety key is (item_id, model, level,
        prompt_condition, prompt_hash, config_version); level alone has to
        carry the distinction between a/b/c for the same underlying question.
      - score_results.py's paired McNemar comparison pairs level (a) against
        level (c) BY item_id -- that pairing only works if all three levels
        of one item share one item_id.

    Raises ValueError immediately on the first structurally invalid row
    (missing/empty level text, gold not among the item's own options, options
    missing a letter, duplicate item_id) rather than emitting a partial or
    malformed record for run_model to fail on later, deep inside a real model
    call loop where a crash is expensive to have hit. Same "crash loudly once,
    immediately" principle harness.format_options() already uses, for the
    same reason: a bad ITEM is a data bug upstream, not a transient failure to
    retry past.
    """
    seen_ids: set[str] = set()
    out: list[dict] = []

    for i, row in enumerate(rows):
        item_id = row.get("item_id")
        if not item_id:
            raise ValueError(f"row {i}: missing item_id")
        if item_id in seen_ids:
            raise ValueError(f"duplicate item_id {item_id!r} in pilot rewrites")
        seen_ids.add(item_id)

        options = row.get("options")
        if not options or not all(letter in options for letter in "ABCD"):
            raise ValueError(
                f"item {item_id}: options missing one or more of A/B/C/D: {options!r}"
            )

        gold = row.get("gold")
        if gold not in options:
            raise ValueError(
                f"item {item_id}: gold {gold!r} is not among its own options {list(options)}"
            )

        for level in LEVELS:
            col = f"level_{level}"
            stem = row.get(col)
            if not stem or not stem.strip():
                raise ValueError(f"item {item_id}: {col} is empty or missing")
            out.append(
                {
                    "item_id": item_id,
                    "level": level,
                    "stem": stem,
                    "options": options,
                    "gold": gold,
                    # Traceability back to the rewrite that produced this item's
                    # levels b/c. Deliberately renamed from the source file's
                    # "prompt_hash" -- that field is the REWRITER prompt's hash
                    # (prompts/rewriter_v5.txt). run_model() independently
                    # writes a DIFFERENT hash under the exact key name
                    # "prompt_hash" into every results row -- the eval prompt
                    # template's hash (prompts/eval_plain_v1.txt etc.), not the
                    # rewriter's. Same field name, two unrelated hashes, two
                    # different files. Renaming here is what keeps a human (or
                    # a script) from ever reading one as the other.
                    "rewriter_prompt_hash": row.get("prompt_hash"),
                    "rewriter_config_version": row.get("config_version"),
                    "structural_flags": row.get("structural_flags", []),
                }
            )

    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default="data/pilot_rewrites.jsonl")
    parser.add_argument("--output", default="data/pilot_items_flat.jsonl")
    args = parser.parse_args()

    in_path = Path(args.input)
    rows = [json.loads(line) for line in in_path.read_text(encoding="utf-8").splitlines() if line.strip()]

    flat = flatten_pilot_rewrites(rows)

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        for rec in flat:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    flagged = sum(1 for r in rows if r.get("structural_flags"))
    print(f"  {len(rows)} pilot items -> {len(flat)} harness-ready records ({len(LEVELS)} levels each)")
    print(
        f"  {flagged}/{len(rows)} source items carry structural_flags -- included as-is; "
        f"the smoke test checks plumbing, not the realism/fidelity gates"
    )
    print(f"  wrote {out_path}")


if __name__ == "__main__":
    main()