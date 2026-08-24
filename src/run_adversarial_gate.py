#!/usr/bin/env python3
"""
Task 3, part 2 -- run the verifier against the adversarial set and gate on it.

Reports, on the 20 corrupt cases and their 20 clean twins:
  sensitivity  share of injected corruptions caught          -- must be 1.00
  FPR          share of faithful rewrites wrongly failed     -- must be <= --max-fpr
  by category  where the verifier is blind
  by effect    corruptions that move the correct letter vs those that do not

The second split is the one that usually decides whether a verifier is real.
Catching a corruption that flips the answer is easy, because the text stops
making sense. Catching a dose unit change that leaves the answer intact is the
job, and that is where the released benchmark actually gets contaminated.

A perfect 20/20 is not proof of a perfect verifier. At n=20 the 95% interval on
sensitivity still runs down to about 0.83, so this gate rules out a bad verifier
rather than certifying a good one. It is a floor, not a certificate.

    python src/run_adversarial_gate.py --backend rules
    ANTHROPIC_API_KEY=... python src/run_adversarial_gate.py --backend anthropic
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from verifier import Verifier  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
CASES = ROOT / "data" / "adversarial" / "adversarial_set.jsonl"
RESULTS = ROOT / "results"


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (max(0.0, (c - h) / d), min(1.0, (c + h) / d))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", default="rules",
                    choices=["rules", "anthropic", "openai", "openrouter",
                             "rules+anthropic", "rules+openai", "rules+openrouter"])
    ap.add_argument("--model", default=None)
    ap.add_argument("--max-fpr", type=float, default=0.10)
    ap.add_argument("--tag", default=None)
    args = ap.parse_args()

    cases = [json.loads(l) for l in CASES.open(encoding="utf-8") if l.strip()]
    if not cases:
        sys.exit("no cases; run build_adversarial.py first")

    v = Verifier(backend=args.backend, model=args.model)
    records = []
    for i, c in enumerate(cases, 1):
        r = v.check(c["original"], c["rewrite"], c["options"], c["gold_letter"])
        caught = r.verdict == "FAIL"
        records.append({
            **{k: c[k] for k in ("case_id", "condition", "corruption_category",
                                 "changes_gold_answer", "expected_verdict", "note")},
            "verdict": r.verdict,
            "correct": caught == (c["expected_verdict"] == "FAIL"),
            "clinical_fact_changed": r.clinical_fact_changed,
            "same_answer_letter": r.same_answer_letter,
            "change_type": r.change_type,
            "what_changed": r.what_changed,
            "evidence_original": r.evidence_original,
            "evidence_rewrite": r.evidence_rewrite,
            "deterministic_flags": r.deterministic_flags,
            "confidence": r.confidence,
            "served_by": r.served_by,
        })
        print(f"[{i:2d}/{len(cases)}] {c['case_id']:24s} {c['condition']:8s} "
              f"want {c['expected_verdict']:5s} got {r.verdict:6s} "
              f"{'ok' if records[-1]['correct'] else 'MISS'}")

    corrupt = [r for r in records if r["condition"] == "corrupt"]
    clean = [r for r in records if r["condition"] == "clean"]
    caught = sum(1 for r in corrupt if r["verdict"] == "FAIL")
    fp = sum(1 for r in clean if r["verdict"] == "FAIL")
    review = sum(1 for r in clean if r["verdict"] == "REVIEW")

    sens = caught / len(corrupt)
    fpr = fp / len(clean)

    by_cat = defaultdict(lambda: [0, 0])
    for r in corrupt:
        by_cat[r["corruption_category"]][1] += 1
        by_cat[r["corruption_category"]][0] += r["verdict"] == "FAIL"
    flip = [r for r in corrupt if r["changes_gold_answer"]]
    keep = [r for r in corrupt if not r["changes_gold_answer"]]

    passed = sens >= 1.0 and fpr <= args.max_fpr
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    tag = args.tag or args.backend.replace("+", "_")

    summary = {
        "run_at_utc": stamp,
        "backend": args.backend,
        "model": v.model,
        "prompt_sha256": v.prompt_sha,
        "n_corrupt": len(corrupt), "n_clean": len(clean),
        # One upstream across every case is what the pinned-verifier claim rests on.
        "served_by": dict(sorted(Counter(r["served_by"] for r in records).items())),
        "sensitivity": round(sens, 4),
        "sensitivity_ci95": [round(x, 4) for x in wilson(caught, len(corrupt))],
        "false_positive_rate": round(fpr, 4),
        "false_positive_rate_ci95": [round(x, 4) for x in wilson(fp, len(clean))],
        "clean_sent_to_review": review,
        "sensitivity_by_category": {k: {"caught": c, "n": n, "rate": round(c / n, 3)}
                                    for k, (c, n) in sorted(by_cat.items())},
        "sensitivity_answer_changing": round(
            sum(r["verdict"] == "FAIL" for r in flip) / len(flip), 3) if flip else None,
        "sensitivity_answer_preserving": round(
            sum(r["verdict"] == "FAIL" for r in keep) / len(keep), 3) if keep else None,
        "gate": "PASS" if passed else "FAIL",
        "max_fpr_allowed": args.max_fpr,
        "missed_corruptions": [{"case_id": r["case_id"], "category": r["corruption_category"],
                                "note": r["note"], "verdict": r["verdict"]}
                               for r in corrupt if r["verdict"] != "FAIL"],
        "false_positives": [{"case_id": r["case_id"], "what_changed": r["what_changed"],
                             "flags": r["deterministic_flags"]}
                            for r in clean if r["verdict"] == "FAIL"],
    }

    RESULTS.mkdir(exist_ok=True)
    (RESULTS / f"adversarial_gate_{tag}.json").write_text(
        json.dumps({"summary": summary, "records": records}, indent=2, ensure_ascii=False),
        encoding="utf-8")

    lines = [
        f"# Adversarial verifier gate -- `{args.backend}`", "",
        f"Run {stamp} · model `{v.model}` · prompt `{v.prompt_sha[:12]}`", "",
        f"**Gate: {summary['gate']}**  (requires sensitivity 1.00 and FPR <= {args.max_fpr:.2f})", "",
        "| metric | value | 95% CI |", "|---|---|---|",
        f"| sensitivity (corruptions caught) | {caught}/{len(corrupt)} = {sens:.2f} | "
        f"{summary['sensitivity_ci95'][0]:.2f}–{summary['sensitivity_ci95'][1]:.2f} |",
        f"| false positive rate (clean failed) | {fp}/{len(clean)} = {fpr:.2f} | "
        f"{summary['false_positive_rate_ci95'][0]:.2f}–{summary['false_positive_rate_ci95'][1]:.2f} |",
        f"| clean sent to manual review | {review}/{len(clean)} | |", "",
        "## By corruption category", "", "| category | caught |", "|---|---|",
    ]
    for k, (c, n) in sorted(by_cat.items()):
        lines.append(f"| {k} | {c}/{n} |")
    lines += [
        "", "## By effect on the correct answer", "", "| corruption | caught |", "|---|---|",
        f"| moves the correct letter | {sum(r['verdict']=='FAIL' for r in flip)}/{len(flip)} |",
        f"| leaves the letter intact | {sum(r['verdict']=='FAIL' for r in keep)}/{len(keep)} |",
        "",
    ]
    if summary["missed_corruptions"]:
        lines += ["## Missed corruptions", ""]
        lines += [f"- `{m['case_id']}` ({m['category']}) — {m['note']}" for m in summary["missed_corruptions"]]
        lines.append("")
    if summary["false_positives"]:
        lines += ["## False positives on faithful rewrites", ""]
        lines += [f"- `{m['case_id']}` — {m['what_changed'] or m['flags']}" for m in summary["false_positives"]]
        lines.append("")
    lines += [
        "## Reading this", "",
        "At n=20 per arm a perfect score still leaves the true sensitivity as low as",
        "~0.83 at 95% confidence. The gate rules out a broken verifier; it does not",
        "certify a good one. Passing it is a precondition for generating the full",
        "set, not evidence that the set is clean.",
    ]
    (RESULTS / f"adversarial_gate_{tag}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"\nsensitivity {caught}/{len(corrupt)} = {sens:.2f}   FPR {fp}/{len(clean)} = {fpr:.2f}"
          f"   review {review}")
    print(f"GATE {summary['gate']} -> results/adversarial_gate_{tag}.md")
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
