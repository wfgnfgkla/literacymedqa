"""
Pilot smoke test: run the full pipeline end to end on the 100-item pilot,
straight through to a scored results file.

This is Patrick's Week 2 task ("Smoke-test the full pipeline end to end on the
pilot: 100 items x 3 levels x every model, straight through to a scored
results file. Measure wall-clock time and dollar cost per 100 items."),
covered here since Patrick is away.

Chain:
    data/pilot_rewrites.jsonl
      -> flatten_pilot.flatten_pilot_rewrites()      (100 items -> 300 records)
      -> harness.run_batch()                          (append to results_file)
      -> score_results.score_results()                (accuracy / gap / McNemar)

Defaults to --mock: zero cost, deterministic fake letters, exercises every line
of plumbing (schema, file I/O, scoring) without spending real API money. This
is step 2 of the plan. Pass --live for step 3 (real calls, real dollars,
real wall-clock -- the number that actually goes to Kiran).

Writes to data/pilot_results.jsonl and scores into data/pilot_scoring/ -- NOT
data/results.jsonl / data/scoring/, the paths the real main run uses. See
harness.run_batch's `results_file` docstring for why pilot output must never
share a file with the real run's own resume-safety check.

Usage:
    python src/pilot_smoke_test.py                    # mock, safe, $0
    python src/pilot_smoke_test.py --live              # real calls, real $
    python src/pilot_smoke_test.py --live --models llama3 medgemma
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cost_tracker import CostTracker  # noqa: E402
from flatten_pilot import flatten_pilot_rewrites  # noqa: E402
from harness import _load_config, _load_prompt, available_models, run_batch  # noqa: E402
import score_results as scoring  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--pilot-file", default="data/pilot_rewrites.jsonl")
    parser.add_argument(
        "--results-file",
        default="data/pilot_results.jsonl",
        help="Kept separate from config.yaml's logging.results_file on purpose.",
    )
    parser.add_argument("--out-dir", default="data/pilot_scoring")
    parser.add_argument("--prompt-template", default="prompts/eval_plain_v1.txt")
    parser.add_argument("--prompt-condition", default="plain")
    parser.add_argument(
        "--models",
        nargs="+",
        default=None,
        help="Model ids to attempt (default: every id in config.yaml's models.evaluated).",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Make real API calls and spend real money. Default is mock (0 cost).",
    )
    parser.add_argument("--sleep-between-calls", type=float, default=0.0)
    args = parser.parse_args(argv)

    mock = not args.live
    cfg = _load_config(args.config)
    model_ids = args.models or [m["id"] for m in cfg["models"]["evaluated"]]

    print(f"{'MOCK' if mock else 'LIVE'} pilot smoke test -- requested models: {model_ids}")
    ready, skip_reasons = available_models(cfg, mock=mock)
    for msg in skip_reasons:
        if msg.split(":")[0] in model_ids:
            print(f"  SKIP  {msg}")
    will_run = [m for m in model_ids if m in ready]
    print(f"  will run: {will_run or '(none)'}")
    if not will_run:
        print("  Nothing to run -- no requested model is ready.", file=sys.stderr)
        return 1

    pilot_path = Path(args.pilot_file)
    pilot_rows = [
        json.loads(line)
        for line in pilot_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    items = flatten_pilot_rewrites(pilot_rows)
    print(f"  {len(pilot_rows)} pilot items -> {len(items)} harness records")

    start = time.monotonic()
    counts = run_batch(
        items,
        will_run,
        args.prompt_template,
        config_path=args.config,
        stage="pilot",
        prompt_condition=args.prompt_condition,
        mock=mock,
        sleep_between_calls=args.sleep_between_calls,
        results_file=args.results_file,
    )
    elapsed = time.monotonic() - start

    print(f"\n  run={counts['run']}  skipped={counts['skipped']}  failed={counts['failed']}")
    if counts["run"]:
        print(f"  wall-clock: {elapsed:.1f}s total, {elapsed / counts['run']:.2f}s/call")
    else:
        print(f"  wall-clock: {elapsed:.1f}s (nothing new run -- already done, or all skipped/failed)")

    # Score ONLY this run's own rows. data/pilot_results.jsonl can (deliberately,
    # see run_batch's docstring) accumulate rows from more than one config_version
    # across successive pilot iterations -- score_results.validate_rows() raises
    # on a duplicate (item_id, model, level, prompt_condition) key, which is
    # exactly what two fingerprints for the same base key looks like. Filtering
    # to the CURRENT fingerprint before scoring is what keeps the reported
    # accuracy numbers describing only the pilot version actually in hand.
    _, current_prompt_hash = _load_prompt(args.prompt_template)
    current_config_version = cfg.get("run", {}).get("config_version")

    all_rows = scoring.load_results(args.results_file)
    current_rows = [
        r
        for r in all_rows
        if r["prompt_condition"] == args.prompt_condition
        and r.get("prompt_hash") == current_prompt_hash
        and r.get("config_version") == current_config_version
    ]
    stale_n = len([r for r in all_rows if r["prompt_condition"] == args.prompt_condition]) - len(current_rows)
    if stale_n:
        print(
            f"  {stale_n} row(s) in {args.results_file} are from a different "
            f"prompt_hash/config_version (an earlier pilot run) -- excluded from "
            f"scoring below; only config_version={current_config_version} counts."
        )

    if current_rows:
        tables = scoring.score_results(current_rows, prompt_condition=args.prompt_condition)
        scoring.write_tables(tables, args.out_dir)
        for name, table_rows in tables.items():
            print(f"  {name}: {len(table_rows)} rows -> {args.out_dir}/{name}.csv")
    else:
        print("  Nothing to score yet for this prompt_condition/fingerprint.")

    print()
    CostTracker.from_config(args.config).summary()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
