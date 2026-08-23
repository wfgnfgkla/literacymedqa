import csv
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from draft_results_table import build_draft_results_table, main  # noqa: E402


def _row(item_id, model, level, correct, prompt_condition="plain", gold="A"):
    return {
        "item_id": item_id,
        "model": model,
        "level": level,
        "prompt_condition": prompt_condition,
        "predicted": gold if correct else "B",
        "gold": gold,
        "correct": correct,
    }


def test_draft_table_uses_real_scoring_metrics():
    rows = []
    for idx in range(10):
        item_id = f"pilot_{idx:02d}"
        rows.append(_row(item_id, "m_pilot", "a", 1))
        rows.append(_row(item_id, "m_pilot", "b", 1 if idx < 8 else 0))
        rows.append(_row(item_id, "m_pilot", "c", 1 if idx < 4 else 0))
        rows.append(
            _row(
                item_id,
                "m_pilot",
                "c",
                1 if idx < 7 else 0,
                prompt_condition="clarify_first",
            )
        )

    table = build_draft_results_table(rows)

    assert len(table) == 1
    row = table[0]
    assert row["model"] == "m_pilot"
    assert row["paired_n"] == 10
    assert row["acc_a_clinical"] == 1.0
    assert row["acc_b_plain"] == 0.8
    assert row["acc_c_low_literacy"] == 0.4
    assert row["gap_a_minus_c"] == pytest.approx(0.6)
    assert row["gap_b_minus_c"] == pytest.approx(0.4)
    assert row["right_to_wrong_flip_rate"] == pytest.approx(0.6)
    assert row["mcnemar_exact_p"] == pytest.approx(0.03125)
    assert row["acc_c_clarify"] == 0.7
    assert row["clarify_recovery"] == pytest.approx(0.3)


def test_cli_writes_csv_and_markdown(tmp_path):
    rows = []
    for idx in range(4):
        item_id = f"pilot_{idx:02d}"
        rows.append(_row(item_id, "m_pilot", "a", 1))
        rows.append(_row(item_id, "m_pilot", "b", 1))
        rows.append(_row(item_id, "m_pilot", "c", 1 if idx < 2 else 0))

    results_path = tmp_path / "pilot_results.jsonl"
    with open(results_path, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")

    out_csv = tmp_path / "table.csv"
    out_md = tmp_path / "table.md"
    assert main([str(results_path), "--out-csv", str(out_csv), "--out-md", str(out_md)]) == 0

    with open(out_csv, newline="", encoding="utf-8") as fh:
        csv_rows = list(csv.DictReader(fh))
    assert csv_rows[0]["model"] == "m_pilot"
    assert float(csv_rows[0]["gap_a_minus_c"]) == 0.5

    markdown = out_md.read_text(encoding="utf-8")
    assert "Pilot Draft Results Table" in markdown
    assert "50.0%" in markdown

