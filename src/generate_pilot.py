#!/usr/bin/env python3
"""
Task 2 -- generate the LiteracyMedQA literacy-level rewrites.

For every item in the frozen base set, this makes ONE call to the pinned rewriter
and gets back both generated literacy levels as JSON. It writes one row per item to
data/pilot_rewrites.jsonl.

    LEVEL (a)  original_clinical        COPIED VERBATIM from the frozen stem.
    LEVEL (b)  plain_firstperson        generated
    LEVEL (c)  lowliteracy_firstperson  generated

Level (a) is never sent to a model. It is the control arm: paraphrasing it, even
harmlessly, destroys the thing it controls for. If you ever find this script
generating level (a), that is a bug, not a feature.

Every row carries `prompt_hash` and `rewriter_version`, including rows that failed.
Appendix A's reproducibility claim rests on those two fields being present on every
single item, so they are written before the call is attempted, not after it succeeds.

Commands
    python src/generate_pilot.py --dry-run
    python src/generate_pilot.py --limit 5
    python src/generate_pilot.py
    python src/generate_pilot.py --resume

Environment
    OPENROUTER_API_KEY   required unless --dry-run. Never written to disk.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import sys
import time
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError as exc:  # pragma: no cover
    raise SystemExit("pyyaml is required: pip install pyyaml") from exc

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cost_tracker import CostTracker  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "config.yaml"
DEFAULT_OUT = ROOT / "data" / "pilot_rewrites.jsonl"

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

# Mirrors harness.py's policy deliberately. Retry transient transport failures only.
RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}
MAX_RETRIES = 5
BASE_BACKOFF_SECONDS = 1.5
# harness.py uses 60s for max_tokens=16 single-letter answers. A rewrite is
# max_tokens=1200 across two levels, so the same ceiling would fire on healthy
# calls. Raised, not removed: a stuck connection must still fail loudly.
CLIENT_TIMEOUT_SECONDS = 120.0

# Number extraction for the fidelity check.
#
# The naive `\d+(?:\.\d+)?` produced a ~60% flag rate on the first 5-item sample and
# every single flag was spurious. Four distinct causes, all handled here:
#
#   "250,000/mm3"      -> split into 250 AND 000, and the mm3 unit yielded a bare 3
#   "1st step"         -> the ordinal in the question sentence yielded a bare 1
#   "36.5C (97.7F)"    -> the model keeps Celsius and drops the redundant Fahrenheit
#   "2 glasses"        -> level (b) legitimately writes "two glasses" in prose
#
# Still `(?:\.\d+)?` and never `\.?\d*` on the tail, so "potassium is 5.8." yields 5.8
# rather than swallowing the sentence period.
#
# The lookarounds are what kill the mm3/1st class: a digit welded to a letter is part
# of a unit or an ordinal, not a clinical quantity. The leading `.` in the lookbehind
# stops us restarting inside a decimal we already consumed.
# The `^` in the lookbehind catches exponent notation: "6,000/mm^3" writes the same
# superscript that "mm3" writes without a caret, and both are units rather than counts.
NUMBER_RE = re.compile(
    r"(?<![A-Za-z0-9.^])\d{1,3}(?:,\d{3})+(?:\.\d+)?(?![A-Za-z])"  # 250,000
    r"|"
    r"(?<![A-Za-z0-9.^])\d+(?:\.\d+)?(?![A-Za-z])"                 # 36.5, 98, 5.8
)

# Locants in hyphenated compound names: the 5 in "5-hydroxyindoleacetic acid" is part
# of the substance's name, not a quantity. v2's C1 rule tells the rewriter to render
# such a substance vaguely ("some acid thing"), which correctly loses the 5 -- so
# requiring it would flag the prompt for obeying its own instructions.
#
# The >=6-letter run is what separates a chemical name from a real measurement:
# "hydroxyindoleacetic" (19) and "hydroxyprogesterone" (19) qualify, while "6-pack"
# (4) and "3-cm" (2) do not and stay required.
COMPOUND_LOCANT = re.compile(r"(?<![A-Za-z0-9.])\d+(?:\.\d+)?-(?=[A-Za-z]{6,})")

# Parenthetical unit restatements. MedQA writes "36.5C (97.7F)" and "4 kg (8.8 lb)";
# a patient repeats one unit, not both. The number inside such a parenthetical is the
# same fact as the one outside it, so it is not independently required.
# MedQA writes the degree sign three ways: "°F", the single glyph "℉" (U+2109), and its
# Celsius twin "℃" (U+2103). Matching only "°F" left "(98.6℉)" unrecognised as a unit
# restatement, so its value was demanded of a rewrite that had correctly kept Celsius.
DEGREE = "°º℃℉"
# Units longest-first, closed by a not-a-letter lookahead rather than \b -- the degree
# glyphs are not word characters, so \b behaves differently around them. The lookahead
# is also what stops "(4 children)" matching "C" as a unit and being stripped whole,
# which would silently drop a required number.
CONVERSION_PAREN = re.compile(
    r"\(\s*[\d,]+(?:\.\d+)?\s*"
    r"(?:[" + DEGREE + r"]\s*)?"
    r"(?:[" + DEGREE + r"]|pounds|pound|ounces|ounce|inches|inch|feet|lbs|lb|oz|kg"
    r"|cm|mm|mL|ft|in|F|C|L|g)"
    r"(?![A-Za-z])[^)]*\)",
    re.IGNORECASE,
)

# Obstetric history is written as a code ("gravida 2, para 1", "G2P1") and retold as an
# ordinal ("my second baby", "my first pregnancy went fine"). The fact survives intact;
# only the surface form changes. Ordinals are accepted ONLY in this context -- adding
# "first" to the general vocabulary would let the common phrase "at first" silently
# satisfy any required 1.
OBSTETRIC = re.compile(r"\b(?:gravida|para)\s*(\d+)|\bG(\d+)P(\d+)\b", re.IGNORECASE)
ORDINALS = {1: "first", 2: "second", 3: "third", 4: "fourth", 5: "fifth",
            6: "sixth", 7: "seventh", 8: "eighth", 9: "ninth", 10: "tenth"}

# Demographics. Age and sex head the decisive-facts list, and first-person narration
# strips the pronoun cue that would otherwise carry sex -- so unless the rewrite states
# it, "a 45-year-old woman" becomes unrecoverable. A number-only check cannot see this:
# in the v3 pilot 56 of 100 items lost sex and every one passed silently.
AGE_RE = re.compile(r"(\d+)[-\s](?:year|yr|month|week|day)s?[-\s]old", re.IGNORECASE)
SEX_NOUN_RE = re.compile(
    r"\b(?:man|male|boy|gentleman|woman|female|girl|lady)\b", re.IGNORECASE)
MALE_MARKERS = re.compile(
    r"\b(man|male|boy|guy|dude|he|his|him|himself|father|dad|son|husband|brother|"
    r"gentleman|mr)\b", re.IGNORECASE)
FEMALE_MARKERS = re.compile(
    r"\b(woman|female|girl|gal|she|her|herself|mother|mom|daughter|wife|sister|"
    r"lady|mrs|ms)\b", re.IGNORECASE)
MALE_WORDS = {"man", "male", "boy", "gentleman"}
FEMALE_WORDS = {"woman", "female", "girl", "lady"}


def stem_age(stem: str) -> float | None:
    m = AGE_RE.search(stem)
    return float(m.group(1)) if m else None


def stem_sex(stem: str) -> str | None:
    """'M' / 'F' / None, from the first sex noun in the stem.

    The noun, not pronoun counts: MedQA opens "A 55-year-old man ...", and later
    pronouns in the same stem always agree with it.
    """
    m = SEX_NOUN_RE.search(stem)
    if not m:
        return None
    w = m.group(0).lower()
    return "M" if w in MALE_WORDS else ("F" if w in FEMALE_WORDS else None)

_ONES = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight",
         "nine", "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen",
         "sixteen", "seventeen", "eighteen", "nineteen"]
_TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy",
         "eighty", "ninety"]


def _spellings(value: float) -> list[str]:
    """Word forms a rewrite might use instead of the digits.

    Only whole numbers below 100. Nobody writes "thirty-six point five" for a
    temperature, and pretending otherwise would invent matches that mask real drops.
    """
    if value != int(value) or not (0 <= value < 100):
        return []
    n = int(value)
    if n < 20:
        return [_ONES[n]]
    tens, ones = divmod(n, 10)
    if ones == 0:
        return [_TENS[tens]]
    return [f"{_TENS[tens]}-{_ONES[ones]}", f"{_TENS[tens]} {_ONES[ones]}"]


def _numbers(text: str) -> set[float]:
    """Numeric values in a piece of text, comma groupings normalized.

    Values, not strings, so "37.0" and "37" are the same fact -- which they are.
    """
    text = COMPOUND_LOCANT.sub(" ", text)
    out: set[float] = set()
    for tok in NUMBER_RE.findall(text):
        try:
            out.add(float(tok.replace(",", "")))
        except ValueError:
            continue
    return out

# A rewrite this much shorter than its original has almost certainly dropped
# content wholesale rather than merely compressed register.
MIN_LENGTH_RATIO = 0.25

# Schema detection. The frozen file is Dong's; these are ordered candidate names,
# most-specific first. If none match we fail loudly with the real keys rather than
# guessing and silently rewriting the wrong field.
ID_KEYS = ("item_id", "id", "uid", "qid", "question_id")
STEM_KEYS = ("question", "stem", "vignette", "clinical_stem", "text", "body")
GOLD_KEYS = ("gold_letter", "answer_idx", "gold", "correct_answer", "answer_key", "label")
OPTION_KEYS = ("options", "choices", "answer_options", "opts")


class MalformedRewrite(Exception):
    """The model returned something that isn't the required JSON object.

    Deliberately NOT retryable. At temperature 0 a malformed response is a content
    problem, and re-asking an identical question gets an identical answer while
    billing for it again.
    """


class TransientAPIError(Exception):
    """A retryable transport failure that survived MAX_RETRIES."""


# --------------------------------------------------------------------------- #
# Config, prompt, frozen set
# --------------------------------------------------------------------------- #

def load_config(path: str | Path) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def load_prompt(path: str | Path) -> tuple[str, str]:
    """Return (template_text, full sha256 hex).

    Full digest, not harness.py's 12-char display prefix: this value goes into the
    reproducibility appendix, where a truncated hash is a weaker claim than a whole
    one and costs nothing to avoid.
    """
    text = Path(path).read_text(encoding="utf-8")
    return text, hashlib.sha256(text.encode("utf-8")).hexdigest()


def render_prompt(template: str, stem: str) -> str:
    """Substitute the stem into the frozen template.

    str.replace, never str.format: the template contains literal JSON braces in its
    worked examples, and .format() would read every one of them as a field
    reference and raise. That is also why the placeholder is not escaped anywhere.
    """
    if "{stem}" not in template:
        raise SystemExit(
            "Rewriter prompt has no {stem} placeholder -- refusing to send a "
            "prompt with no item in it."
        )
    return template.replace("{stem}", stem)


def detect_keys(rows: list[dict]) -> dict[str, str]:
    """Work out which keys hold id / stem / gold / options.

    Checked against the union of keys across all rows, so one ragged row doesn't
    dictate the schema. Options is optional (it is carried through to the output but
    nothing here reasons about it); the other three are required.
    """
    present: set[str] = set()
    for r in rows:
        present.update(r.keys())

    def pick(candidates: tuple[str, ...]) -> str | None:
        for c in candidates:
            if c in present:
                return c
        return None

    found = {
        "id": pick(ID_KEYS),
        "stem": pick(STEM_KEYS),
        "gold": pick(GOLD_KEYS),
        "options": pick(OPTION_KEYS),
    }
    missing = [k for k in ("id", "stem", "gold") if found[k] is None]
    if missing:
        raise SystemExit(
            "Could not detect required field(s) "
            f"{missing} in the frozen set.\n"
            f"  Keys actually present: {sorted(present)}\n"
            f"  Candidates tried:\n"
            f"    id    {list(ID_KEYS)}\n"
            f"    stem  {list(STEM_KEYS)}\n"
            f"    gold  {list(GOLD_KEYS)}\n"
            "Add the real key name to the candidate list at the top of this file "
            "rather than renaming the frozen file."
        )
    return found


def load_frozen_set(path: str | Path | None) -> tuple[list[dict], dict[str, str]]:
    if not path:
        raise SystemExit("dataset.base_file is not set in config.")
    p = Path(path)
    if not p.is_absolute():
        p = ROOT / p
    if not p.exists():
        raise SystemExit(
            f"Frozen base set not found: {p}\n"
            "It is gitignored by design. Rebuild it with:\n"
            "  python src/sample_and_freeze.py freeze --n 500 --seed 20260811 "
            "--exclude-file data/adversarial/held_out_ids.txt"
        )
    rows: list[dict] = []
    with open(p, "r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise SystemExit(f"{p}:{lineno} is not valid JSON: {exc}") from exc
    if not rows:
        raise SystemExit(f"Frozen base set {p} is empty.")
    return rows, detect_keys(rows)


# --------------------------------------------------------------------------- #
# Structural checks -- flag, never discard
# --------------------------------------------------------------------------- #

def structural_flags(stem: str, level_b: str, level_c: str) -> list[str]:
    """Cheap mechanical checks on a rewrite pair.

    These FLAG. They do not drop, and they do not gate. A dropped lab value and a
    terse-but-faithful rewrite look similar from here, and telling them apart is the
    verifier's job and the manual audit's job, not a regex's. Recording the flag and
    keeping the item preserves that decision for someone who can make it.
    """
    flags: list[str] = []
    # Conversion parentheticals stripped BEFORE extraction, so the Fahrenheit in
    # "36.5C (97.7F)" is never required in the first place.
    required = _numbers(CONVERSION_PAREN.sub(" ", stem))

    obstetric = {float(g) for m in OBSTETRIC.finditer(stem) for g in m.groups() if g}
    age = stem_age(stem)
    sex = stem_sex(stem)

    for label, text in (("b", level_b), ("c", level_c)):
        if text is None:
            flags.append(f"missing_level_{label}")
            continue
        if not text.strip():
            flags.append(f"empty_level_{label}")
            continue
        if len(text) < MIN_LENGTH_RATIO * len(stem):
            flags.append(f"short_level_{label}")

        # Compared as numeric values, not substrings: a substring test for "3" would
        # be satisfied by the "3" inside "37", silently passing an item whose 3 really
        # did go missing.
        present = _numbers(text)
        lowered = text.lower()

        def satisfied(v: float) -> bool:
            if v in present:
                return True
            if any(w in lowered for w in _spellings(v)):
                return True
            # "gravida 2" retold as "my second baby".
            if v in obstetric and ORDINALS.get(int(v), "\0") in lowered:
                return True
            return False

        missing = sorted(v for v in required if not satisfied(v))
        if missing:
            flags.append(f"dropped_numbers_{label}:{','.join(f'{v:g}' for v in missing)}")

        # Demographics, checked separately because sex is not a number and the loop
        # above is structurally incapable of seeing it.
        if age is not None and not satisfied(age):
            flags.append(f"missing_age_{label}")
        if sex is not None:
            pattern = MALE_MARKERS if sex == "M" else FEMALE_MARKERS
            if not pattern.search(text):
                flags.append(f"missing_sex_{label}")

    return flags


# --------------------------------------------------------------------------- #
# OpenRouter client
# --------------------------------------------------------------------------- #

def build_client():
    """OpenRouter speaks the OpenAI wire format, so the openai SDK works against it.

    This does NOT go through harness.py: harness routes azure_openai / nvidia_build /
    local_transformers and has no openrouter branch, and the provider-pinning
    extra_body below has no equivalent there.
    """
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise SystemExit(
            "OPENROUTER_API_KEY is not set.\n"
            "  PowerShell:  $env:OPENROUTER_API_KEY = '...'\n"
            "  bash:        export OPENROUTER_API_KEY='...'\n"
            "Never put the key in config.yaml or any tracked file."
        )
    try:
        from openai import OpenAI
    except ImportError as exc:  # pragma: no cover
        raise SystemExit("openai is required: pip install openai") from exc

    return OpenAI(
        api_key=api_key,
        base_url=OPENROUTER_BASE_URL,
        timeout=CLIENT_TIMEOUT_SECONDS,
        # The SDK's own retries would stack with call_rewriter's policy and make
        # observed backoff meaningless. One retry policy, defined in one place.
        max_retries=0,
    )


def _extract_served_by(resp: Any) -> str | None:
    """Which upstream actually served this call.

    OpenRouter fans a single model string out across upstreams that differ in
    quantization, so 'openai/gpt-4o-mini' alone does not identify what ran. The
    field is an OpenRouter extension, so it arrives outside the typed OpenAI schema
    and has to be dug out of the extras.
    """
    candidates = (
        lambda: getattr(resp, "provider", None),
        lambda: (getattr(resp, "model_extra", None) or {}).get("provider"),
        lambda: resp.model_dump().get("provider"),
    )
    for get in candidates:
        try:
            value = get()
        except Exception:
            continue
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _strip_fences(text: str) -> str:
    t = text.strip()
    if not t.startswith("```"):
        return t
    lines = t.splitlines()[1:]
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def parse_rewrite(raw: str) -> tuple[str, str]:
    """Pull level_b and level_c out of the model's reply.

    The prompt forbids markdown fences; models emit them anyway, and a fence is a
    formatting artifact rather than a content failure, so it is stripped rather than
    counted against the item. Anything past that is a real malformed response.
    """
    text = _strip_fences(raw)
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise MalformedRewrite(f"no JSON object in response: {raw[:200]!r}")
        try:
            obj = json.loads(text[start:end + 1])
        except json.JSONDecodeError as exc:
            raise MalformedRewrite(f"unparseable JSON: {exc}: {raw[:200]!r}") from exc

    if not isinstance(obj, dict):
        raise MalformedRewrite(f"expected a JSON object, got {type(obj).__name__}")

    missing = [k for k in ("level_b", "level_c") if k not in obj]
    if missing:
        raise MalformedRewrite(f"response missing {missing}: got keys {sorted(obj)}")

    # Coerced to str rather than type-checked: a model returning a number or null
    # here is malformed, but the structural checks downstream report *which* level
    # is empty far more usefully than a type error would.
    return str(obj["level_b"]), str(obj["level_c"])


def call_rewriter(client, rewriter: dict, rendered: str) -> tuple[str, Any]:
    """One rewrite call, with retries on transport failures only."""
    from openai import APIConnectionError, APIStatusError, APITimeoutError

    kwargs: dict[str, Any] = dict(
        model=rewriter["name"],
        messages=[{"role": "user", "content": rendered}],
        temperature=rewriter.get("temperature", 0),
        max_tokens=rewriter.get("max_tokens", 1200),
        extra_body={
            "provider": {
                # Without this OpenRouter may silently fall back to another upstream
                # mid-run, which would make the version pin -- and the whole
                # reproducibility claim -- describe something that didn't happen.
                "allow_fallbacks": False,
                # Refuse an upstream that would quietly ignore temperature/seed
                # rather than serving a differently-sampled result under the pin.
                "require_parameters": True,
            }
        },
    )
    if rewriter.get("top_p") is not None:
        kwargs["top_p"] = rewriter["top_p"]
    # Same reasoning as harness.py: an explicit null seed is a different wire value
    # than an absent one, and some OpenAI-compatible servers reject it.
    if rewriter.get("seed") is not None:
        kwargs["seed"] = rewriter["seed"]

    last_exc: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = client.chat.completions.create(**kwargs)
        except (APIConnectionError, APITimeoutError) as exc:
            last_exc = exc
        except APIStatusError as exc:
            if getattr(exc, "status_code", None) not in RETRYABLE_STATUS:
                raise  # non-transient: fail fast rather than burn the budget
            last_exc = exc
        else:
            if not getattr(resp, "choices", None):
                # Content filtering and provider edge cases can return an empty
                # choices list; resp.choices[0] would then raise a bare IndexError
                # that the per-item handler isn't looking for.
                raise MalformedRewrite("response contained no choices")
            return resp.choices[0].message.content or "", resp

        if attempt < MAX_RETRIES:
            time.sleep(BASE_BACKOFF_SECONDS * (2 ** (attempt - 1)) + random.uniform(0, 0.5))

    raise TransientAPIError(f"exhausted {MAX_RETRIES} retries: {last_exc}") from last_exc


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #

def write_row(path: Path, row: dict) -> None:
    """Append one row and fsync it.

    One line at a time, flushed and fsynced, because a crash at item 87 should cost
    item 87 and not the 86 paid-for calls before it.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def already_done(path: Path) -> set[str]:
    if not path.exists():
        return set()
    done: set[str] = set()
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue  # a torn tail line from a hard kill; it will be redone
            if "item_id" in row:
                done.add(row["item_id"])
    return done


def estimate_tokens(text: str) -> int:
    """Rough character-based estimate. Approximate on purpose.

    Deliberately not tiktoken: the real tokenizer for whatever upstream OpenRouter
    picks isn't knowable before the call, and a precise-looking number derived from
    the wrong tokenizer is worse than an obviously rough one. Real token counts come
    back from the API and are what the cost tracker logs.
    """
    return max(1, len(text) // 4)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--config", default=str(DEFAULT_CONFIG))
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--limit", type=int, default=None,
                    help="Only process the first N items (after --resume filtering).")
    ap.add_argument("--dry-run", action="store_true",
                    help="Show the plan and a token estimate. Makes no API calls.")
    ap.add_argument("--resume", action="store_true",
                    help="Skip item_ids already present in the output file.")
    args = ap.parse_args()

    cfg = load_config(args.config)
    run_cfg = cfg.get("run", {})
    rewriter = cfg.get("models", {}).get("rewriter", {})
    gen_cfg = cfg.get("generation", {})

    config_version = run_cfg.get("config_version")
    lock_status = run_cfg.get("lock_status")
    pilot_n = run_cfg.get("pilot_n")

    if rewriter.get("provider") != "openrouter":
        raise SystemExit(
            f"This script speaks OpenRouter only, but models.rewriter.provider is "
            f"{rewriter.get('provider')!r}. Point it at openrouter or use harness.py."
        )
    if not rewriter.get("name"):
        raise SystemExit("models.rewriter.name is not set.")
    if not rewriter.get("version"):
        # The task requires a model version on every row. A null here would write
        # the key with nothing in it -- satisfying a presence check while making the
        # reproducibility appendix untrue.
        raise SystemExit(
            "models.rewriter.version is null. Every row must carry a real version "
            "string -- refusing to write rows whose version field is empty."
        )

    prompt_path = gen_cfg.get("rewriter_prompt")
    if not prompt_path:
        raise SystemExit("generation.rewriter_prompt is not set in config.")
    template, prompt_hash = load_prompt(ROOT / prompt_path)

    rows, keys = load_frozen_set(cfg.get("dataset", {}).get("base_file"))

    out_path = Path(args.out)
    if not out_path.is_absolute():
        out_path = ROOT / out_path

    skip = already_done(out_path) if args.resume else set()

    # Scope is decided from the FULL frozen set, BEFORE the resume filter. Slicing
    # after the skip list would let --resume change the target: with 100 rows already
    # written and n_items=300, slicing the 400 remaining would generate 300 more and
    # land at 400 total, not 300.
    #
    # dataset.n_items is READ HERE and not merely declared. It used to be read only by
    # validate_config.py, so it capped nothing: the run was bounded by pilot_mode or by
    # the size of base_file, and n_items agreed with the outcome only because it
    # happened to equal the frozen set size. Same shape of trap pilot_mode was.
    n_items = (cfg.get("dataset") or {}).get("n_items")
    if run_cfg.get("pilot_mode") and pilot_n:
        target, scope = rows[:pilot_n], f"pilot_mode on, pilot_n={pilot_n}"
    elif n_items:
        if n_items > len(rows):
            raise SystemExit(
                f"dataset.n_items={n_items} exceeds the frozen set: "
                f"{len(rows)} items in {cfg['dataset']['base_file']}. "
                "Refusing to generate fewer items than the config declares. Either lower "
                "n_items or re-freeze a larger base set -- silently producing "
                f"{len(rows)} while the config says {n_items} is how a sample size stops "
                "meaning anything."
            )
        target, scope = rows[:n_items], f"dataset.n_items={n_items}"
    else:
        target, scope = rows, "entire frozen set (n_items unset)"

    pending = [r for r in target if r[keys["id"]] not in skip]

    # --limit stays a smoke-test knob and deliberately applies AFTER the skip list:
    # it means "give me N more calls", not "make the target N".
    if args.limit is not None:
        pending = pending[:args.limit]
        scope = f"--limit {args.limit} (overrides {scope})"

    print(f"  config          {args.config} (v{config_version}, lock_status={lock_status})")
    print(f"  rewriter        {rewriter['name']} @ {rewriter['version']}")
    print(f"  provider        openrouter (allow_fallbacks=False, require_parameters=True)")
    print(f"  prompt          {prompt_path}")
    print(f"  prompt_hash     {prompt_hash}")
    print(f"  temperature     {rewriter.get('temperature')}  top_p {rewriter.get('top_p')}  "
          f"max_tokens {rewriter.get('max_tokens')}  seed {rewriter.get('seed')}")
    print(f"  frozen set      {cfg.get('dataset', {}).get('base_file')} ({len(rows)} items)")
    print(f"  detected keys   id={keys['id']} stem={keys['stem']} gold={keys['gold']} "
          f"options={keys['options']}")
    print(f"  output          {out_path}")
    if args.resume:
        print(f"  resume          skipping {len(skip)} already-written items")
    print(f"  scope           {scope}")
    print(f"  to process      {len(pending)} items")
    print(f"  levels          (a) copied verbatim | (b) generated | (c) generated")

    if not pending:
        print("\nNothing to do.")
        return 0

    if args.dry_run:
        est_in = sum(estimate_tokens(render_prompt(template, str(r[keys["stem"]]))) for r in pending)
        est_out = len(pending) * int(rewriter.get("max_tokens", 1200))
        print("\n  DRY RUN -- no API calls made.")
        print(f"  est. input tokens   ~{est_in:,} (rough, chars/4)")
        print(f"  est. output tokens  <={est_out:,} (max_tokens ceiling, not a prediction)")
        rates = (cfg.get("cost_rates_usd_per_1m") or {}).get(rewriter["name"]) or {}
        if rates.get("input") is None or rates.get("output") is None:
            print(f"  est. cost           unavailable -- no cost rate for "
                  f"'{rewriter['name']}' in cost_rates_usd_per_1m")
        else:
            ceiling = est_in / 1e6 * rates["input"] + est_out / 1e6 * rates["output"]
            print(f"  est. cost           <=${ceiling:.4f}")
        print(f"\n  First item: {pending[0][keys['id']]}")
        print(f"  {str(pending[0][keys['stem']])[:300]}...")
        return 0

    client = build_client()
    tracker = CostTracker.from_config(args.config)

    clean = flagged = failed = 0
    served: dict[str, int] = {}

    for i, item in enumerate(pending, 1):
        item_id = item[keys["id"]]
        stem = str(item[keys["stem"]])

        # Built before the call, so a failure still produces a row carrying the hash
        # and version. The task requires them on every single item, and "every item
        # that happened to succeed" is a different, weaker claim.
        row: dict[str, Any] = {
            "item_id": item_id,
            "level_a": stem,          # COPIED. Never generated. See module docstring.
            "gold": item.get(keys["gold"]),
            "options": item.get(keys["options"]) if keys["options"] else None,
            "prompt_hash": prompt_hash,
            "rewriter_model": rewriter["name"],
            "rewriter_version": rewriter["version"],
            "config_version": config_version,
            "lock_status": lock_status,
            "served_by": None,
            "structural_flags": [],
        }

        try:
            rendered = render_prompt(template, stem)
            raw, resp = call_rewriter(client, rewriter, rendered)

            served_by = _extract_served_by(resp)
            row["served_by"] = served_by
            key = served_by or "unknown"
            served[key] = served.get(key, 0) + 1

            usage = getattr(resp, "usage", None)
            if usage is not None:
                tracker.log(
                    stage="rewrite",
                    model=rewriter["name"],
                    input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                    output_tokens=getattr(usage, "completion_tokens", 0) or 0,
                    item_id=item_id,
                    level="bc",
                    served_by=served_by,
                    rewriter_version=rewriter["version"],
                )

            level_b, level_c = parse_rewrite(raw)
            row["level_b"] = level_b
            row["level_c"] = level_c
            row["structural_flags"] = structural_flags(stem, level_b, level_c)

            if row["structural_flags"]:
                flagged += 1
                status = "FLAGGED " + ",".join(row["structural_flags"])[:60]
            else:
                clean += 1
                status = "clean"

        except MalformedRewrite as exc:
            row["error"] = f"malformed_response: {exc}"
            failed += 1
            status = "FAILED (malformed)"
        except TransientAPIError as exc:
            row["error"] = f"transient_api_error: {exc}"
            failed += 1
            status = "FAILED (transport)"
        except Exception as exc:  # noqa: BLE001
            # One bad item must not take down a paid run mid-flight.
            row["error"] = f"{type(exc).__name__}: {exc}"
            failed += 1
            status = f"FAILED ({type(exc).__name__})"

        write_row(out_path, row)
        print(f"  [{i}/{len(pending)}] {item_id}  {status}")

    print(f"\n  clean {clean}   flagged {flagged}   failed {failed}")
    if served:
        print(f"  served_by: {json.dumps(served)}")
        if len([k for k in served if k != "unknown"]) > 1:
            print("  WARNING: served_by varies across items. The version pin does not "
                  "identify a single upstream -- pin models.rewriter.upstream_provider "
                  "before making any reproducibility claim.")
    print()
    tracker.summary()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
