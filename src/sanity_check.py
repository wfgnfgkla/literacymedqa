"""
50-item sanity check.

"If a model is far off, the harness is broken, not the model." -- run this BEFORE
trusting the harness for anything real, and again any time harness.py changes.

    python src/sanity_check.py            # real API calls
    python src/sanity_check.py --mock     # no keys, no GPU, tests the full code path

NOTE ON SOURCE: this pulls 50 items directly from GBaker/MedQA-USMLE-4-options on
Hugging Face as a quick smoke test. This is NOT Dong's official frozen
data/medqa_base.jsonl (step 1 in the README's run order) -- once that file exists,
prefer it for anything beyond this harness check, so the smoke test and the real
pipeline are drawing from the same frozen item set.

TARGETS (see CHECKS below for where these numbers come from and why the bands are
this wide): medgemma ~64.4% published on MedQA (n=50 acceptance band 48-80%, since
the 95% CI at n=50 is roughly +/-13pts -- this is a coarse tripwire, not a precision
test). llama3: no single clean published MedQA figure for 3.3-70B specifically;
Llama-3.1-70B class models land ~78-80% on the 4-option variant, so band is 65-92%.
gpt_closed is skipped until the mentor picks a model (name is null in config.yaml).

The parse-failure rate is the MORE sensitive signal: a broken prompt or a broken
parser shows up there long before accuracy visibly drifts. >5% unparseable fails
the check regardless of what accuracy says.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import available_models, catchable_run_errors, run_model  # noqa: E402
from cost_tracker import CostTracker  # noqa: E402

PARSE_FAILURE_MAX_RATE = 0.05

# Fixed at 50 per the task spec ("Run all models on 50 original MedQA items"). This is
# DELIBERATELY NOT cfg["run"]["pilot_n"] (=100) -- that field controls the REWRITER's
# pilot-generation batch (README step 3, a different pipeline stage). Same word
# "pilot", two unrelated numbers; conflating them was a bug caught during testing.
SANITY_CHECK_N = 50

# (min_acc, max_acc, published_point_estimate, source_note)
TARGETS: dict[str, tuple[float, float, float, str]] = {
    "medgemma": (0.48, 0.80, 0.644, "MedGemma-4B-it published MedQA accuracy (Sellergren et al. 2025)"),
    "llama3": (0.65, 0.92, 0.79, "no single clean published figure for 3.3-70B; Llama-3.1-70B class ~78-80% on the 4-option variant"),
    "gpt_closed": (0.0, 1.0, None, "no target yet -- model not chosen"),
}


def load_medqa_sample(n: int, seed: int) -> list[dict]:
    """
    Load `n` items deterministically from the MedQA US 4-option test split.

    Field mapping (confirmed against the live dataset schema, not guessed):
        question    -> stem
        options     -> already {"A": ..., "B": ..., "C": ..., "D": ...}
        answer_idx  -> gold letter
    """
    from datasets import load_dataset

    ds = load_dataset("GBaker/MedQA-USMLE-4-options", split="test")
    idx = list(range(len(ds)))
    random.Random(seed).shuffle(idx)
    chosen = idx[:n]

    items = []
    for i, row_idx in enumerate(chosen):
        row = ds[row_idx]
        items.append({
            "item_id": f"sanity_{i:04d}_hf{row_idx}",
            "stem": row["question"],
            "options": row["options"],
            "gold": row["answer_idx"],
            "level": "a",
        })
    return items


def load_mock_sample(n: int, seed: int) -> list[dict]:
    """Synthetic stand-in with the identical item shape, for --mock runs with no
    network access at all (used in CI / offline dev, not just missing API keys)."""
    rng = random.Random(seed)
    items = []
    for i in range(n):
        gold = rng.choice(["A", "B", "C", "D"])
        items.append({
            "item_id": f"sanity_mock_{i:04d}",
            "stem": f"Synthetic sanity-check stem #{i}.",
            "options": {"A": "opt A", "B": "opt B", "C": "opt C", "D": "opt D"},
            "gold": gold,
            "level": "a",
        })
    return items


def run_sanity_check(config_path: str = "config.yaml", mock: bool = False,
                      only_models: list[str] | None = None) -> int:
    cfg = yaml.safe_load(open(config_path, encoding="utf-8"))
    n = SANITY_CHECK_N
    seed = cfg["run"]["seed"]

    print(f"Loading {n} items (seed={seed})...")
    if mock:
        # --mock avoids the HF network call too, so this is runnable fully offline.
        items = load_mock_sample(n, seed)
    else:
        try:
            items = load_medqa_sample(n, seed)
        except Exception as exc:
            print(f"FATAL: could not load MedQA sample: {exc}", file=sys.stderr)
            print("(datasets library missing, or no network access to Hugging Face?)",
                  file=sys.stderr)
            return 2

    ready, skipped = available_models(cfg, mock=mock)
    for msg in skipped:
        print(f"  SKIP {msg}")

    if only_models:
        # An explicit --models filter is a user CHOICE, distinct from a model
        # being unavailable -- worth its own message so it's clear this model
        # was deliberately excluded, not silently missing for an unknown reason
        # (e.g. a provider whose credentials check out fine but is behaving
        # badly in practice -- available_models() can't detect that in advance,
        # since it only checks credentials exist, not that the endpoint is
        # actually healthy).
        excluded = set(ready) - set(only_models)
        for model_id in sorted(excluded):
            print(f"  SKIP {model_id}: excluded via --models filter")
        ready = [m for m in ready if m in only_models]

    if not ready:
        print("No models available to run. Nothing to check.", file=sys.stderr)
        return 2

    tracker = CostTracker.from_config(config_path)
    out_path = Path(cfg["logging"]["results_file"]).parent / "sanity_check_results.jsonl"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    catchable_errors = catchable_run_errors()

    all_pass = True
    print()
    header = (f"{'model':<10}{'n':>5}{'acc':>8}{'parse_fail':>12}{'errors':>8}"
              f"{'target':>16}{'verdict':>10}")
    print(header)
    print("-" * len(header))

    with open(out_path, "w", encoding="utf-8") as out_fh:
        for model_id in ready:
            correct = 0
            scored = 0  # excludes unparseable, so accuracy isn't diluted by them
            unparseable = 0
            errored = 0  # items where run_model() itself raised (API failure, not
                          # a parse failure) -- see catchable_errors below

            print(f"  [{model_id}] starting {len(items)} items...")
            model_start = time.time()

            for i, item in enumerate(items, start=1):
                try:
                    record = run_model(
                        model_id,
                        item,
                        "prompts/eval_plain_v1.txt",
                        config_path=config_path,
                        stage="pilot",
                        prompt_condition="plain",
                        tracker=tracker,
                        mock=mock,
                    )
                except catchable_errors as exc:
                    # A single flaky item (timeout, rate limit that outlasted the
                    # harness's own retries, a one-off 5xx) must not take down the
                    # whole sanity check and discard every item already completed
                    # -- that would be exactly the kind of fragility this script
                    # exists to catch, just relocated into the diagnostic tool
                    # itself. Log it, count it, move on.
                    print(f"  FAILED {model_id} / {item['item_id']}: "
                          f"{type(exc).__name__}: {exc}", file=sys.stderr)
                    errored += 1
                    continue

                out_fh.write(json.dumps(record, ensure_ascii=False) + "\n")
                out_fh.flush()
                os.fsync(out_fh.fileno())
                if record["predicted"] is None:
                    unparseable += 1
                else:
                    scored += 1
                    if record["correct"]:
                        correct += 1

                # Periodic, guaranteed-visible progress -- plain new lines, not a
                # carriage-return progress bar. \r-based updates render
                # inconsistently across notebook frontends (Jupyter, Kaggle,
                # Colab, plain terminal), sometimes not at all -- and with zero
                # visible output during processing, "working slowly" (rate
                # limits, retries) and "actually hung" look identical from the
                # outside. Printed AFTER each item completes (not before), so
                # the count shown is always accurate, including the final line
                # confirming all items actually finished.
                if i % 10 == 0 or i == len(items):
                    elapsed = time.time() - model_start
                    print(f"  [{model_id}] {i}/{len(items)} done "
                          f"({elapsed:.0f}s elapsed, {errored} failed so far)")

            attempted = len(items) - errored  # denominator for rates below: items
                                               # that actually got a response, not
                                               # ones that errored before parsing
            acc = correct / scored if scored else 0.0
            parse_fail_rate = unparseable / attempted if attempted else 0.0
            error_rate = errored / len(items)
            parse_ok = parse_fail_rate <= PARSE_FAILURE_MAX_RATE
            # A high error rate is its own "harness might be broken" signal, same
            # spirit as the parse-failure check -- if the API keeps failing outright,
            # that's at least as informative as a parsing problem, and worth the
            # same visibility rather than just being averaged away.
            error_ok = error_rate <= PARSE_FAILURE_MAX_RATE

            recognized = model_id in TARGETS
            if recognized:
                lo, hi, point, note = TARGETS[model_id]
            else:
                # Not "no target yet" (that's the gpt_closed case below) -- this
                # model_id isn't even in TARGETS at all. Most likely config.yaml
                # and this file have drifted (a model id got renamed/added on one
                # side and not the other). That's a real problem worth a loud
                # warning and a CHECK verdict, not a silent "n/a" that looks the
                # same as the legitimate placeholder case.
                lo, hi, point, note = 0.0, 1.0, None, "UNRECOGNIZED model_id"
                print(f"  WARNING: no TARGETS entry for model_id={model_id!r} -- "
                      f"config.yaml and sanity_check.py have likely drifted out of "
                      f"sync.", file=sys.stderr)

            if not recognized:
                acc_ok = False
                verdict = "CHECK"
            elif point is None:
                # Legitimate placeholder (gpt_closed, before the mentor picks a
                # model). A band of 0-100% would make "PASS" trivially true and
                # meaningless, so show N/A instead -- unless parsing or the API
                # itself is broken, which is still worth flagging regardless of
                # any accuracy target existing.
                acc_ok = None
                verdict = "CHECK" if not (parse_ok and error_ok) else "N/A"
            else:
                acc_ok = lo <= acc <= hi
                verdict = "PASS" if (acc_ok and parse_ok and error_ok) else "CHECK"

            if verdict == "CHECK":
                all_pass = False

            target_str = f"{lo:.0%}-{hi:.0%}" if point is not None else "n/a"
            print(f"{model_id:<10}{len(items):>5}{acc:>8.1%}{parse_fail_rate:>12.1%}"
                  f"{error_rate:>8.1%}{target_str:>16}{verdict:>10}")
            if not error_ok:
                print(f"  -> API error rate {error_rate:.1%} exceeds "
                      f"{PARSE_FAILURE_MAX_RATE:.0%} threshold ({errored}/{len(items)} "
                      f"items raised instead of returning a response). This points at "
                      f"the harness or the API connection, not model quality.")
            if not parse_ok:
                print(f"  -> parse-failure rate {parse_fail_rate:.1%} exceeds "
                      f"{PARSE_FAILURE_MAX_RATE:.0%} threshold. This is the harness-is-"
                      f"broken signal -- check prompt/parser before trusting accuracy.")
            if not acc_ok and point is not None:
                print(f"  -> accuracy {acc:.1%} outside [{lo:.0%}, {hi:.0%}] band "
                      f"(reference: {note}).")

    print()
    print("Results written to", out_path)
    print("ALL CHECKS PASSED" if all_pass else "ONE OR MORE MODELS NEED A LOOK (see CHECK rows above)")
    return 0 if all_pass else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--mock", action="store_true",
                         help="No API keys, no GPU, no network -- fakes everything "
                              "including the dataset load, to test the full code path.")
    parser.add_argument("--models", nargs="+", default=None,
                         help="Restrict to these model ids only (e.g. --models "
                              "medgemma). Useful when a model's credentials check "
                              "out fine but the provider is actually misbehaving "
                              "in practice -- available_models() can only detect "
                              "missing credentials up front, not a provider that's "
                              "timing out on every real call, so it can't skip "
                              "that automatically. This lets you exclude it "
                              "explicitly instead of waiting through 5 retries "
                              "per item on something already known to be down.")
    args = parser.parse_args()
    sys.exit(run_sanity_check(args.config, mock=args.mock, only_models=args.models))
