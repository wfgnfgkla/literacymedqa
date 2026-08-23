import csv
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from realism_gate import fleiss_kappa, main, score_realism_gate, validate_ratings  # noqa: E402


def _rating(item_id, gold_label, rater_id, rating):
    return {
        "item_id": item_id,
        "gold_label": gold_label,
        "rater_id": rater_id,
        "rating": rating,
    }


def test_realism_gate_scores_accuracy_kappa_and_pass_verdict():
    rows = [
        _rating("i1", "generated", "r1", "generated"),
        _rating("i1", "generated", "r2", "real"),
        _rating("i1", "generated", "r3", "generated"),
        _rating("i2", "real", "r1", "real"),
        _rating("i2", "real", "r2", "real"),
        _rating("i2", "real", "r3", "generated"),
        _rating("i3", "generated", "r1", "real"),
        _rating("i3", "generated", "r2", "real"),
        _rating("i3", "generated", "r3", "generated"),
        _rating("i4", "real", "r1", "generated"),
        _rating("i4", "real", "r2", "real"),
        _rating("i4", "real", "r3", "real"),
    ]

    results = score_realism_gate(rows)

    by_rater = {row["rater_id"]: row for row in results["rater_accuracy"]}
    assert by_rater["r1"]["accuracy"] == 0.5
    assert by_rater["r2"]["accuracy"] == 0.5
    assert by_rater["r3"]["accuracy"] == 0.75

    overall = results["overall"][0]
    assert overall["overall_accuracy"] == pytest.approx(7 / 12)
    assert overall["gate_pass"] is True
    assert overall["fleiss_kappa"] == pytest.approx(-0.3714285714)
    assert "passes" in results["verdict"]
    assert "58.3%" in results["verdict"]


def test_realism_gate_fails_when_raters_identify_generated_items_too_well():
    rows = []
    for item_idx, gold in enumerate(["real", "generated", "real", "generated"]):
        for rater in ("r1", "r2", "r3"):
            rows.append(_rating(f"i{item_idx}", gold, rater, gold))

    results = score_realism_gate(rows)
    assert results["overall"][0]["overall_accuracy"] == 1.0
    assert results["overall"][0]["gate_pass"] is False
    assert "fails" in results["verdict"]
    assert "Revise the rewrite prompt" in results["verdict"]


def test_fleiss_kappa_perfect_agreement():
    assert fleiss_kappa(
        [
            {"real": 3, "generated": 0},
            {"real": 0, "generated": 3},
        ]
    ) == 1.0


def test_validation_rejects_duplicate_and_uneven_raters():
    rows = [
        _rating("i1", "real", "r1", "real"),
        _rating("i1", "real", "r1", "generated"),
    ]
    with pytest.raises(ValueError, match="duplicate"):
        validate_ratings(rows)

    rows = [
        _rating("i1", "real", "r1", "real"),
        _rating("i1", "real", "r2", "real"),
        _rating("i2", "generated", "r1", "generated"),
    ]
    with pytest.raises(ValueError, match="same number of raters"):
        validate_ratings(rows)


def test_cli_writes_summary_and_verdict(tmp_path):
    ratings_path = tmp_path / "ratings.csv"
    rows = [
        _rating("i1", "generated", "r1", "generated"),
        _rating("i1", "generated", "r2", "real"),
        _rating("i1", "generated", "r3", "generated"),
        _rating("i2", "real", "r1", "real"),
        _rating("i2", "real", "r2", "real"),
        _rating("i2", "real", "r3", "generated"),
    ]
    with open(ratings_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(
            fh, fieldnames=["item_id", "gold_label", "rater_id", "rating"]
        )
        writer.writeheader()
        writer.writerows(rows)

    out_dir = tmp_path / "out"
    assert main([str(ratings_path), "--out-dir", str(out_dir)]) == 0
    assert (out_dir / "rater_accuracy.csv").exists()
    assert (out_dir / "realism_gate_summary.csv").exists()
    verdict = (out_dir / "realism_gate_verdict.txt").read_text(encoding="utf-8")
    assert "Realism blind-rater gate" in verdict
