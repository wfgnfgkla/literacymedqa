#!/usr/bin/env python3
"""
Task 2 -- the fidelity verifier: prompt, deterministic pre-checks, and harness.

A verdict has three parts.

1. Deterministic checks, always run, no model call. Answer options must be byte
   identical and the gold letter must be unchanged -- those are contract
   violations, not judgement calls, and a model should never be asked to rule on
   them. The same pass extracts number+unit pairs, negation cues, and
   demographic tokens for the record.

2. The rules backend, which turns those extractions into a verdict on its own.
   It is a cheap pre-filter and it is what lets the adversarial gate run with no
   API key. It is not a substitute for the LLM verifier: it cannot see a dropped
   finding or a reworded exposure.

3. The LLM backend, which runs the pinned prompt in src/prompts/ at temperature
   0 and returns the structured verdict.

Verdict is PASS, FAIL, or REVIEW. REVIEW exists so that "unsure" does not get
silently rounded to PASS -- a verifier with no abstain option quietly converts
its own uncertainty into a clean item.

  from verifier import Verifier
  v = Verifier(backend="rules")            # or "anthropic", "openai", "nvidia",
                                           # "rules+anthropic", "rules+openai", "rules+nvidia"
  r = v.check(original, rewrite, options, gold_letter)
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from dataclasses import dataclass, field, asdict
from pathlib import Path

PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "verifier_v1.txt"

CHANGE_TYPES = {
    "none", "altered_number", "unit_change", "changed_timeline", "changed_demographic",
    "negation_flip", "dropped_finding", "added_finding", "changed_question", "other",
}

# ---------------------------------------------------------------- extraction

UNIT = (r"mg/dL|g/dL|mEq/L|mmol/L|ng/mL|pg/mL|U/L|mOsmol/kg|mm3|/mm3|/hpf|mmHg|mm Hg|"
        r"kg/m2|micrograms?|milligrams?|milliliters?|millilitres?|kilograms?|grams?|"
        r"pounds?|ounces?|litres?|liters?|mcg|µg|mg|kg|lb|oz|mL|L\b|g\b|fL|%|/min|"
        r"°C|°F|C\b|F\b|cm|weeks?|days?|hours?|months?|years?|minutes?")
WORDNUM = {"one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6",
           "seven": "7", "eight": "8", "nine": "9", "ten": "10", "eleven": "11",
           "twelve": "12"}
NUM_UNIT = re.compile(rf"((?:\d[\d,]*(?:\.\d+)?)|(?:{'|'.join(WORDNUM)}))[\s\-]{{0,2}}({UNIT})", re.I)
ROMAN = {"i": "1", "ii": "2", "iii": "3", "iv": "4", "v": "5", "vi": "6"}
LEADS = re.compile(r"\bleads?\s+((?:[IVX]+|aV[RLF])(?:\s*,?\s*(?:and\s+)?(?:[IVX]+|aV[RLF]))*)")
BARE_NUM = re.compile(r"\b(\d[\d,]*(?:\.\d+)?)\b")
BP = re.compile(r"\b(\d{2,3})\s*/\s*(\d{2,3})\b")
AGE = re.compile(r"\b(\d{1,3})\s*[- ]?(?:year|month|week|day)s?[- ]?old\b", re.I)
AGE_LAY = re.compile(r"\b(?:im|i'm|i am|hes|he's|shes|she's|is)\s+(\d{1,3})\s+"
                     r"(?:and\b|years? old\b|months? old\b)", re.I)
NEG = re.compile(
    r"\b(no|not|never|non|denies|denied|without|absent|negative|didnt|didn't|doesnt|"
    r"doesn't|dont|don't|isnt|isn't|arent|aren't|wasnt|wasn't|cant|can't|couldnt|"
    r"couldn't|hasnt|hasn't|havent|haven't|unremarkable|none)\b", re.I)
SEX = re.compile(r"\b(man|woman|male|female|boy|girl|son|daughter|brother|sister|"
                 r"mother|father|he|she|his|her|him)\b", re.I)


_UNIT_ALIASES = {
    "liter": "l", "liters": "l", "litre": "l", "litres": "l",
    "milliliter": "ml", "milliliters": "ml", "millilitre": "ml", "millilitres": "ml",
    "milligram": "mg", "milligrams": "mg", "microgram": "mcg", "micrograms": "mcg",
    "µg": "mcg", "gram": "g", "grams": "g", "kilogram": "kg", "kilograms": "kg",
    "pound": "lb", "pounds": "lb", "ounce": "oz", "ounces": "oz",
    "week": "weeks", "day": "days", "hour": "hours", "month": "months",
    "year": "years", "minute": "minutes", "mmhg": "mmhg", "°c": "c", "°f": "f",
}


def _norm_unit(u: str) -> str:
    u = u.strip().lower().replace(" ", "")
    return _UNIT_ALIASES.get(u, u)


def _num(s: str) -> str:
    s = WORDNUM.get(s.strip().lower(), s)
    return f"{float(s.replace(',', '')):g}"


def _normalize(text: str) -> str:
    """Lay writers use digits where exam text uses Roman numerals for ECG leads."""
    def sub(m):
        body = re.sub(r"\b([IVX]+)\b", lambda r: ROMAN.get(r.group(1).lower(), r.group(1)), m.group(1))
        return "leads " + body
    return LEADS.sub(sub, text)


def numeric_profile(text: str) -> dict:
    """
    Values and their units, kept separate on purpose.

    A low-literacy rewrite drops units constantly -- "K+: 3.3 mEq/L" becomes
    "my potassium was 3.3". That is the paraphrase working, not a corruption.
    So values are compared as a multiset, and units only matter when the rewrite
    attaches a DIFFERENT one to a value it kept.
    """
    text = _normalize(text)
    values = Counter(_num(n) for n in BARE_NUM.findall(text))
    units = defaultdict(set)
    for n, u in NUM_UNIT.findall(text):
        if not n[0].isdigit():
            values[_num(n)] += 0  # word-numbers inform units, not the value multiset
        units[_num(n)].add(_norm_unit(u))
    return {"values": values, "units": dict(units),
            "bps": sorted(f"{a}/{b}" for a, b in BP.findall(text))}


def demographic_profile(text: str) -> dict:
    ages = sorted(set(AGE.findall(text)) | set(AGE_LAY.findall(text)))
    return {"ages": ages, "sex_terms": sorted({t.lower() for t in SEX.findall(text)})}


@dataclass
class Verdict:
    verdict: str                      # PASS | FAIL | REVIEW
    # the three fields the proposal specified, kept verbatim
    same_answer_letter: str = "unknown"        # yes | no | unsure | unknown
    clinical_fact_changed: str = "unknown"     # yes | no | unsure | unknown
    what_changed: str = ""
    # extensions
    change_type: str = "none"
    evidence_original: str = ""
    evidence_rewrite: str = ""
    jargon_removed: str = "unknown"
    confidence: float = 0.0
    options_identical: bool = True
    gold_letter_identical: bool = True
    deterministic_flags: list = field(default_factory=list)
    backend: str = ""
    prompt_sha256: str = ""
    model: str = ""
    latency_s: float = 0.0
    raw: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


class Verifier:
    def __init__(self, backend: str = "rules", model: str | None = None,
                 max_retries: int = 3, bare_number_tolerance: int = 0):
        self.backend = backend
        self.prompt = PROMPT_PATH.read_text(encoding="utf-8")
        self.prompt_sha = hashlib.sha256(self.prompt.encode()).hexdigest()
        self.max_retries = max_retries
        self.bare_tol = bare_number_tolerance
        self.model = model or {
            "anthropic": "claude-sonnet-4-6",
            "openai": "gpt-4o-2024-11-20",
            "nvidia": "qwen/qwen2.5-72b-instruct",
        }.get(backend.split("+")[-1], "rules-only")

    # ------------------------------------------------------------ layer 1
    @staticmethod
    def contract_checks(options_a: dict, options_b: dict, gold_a: str, gold_b: str) -> list[str]:
        flags = []
        if options_a != options_b:
            flags.append("options_not_identical")
        if gold_a != gold_b:
            flags.append("gold_letter_changed")
        return flags

    def surface_diff(self, original: str, rewrite: str) -> tuple[list[str], list[str]]:
        """Returns (hard_flags, soft_flags).

        Hard flags are corruptions: a value appeared that was not there before, a
        value was swapped, a unit changed, an age changed, negations collapsed.
        Soft flags are the signature of simplification -- values quietly dropped
        -- which is expected and never fails an item on its own.
        """
        hard, soft = [], []
        a, b = numeric_profile(original), numeric_profile(rewrite)

        lost = sorted((a["values"] - b["values"]).elements())
        gained = sorted((b["values"] - a["values"]).elements())
        if gained and lost:
            hard.append(f"numeric_value_changed lost={lost} gained={gained}")
        elif gained:
            hard.append(f"numeric_value_added gained={gained}")
        elif len(lost) > self.bare_tol:
            soft.append(f"numeric_value_dropped lost={lost}")

        for val, ua in a["units"].items():
            ub = b["units"].get(val)
            if ub and not (ub & ua):
                hard.append(f"unit_change {val}: {sorted(ua)} -> {sorted(ub)}")

        if a["bps"] != b["bps"] and (set(b["bps"]) - set(a["bps"])):
            hard.append(f"blood_pressure_changed {a['bps']} -> {b['bps']}")

        # No separate age rule. A changed age already shows up as a swapped value,
        # and lay phrasing ("Im 56") makes any age regex collide with vitals.

        # Negation is a SOFT signal only. Faithful rewrites reword "unremarkable"
        # and "no recurrence" into "normal" and "nothing came back", so counting
        # cues cannot separate a flip from a paraphrase. The LLM layer owns this.
        na, nb = len(NEG.findall(original)), len(NEG.findall(rewrite))
        if abs(na - nb) >= 2:
            soft.append(f"negation_count_shift {na} -> {nb}")
        return hard, soft

    # ------------------------------------------------------------ layer 3
    def _call_anthropic(self, prompt: str) -> str:
        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise RuntimeError("ANTHROPIC_API_KEY not set")
        body = json.dumps({
            "model": self.model, "max_tokens": 700, "temperature": 0,
            "messages": [{"role": "user", "content": prompt}],
        }).encode()
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages", data=body,
            headers={"content-type": "application/json", "x-api-key": key,
                     "anthropic-version": "2023-06-01"})
        with urllib.request.urlopen(req, timeout=120) as r:
            data = json.loads(r.read())
        return "".join(b.get("text", "") for b in data.get("content", []))

    def _call_openai(self, prompt: str) -> str:
        key = os.environ.get("OPENAI_API_KEY")
        if not key:
            raise RuntimeError("OPENAI_API_KEY not set")
        body = json.dumps({
            "model": self.model, "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "user", "content": prompt}],
        }).encode()
        req = urllib.request.Request(
            "https://api.openai.com/v1/chat/completions", data=body,
            headers={"content-type": "application/json", "authorization": f"Bearer {key}"})
        with urllib.request.urlopen(req, timeout=120) as r:
            data = json.loads(r.read())
        return data["choices"][0]["message"]["content"]

    def _call_nvidia(self, prompt: str) -> str:
        """NVIDIA Build. This is the backend config.yaml actually declares
        (models.verifier: nvidia_build / qwen/qwen2.5-72b-instruct), and the only
        one that satisfies the disjoint-sets rule now that openai/gpt-4o sits in
        models.evaluated -- gpt-4o must not grade text it will later be scored on.

        Same OpenAI-compatible surface and same base_url harness.py already uses
        for nvidia_build, so credentials and endpoint stay consistent repo-wide.
        """
        key = os.environ.get("NVIDIA_API_KEY")
        if not key:
            raise RuntimeError("NVIDIA_API_KEY not set")
        body = json.dumps({
            "model": self.model, "temperature": 0, "max_tokens": 700,
            # No response_format: NVIDIA Build does not accept it for every model,
            # and a rejected parameter would fail the call outright. The pinned
            # prompt already demands "a single JSON object and nothing else", and
            # _parse() strips fences defensively, so JSON mode buys nothing here.
            "messages": [{"role": "user", "content": prompt}],
        }).encode()
        req = urllib.request.Request(
            "https://integrate.api.nvidia.com/v1/chat/completions", data=body,
            headers={"content-type": "application/json", "authorization": f"Bearer {key}"})
        with urllib.request.urlopen(req, timeout=120) as r:
            data = json.loads(r.read())
        return data["choices"][0]["message"]["content"]

    @staticmethod
    def _parse(raw: str) -> dict:
        t = raw.strip()
        if t.startswith("```"):
            t = re.sub(r"^```(?:json)?|```$", "", t, flags=re.M).strip()
        i, j = t.find("{"), t.rfind("}")
        if i == -1 or j == -1:
            raise ValueError("no JSON object in response")
        return json.loads(t[i:j + 1])

    def _llm(self, original: str, rewrite: str, options: dict, gold: str) -> tuple[dict, str, float]:
        prompt = (self.prompt
                  .replace("<<<ORIGINAL>>>", original)
                  .replace("<<<REWRITE>>>", rewrite)
                  .replace("<<<OPTIONS>>>", json.dumps(options, ensure_ascii=False, indent=2))
                  .replace("<<<GOLD>>>", gold))
        if "anthropic" in self.backend:
            call = self._call_anthropic
        elif "nvidia" in self.backend:
            call = self._call_nvidia
        else:
            call = self._call_openai
        last, t0 = None, time.time()
        for attempt in range(self.max_retries):
            try:
                raw = call(prompt)
                return self._parse(raw), raw, time.time() - t0
            except (urllib.error.HTTPError, urllib.error.URLError, ValueError,
                    json.JSONDecodeError, RuntimeError) as e:
                last = e
                time.sleep(1.5 * (attempt + 1))
        raise RuntimeError(f"verifier call failed after {self.max_retries} attempts: {last}")

    # ------------------------------------------------------------ public
    def check(self, original: str, rewrite: str, options: dict, gold_letter: str,
              rewrite_options: dict | None = None, rewrite_gold: str | None = None) -> Verdict:
        ro = rewrite_options if rewrite_options is not None else options
        rg = rewrite_gold if rewrite_gold is not None else gold_letter

        contract = self.contract_checks(options, ro, gold_letter, rg)
        surface, soft = self.surface_diff(original, rewrite)
        flags = contract + surface + [f"soft:{s}" for s in soft]

        if contract:
            return Verdict(verdict="FAIL", same_answer_letter="no", clinical_fact_changed="yes",
                           what_changed="; ".join(contract), change_type="other",
                           options_identical="options_not_identical" not in contract,
                           gold_letter_identical="gold_letter_changed" not in contract,
                           deterministic_flags=flags, backend="contract",
                           prompt_sha256=self.prompt_sha, confidence=1.0)

        if self.backend == "rules" or (self.backend.startswith("rules+") and surface):
            changed = bool(surface)
            return Verdict(
                verdict="FAIL" if changed else "PASS",
                same_answer_letter="unsure" if changed else "unsure",
                clinical_fact_changed="yes" if changed else "no",
                what_changed="; ".join(surface),
                change_type=self._guess_type(surface),
                deterministic_flags=flags, backend="rules",
                prompt_sha256=self.prompt_sha, confidence=0.9 if changed else 0.5,
            )

        parsed, raw, dt = self._llm(original, rewrite, options, gold_letter)
        cfc = str(parsed.get("clinical_fact_changed", "unsure")).lower()
        asc = str(parsed.get("answer_support_changed", "unsure")).lower()
        ctype = str(parsed.get("change_type", "other")).lower()
        if ctype not in CHANGE_TYPES:
            ctype = "other"
        conf = float(parsed.get("confidence", 0.0) or 0.0)

        if cfc == "yes" or asc == "yes":
            verdict = "FAIL"
        elif "unsure" in (cfc, asc) or conf < 0.5:
            verdict = "REVIEW"
        else:
            verdict = "PASS"
        if surface and verdict == "PASS":
            verdict = "REVIEW"  # code saw a numeric change the model waved through

        return Verdict(
            verdict=verdict,
            same_answer_letter={"yes": "no", "no": "yes"}.get(asc, "unsure"),
            clinical_fact_changed=cfc,
            what_changed=str(parsed.get("what_changed", "")),
            change_type=ctype,
            evidence_original=str(parsed.get("evidence_original", "")),
            evidence_rewrite=str(parsed.get("evidence_rewrite", "")),
            jargon_removed=str(parsed.get("jargon_removed", "unknown")).lower(),
            confidence=conf, deterministic_flags=flags,
            backend=self.backend, prompt_sha256=self.prompt_sha, model=self.model,
            latency_s=round(dt, 2), raw=raw,
        )

    @staticmethod
    def _guess_type(surface: list[str]) -> str:
        s = " ".join(surface)
        if "age_differs" in s:
            return "changed_demographic"
        if "negation_count_shift" in s:
            return "negation_flip"
        if "blood_pressure_differs" in s or "numeric_pairs_differ" in s or "bare_numbers_lost" in s:
            return "altered_number"
        return "other"


if __name__ == "__main__":
    v = Verifier(backend="rules")
    a = "A 56-year-old man. K+: 3.3 mEq/L. He has no chest pain."
    print(v.check(a, "Im 56. My potassium was 3.3. I dont have chest pain.", {"A": "x"}, "A").verdict)
    print(v.check(a, "Im 56. My potassium was 4.1. I dont have chest pain.", {"A": "x"}, "A").verdict)
