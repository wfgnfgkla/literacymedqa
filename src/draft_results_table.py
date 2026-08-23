"""
Build a draft, team-facing results table from pilot scored results.

This is intentionally a presentation layer over src/score_results.py: every
number still comes from the canonical result rows, but this script puts the pilot
numbers into the compact table shape the team will eventually see in the paper.
"""

from __future__ import annotations

import argparse
import csv
import math
from pathlib import Path
from typing import Any

from score_results import (
    accuracy_by_model_level,
    literacy_gap_and_flips,
    load_results,
    mcnemar_by_model,
    validate_rows,
)


TABLE_COLUMNS = [
    "model",
    "paired_n",
    "acc_a_clinical",
    "acc_b_plain",
    "acc_c_low_literacy",
    "gap_a_minus_c",
    "gap_b_minus_c",
    "right_to_wrong_flip_rate",
    "mcnemar_exact_p",
    "acc_c_clarify",
    "clarify_recovery",
]


def build_draft_results_table(
    rows: list[dict[str, Any]],
    *,
    prompt_condition: str = "plain",
    clarify_prompt_condition: str = "clarify_first",
    clinical_level: str = "a",
    plain_level: str = "b",
    low_literacy_level: str = "c",
) -> list[dict[str, Any]]:
    validate_rows(rows)

    accuracy = {
        (row["model"], row["level"]): row
        for row in accuracy_by_model_level(rows, prompt_condition=prompt_condition)
    }
    gaps = {
        row["model"]: row
        for row in literacy_gap_and_flips(
            rows,
            prompt_condition=prompt_condition,
            clinical_level=clinical_level,
            low_literacy_level=low_literacy_level,
        )
    }
    mcnemar = {
        row["model"]: row
        for row in mcnemar_by_model(
            rows,
            prompt_condition=prompt_condition,
            clinical_level=clinical_level,
            low_literacy_level=low_literacy_level,
        )
    }
    clarify_accuracy = {
        (row["model"], row["level"]): row
        for row in accuracy_by_model_level(
            rows, prompt_condition=clarify_prompt_condition
        )
    }

    models = sorted(gaps)
    table = []
    for model in models:
        acc_a = _accuracy_value(accuracy, model, clinical_level)
        acc_b = _accuracy_value(accuracy, model, plain_level)
        acc_c = _accuracy_value(accuracy, model, low_literacy_level)
        acc_c_clarify = _accuracy_value(clarify_accuracy, model, low_literacy_level)
        gap_b_c = acc_b - acc_c if _is_number(acc_b) and _is_number(acc_c) else math.nan
        clarify_recovery = (
            acc_c_clarify - acc_c
            if _is_number(acc_c_clarify) and _is_number(acc_c)
            else math.nan
        )

        table.append(
            {
                "model": model,
                "paired_n": gaps[model]["paired_n"],
                "acc_a_clinical": acc_a,
                "acc_b_plain": acc_b,
                "acc_c_low_literacy": acc_c,
                "gap_a_minus_c": gaps[model]["literacy_gap"],
                "gap_b_minus_c": gap_b_c,
                "right_to_wrong_flip_rate": gaps[model][
                    "right_to_wrong_flip_rate"
                ],
                "mcnemar_exact_p": mcnemar.get(model, {}).get(
                    "mcnemar_exact_p", math.nan
                ),
                "acc_c_clarify": acc_c_clarify,
                "clarify_recovery": clarify_recovery,
            }
        )
    return table


def write_draft_table(table: list[dict[str, Any]], output_csv: str | Path) -> None:
    output_csv = Path(output_csv)
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    with open(output_csv, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=TABLE_COLUMNS)
        writer.writeheader()
        writer.writerows(table)


def write_markdown_table(table: list[dict[str, Any]], output_md: str | Path) -> None:
    output_md = Path(output_md)
    output_md.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Pilot Draft Results Table",
        "",
        "_Small-sample pilot numbers. Treat as a format preview, not final results._",
        "",
        "| Model | n | Acc(a) clinical | Acc(b) plain | Acc(c) low-lit | Gap a-c | Gap b-c | Right->wrong | McNemar p | Acc(c) clarify | Clarify recovery |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in table:
        lines.append(
            "| {model} | {paired_n} | {acc_a} | {acc_b} | {acc_c} | {gap_ac} | "
            "{gap_bc} | {flip} | {p} | {clarify} | {recovery} |".format(
                model=row["model"],
                paired_n=row["paired_n"],
                acc_a=_fmt_pct(row["acc_a_clinical"]),
                acc_b=_fmt_pct(row["acc_b_plain"]),
                acc_c=_fmt_pct(row["acc_c_low_literacy"]),
                gap_ac=_fmt_pct(row["gap_a_minus_c"], signed=True),
                gap_bc=_fmt_pct(row["gap_b_minus_c"], signed=True),
                flip=_fmt_pct(row["right_to_wrong_flip_rate"]),
                p=_fmt_p(row["mcnemar_exact_p"]),
                clarify=_fmt_pct(row["acc_c_clarify"]),
                recovery=_fmt_pct(row["clarify_recovery"], signed=True),
            )
        )
    output_md.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _accuracy_value(
    accuracy: dict[tuple[str, str], dict[str, Any]], model: str, level: str
) -> float:
    row = accuracy.get((model, level))
    return row["accuracy"] if row else math.nan


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not math.isnan(value)


def _fmt_pct(value: Any, *, signed: bool = False) -> str:
    if not _is_number(value):
        return ""
    sign = "+" if signed and value > 0 else ""
    return f"{sign}{value:.1%}"


def _fmt_p(value: Any) -> str:
    if not _is_number(value):
        return ""
    return f"{value:.3g}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Create a draft pilot results table from canonical results."
    )
    parser.add_argument(
        "results_file",
        nargs="?",
        default="data/pilot_results.jsonl",
        help="Pilot canonical results file (.jsonl or .csv).",
    )
    parser.add_argument(
        "--out-csv",
        default="data/pilot_draft_results_table.csv",
        help="CSV output path.",
    )
    parser.add_argument(
        "--out-md",
        default="data/pilot_draft_results_table.md",
        help="Markdown output path.",
    )
    parser.add_argument("--prompt-condition", default="plain")
    parser.add_argument("--clarify-prompt-condition", default="clarify_first")
    args = parser.parse_args(argv)

    rows = load_results(args.results_file)
    table = build_draft_results_table(
        rows,
        prompt_condition=args.prompt_condition,
        clarify_prompt_condition=args.clarify_prompt_condition,
    )
    write_draft_table(table, args.out_csv)
    write_markdown_table(table, args.out_md)
    print(f"wrote {len(table)} model rows to {args.out_csv} and {args.out_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

