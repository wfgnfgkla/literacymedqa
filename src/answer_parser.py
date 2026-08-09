"""
Answer parsing for LiteracyMedQA.

Eval prompts ask for a single letter (A-D) and nothing else. Real model output is not
always that clean. This module's only job is to recover the letter when it is genuinely
recoverable, and to say "I don't know" (None) rather than guess when it isn't.

The one rule that matters: never silently pick a letter when the text is ambiguous.
Guessing an answer would inject noise into the exact effect this study is trying to
measure. Unparseable responses are logged, not scored wrong -- see log_unparseable().

    from answer_parser import parse_answer

    parse_answer("A")              -> "A"
    parse_answer("Answer: B")      -> "B"
    parse_answer("A or B")         -> None   (ambiguous, two distinct letters)
    parse_answer("The patient has anemia") -> None   (lowercase 'a' in a word, not a letter)
"""

from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

VALID_LETTERS_DEFAULT = ("A", "B", "C", "D")

# Patterns are built per-call from the actual valid_letters (see _explicit_patterns
# / _standalone_pattern below), NOT hardcoded to A-D. re.compile() is internally
# cached by Python for identical pattern strings, so calling it every parse_answer()
# invocation costs a dict lookup, not a real recompilation -- this doesn't need its
# own cache to stay cheap.
#
# KNOWN EDGE CASE (reviewed, not fixed): the explicit-pattern stage is case-
# insensitive on the captured letter, so "Answer: a good choice" would parse as "A"
# -- "a" the indefinite article is indistinguishable from "a" the letter in this
# position. Not fixed because: (1) it requires a model to violate the "respond with
# ONLY the letter" instruction in a specific way that's already a bigger problem
# than the parse; (2) making the capture case-sensitive here would break the
# intentional "answer:d" -> "D" behavior tested in test_parser.py, which is a real
# and more common case (models that just answer in lowercase). Flagging this in
# case it shows up in a real parse-failure audit later.


def _letters_char_class(valid: set[str]) -> str:
    """Build a regex character-class body (the part between [ and ]) from the
    actual valid letter set, escaped defensively even though real usage is always
    plain A-Z single characters."""
    return "".join(re.escape(letter) for letter in sorted(valid))


def _explicit_patterns(valid: set[str]) -> list[re.Pattern]:
    cls = _letters_char_class(valid)
    return [
        re.compile(rf"ANSWER\s*[:\-]?\s*\(?([{cls}])\)?\b", re.IGNORECASE),
        re.compile(rf"\(([{cls}])\)"),
        re.compile(rf"\*\*([{cls}])\*\*", re.IGNORECASE),
    ]


def _standalone_pattern(valid: set[str]) -> re.Pattern:
    # Case-sensitive on purpose: lowercase "a" inside a normal word ("anemia") must
    # not match. See the case-insensitivity caveat above for the one place that
    # protection doesn't fully extend to.
    cls = _letters_char_class(valid)
    return re.compile(rf"\b([{cls}])\b")


_write_lock = threading.Lock()


def parse_answer(raw: str, valid_letters: Iterable[str] = VALID_LETTERS_DEFAULT) -> str | None:
    """
    Parse a single A-D answer out of raw model output.

    Returns the letter (uppercase, one of valid_letters) on success, or None if the
    response is empty, contains no recognizable letter, or is ambiguous (two or more
    distinct letters found at the same parsing stage).
    """
    valid = set(valid_letters)
    if raw is None:
        return None

    text = raw.strip()
    if not text:
        return None

    # Stage 1: the whole response, stripped of surrounding whitespace and a single
    # trailing period, is exactly one valid letter (case-insensitive).
    stage1 = text.rstrip(".").strip().upper()
    if stage1 in valid:
        return stage1

    # Stage 2: explicit patterns, e.g. "Answer: B", "(C)", "**D**".
    for pattern in _explicit_patterns(valid):
        matches = {m.group(1).upper() for m in pattern.finditer(text)}
        matches &= valid
        if len(matches) == 1:
            return next(iter(matches))
        if len(matches) > 1:
            return None  # ambiguous within this stage -- do not fall through

    # Stage 3: any standalone, word-bounded, UPPERCASE letter anywhere in the text.
    matches = {m.group(1) for m in _standalone_pattern(valid).finditer(text)}
    matches &= valid
    if len(matches) == 1:
        return next(iter(matches))
    # zero matches -> unparseable; two-or-more distinct matches -> ambiguous.
    # Both return None; the caller doesn't need to distinguish the reason to score
    # correctly, but log_unparseable() records the raw text either way for audit.
    return None


def log_unparseable(
    log_path: str | Path,
    *,
    item_id: str,
    model: str,
    level: str,
    prompt_condition: str,
    raw_output: str,
) -> None:
    """
    Append one record to the parse-failure log. Called by the harness whenever
    parse_answer() returns None, per config.yaml's parsing.log_unparseable: true.

    Thread-safe append, matching the pattern in cost_tracker.py (a crash mid-run
    must not corrupt or lose the file).
    """
    path = Path(log_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "item_id": item_id,
        "model": model,
        "level": level,
        "prompt_condition": prompt_condition,
        "raw_output": raw_output,
    }
    line = json.dumps(record, ensure_ascii=False)
    with _write_lock:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
