"""
Tests for src/answer_parser.py. No network access required -- run these first,
before anything that touches an API.

    pytest tests/test_parser.py -v
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from answer_parser import parse_answer  # noqa: E402


# Each case: (raw_input, expected_output)
CASES = [
    ("A", "A"),
    (" b ", "B"),
    ("C.", "C"),
    ("D\n", "D"),
    ("Answer: A", "A"),
    ("answer - b", "B"),
    ("(C)", "C"),
    ("**D**", "D"),
    ("The answer is A", "A"),
    ("I think B is correct", "B"),
    ("A or B", None),
    ("E", None),
    ("", None),
    ("The patient has anemia", None),
    ("AA", None),
    ("A.", "A"),
    ("Both A and A are correct", "A"),
]


def test_fixtures():
    failures = []
    for raw, expected in CASES:
        got = parse_answer(raw)
        if got != expected:
            failures.append(f"parse_answer({raw!r}) = {got!r}, expected {expected!r}")
    assert not failures, "\n" + "\n".join(failures)


def test_none_input():
    assert parse_answer(None) is None


def test_whitespace_only():
    assert parse_answer("   \n\t  ") is None


def test_lowercase_word_not_matched_standalone():
    # A lowercase 'a' inside an ordinary word must never be mistaken for the letter.
    assert parse_answer("a good answer choice") is None


def test_case_insensitive_explicit_pattern():
    assert parse_answer("ANSWER: c") == "C"
    assert parse_answer("answer:d") == "D"


def test_custom_valid_letters():
    # parse_answer supports an arbitrary letter set, in case a future prompt
    # condition uses more/fewer than 4 options. Trivial case (stage 1 only):
    assert parse_answer("E", valid_letters=("A", "B", "C", "D", "E")) == "E"
    assert parse_answer("F", valid_letters=("A", "B", "C", "D", "E")) is None


def test_custom_valid_letters_through_fallback_stages():
    # The trivial case above only exercises stage 1 (whole response is one letter).
    # This exercises stages 2 and 3, which are where a hardcoded-to-A-D regex would
    # silently fail for a letter outside that range -- this is the case that
    # actually proves the letter set is honored, not just accepted as a parameter.
    letters = ("A", "B", "C", "D", "E")
    assert parse_answer("Answer: E", valid_letters=letters) == "E"
    assert parse_answer("(E)", valid_letters=letters) == "E"
    assert parse_answer("**E**", valid_letters=letters) == "E"
    assert parse_answer("I think E is correct", valid_letters=letters) == "E"


def test_ambiguous_explicit_pattern_stage():
    # Two distinct letters matched within the SAME stage must be ambiguous, not
    # resolved by falling through to a later stage.
    assert parse_answer("(A) or (B)?") is None


def test_log_unparseable_writes_record(tmp_path):
    from answer_parser import log_unparseable
    import json

    log_path = tmp_path / "parse_failures.jsonl"
    log_unparseable(
        log_path,
        item_id="medqa_00001",
        model="llama3",
        level="c",
        prompt_condition="plain",
        raw_output="not a real answer",
    )
    lines = log_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["item_id"] == "medqa_00001"
    assert record["raw_output"] == "not a real answer"
    assert "ts" in record


def test_log_unparseable_appends():
    import tempfile
    from answer_parser import log_unparseable
    import json

    with tempfile.TemporaryDirectory() as d:
        log_path = Path(d) / "parse_failures.jsonl"
        for i in range(3):
            log_unparseable(
                log_path,
                item_id=f"item_{i}",
                model="medgemma",
                level="c",
                prompt_condition="clarify_first",
                raw_output=f"garbage {i}",
            )
        lines = log_path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 3
        assert [json.loads(line)["item_id"] for line in lines] == [
            "item_0", "item_1", "item_2",
        ]
