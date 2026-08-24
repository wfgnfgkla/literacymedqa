#!/usr/bin/env python3
"""
Task 3, part 3 -- run the fidelity verifier over the pilot, regenerate, then drop.

Reads data/pilot_rewrites.jsonl and NEVER writes to it. Verdicts go to
data/pilot_verdicts.jsonl, one record per item x level (100 items x 3 = 300).

WHY LEVEL (a) IS RUN AT ALL, AND WHY ITS NUMBER IS NOT A DROP RATE

Level (a) is the unmodified MedQA stem, copied verbatim by generate_pilot.py and
never sent to a rewriter. Verifying it means asking the verifier whether a string
differs from itself, so the honest expectation is a FAIL rate of zero. It is run
as a NEGATIVE CONTROL: any level (a) failure is a verifier false positive, not a
data defect, and it puts a floor under how much of the (b) and (c) numbers can be
believed. A level (a) FAIL is therefore reported and never regenerated -- there is
nothing to regenerate, the text is the source of truth.

For the same reason the three rates are reported separately and never pooled. A
pooled rate over 300 mixes one control with two treatment conditions, and the
resulting number would mean nothing in the paper.

REGENERATION

A level (b) or (c) FAIL gets up to `generation.max_regeneration_attempts` fresh
rewrites, using the same pinned rewriter and the same frozen prompt the pilot was
generated with (asserted at startup by prompt hash, not assumed). Still failing
after that, the level-instance is DROPPED rather than retried further: unlimited
retries select for rewrites the verifier happens to accept, which biases the
surviving set exactly where the benchmark is supposed to be neutral.

One rewriter call returns BOTH levels, so when (b) and (c) both fail, one
regeneration serves both. Only the failing level's text is taken -- a passing
level is never replaced by a later call, so a surviving pair can come from two
different calls. `regen_attempts` and `regenerated` record that per level.

REVIEW IS NOT A DROP

The verifier returns PASS, FAIL or REVIEW, where REVIEW exists so "unsure" is not
silently rounded to PASS. REVIEW items are kept, counted separately, and are the
natural population for the manual audit. Treating them as drops would discard
items over verifier uncertainty; treating them as passes would launder it.

    python src/verify_pilot.py --limit 5
    python src/verify_pilot.py
    python src/verify_pilot.py --resume
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path

try:
    import yaml
except ImportError as exc:  # pragma: no cover
    raise SystemExit("pyyaml is required: pip install pyyaml") from exc

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cost_tracker import CostTracker  # noqa: E402
from verifier import Verifier  # noqa: E402
import generate_pilot as gp  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_IN = ROOT / "data" / "pilot_rewrites.jsonl"
DEFAULT_OUT = ROOT / "data" / "pilot_verdicts.jsonl"
LEVELS = ("a", "b", "c")
GENERATED = ("b", "c")          # (a) is copied, never generated, never regenerated


def write_row(path: Path, row: dict) -> None:
    """Append and fsync, so a crash costs the current item and not the run."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def already_done(path: Path) -> set[tuple[str, str]]:
    if not path.exists():
        return set()
    done = set()
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue           # torn tail from a hard kill; it will be redone
            if "item_id" in r and "level" in r:
                done.add((r["item_id"], r["level"]))
    return done


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    ap.add_argument("--input", default=str(DEFAULT_IN))
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--backend", default="openrouter",
                    help="Backend to verify with. Default matches the one that "
                         "passed the adversarial gate; changing it invalidates that gate.")
    ap.add_argument("--limit", type=int, default=None, help="First N items only.")
    ap.add_argument("--resume", action="store_true",
                    help="Skip (item_id, level) pairs already in the output.")
    ap.add_argument("--no-regen", action="store_true",
                    help="Verify only; do not regenerate FAILs. A FAIL is then recorded "
                         "as regen_pending, NOT as a drop -- a drop means 3 attempts were "
                         "actually spent and failed, and reporting an unattempted item as "
                         "dropped would overstate the drop rate.")
    args = ap.parse_args()

    cfg = yaml.safe_load(open(args.config, encoding="utf-8"))
    rewriter = cfg["models"]["rewriter"]
    max_attempts = int(cfg["generation"]["max_regeneration_attempts"])

    rows = [json.loads(l) for l in open(args.input, encoding="utf-8") if l.strip()]
    template, prompt_hash = gp.load_prompt(ROOT / cfg["generation"]["rewriter_prompt"])

    # Regeneration is only legitimate if it reproduces the pilot's own conditions.
    # Assert that rather than trusting it: a silently different prompt would make
    # regenerated items incomparable to the ones that passed first time.
    recorded = {r.get("prompt_hash") for r in rows if "prompt_hash" in r}
    if recorded and prompt_hash not in recorded:
        raise SystemExit(
            f"Frozen prompt has changed since the pilot was generated.\n"
            f"  config prompt : {cfg['generation']['rewriter_prompt']} -> {prompt_hash[:16]}\n"
            f"  pilot recorded: {', '.join(sorted(h[:16] for h in recorded))}\n"
            "Regenerating against a different prompt would make the regenerated items "
            "incomparable to the rest of the pilot. Refusing."
        )

    out_path = Path(args.out)
    skip = already_done(out_path) if args.resume else set()
    if args.limit is not None:
        rows = rows[:args.limit]

    v = Verifier(backend=args.backend)
    tracker = CostTracker.from_config(args.config)
    client = None                  # rewriter client, built lazily; only if a regen is needed

    print(f"  input        {args.input} ({len(rows)} items)")
    print(f"  verifier     {v.model}  backend={args.backend}  prompt={v.prompt_sha[:12]}")
    print(f"  rewriter     {rewriter['name']} @ {rewriter['version']}  prompt={prompt_hash[:12]}")
    print(f"  max attempts {max_attempts} then DROP")
    print(f"  output       {out_path}")
    if skip:
        print(f"  resume       skipping {len(skip)} already-verified level-instances")
    print()

    stats = {lvl: Counter() for lvl in LEVELS}
    served = Counter()
    regen_calls = 0

    def verify(original: str, rewrite: str, options: dict, gold: str, item_id: str, level: str):
        r = v.check(original, rewrite, options, gold)
        if r.served_by:
            served[r.served_by] += 1
        u = getattr(v, "last_usage", None) or {}
        if u:
            tracker.log(stage="verify", model=v.model,
                        input_tokens=u.get("prompt_tokens", 0) or 0,
                        output_tokens=u.get("completion_tokens", 0) or 0,
                        item_id=item_id, level=level, served_by=r.served_by)
        return r

    for i, row in enumerate(rows, 1):
        item_id = row["item_id"]
        original = row["level_a"]
        options = row.get("options") or {}
        gold = row.get("gold")
        if "error" in row or not gold or not options:
            print(f"  [{i}/{len(rows)}] {item_id}  SKIPPED (incomplete source row)")
            continue

        verdicts: dict[str, object] = {}
        for lvl in LEVELS:
            if (item_id, lvl) in skip:
                continue
            verdicts[lvl] = verify(original, row[f"level_{lvl}"], options, gold, item_id, lvl)

        # Regenerate only the generated levels that FAILed. One call yields both.
        attempts = {lvl: 0 for lvl in GENERATED}
        text = {lvl: row[f"level_{lvl}"] for lvl in GENERATED}
        regenerated = {lvl: False for lvl in GENERATED}
        failing = [l for l in GENERATED if l in verdicts and verdicts[l].verdict == "FAIL"]
        blocked = set()

        while (not args.no_regen and failing
               and max(attempts[l] for l in failing) < max_attempts):
            try:
                if client is None:
                    client = gp.build_client()
                raw, resp = gp.call_rewriter(client, rewriter,
                                             gp.render_prompt(template, original))
                regen_calls += 1
                usage = getattr(resp, "usage", None)
                if usage is not None:
                    tracker.log(stage="rewrite", model=rewriter["name"],
                                input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                                output_tokens=getattr(usage, "completion_tokens", 0) or 0,
                                item_id=item_id, level="regen",
                                served_by=gp._extract_served_by(resp))
                new = dict(zip(GENERATED, gp.parse_rewrite(raw)))
            except Exception as exc:                     # noqa: BLE001
                # The rewriter is unreachable (its SDK and the verifier's urllib need
                # incompatible SSL setups on this machine -- see CHANGELOG). Do NOT
                # burn attempts on that: an environment failure is not the item
                # failing to be rewritable, and counting it as one would inflate the
                # drop rate with something the data had no part in.
                blocked.update(failing)
                print(f"      regen unavailable for {item_id}: {type(exc).__name__}: "
                      f"{str(exc)[:80]}")
                break

            for lvl in list(failing):
                attempts[lvl] += 1
                cand = new[lvl]
                r = verify(original, cand, options, gold, item_id, lvl)
                if r.verdict != "FAIL":
                    text[lvl] = cand
                    verdicts[lvl] = r
                    regenerated[lvl] = True
                    failing.remove(lvl)
                elif attempts[lvl] >= max_attempts:
                    verdicts[lvl] = r
                    failing.remove(lvl)

        for lvl in LEVELS:
            if lvl not in verdicts:
                continue
            r = verdicts[lvl]
            dropped = (lvl in GENERATED and r.verdict == "FAIL"
                       and attempts.get(lvl, 0) >= max_attempts and lvl not in blocked)
            pending = (lvl in GENERATED and r.verdict == "FAIL" and not dropped)
            stats[lvl][r.verdict] += 1
            if dropped:
                stats[lvl]["DROPPED"] += 1
            if pending:
                stats[lvl]["PENDING"] += 1
            write_row(out_path, {
                "item_id": item_id,
                "level": lvl,
                "verdict": r.verdict,
                "dropped": dropped,
                "regen_pending": pending,
                "regen_blocked": lvl in blocked,
                "regen_attempts": attempts.get(lvl, 0),
                "regenerated": regenerated.get(lvl, False),
                "is_control": lvl == "a",
                "clinical_fact_changed": r.clinical_fact_changed,
                "same_answer_letter": r.same_answer_letter,
                "change_type": r.change_type,
                "what_changed": r.what_changed,
                "evidence_original": r.evidence_original,
                "evidence_rewrite": r.evidence_rewrite,
                "jargon_removed": r.jargon_removed,
                "confidence": r.confidence,
                "deterministic_flags": r.deterministic_flags,
                "structural_flags": row.get("structural_flags", []),
                "verifier_model": r.model,
                "verifier_backend": r.backend,
                "verifier_prompt_sha256": r.prompt_sha256,
                "served_by": r.served_by,
                "rewriter_prompt_hash": prompt_hash,
                "config_version": cfg["run"]["config_version"],
            })
        line = "  ".join(f"{l}:{verdicts[l].verdict}" for l in LEVELS if l in verdicts)
        extra = f"  regen={ {k: v for k, v in attempts.items() if v} }" if any(attempts.values()) else ""
        print(f"  [{i}/{len(rows)}] {item_id}  {line}{extra}")

    print()
    print("  DROP RATE PER LEVEL -- reported separately, never pooled")
    print(f"  {'level':22s} {'n':>4} {'PASS':>6} {'REVIEW':>7} {'FAIL':>6} {'DROPPED':>8}  drop rate")
    for lvl in LEVELS:
        s = stats[lvl]
        n = s["PASS"] + s["REVIEW"] + s["FAIL"]
        if not n:
            continue
        label = {"a": "(a) control, copied", "b": "(b) plain", "c": "(c) low literacy"}[lvl]
        rate = f"{s['DROPPED'] / n * 100:.0f}%" if lvl in GENERATED else "n/a (control)"
        print(f"  {label:22s} {n:>4} {s['PASS']:>6} {s['REVIEW']:>7} {s['FAIL']:>6} "
              f"{s['DROPPED']:>8}  {rate}")
    pend = sum(stats[l]["PENDING"] for l in GENERATED)
    if pend:
        print()
        print(f"  {pend} FAILed level-instance(s) are regen_pending, NOT dropped: no "
              "regeneration attempt was spent on them. The drop rates above are therefore "
              "a LOWER BOUND until regeneration runs.")
    if stats["a"]["FAIL"]:
        print(f"\n  WARNING: level (a) failed {stats['a']['FAIL']} times. Level (a) is the "
              "unmodified stem verified against itself, so these are verifier FALSE "
              "POSITIVES, not data defects, and they bound how far the (b)/(c) numbers "
              "can be trusted.")
    print(f"\n  regeneration calls: {regen_calls}")
    print(f"  served_by: {dict(served)}"
          + ("" if len(served) <= 1 else "   ** VARIES -- pin did not hold **"))
    print()
    tracker.summary()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
