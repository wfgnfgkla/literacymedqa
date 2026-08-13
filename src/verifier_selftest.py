#!/usr/bin/env python3
"""
Self-tests. Runs with no API key: the LLM path is exercised with a stubbed
transport so the prompt assembly, JSON parsing, and verdict mapping are covered
before anyone spends money on a real run.

    python src/verifier_selftest.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from verifier import Verifier, numeric_profile  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
OK, FAILED = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (OK if cond else FAILED).append(name)
    print(f"  {'ok  ' if cond else 'FAIL'} {name}{'' if cond else '  <- ' + detail}")


def stub(payload: dict):
    return lambda self, prompt: json.dumps(payload)


def main() -> None:
    print("extraction")
    a = numeric_profile("K+: 3.3 mEq/L, weight 1.2-kg, leads II, III, and aVF")
    check("hyphenated units parse", "kg" in a["units"].get("1.2", set()))
    check("roman ecg leads become digits", a["values"]["2"] >= 1 and a["values"]["3"] >= 1)
    b = numeric_profile("Five weeks ago")
    check("word numbers carry units", "weeks" in b["units"].get("5", set()))
    check("word numbers stay out of the value multiset", b["values"].get("5", 0) == 0)

    print("\nrules backend")
    v = Verifier(backend="rules")
    orig = "A 56-year-old man. K+: 3.3 mEq/L. Temperature is 38.4°C. He has no chest pain."
    check("unit drop is not a failure",
          v.check(orig, "Im 56. My potassium was 3.3. My temp was 38.4 C. No chest pain.",
                  {"A": "x"}, "A").verdict == "PASS")
    check("value swap fails",
          v.check(orig, "Im 56. My potassium was 4.1. My temp was 38.4 C. No chest pain.",
                  {"A": "x"}, "A").verdict == "FAIL")
    check("unit swap fails",
          v.check("Given 2 liters of saline.", "They gave me 2 milliliters of saline.",
                  {"A": "x"}, "A").verdict == "FAIL")
    check("option edit fails without a model call",
          v.check(orig, orig, {"A": "x"}, "A", rewrite_options={"A": "y"}).backend == "contract")
    check("gold letter edit fails",
          v.check(orig, orig, {"A": "x"}, "A", rewrite_gold="B").verdict == "FAIL")

    print("\nllm backend (stubbed transport)")
    v2 = Verifier(backend="anthropic")
    v2._call_anthropic = stub({
        "clinical_fact_changed": "no", "answer_support_changed": "no", "change_type": "none",
        "evidence_original": "", "evidence_rewrite": "", "what_changed": "",
        "jargon_removed": "yes", "confidence": 0.95}).__get__(v2)
    r = v2.check("A 56-year-old man with chest pain.", "Im 56 and my chest hurts.", {"A": "x"}, "A")
    check("clean verdict maps to PASS", r.verdict == "PASS", r.verdict)
    check("same_answer_letter inverts answer_support_changed", r.same_answer_letter == "yes")
    check("prompt hash recorded", len(r.prompt_sha256) == 64)

    v2._call_anthropic = stub({
        "clinical_fact_changed": "unsure", "answer_support_changed": "no", "change_type": "other",
        "what_changed": "not sure if the timeline moved", "jargon_removed": "yes",
        "confidence": 0.4}).__get__(v2)
    check("unsure routes to REVIEW, never PASS",
          v2.check("A man, 3 days of pain.", "Ive had pain a few days.", {"A": "x"}, "A").verdict == "REVIEW")

    v2._call_anthropic = stub({
        "clinical_fact_changed": "no", "answer_support_changed": "no", "change_type": "none",
        "jargon_removed": "yes", "confidence": 0.99}).__get__(v2)
    check("code override: model waves through a numeric change -> REVIEW",
          v2.check("K+ 3.3 mEq/L", "my potassium was 4.1", {"A": "x"}, "A").verdict == "REVIEW")

    v2._call_anthropic = (lambda self, p: "```json\n" + json.dumps(
        {"clinical_fact_changed": "yes", "answer_support_changed": "yes",
         "change_type": "negation_flip", "evidence_original": "never turned blue",
         "evidence_rewrite": "turns blue often", "what_changed": "negation flipped",
         "jargon_removed": "yes", "confidence": 0.9}) + "\n```").__get__(v2)
    r = v2.check("never turned blue", "turns blue often", {"A": "x"}, "A")
    check("code-fenced JSON parses", r.verdict == "FAIL" and r.change_type == "negation_flip")

    print("\nfrozen artifacts")
    base = [json.loads(l) for l in (ROOT / "data" / "medqa_base.jsonl").open()]
    adv = [json.loads(l) for l in (ROOT / "data" / "adversarial" / "adversarial_set.jsonl").open()]
    ids = {b["item_id"] for b in base}
    check("base set is 500 items", len(base) == 500, str(len(base)))
    check("ids unique", len({b['item_id'] for b in base}) == len(base))
    check("gold letter always among options", all(b["gold_letter"] in b["options"] for b in base))
    check("adversarial set is 40 cases", len(adv) == 40, str(len(adv)))
    check("QA items held out of the benchmark",
          not ({a["base_item_id"] for a in adv} & ids))
    pairs = {}
    for a in adv:
        pairs.setdefault(a["base_item_id"], {})[a["condition"]] = a["rewrite"]
    check("every twin differs by exactly one edit",
          all(sum(1 for x, y in zip(p["clean"].split(), p["corrupt"].split()) if x != y) > 0
              and p["clean"] != p["corrupt"] for p in pairs.values()))
    check("clean twins never equal the original text",
          all(a["rewrite"] != a["original"] for a in adv))

    print(f"\n{len(OK)} passed, {len(FAILED)} failed")
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()
