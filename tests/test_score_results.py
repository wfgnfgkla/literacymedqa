import math
import csv
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from score_results import (  # noqa: E402
    main,
    literacy_gap_and_flips,
    mcnemar_by_model,
    score_results,
    validate_rows,
)


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


def test_accuracy_gap_flip_rate_and_mcnemar():
    rows = [
        _row("i1", "m1", "a", 1),
        _row("i1", "m1", "c", 0),
        _row("i2", "m1", "a", 1),
        _row("i2", "m1", "c", 1),
        _row("i3", "m1", "a", 0),
        _row("i3", "m1", "c", 1),
        _row("i4", "m1", "a", 0),
        _row("i4", "m1", "c", 0),
        _row("i5", "m1", "a", 1),
        _row("i5", "m1", "c", 0),
    ]

    tables = score_results(rows)

    accuracy = {(r["model"], r["level"]): r for r in tables["accuracy"]}
    assert accuracy[("m1", "a")]["accuracy"] == 3 / 5
    assert accuracy[("m1", "c")]["accuracy"] == 2 / 5

    gap = tables["literacy_gap"][0]
    assert gap["literacy_gap"] == pytest.approx(0.2)
    assert gap["right_to_wrong_n"] == 2
    assert gap["clinical_correct_n"] == 3
    assert gap["right_to_wrong_flip_rate"] == pytest.approx(2 / 3)

    mcnemar = tables["mcnemar"][0]
    assert mcnemar["both_correct"] == 1
    assert mcnemar["clinical_only"] == 2
    assert mcnemar["low_literacy_only"] == 1
    assert mcnemar["neither_correct"] == 1
    assert mcnemar["mcnemar_statistic"] == 0
    assert mcnemar["mcnemar_exact_p"] == 1.0


def test_gap_uses_only_paired_items():
    rows = [
        _row("i1", "m1", "a", 1),
        _row("i1", "m1", "c", 0),
        _row("i2", "m1", "a", 1),
        # i2 has no c row and should not enter paired gap/flip calculations.
    ]

    gap = literacy_gap_and_flips(rows, prompt_condition="plain")[0]
    assert gap["paired_n"] == 1
    assert gap["clinical_accuracy"] == 1
    assert gap["low_literacy_accuracy"] == 0
    assert gap["literacy_gap"] == 1


def test_prompt_condition_filtering():
    rows = [
        _row("i1", "m1", "a", 1, prompt_condition="plain"),
        _row("i1", "m1", "c", 0, prompt_condition="plain"),
        _row("i1", "m1", "a", 0, prompt_condition="clarify_first"),
        _row("i1", "m1", "c", 0, prompt_condition="clarify_first"),
    ]

    gap = literacy_gap_and_flips(rows, prompt_condition="plain")[0]
    assert gap["literacy_gap"] == 1


def test_validation_rejects_duplicates_and_inconsistent_gold():
    rows = [_row("i1", "m1", "a", 1), _row("i1", "m1", "a", 1)]
    with pytest.raises(ValueError, match="duplicate"):
        validate_rows(rows)

    rows = [
        _row("i1", "m1", "a", 1, gold="A"),
        _row("i1", "m1", "c", 1, gold="C"),
    ]
    with pytest.raises(ValueError, match="inconsistent gold"):
        validate_rows(rows)


def test_none_correct_counts_as_zero_for_scoring():
    rows = [
        _row("i1", "m1", "a", 1),
        {
            "item_id": "i1",
            "model": "m1",
            "level": "c",
            "prompt_condition": "plain",
            "predicted": None,
            "gold": "A",
            "correct": None,
        },
    ]

    gap = score_results(rows)["literacy_gap"][0]
    assert gap["low_literacy_accuracy"] == 0
    assert gap["right_to_wrong_flip_rate"] == 1


def test_flip_rate_nan_when_model_never_correct_at_clinical_level():
    rows = [
        _row("i1", "m1", "a", 0),
        _row("i1", "m1", "c", 0),
    ]

    gap = score_results(rows)["literacy_gap"][0]
    assert math.isnan(gap["right_to_wrong_flip_rate"])


def test_mcnemar_all_tied_has_p_one():
    rows = [
        _row("i1", "m1", "a", 1),
        _row("i1", "m1", "c", 1),
    ]

    result = mcnemar_by_model(rows, prompt_condition="plain")[0]
    assert result["mcnemar_statistic"] == 0
    assert result["mcnemar_exact_p"] == 1


def test_whole_scoring_path_detects_planted_gap(tmp_path):
    # Plant an obvious effect: model m_gap gets all 10 clinical/original items
    # right at level a and only 4/10 low-literacy items right at level c.
    # The full scoring path should recover acc(a)=1.0, acc(c)=0.4, gap=0.6,
    # and a 6/10 right-to-wrong flip rate from file input through CSV output.
    rows = []
    for idx in range(10):
        item_id = f"synthetic_{idx:02d}"
        rows.append(_row(item_id, "m_gap", "a", 1))
        rows.append(_row(item_id, "m_gap", "b", 1))
        rows.append(_row(item_id, "m_gap", "c", 1 if idx < 4 else 0))

    results_path = tmp_path / "results.jsonl"
    with open(results_path, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")

    out_dir = tmp_path / "scoring"
    exit_code = main([str(results_path), "--out-dir", str(out_dir)])
    assert exit_code == 0

    accuracy_rows = _read_csv(out_dir / "accuracy.csv")
    accuracy = {(r["model"], r["level"]): float(r["accuracy"]) for r in accuracy_rows}
    assert accuracy[("m_gap", "a")] == 1.0
    assert accuracy[("m_gap", "b")] == 1.0
    assert accuracy[("m_gap", "c")] == 0.4

    gap_rows = _read_csv(out_dir / "literacy_gap.csv")
    assert len(gap_rows) == 1
    gap = gap_rows[0]
    assert float(gap["clinical_accuracy"]) == 1.0
    assert float(gap["low_literacy_accuracy"]) == 0.4
    assert float(gap["literacy_gap"]) == pytest.approx(0.6)
    assert int(gap["right_to_wrong_n"]) == 6
    assert int(gap["clinical_correct_n"]) == 10
    assert float(gap["right_to_wrong_flip_rate"]) == pytest.approx(0.6)

    mcnemar_rows = _read_csv(out_dir / "mcnemar.csv")
    assert len(mcnemar_rows) == 1
    mcnemar = mcnemar_rows[0]
    assert int(mcnemar["clinical_only"]) == 6
    assert int(mcnemar["low_literacy_only"]) == 0
    assert int(mcnemar["both_correct"]) == 4
    assert int(mcnemar["neither_correct"]) == 0
    assert float(mcnemar["mcnemar_exact_p"]) == pytest.approx(0.03125)


def _read_csv(path):
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))
