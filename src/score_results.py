"""
Scoring utilities for LiteracyMedQA.

Reads the canonical results rows:

    item_id, model, level, prompt_condition, predicted, gold, correct

and computes the headline metrics:

    - accuracy per model per level
    - literacy gap = acc(a) - acc(c)
    - right->wrong flip rate
    - paired McNemar test for a vs c

The CLI accepts either CSV (the schema doc's interchange format) or JSONL (the
current harness output format), as long as the required columns are present.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Any

REQUIRED_COLUMNS = {
    "item_id",
    "model",
    "level",
    "prompt_condition",
    "predicted",
    "gold",
    "correct",
}


def load_results(path: str | Path) -> list[dict[str, Any]]:
    """Load scored result rows from .csv or line-delimited .json/.jsonl."""
    path = Path(path)
    if path.suffix.lower() == ".csv":
        with open(path, newline="", encoding="utf-8") as fh:
            return [dict(row) for row in csv.DictReader(fh)]

    rows = []
    with open(path, encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{lineno}: invalid JSONL row") from exc
    return rows


def validate_rows(rows: list[dict[str, Any]]) -> None:
    """Fail early on schema issues that would make paired metrics misleading."""
    seen = set()
    gold_by_item = {}

    for idx, row in enumerate(rows, start=1):
        missing = REQUIRED_COLUMNS - row.keys()
        if missing:
            raise ValueError(f"row {idx}: missing required columns: {sorted(missing)}")

        key = (
            row["item_id"],
            row["model"],
            row["level"],
            row["prompt_condition"],
        )
        if key in seen:
            raise ValueError(f"row {idx}: duplicate result key: {key}")
        seen.add(key)

        item_id = row["item_id"]
        gold = row["gold"]
        if item_id in gold_by_item and gold_by_item[item_id] != gold:
            raise ValueError(
                f"row {idx}: item_id={item_id!r} has inconsistent gold values "
                f"{gold_by_item[item_id]!r} and {gold!r}"
            )
        gold_by_item[item_id] = gold

        normalized_correct = _correct_as_int(row["correct"])
        if row.get("predicted") is not None:
            expected = 1 if row["predicted"] == gold else 0
            # The harness uses correct=None for unparseable responses. Everything
            # else should agree with predicted-vs-gold so scoring cannot silently
            # inherit a hand-edited correctness error.
            if row["correct"] is not None and normalized_correct != expected:
                raise ValueError(
                    f"row {idx}: correct={row['correct']!r} disagrees with "
                    f"predicted={row['predicted']!r}, gold={gold!r}"
                )


def accuracy_by_model_level(
    rows: list[dict[str, Any]], *, prompt_condition: str
) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], list[int]] = defaultdict(list)
    for row in rows:
        if row["prompt_condition"] != prompt_condition:
            continue
        groups[(row["model"], row["level"])].append(_correct_as_int(row["correct"]))

    out = []
    for (model, level), values in sorted(groups.items()):
        n = len(values)
        correct_n = sum(values)
        out.append(
            {
                "model": model,
                "level": level,
                "prompt_condition": prompt_condition,
                "n": n,
                "correct_n": correct_n,
                "accuracy": correct_n / n if n else math.nan,
            }
        )
    return out


def literacy_gap_and_flips(
    rows: list[dict[str, Any]],
    *,
    prompt_condition: str,
    clinical_level: str = "a",
    low_literacy_level: str = "c",
) -> list[dict[str, Any]]:
    indexed = _paired_index(rows, prompt_condition=prompt_condition)
    out = []

    for model in sorted({model for model, _ in indexed}):
        pairs = _level_pairs(
            indexed,
            model=model,
            clinical_level=clinical_level,
            low_literacy_level=low_literacy_level,
        )
        if not pairs:
            continue

        clinical_values = [pair[0] for pair in pairs]
        low_values = [pair[1] for pair in pairs]
        clinical_acc = sum(clinical_values) / len(clinical_values)
        low_acc = sum(low_values) / len(low_values)
        right_at_clinical = sum(1 for clinical, _ in pairs if clinical == 1)
        right_to_wrong = sum(
            1 for clinical, low in pairs if clinical == 1 and low == 0
        )

        out.append(
            {
                "model": model,
                "prompt_condition": prompt_condition,
                "clinical_level": clinical_level,
                "low_literacy_level": low_literacy_level,
                "paired_n": len(pairs),
                "clinical_accuracy": clinical_acc,
                "low_literacy_accuracy": low_acc,
                "literacy_gap": clinical_acc - low_acc,
                "right_to_wrong_n": right_to_wrong,
                "clinical_correct_n": right_at_clinical,
                "right_to_wrong_flip_rate": (
                    right_to_wrong / right_at_clinical
                    if right_at_clinical
                    else math.nan
                ),
            }
        )
    return out


def mcnemar_by_model(
    rows: list[dict[str, Any]],
    *,
    prompt_condition: str,
    clinical_level: str = "a",
    low_literacy_level: str = "c",
) -> list[dict[str, Any]]:
    indexed = _paired_index(rows, prompt_condition=prompt_condition)
    out = []

    for model in sorted({model for model, _ in indexed}):
        pairs = _level_pairs(
            indexed,
            model=model,
            clinical_level=clinical_level,
            low_literacy_level=low_literacy_level,
        )
        if not pairs:
            continue

        both_correct = sum(1 for clinical, low in pairs if clinical == 1 and low == 1)
        clinical_only = sum(1 for clinical, low in pairs if clinical == 1 and low == 0)
        low_only = sum(1 for clinical, low in pairs if clinical == 0 and low == 1)
        neither_correct = sum(1 for clinical, low in pairs if clinical == 0 and low == 0)
        discordant = clinical_only + low_only

        out.append(
            {
                "model": model,
                "prompt_condition": prompt_condition,
                "clinical_level": clinical_level,
                "low_literacy_level": low_literacy_level,
                "paired_n": len(pairs),
                "both_correct": both_correct,
                "clinical_only": clinical_only,
                "low_literacy_only": low_only,
                "neither_correct": neither_correct,
                "mcnemar_statistic": _mcnemar_chi2_continuity(
                    clinical_only, low_only
                ),
                "mcnemar_exact_p": _mcnemar_exact_p(clinical_only, low_only),
            }
        )
    return out


def score_results(
    rows: list[dict[str, Any]],
    *,
    prompt_condition: str = "plain",
    clinical_level: str = "a",
    low_literacy_level: str = "c",
) -> dict[str, list[dict[str, Any]]]:
    validate_rows(rows)
    return {
        "accuracy": accuracy_by_model_level(rows, prompt_condition=prompt_condition),
        "literacy_gap": literacy_gap_and_flips(
            rows,
            prompt_condition=prompt_condition,
            clinical_level=clinical_level,
            low_literacy_level=low_literacy_level,
        ),
        "mcnemar": mcnemar_by_model(
            rows,
            prompt_condition=prompt_condition,
            clinical_level=clinical_level,
            low_literacy_level=low_literacy_level,
        ),
    }


def write_tables(tables: dict[str, list[dict[str, Any]]], output_dir: str | Path) -> None:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    for name, rows in tables.items():
        if not rows:
            continue
        _write_csv(output_dir / f"{name}.csv", rows)


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def _paired_index(
    rows: list[dict[str, Any]], *, prompt_condition: str
) -> dict[tuple[str, str], dict[str, int]]:
    indexed: dict[tuple[str, str], dict[str, int]] = defaultdict(dict)
    for row in rows:
        if row["prompt_condition"] != prompt_condition:
            continue
        key = (row["model"], row["item_id"])
        indexed[key][row["level"]] = _correct_as_int(row["correct"])
    return indexed


def _level_pairs(
    indexed: dict[tuple[str, str], dict[str, int]],
    *,
    model: str,
    clinical_level: str,
    low_literacy_level: str,
) -> list[tuple[int, int]]:
    pairs = []
    for (row_model, _), by_level in indexed.items():
        if row_model != model:
            continue
        if clinical_level in by_level and low_literacy_level in by_level:
            pairs.append((by_level[clinical_level], by_level[low_literacy_level]))
    return pairs


def _correct_as_int(value: Any) -> int:
    if value in (True, 1, "1", "true", "True", "TRUE"):
        return 1
    if value in (False, 0, "0", "false", "False", "FALSE", None, ""):
        return 0
    raise ValueError(f"invalid correct value: {value!r}")


def _mcnemar_chi2_continuity(clinical_only: int, low_only: int) -> float:
    discordant = clinical_only + low_only
    if discordant == 0:
        return 0.0
    corrected_delta = max(abs(clinical_only - low_only) - 1, 0)
    return corrected_delta ** 2 / discordant


def _mcnemar_exact_p(clinical_only: int, low_only: int) -> float:
    discordant = clinical_only + low_only
    if discordant == 0:
        return 1.0
    smaller = min(clinical_only, low_only)
    tail = sum(math.comb(discordant, k) for k in range(smaller + 1)) / (2 ** discordant)
    return min(1.0, 2 * tail)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Score LiteracyMedQA results.")
    parser.add_argument(
        "results_file",
        nargs="?",
        default="data/results.jsonl",
        help="Canonical results file (.jsonl or .csv).",
    )
    parser.add_argument(
        "--out-dir",
        default="data/scoring",
        help="Directory for accuracy.csv, literacy_gap.csv, and mcnemar.csv.",
    )
    parser.add_argument(
        "--prompt-condition",
        default="plain",
        help="Standard evaluation prompt condition to score.",
    )
    parser.add_argument(
        "--clinical-level",
        default="a",
        help="Clinical/original level for the gap numerator.",
    )
    parser.add_argument(
        "--low-literacy-level",
        default="c",
        help="Low-literacy level for the gap denominator.",
    )
    args = parser.parse_args(argv)

    rows = load_results(args.results_file)
    tables = score_results(
        rows,
        prompt_condition=args.prompt_condition,
        clinical_level=args.clinical_level,
        low_literacy_level=args.low_literacy_level,
    )
    write_tables(tables, args.out_dir)
    for name, table_rows in tables.items():
        print(f"{name}: {len(table_rows)} rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
