"""
Tests for src/flatten_pilot.py.

Pure-function tests against flatten_pilot_rewrites() directly -- no file I/O,
no real pilot data required. See test_flatten_pilot_against_real_data() at the
bottom for the one test that does touch the real repo file, skipped cleanly if
it isn't present (e.g. a fresh clone before the pilot has been generated).
"""

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from flatten_pilot import flatten_pilot_rewrites  # noqa: E402


def _row(item_id="LMQ-1", **overrides):
    base = {
        "item_id": item_id,
        "gold": "B",
        "options": {"A": "a", "B": "b", "C": "c", "D": "d"},
        "level_a": "clinical text",
        "level_b": "plain text",
        "level_c": "low literacy text",
        "prompt_hash": "deadbeef",
        "config_version": 5,
        "structural_flags": [],
    }
    base.update(overrides)
    return base


# --------------------------------------------------------------------------- #
# Correct-input behavior
# --------------------------------------------------------------------------- #


def test_flattens_one_row_into_three_level_records():
    out = flatten_pilot_rewrites([_row()])
    assert len(out) == 3
    assert {r["level"] for r in out} == {"a", "b", "c"}


def test_item_id_identical_across_levels():
    # Deliberate, not incidental -- see the docstring in flatten_pilot.py.
    # run_batch's resume key and score_results.py's paired comparison both
    # depend on this.
    out = flatten_pilot_rewrites([_row(item_id="LMQ-42")])
    assert {r["item_id"] for r in out} == {"LMQ-42"}


def test_stem_pulled_from_correct_level_column():
    out = flatten_pilot_rewrites([_row()])
    by_level = {r["level"]: r["stem"] for r in out}
    assert by_level["a"] == "clinical text"
    assert by_level["b"] == "plain text"
    assert by_level["c"] == "low literacy text"


def test_gold_and_options_carried_through_unchanged():
    out = flatten_pilot_rewrites([_row()])
    for r in out:
        assert r["gold"] == "B"
        assert r["options"] == {"A": "a", "B": "b", "C": "c", "D": "d"}


def test_rewriter_prompt_hash_renamed_not_left_as_prompt_hash():
    # The source file's "prompt_hash" is the REWRITER prompt's hash. run_model()
    # writes a DIFFERENT hash (the eval template's) under that exact key name.
    # If this field were left named "prompt_hash" here, a careless read later
    # could silently mix the two up.
    out = flatten_pilot_rewrites([_row()])
    for r in out:
        assert r["rewriter_prompt_hash"] == "deadbeef"
        assert "prompt_hash" not in r


def test_multiple_items_preserve_count_and_distinct_ids():
    rows = [_row(item_id=f"LMQ-{i}") for i in range(5)]
    out = flatten_pilot_rewrites(rows)
    assert len(out) == 15
    assert len({r["item_id"] for r in out}) == 5


def test_structural_flags_carried_through():
    out = flatten_pilot_rewrites([_row(structural_flags=["jargon_leak"])])
    for r in out:
        assert r["structural_flags"] == ["jargon_leak"]


def test_row_order_preserved():
    rows = [_row(item_id=f"LMQ-{i}") for i in range(3)]
    out = flatten_pilot_rewrites(rows)
    # a,b,c for item 0, then a,b,c for item 1, then a,b,c for item 2
    assert [r["item_id"] for r in out] == ["LMQ-0"] * 3 + ["LMQ-1"] * 3 + ["LMQ-2"] * 3


# --------------------------------------------------------------------------- #
# Malformed input -- must fail loudly and immediately, not emit a bad record
# --------------------------------------------------------------------------- #


def test_duplicate_item_id_raises():
    with pytest.raises(ValueError, match="duplicate item_id"):
        flatten_pilot_rewrites([_row(item_id="LMQ-1"), _row(item_id="LMQ-1")])


def test_missing_item_id_raises():
    row = _row()
    del row["item_id"]
    with pytest.raises(ValueError, match="missing item_id"):
        flatten_pilot_rewrites([row])


def test_options_missing_a_letter_raises():
    row = _row(options={"A": "a", "B": "b", "C": "c"})  # no D
    with pytest.raises(ValueError, match="options missing"):
        flatten_pilot_rewrites([row])


def test_gold_not_in_options_raises():
    with pytest.raises(ValueError, match="not among its own options"):
        flatten_pilot_rewrites([_row(gold="E")])


def test_empty_level_text_raises():
    with pytest.raises(ValueError, match="level_c"):
        flatten_pilot_rewrites([_row(level_c="")])


def test_whitespace_only_level_text_raises():
    with pytest.raises(ValueError, match="level_b"):
        flatten_pilot_rewrites([_row(level_b="   ")])


def test_missing_level_column_raises():
    row = _row()
    del row["level_a"]
    with pytest.raises(ValueError, match="level_a"):
        flatten_pilot_rewrites([row])


# --------------------------------------------------------------------------- #
# Real data -- catches anything the synthetic fixtures above don't think of
# --------------------------------------------------------------------------- #


def test_flatten_pilot_against_real_data():
    real_path = REPO_ROOT / "data" / "pilot_rewrites.jsonl"
    if not real_path.exists():
        pytest.skip("data/pilot_rewrites.jsonl not present (pilot not generated yet)")

    rows = [json.loads(line) for line in real_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    out = flatten_pilot_rewrites(rows)

    assert len(out) == len(rows) * 3
    assert len({r["item_id"] for r in out}) == len(rows)
    for r in out:
        assert r["level"] in ("a", "b", "c")
        assert r["stem"].strip()
        assert r["gold"] in r["options"]
        assert set("ABCD") <= set(r["options"])