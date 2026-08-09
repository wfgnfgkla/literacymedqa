"""
Inference harness for LiteracyMedQA.

One function is the whole public surface:

    run_model(model_id, item, prompt_template, tracker=..., mock=...)

Everything else in this file exists to make that one call correct: reading model
config, routing to the right OpenAI-compatible endpoint (Azure / NVIDIA Build / local
vLLM all speak the same API, so there is exactly one client code path), retrying only
on transient failures, and logging every call through CostTracker.

Provider credentials come from environment variables (Kaggle Secrets sets these):
    AZURE_OPENAI_API_KEY, AZURE_OPENAI_ENDPOINT      (provider: azure_openai)
    NVIDIA_API_KEY                                   (provider: nvidia_build)
    (none needed for provider: local_hf -- vLLM's local server takes any key)

If a model's provider key is missing, that model is skipped with a printed warning,
never silently scored as 0%. See `available_models()`.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from answer_parser import log_unparseable, parse_answer  # noqa: E402
from cost_tracker import CostTracker  # noqa: E402

RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}
MAX_RETRIES = 5
BASE_BACKOFF_SECONDS = 1.5


class ModelNotConfigured(Exception):
    """Raised when a model in config.yaml is referenced but not yet locked (name/
    version null)."""


class CredentialsMissing(Exception):
    """Raised when a model IS configured but its provider's required environment
    variable isn't set. Kept distinct from ModelNotConfigured (a config-file problem)
    vs. this (an environment problem) -- the fix for each is different, and callers
    that want to report "why did this model not run" want to say which."""


class TransientAPIError(Exception):
    """Wraps a retryable failure after retries are exhausted, so callers can tell
    it apart from a genuine bug in the calling code."""


def catchable_run_errors() -> tuple[type[BaseException], ...]:
    """
    The set of exceptions that mean "this one call failed" rather than "this
    program has a bug". Every caller that loops over run_model() for many items/
    models (run_batch below, sanity_check.py, anything added later) should catch
    exactly this set per-item, log it, and move on -- never let one bad call take
    down the whole run.

    Centralized here on purpose: an earlier version of this file fixed exactly this
    class of bug in run_batch() but left sanity_check.py's own per-item loop with
    no exception handling at all, so a single transient failure there could still
    crash the entire sanity check. A shared function means every caller stays in
    sync by construction instead of by someone remembering to update N copies.
    """
    try:
        from openai import OpenAIError
        return (ModelNotConfigured, CredentialsMissing, TransientAPIError, OpenAIError)
    except ImportError:
        return (ModelNotConfigured, CredentialsMissing, TransientAPIError)


# --------------------------------------------------------------------------- #
# Config loading
# --------------------------------------------------------------------------- #


@dataclass
class ResolvedModel:
    id: str
    provider: str
    name: str
    version: str | None
    temperature: float
    max_tokens: int
    seed: int | None
    dtype: str | None = None


def _load_config(config_path: str | Path) -> dict:
    with open(config_path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _resolve_model(cfg: dict, model_id: str) -> ResolvedModel:
    """
    Find `model_id` among models.evaluated (or the special ids 'rewriter'/'verifier')
    and return a ResolvedModel. Raises ModelNotConfigured if the entry's `name` is
    null -- this is the exact situation the repo's validate_config.py flags as a
    hard BLOCK, and the harness must fail the same way, not silently proceed.
    """
    if model_id in ("rewriter", "verifier"):
        entry = cfg["models"][model_id]
    else:
        entry = next(
            (m for m in cfg["models"]["evaluated"] if m["id"] == model_id), None
        )
        if entry is None:
            raise ModelNotConfigured(
                f"'{model_id}' is not in models.evaluated in config.yaml"
            )

    if not entry.get("name"):
        raise ModelNotConfigured(
            f"models.{'evaluated.' if model_id not in ('rewriter','verifier') else ''}"
            f"{model_id}.name is null in config.yaml -- not chosen yet."
        )

    return ResolvedModel(
        id=model_id,
        provider=entry["provider"],
        name=entry["name"],
        version=entry.get("version"),
        temperature=entry.get("temperature", 0),
        max_tokens=entry.get("max_tokens", 16),
        seed=entry.get("seed"),
        dtype=entry.get("dtype"),
    )


def available_models(cfg: dict, mock: bool = False) -> tuple[list[str], list[str]]:
    """
    Split models.evaluated into (ready, skipped) by id. A model is skipped if its
    name isn't locked yet, its provider's required credential is missing, or (when
    mock=False) its local server isn't reachable. Never raises -- this is meant to
    be called up front so a batch run can print clear warnings and proceed with
    whatever IS available, instead of discovering the problem mid-run via a crash.

    mock=True skips all credential/reachability checks entirely, since mock runs
    never touch a real client -- a model missing real credentials should still show
    up as "ready" for a mock run.
    """
    ready, skipped = [], []
    for entry in cfg["models"]["evaluated"]:
        model_id = entry["id"]
        if not entry.get("name"):
            skipped.append(f"{model_id}: not chosen yet (name is null)")
            continue
        if not mock:
            provider = entry["provider"]
            reason = _missing_credential(provider)
            if reason:
                skipped.append(f"{model_id}: {reason}")
                continue
            if provider == "local_hf" and not _local_server_reachable():
                skipped.append(
                    f"{model_id}: local vLLM server not reachable at "
                    f"http://localhost:8000 -- is it started? (see KAGGLE_SETUP.md cell 3)"
                )
                continue
        ready.append(model_id)
    return ready, skipped


# --------------------------------------------------------------------------- #
# Provider client -- one code path for all three OpenAI-compatible backends
# --------------------------------------------------------------------------- #


def _missing_credential(provider: str) -> str | None:
    """
    Single source of truth for "does this provider have what it needs in the
    environment". Used by both available_models() (pre-flight reporting) and
    _make_client() (so a client construction fails with a clear CredentialsMissing
    instead of a bare KeyError on a dict lookup).

    Returns a human-readable reason if something's missing, else None.
    """
    if provider == "azure_openai":
        if not (os.environ.get("AZURE_OPENAI_API_KEY") and os.environ.get("AZURE_OPENAI_ENDPOINT")):
            return "AZURE_OPENAI_API_KEY / AZURE_OPENAI_ENDPOINT not set"
    elif provider == "nvidia_build":
        if not os.environ.get("NVIDIA_API_KEY"):
            return "NVIDIA_API_KEY not set"
    # local_hf (vLLM) needs no credential -- reachability is checked separately,
    # since "no key needed" and "server not actually running" are different problems.
    return None


def _local_server_reachable(base_url: str = "http://localhost:8000/v1", timeout: float = 2.0) -> bool:
    """
    Quick reachability check for the local vLLM server. Stdlib only (no new
    dependency) since this needs to work from inside harness.py, which otherwise
    only imports `openai` lazily. Exists so a not-yet-started (or crashed) vLLM
    server fails ONCE with a clear message, instead of every single item grinding
    through MAX_RETRIES of exponential backoff against a connection that was never
    going to succeed.
    """
    import urllib.error
    import urllib.request

    try:
        urllib.request.urlopen(f"{base_url}/models", timeout=timeout)
        return True
    except (urllib.error.URLError, OSError):
        return False


def _make_client(provider: str):
    """Lazy import + construct so a missing `openai` package only breaks providers
    that actually need it, and so --mock runs need no client at all."""
    from openai import AzureOpenAI, OpenAI

    reason = _missing_credential(provider)
    if reason:
        raise CredentialsMissing(f"provider={provider!r}: {reason}")

    # max_retries=0 on every client: the SDK's own built-in retry would otherwise
    # silently stack with _call_with_retry()'s policy below, making backoff behavior
    # unpredictable. _call_with_retry is the single source of truth for retries.
    if provider == "azure_openai":
        return AzureOpenAI(
            api_key=os.environ["AZURE_OPENAI_API_KEY"],
            azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
            api_version="2024-10-21",
            max_retries=0,
        )
    if provider == "nvidia_build":
        return OpenAI(
            api_key=os.environ["NVIDIA_API_KEY"],
            base_url="https://integrate.api.nvidia.com/v1",
            max_retries=0,
        )
    if provider == "local_hf":
        # vLLM's OpenAI-compatible server. Any non-empty api_key is accepted.
        return OpenAI(api_key="vllm-local", base_url="http://localhost:8000/v1", max_retries=0)
    raise ValueError(f"Unknown provider: {provider!r}")


_client_cache: dict[tuple[str, str], Any] = {}


def _credential_fingerprint(provider: str) -> str:
    """What actually goes into building this provider's client, so the cache key
    changes if it changes -- see _client_for() for why this matters."""
    if provider == "azure_openai":
        return f"{os.environ.get('AZURE_OPENAI_API_KEY', '')}:{os.environ.get('AZURE_OPENAI_ENDPOINT', '')}"
    if provider == "nvidia_build":
        return os.environ.get("NVIDIA_API_KEY", "")
    return ""  # local_hf has no credential to fingerprint


def _client_for(provider: str):
    # Keyed by (provider, credential fingerprint), not just provider. A plain
    # provider-only cache means: set a wrong key, run some calls, fix the key in a
    # later notebook cell (Kaggle Secrets re-run, or just os.environ reassignment)
    # -- and the harness would silently keep using the stale client built from the
    # old key, since nothing ever invalidates the cache. Fingerprinting the actual
    # credential means a changed env var transparently builds a fresh client.
    cache_key = (provider, _credential_fingerprint(provider))
    if cache_key not in _client_cache:
        _client_cache[cache_key] = _make_client(provider)
    return _client_cache[cache_key]


def _call_with_retry(provider: str, **kwargs) -> Any:
    """
    Retry ONLY on transient failures (rate limits, 5xx, timeouts, connection errors).
    A 4xx that isn't 408/409/425/429 will not fix itself on retry -- retrying it just
    burns quota, so those raise immediately.
    """
    from openai import APIConnectionError, APIStatusError, APITimeoutError

    client = _client_for(provider)
    last_exc: Exception | None = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return client.chat.completions.create(**kwargs)
        except (APIConnectionError, APITimeoutError) as exc:
            last_exc = exc
        except APIStatusError as exc:
            status = getattr(exc, "status_code", None)
            if status not in RETRYABLE_STATUS:
                raise  # non-transient: fail fast, don't waste attempts
            last_exc = exc

        if attempt < MAX_RETRIES:
            sleep_s = BASE_BACKOFF_SECONDS * (2 ** (attempt - 1)) + random.uniform(0, 0.5)
            time.sleep(sleep_s)

    raise TransientAPIError(
        f"Exhausted {MAX_RETRIES} retries against provider={provider!r}: {last_exc}"
    ) from last_exc


# --------------------------------------------------------------------------- #
# Prompt formatting
# --------------------------------------------------------------------------- #

_prompt_cache: dict[str, tuple[str, str]] = {}  # path -> (text, hash12)


def _load_prompt(template_path: str | Path) -> tuple[str, str]:
    key = str(template_path)
    if key not in _prompt_cache:
        text = Path(template_path).read_text(encoding="utf-8")
        h = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
        _prompt_cache[key] = (text, h)
    return _prompt_cache[key]


def format_options(options: dict[str, str]) -> str:
    """Fixed A-D order regardless of dict insertion order, since MedQA sometimes
    stores options unordered and the prompt promises 'A. ... D. ...' formatting."""
    formatted = "\n".join(f"{letter}. {options[letter]}" for letter in ("A", "B", "C", "D") if letter in options)
    if not formatted:
        # Deliberately NOT wrapped in a catchable-and-skip exception like an API
        # failure would be: this indicates the ITEM DATA is malformed (a real bug
        # upstream in whatever produced this item), not a transient call failure.
        # Silently skipping malformed items the same way a flaky API call gets
        # skipped would mask a systematic data problem as a shrinking sample size
        # nobody notices -- better to crash loudly once, immediately, than send a
        # multiple-choice prompt with zero options listed to a model.
        raise ValueError(
            f"options dict has none of A/B/C/D: {options!r} -- malformed item, "
            f"not sending this to a model."
        )
    return formatted


_PLACEHOLDER_PATTERN = re.compile(r"\{stem\}|\{options\}")


def render_prompt(template_text: str, item: dict) -> str:
    """
    Single-pass substitution over the ORIGINAL template text via re.sub, NOT
    chained str.replace() calls.

    Chaining template_text.replace("{stem}", ...).replace("{options}", ...) has a
    real correctness bug: if the substituted VALUE for {stem} itself happens to
    contain the literal text "{options}" (an unusual patient-voice rewrite that
    quotes something with braces, say), the second .replace() operates on the
    already-mutated string and finds + corrupts that occurrence too, even though
    it was never a real placeholder. re.sub() doesn't have this problem: it
    resolves every match against the ORIGINAL template's positions in one pass,
    and never rescans substituted content for further matches.
    """
    options_text = format_options(item["options"])

    def _sub(match: re.Match) -> str:
        return item["stem"] if match.group(0) == "{stem}" else options_text

    return _PLACEHOLDER_PATTERN.sub(_sub, template_text)


# --------------------------------------------------------------------------- #
# The one public function
# --------------------------------------------------------------------------- #


def run_model(
    model_id: str,
    item: dict,
    prompt_template: str | Path,
    *,
    config_path: str | Path = "config.yaml",
    stage: str = "eval",
    prompt_condition: str | None = None,
    tracker: CostTracker | None = None,
    mock: bool = False,
    parse_failure_log: str | Path = "logs/parse_failures.jsonl",
) -> dict[str, Any]:
    """
    Run one item through one model with one prompt template. Returns a dict matching
    config.yaml's logging.results_schema exactly:
        item_id, model, level, prompt_condition, predicted, gold, correct,
        raw_output, prompt_hash, config_version

    `prompt_condition` defaults to the template's filename stem (e.g. "eval_plain_v1")
    if not given explicitly -- pass it explicitly when you want it to match the short
    ids in config.yaml's prompt_conditions (e.g. "plain", "clarify_first").
    """
    cfg = _load_config(config_path)
    resolved = _resolve_model(cfg, model_id)
    template_text, prompt_hash = _load_prompt(prompt_template)
    rendered = render_prompt(template_text, item)
    cond = prompt_condition or Path(prompt_template).stem
    config_version = cfg.get("run", {}).get("config_version")

    # Same reasoning as format_options()'s empty-options check: a missing gold
    # answer is a malformed-item problem, not a model/API problem. Left
    # unchecked, "correct" would always evaluate False for this item regardless
    # of what the model answered (nothing equals None), silently deflating
    # accuracy with zero warning. Checked here, before any API call, so a
    # malformed item is caught before spending money on it -- same placement
    # rationale as the options check above.
    if item.get("gold") is None:
        raise ValueError(
            f"item {item.get('item_id')!r} has no 'gold' answer -- malformed "
            f"item, not sending this to a model."
        )

    if mock:
        raw_output, in_tok, out_tok = _mock_response(item, rendered, model_id=model_id, prompt_condition=cond)
    else:
        call_kwargs = dict(
            model=resolved.name,
            messages=[{"role": "user", "content": rendered}],
            temperature=resolved.temperature,
            max_tokens=resolved.max_tokens,
        )
        # Only include seed when actually set. config.yaml itself notes "not every
        # provider honours a seed parameter" -- sending an explicit `"seed": null`
        # is a different wire value than omitting the field, and some strict
        # OpenAI-compatible servers reject the former.
        if resolved.seed is not None:
            call_kwargs["seed"] = resolved.seed
        response = _call_with_retry(resolved.provider, **call_kwargs)
        # response.choices can legitimately come back empty (content filtering,
        # provider-side edge cases) -- response.choices[0] would then raise a raw
        # IndexError, which is NOT in catchable_run_errors()'s exception set, so it
        # would crash the whole batch/sanity-check exactly like the bug fixed for
        # API-level failures, just triggered a different way. Turning it into
        # TransientAPIError (not retried here -- a degenerate response at
        # temperature 0 would just repeat, wasting the retry budget) routes it
        # through the same per-item catch-and-continue machinery instead.
        if not response.choices:
            raise TransientAPIError(
                f"provider={resolved.provider!r} model={resolved.name!r} returned "
                f"zero choices (possible content filtering or provider-side issue)."
            )
        raw_output = response.choices[0].message.content or ""
        usage = getattr(response, "usage", None)
        in_tok = getattr(usage, "prompt_tokens", 0) if usage else 0
        out_tok = getattr(usage, "completion_tokens", 0) if usage else 0

    if tracker is not None:
        tracker.log(
            stage=stage,
            model=resolved.name,
            input_tokens=in_tok,
            output_tokens=out_tok,
            item_id=item["item_id"],
            level=item.get("level"),
            prompt_condition=cond,
        )

    # config.yaml's parsing.valid_letters is the declared source of truth for what
    # counts as a valid answer -- wire it through explicitly rather than relying on
    # answer_parser's own hardcoded default happening to agree with it. They agree
    # today (both A-D), but a config value nothing reads isn't really configuring
    # anything; if someone changes it later expecting different behavior, this is
    # what makes that change actually take effect instead of being silently ignored.
    cfg_valid_letters = cfg.get("parsing", {}).get("valid_letters")
    if cfg_valid_letters:
        predicted = parse_answer(raw_output, valid_letters=cfg_valid_letters)
    else:
        predicted = parse_answer(raw_output)
    if predicted is None:
        log_unparseable(
            parse_failure_log,
            item_id=item["item_id"],
            model=resolved.name,
            level=item.get("level", ""),
            prompt_condition=cond,
            raw_output=raw_output,
        )

    gold = item.get("gold")
    return {
        "item_id": item["item_id"],
        "model": resolved.name,
        "level": item.get("level"),
        "prompt_condition": cond,
        "predicted": predicted,
        "gold": gold,
        # correct is None (not False) when unparseable -- "wrong" and "unparseable"
        # must stay distinguishable for scoring to decide how to treat each.
        "correct": (predicted == gold) if predicted is not None else None,
        "raw_output": raw_output,
        "prompt_hash": prompt_hash,
        "config_version": config_version,
    }


def _mock_response(
    item: dict, rendered_prompt: str, *, model_id: str = "", prompt_condition: str = ""
) -> tuple[str, int, int]:
    """Deterministic pseudo-random letter, seeded by (item_id, model_id,
    prompt_condition) so mock runs are reproducible AND vary across models/conditions
    for the same item -- seeding on item_id alone would give every model the exact
    same fake answer on every item, which is a weaker test of the surrounding
    plumbing (e.g. it couldn't distinguish a bug that mixes up per-model results).
    Zero real cost; token counts are rough length-based estimates just so the cost
    tracker's totals aren't literally zero in mock mode."""
    rng = random.Random(f"{item['item_id']}|{model_id}|{prompt_condition}")
    letter = rng.choice(["A", "B", "C", "D"])
    in_tok = max(1, len(rendered_prompt) // 4)
    out_tok = 1
    return letter, in_tok, out_tok


# --------------------------------------------------------------------------- #
# Batch runner -- resume-safe
# --------------------------------------------------------------------------- #


def _existing_keys(results_path: str | Path) -> set[tuple]:
    path = Path(results_path)
    if not path.exists():
        return set()
    keys = set()
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            keys.add((r.get("item_id"), r.get("model"), r.get("level"), r.get("prompt_condition")))
    return keys


def run_batch(
    items: list[dict],
    model_ids: list[str],
    prompt_template: str | Path,
    *,
    config_path: str | Path = "config.yaml",
    stage: str = "eval",
    prompt_condition: str | None = None,
    mock: bool = False,
    sleep_between_calls: float = 0.0,
) -> dict[str, int]:
    """
    Run every (item, model) pair, appending each result as one JSON line to
    config.yaml's logging.results_file. Resume-safe: on start, any
    (item_id, model, level, prompt_condition) key already present in the results
    file is skipped, so restarting after a crash never re-spends money on work
    already done and never duplicates rows.

    A problem with ONE model (missing credentials, an auth error, a bad request,
    a dead local server) is logged and that model is skipped -- it does NOT abort
    the run for every other model and item. That resilience is the entire point of
    a resume-safe batch runner; a single-model failure taking down the whole batch
    would defeat it.

    Returns a dict of {"run": n, "skipped": n, "failed": n} for the caller to report.
    """
    # See catchable_run_errors()'s docstring for why this is centralized rather
    # than a local copy of the exception tuple.
    catchable_errors = catchable_run_errors()

    cfg = _load_config(config_path)
    results_path = Path(cfg["logging"]["results_file"])
    results_path.parent.mkdir(parents=True, exist_ok=True)
    tracker = CostTracker.from_config(config_path)

    # Pre-flight: skip models with no credentials / unreachable local server BEFORE
    # spending time on them, same clean "SKIP" reporting sanity_check.py already
    # does. Skipped entirely in mock mode, which never needs real credentials.
    if not mock:
        ready, skip_reasons = available_models(cfg, mock=False)
        known_ids = {m["id"] for m in cfg["models"]["evaluated"]}
        for requested_id in model_ids:
            # A caller-supplied id that isn't even in config.yaml at all (a typo,
            # e.g. "llama_3" instead of "llama3") would otherwise be silently
            # dropped by the filter below with zero explanation -- available_models()
            # only reports on entries that exist in config, so a name that doesn't
            # exist there produces no skip_reasons entry either. Warn explicitly.
            if requested_id not in known_ids:
                print(f"  SKIP {requested_id}: not found in config.yaml "
                      f"models.evaluated (typo?)", file=sys.stderr)
        for msg in skip_reasons:
            if msg.split(":")[0] in model_ids:
                print(f"  SKIP {msg}", file=sys.stderr)
        model_ids = [m for m in model_ids if m in ready]

    already = _existing_keys(results_path)
    counts = {"run": 0, "skipped": 0, "failed": 0}

    with open(results_path, "a", encoding="utf-8") as out_fh:
        for model_id in model_ids:
            try:
                resolved_name = _resolve_model(cfg, model_id).name
            except ModelNotConfigured as exc:
                print(f"  SKIP model {model_id}: {exc}", file=sys.stderr)
                continue

            cond = prompt_condition or Path(prompt_template).stem
            for item in items:
                key = (item["item_id"], resolved_name, item.get("level"), cond)
                if key in already:
                    counts["skipped"] += 1
                    continue
                try:
                    record = run_model(
                        model_id,
                        item,
                        prompt_template,
                        config_path=config_path,
                        stage=stage,
                        prompt_condition=prompt_condition,
                        tracker=tracker,
                        mock=mock,
                        parse_failure_log=cfg["logging"].get(
                            "parse_failure_log", "logs/parse_failures.jsonl"
                        ),
                    )
                except catchable_errors as exc:
                    print(f"  FAILED {model_id} / {item['item_id']}: "
                          f"{type(exc).__name__}: {exc}", file=sys.stderr)
                    counts["failed"] += 1
                    continue

                out_fh.write(json.dumps(record, ensure_ascii=False) + "\n")
                out_fh.flush()
                os.fsync(out_fh.fileno())
                already.add(key)
                counts["run"] += 1
                if sleep_between_calls:
                    time.sleep(sleep_between_calls)

    return counts
