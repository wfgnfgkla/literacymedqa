"""
Realism-gate scoring for blind rater validation.

Input CSV schema:

    item_id, gold_label, rater_id, rating

where gold_label and rating are both one of:

    real, generated

Each row is one rater's blind judgment for one shuffled realism-set item. The
script computes rater accuracy against the hidden source label, Fleiss' kappa
for inter-rater agreement, and a one-paragraph gate verdict.
"""

from __future__ import annotations

import argparse
import csv
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

REQUIRED_COLUMNS = {"item_id", "gold_label", "rater_id", "rating"}
LABELS = ("real", "generated")
MAX_RATER_ACCURACY = 0.75


def load_ratings(path: str | Path) -> list[dict[str, str]]:
    with open(path, newline="", encoding="utf-8") as fh:
        return [dict(row) for row in csv.DictReader(fh)]


def validate_ratings(rows: list[dict[str, str]]) -> None:
    if not rows:
        raise ValueError("ratings file is empty")

    seen = set()
    raters_by_item: dict[str, set[str]] = defaultdict(set)
    gold_by_item = {}

    for idx, row in enumerate(rows, start=1):
        missing = REQUIRED_COLUMNS - row.keys()
        if missing:
            raise ValueError(f"row {idx}: missing required columns: {sorted(missing)}")

        item_id = row["item_id"]
        rater_id = row["rater_id"]
        key = (item_id, rater_id)
        if key in seen:
            raise ValueError(f"row {idx}: duplicate rating for {key}")
        seen.add(key)
        raters_by_item[item_id].add(rater_id)

        gold = _normalize_label(row["gold_label"], field="gold_label", row_idx=idx)
        _normalize_label(row["rating"], field="rating", row_idx=idx)

        if item_id in gold_by_item and gold_by_item[item_id] != gold:
            raise ValueError(
                f"row {idx}: item_id={item_id!r} has inconsistent gold labels "
                f"{gold_by_item[item_id]!r} and {gold!r}"
            )
        gold_by_item[item_id] = gold

    rater_counts = {item_id: len(raters) for item_id, raters in raters_by_item.items()}
    expected = next(iter(rater_counts.values()))
    uneven = {item_id: n for item_id, n in rater_counts.items() if n != expected}
    if uneven:
        raise ValueError(
            "Fleiss' kappa requires the same number of raters per item; "
            f"found uneven counts: {uneven}"
        )
    if expected < 2:
        raise ValueError("Fleiss' kappa requires at least two raters per item")


def score_realism_gate(
    rows: list[dict[str, str]], *, max_accuracy: float = MAX_RATER_ACCURACY
) -> dict[str, Any]:
    validate_ratings(rows)

    normalized = [
        {
            "item_id": row["item_id"],
            "gold_label": _normalize_label(row["gold_label"]),
            "rater_id": row["rater_id"],
            "rating": _normalize_label(row["rating"]),
        }
        for row in rows
    ]

    rater_accuracy = _rater_accuracy(normalized)
    overall_accuracy = sum(r["correct_n"] for r in rater_accuracy) / sum(
        r["n"] for r in rater_accuracy
    )
    item_counts = _item_label_counts(normalized)
    kappa = fleiss_kappa(item_counts)
    verdict = build_gate_verdict(
        n_items=len(item_counts),
        n_raters=len({row["rater_id"] for row in normalized}),
        overall_accuracy=overall_accuracy,
        max_accuracy=max_accuracy,
        fleiss_kappa=kappa,
    )

    return {
        "rater_accuracy": rater_accuracy,
        "overall": [
            {
                "n_items": len(item_counts),
                "n_raters": len({row["rater_id"] for row in normalized}),
                "n_ratings": len(normalized),
                "overall_accuracy": overall_accuracy,
                "max_accuracy": max_accuracy,
                "fleiss_kappa": kappa,
                "gate_pass": overall_accuracy <= max_accuracy,
            }
        ],
        "verdict": verdict,
    }


def fleiss_kappa(item_label_counts: list[dict[str, int]]) -> float:
    """Compute Fleiss' kappa for fixed-rater, nominal-category ratings."""
    if not item_label_counts:
        raise ValueError("no item counts provided")

    n_raters = sum(item_label_counts[0].values())
    if n_raters < 2:
        raise ValueError("Fleiss' kappa requires at least two raters per item")
    if any(sum(counts.values()) != n_raters for counts in item_label_counts):
        raise ValueError("every item must have the same number of ratings")

    n_items = len(item_label_counts)
    p_item = []
    category_totals = Counter()
    for counts in item_label_counts:
        category_totals.update(counts)
        agreement = sum(count * (count - 1) for count in counts.values())
        p_item.append(agreement / (n_raters * (n_raters - 1)))

    p_bar = sum(p_item) / n_items
    total_ratings = n_items * n_raters
    p_expected = sum((category_totals[label] / total_ratings) ** 2 for label in LABELS)

    if p_expected == 1:
        return 1.0 if p_bar == 1 else 0.0
    return (p_bar - p_expected) / (1 - p_expected)


def build_gate_verdict(
    *,
    n_items: int,
    n_raters: int,
    overall_accuracy: float,
    max_accuracy: float,
    fleiss_kappa: float,
) -> str:
    status = "passes" if overall_accuracy <= max_accuracy else "fails"
    direction = "at or below" if overall_accuracy <= max_accuracy else "above"
    return (
        f"Realism blind-rater gate {status}: across {n_raters} raters and "
        f"{n_items} items, raters identified real vs generated examples with "
        f"{_pct(overall_accuracy)} accuracy, which is {direction} the "
        f"pre-registered {_pct(max_accuracy)} threshold. Inter-rater agreement "
        f"was Fleiss' kappa = {fleiss_kappa:.3f}. "
        f"{'Proceed to full generation, pending the separate readability/FK gate.' if overall_accuracy <= max_accuracy else 'Revise the rewrite prompt and rerun the realism pilot before scaling.'}"
    )


def write_outputs(results: dict[str, Any], output_dir: str | Path) -> None:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(output_dir / "rater_accuracy.csv", results["rater_accuracy"])
    _write_csv(output_dir / "realism_gate_summary.csv", results["overall"])
    (output_dir / "realism_gate_verdict.txt").write_text(
        results["verdict"] + "\n", encoding="utf-8"
    )


def _rater_accuracy(rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        grouped[row["rater_id"]].append(row)

    out = []
    for rater_id, rater_rows in sorted(grouped.items()):
        correct_n = sum(1 for row in rater_rows if row["rating"] == row["gold_label"])
        n = len(rater_rows)
        out.append(
            {
                "rater_id": rater_id,
                "n": n,
                "correct_n": correct_n,
                "accuracy": correct_n / n if n else 0.0,
            }
        )
    return out


def _item_label_counts(rows: list[dict[str, str]]) -> list[dict[str, int]]:
    by_item: dict[str, Counter[str]] = defaultdict(Counter)
    for row in rows:
        by_item[row["item_id"]][row["rating"]] += 1
    return [
        {label: by_item[item_id].get(label, 0) for label in LABELS}
        for item_id in sorted(by_item)
    ]


def _normalize_label(value: str, *, field: str = "label", row_idx: int | None = None) -> str:
    normalized = value.strip().lower()
    if normalized not in LABELS:
        prefix = f"row {row_idx}: " if row_idx is not None else ""
        raise ValueError(
            f"{prefix}{field} must be one of {LABELS}, got {value!r}"
        )
    return normalized


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def _pct(value: float) -> str:
    return f"{value:.1%}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Score the realism blind-rater gate.")
    parser.add_argument(
        "ratings_file",
        help="CSV with columns: item_id,gold_label,rater_id,rating",
    )
    parser.add_argument(
        "--out-dir",
        default="data/realism_gate",
        help="Directory for rater_accuracy.csv, summary CSV, and verdict text.",
    )
    parser.add_argument(
        "--max-accuracy",
        type=float,
        default=MAX_RATER_ACCURACY,
        help="Gate threshold. Default: 0.75.",
    )
    args = parser.parse_args(argv)

    rows = load_ratings(args.ratings_file)
    results = score_realism_gate(rows, max_accuracy=args.max_accuracy)
    write_outputs(results, args.out_dir)
    print(results["verdict"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

