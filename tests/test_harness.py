"""
Tests for src/harness.py.

Fully offline: no network, no GPU, no real API keys. Everything that would touch a
real provider goes through mock=True or monkeypatching. Run from the repo root
(matches KAGGLE_SETUP.md's own convention, since prompt paths like
"prompts/eval_plain_v1.txt" are relative to cwd, not resolved via config):

    pytest tests/test_harness.py -v

Every test uses an ISOLATED config (see isolated_config fixture) that points
logging.results_file / cost_log into pytest's tmp_path, never into the real repo's
data/ or logs/ directories -- a test suite that could clobber real accumulated
pipeline progress if run at the wrong moment would be worse than no test suite.
"""

import json
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

import harness  # noqa: E402
from harness import (  # noqa: E402
    CredentialsMissing,
    ModelNotConfigured,
    TransientAPIError,
    available_models,
    catchable_run_errors,
    format_options,
    render_prompt,
    run_batch,
    run_model,
)


@pytest.fixture
def isolated_config(tmp_path):
    """A copy of the real config.yaml with logging paths redirected into tmp_path,
    so tests never touch the real repo's data/logs directories."""
    real_cfg = yaml.safe_load((REPO_ROOT / "config.yaml").read_text())
    real_cfg["logging"]["results_file"] = str(tmp_path / "results.jsonl")
    real_cfg["logging"]["cost_log"] = str(tmp_path / "api_calls.jsonl")
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.safe_dump(real_cfg))
    return cfg_path, real_cfg


@pytest.fixture
def sample_items():
    return [
        {"item_id": f"test_{i:04d}", "stem": f"stem {i}", "level": "a",
         "options": {"A": "a", "B": "b", "C": "c", "D": "d"}, "gold": "A"}
        for i in range(5)
    ]


@pytest.fixture(autouse=True)
def _restore_harness_internals():
    """Several tests monkeypatch module-level harness functions (_call_with_retry,
    _missing_credential, etc.) to simulate provider behavior without a real network.
    Restore the real ones after every test so patches never leak between tests."""
    originals = {
        name: getattr(harness, name)
        for name in ("_call_with_retry", "_missing_credential", "_local_server_reachable",
                     "_get_local_model")
    }
    yield
    for name, fn in originals.items():
        setattr(harness, name, fn)
    harness._client_cache.clear()


# --------------------------------------------------------------------------- #
# Resume safety
# --------------------------------------------------------------------------- #


def test_resume_safety_no_duplicates_on_rerun(isolated_config, sample_items):
    cfg_path, _ = isolated_config
    c1 = run_batch(sample_items, ["llama3", "medgemma"], "prompts/eval_plain_v1.txt",
                    config_path=cfg_path, prompt_condition="plain", mock=True)
    c2 = run_batch(sample_items, ["llama3", "medgemma"], "prompts/eval_plain_v1.txt",
                    config_path=cfg_path, prompt_condition="plain", mock=True)

    assert c1["run"] == 10  # 5 items x 2 models
    assert c2["run"] == 0
    assert c2["skipped"] == 10

    results_path = Path(yaml.safe_load(cfg_path.read_text())["logging"]["results_file"])
    n_lines = sum(1 for _ in open(results_path))
    assert n_lines == 10, "rerun must not duplicate rows"


def test_resume_safety_only_new_items_run(isolated_config, sample_items):
    cfg_path, _ = isolated_config
    run_batch(sample_items, ["llama3"], "prompts/eval_plain_v1.txt",
              config_path=cfg_path, prompt_condition="plain", mock=True)

    more_items = sample_items + [
        {"item_id": "test_0005", "stem": "new", "level": "a",
         "options": {"A": "a", "B": "b", "C": "c", "D": "d"}, "gold": "B"},
    ]
    c2 = run_batch(more_items, ["llama3"], "prompts/eval_plain_v1.txt",
                    config_path=cfg_path, prompt_condition="plain", mock=True)
    assert c2["run"] == 1
    assert c2["skipped"] == 5


# --------------------------------------------------------------------------- #
# Crash resilience -- a single bad item/model must not kill the whole batch
# --------------------------------------------------------------------------- #


def test_one_bad_item_does_not_crash_the_batch(isolated_config, sample_items):
    cfg_path, _ = isolated_config
    real_run_model = harness.run_model
    call_count = {"n": 0}

    def flaky(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 3:
            raise TransientAPIError("simulated flaky call")
        return real_run_model(*args, **kwargs)

    harness.run_model = flaky
    try:
        counts = run_batch(sample_items, ["llama3"], "prompts/eval_plain_v1.txt",
                            config_path=cfg_path, prompt_condition="plain", mock=True)
    finally:
        harness.run_model = real_run_model

    assert counts["failed"] == 1
    assert counts["run"] == 4  # the other 4 items still completed


def test_unconfigured_credentials_do_not_crash_batch(isolated_config, sample_items):
    # Real (non-mock) mode, nothing configured -- must skip cleanly, never raise.
    cfg_path, _ = isolated_config
    counts = run_batch(sample_items, ["llama3", "medgemma", "gpt_closed"],
                        "prompts/eval_plain_v1.txt", config_path=cfg_path,
                        prompt_condition="plain", mock=False)
    assert counts == {"run": 0, "skipped": 0, "failed": 0}


def test_typo_in_model_id_warns_not_silently_dropped(isolated_config, sample_items, capsys):
    cfg_path, _ = isolated_config
    run_batch(sample_items, ["llama_3"], "prompts/eval_plain_v1.txt",
              config_path=cfg_path, prompt_condition="plain", mock=False)
    captured = capsys.readouterr()
    assert "llama_3" in captured.err
    assert "not found in config.yaml" in captured.err


def test_empty_choices_response_is_catchable_not_a_crash(isolated_config, sample_items):
    cfg_path, _ = isolated_config

    class FakeResponse:
        choices = []
        usage = None

    harness._call_with_retry = lambda provider, **kwargs: FakeResponse()
    harness._missing_credential = lambda provider: None
    harness._local_server_reachable = lambda *a, **kw: True

    item = sample_items[0]
    with pytest.raises(TransientAPIError):
        run_model("llama3", item, "prompts/eval_plain_v1.txt",
                  config_path=cfg_path, prompt_condition="plain", mock=False)

    # And through run_batch, it must be caught per-item, not propagate.
    counts = run_batch([item], ["llama3"], "prompts/eval_plain_v1.txt",
                        config_path=cfg_path, prompt_condition="plain", mock=False)
    assert counts["failed"] == 1


# --------------------------------------------------------------------------- #
# available_models() / mock semantics
# --------------------------------------------------------------------------- #


def test_mock_mode_bypasses_credential_checks(isolated_config):
    _, cfg = isolated_config
    ready, skipped = available_models(cfg, mock=True)
    assert "llama3" in ready
    assert "medgemma" in ready
    assert not any("NVIDIA_API_KEY" in s for s in skipped)


def test_real_mode_reports_missing_credentials(isolated_config):
    _, cfg = isolated_config
    ready, skipped = available_models(cfg, mock=False)
    assert ready == []
    assert any("NVIDIA_API_KEY" in s for s in skipped)
    assert any("gpt_closed" in s and "not chosen" in s for s in skipped)


# --------------------------------------------------------------------------- #
# render_prompt / format_options
# --------------------------------------------------------------------------- #


def test_render_prompt_safe_against_placeholder_text_in_stem():
    template = "Q:\n{stem}\n\nOPTS:\n{options}\n"
    item = {
        "stem": "the doctor mentioned {options} but I did not understand",
        "options": {"A": "MI", "B": "GERD", "C": "PE", "D": "Pneumonia"},
    }
    result = render_prompt(template, item)
    # The real options block should appear exactly once (at its real slot); the
    # literal "{options}" text inside the stem must survive untouched.
    assert result.count("A. MI") == 1
    assert "the doctor mentioned {options} but I did not understand" in result


def test_render_prompt_handles_stray_braces_elsewhere():
    # str.format() would crash on this; re.sub-based substitution must not.
    template = 'Q:\n{stem}\n\nOPTS:\n{options}\nExample: {"foo": "bar"}\n'
    item = {"stem": "s", "options": {"A": "a", "B": "b", "C": "c", "D": "d"}}
    result = render_prompt(template, item)
    assert '{"foo": "bar"}' in result


def test_format_options_raises_on_empty():
    with pytest.raises(ValueError):
        format_options({})


def test_missing_gold_raises_before_any_call(isolated_config):
    # Symmetric to the format_options check above: a missing gold answer would
    # otherwise silently score as "wrong" forever (nothing equals None), quietly
    # deflating accuracy with no warning. Must raise, and must NOT be one of the
    # per-item-catchable errors -- it's a malformed-item problem, not a flaky call.
    cfg_path, _ = isolated_config
    item = {"item_id": "malformed_1", "stem": "s", "level": "a",
            "options": {"A": "a", "B": "b", "C": "c", "D": "d"}}  # no 'gold'
    with pytest.raises(ValueError):
        run_model("llama3", item, "prompts/eval_plain_v1.txt",
                  config_path=cfg_path, prompt_condition="plain", mock=True)
    assert ValueError not in catchable_run_errors()


def test_format_options_fixed_order():
    options = {"D": "fourth", "B": "second", "A": "first", "C": "third"}
    assert format_options(options) == "A. first\nB. second\nC. third\nD. fourth"


def test_config_valid_letters_actually_wired_through(isolated_config, monkeypatch):
    # config.yaml declares parsing.valid_letters as the source of truth for what
    # counts as a valid answer. This proves run_model() actually reads it, rather
    # than silently using answer_parser's own hardcoded default -- the two
    # currently agree (both A-D), so a test that doesn't change the config value
    # wouldn't catch a regression here. Point the config at a DIFFERENT letter set
    # and confirm behavior actually changes.
    cfg_path, cfg = isolated_config
    cfg["parsing"]["valid_letters"] = ["X", "Y"]
    cfg_path.write_text(yaml.safe_dump(cfg))

    monkeypatch.setattr(
        harness, "_mock_response",
        lambda item, rendered, **kw: ("X", 10, 1),
    )
    item = {"item_id": "t", "stem": "s", "level": "a",
            "options": {"A": "a", "B": "b", "C": "c", "D": "d"}, "gold": "X"}
    record = run_model("llama3", item, "prompts/eval_plain_v1.txt",
                        config_path=cfg_path, prompt_condition="plain", mock=True)
    assert record["predicted"] == "X", (
        "config.yaml's parsing.valid_letters was changed to ['X','Y'] but the "
        "parser still isn't recognizing 'X' as valid -- the config value isn't "
        "actually being read."
    )


# --------------------------------------------------------------------------- #
# Credential-fingerprint cache invalidation
# --------------------------------------------------------------------------- #


def test_client_cache_invalidates_on_credential_change(monkeypatch):
    call_log = []

    def fake_make_client(provider):
        call_log.append(provider)
        return object()

    harness._client_cache.clear()
    monkeypatch.setattr(harness, "_make_client", fake_make_client)
    monkeypatch.setenv("NVIDIA_API_KEY", "key-v1")

    c1 = harness._client_for("nvidia_build")
    c1_again = harness._client_for("nvidia_build")
    assert c1 is c1_again
    assert len(call_log) == 1

    monkeypatch.setenv("NVIDIA_API_KEY", "key-v2-corrected")
    c2 = harness._client_for("nvidia_build")
    assert c2 is not c1, "stale client reused after credential changed"
    assert len(call_log) == 2


# --------------------------------------------------------------------------- #
# catchable_run_errors() -- the shared exception set both run_batch and
# sanity_check.py rely on staying in sync
# --------------------------------------------------------------------------- #


def test_catchable_run_errors_includes_openai_errors():
    errors = catchable_run_errors()
    assert ModelNotConfigured in errors
    assert CredentialsMissing in errors
    assert TransientAPIError in errors
    import openai
    assert any(issubclass(openai.OpenAIError, e) or e is openai.OpenAIError for e in errors)


# --------------------------------------------------------------------------- #
# local_transformers provider -- direct in-process generation, no server
# --------------------------------------------------------------------------- #


class _FakeTensor:
    """Minimal stand-in for a torch.Tensor -- just enough surface area
    (shape, indexing, .to()) for _call_local_transformers' logic to run against,
    without needing the real multi-GB torch package in the test environment."""

    def __init__(self, data):
        self.data = data

    @property
    def shape(self):
        return (len(self.data), len(self.data[0]) if self.data and isinstance(self.data[0], list) else 1)

    def to(self, device):
        return self

    def __getitem__(self, idx):
        if isinstance(idx, int):
            row = self.data[idx]
            return _FakeTensor(row) if isinstance(row, list) else row
        return _FakeTensor(self.data[idx])

    def __iter__(self):
        return iter(self.data)


@pytest.fixture
def fake_torch(monkeypatch):
    import types

    mod = types.ModuleType("torch")
    mod.tensor = lambda data: _FakeTensor(data)
    mod.cat = lambda tensors, dim: _FakeTensor([tensors[0].data[0] + tensors[1].data[0]])

    class NoGrad:
        def __enter__(self):
            return None

        def __exit__(self, *a):
            return False

    mod.no_grad = NoGrad
    mod.manual_seed = lambda s: None
    mod.float16 = "float16"
    monkeypatch.setitem(sys.modules, "torch", mod)
    return mod


def test_local_transformers_provider_full_path(isolated_config, fake_torch):
    """
    End-to-end through run_model() for the medgemma/local_transformers provider:
    provider routing bypasses the OpenAI client entirely, the response gets
    correctly shimmed into the same shape every other provider produces, and
    parsing/scoring/schema all work identically regardless of provider.
    """
    class FakeTokenizer:
        eos_token_id = 0

        def apply_chat_template(self, msgs, add_generation_prompt=True, return_tensors="pt"):
            return fake_torch.tensor([[1, 2, 3, 4, 5]])

        def decode(self, ids, skip_special_tokens=True):
            return "A"

    class FakeModel:
        device = "cpu"

        def generate(self, input_ids, **kwargs):
            return fake_torch.cat([input_ids, fake_torch.tensor([[99]])], dim=1)

    harness._get_local_model = lambda name: (FakeTokenizer(), FakeModel())

    cfg_path, _ = isolated_config
    item = {"item_id": "test1", "stem": "A patient presents with fever.", "level": "a",
            "options": {"A": "flu", "B": "cold", "C": "covid", "D": "strep"}, "gold": "A"}

    record = run_model("medgemma", item, "prompts/eval_plain_v1.txt",
                        config_path=cfg_path, prompt_condition="plain", mock=False)
    assert record["predicted"] == "A"
    assert record["correct"] is True
    assert record["model"] == "google/medgemma-4b-it"


def test_local_transformers_no_credential_needed(isolated_config, monkeypatch):
    # local_transformers needs no API key at all -- confirms it doesn't show up
    # in available_models()'s skip list the way azure_openai/nvidia_build do
    # when their env vars are missing. Package availability is a separate concern
    # (see test_local_transformers_unavailable_when_packages_missing below), so
    # it's patched to True here to isolate what this test actually checks.
    monkeypatch.setattr(harness, "_local_transformers_available", lambda: True)
    _, cfg = isolated_config
    ready, skipped = available_models(cfg, mock=False)
    assert "medgemma" in ready
    assert not any("medgemma" in s for s in skipped)


def test_local_transformers_unavailable_when_packages_missing(isolated_config, monkeypatch):
    # The other side of the same coin: if torch/transformers genuinely aren't
    # importable, medgemma must be cleanly skipped with a clear message -- not
    # crash deep inside generation the first time run_model() tries to use it.
    monkeypatch.setattr(harness, "_local_transformers_available", lambda: False)
    _, cfg = isolated_config
    ready, skipped = available_models(cfg, mock=False)
    assert "medgemma" not in ready
    assert any("medgemma" in s and "torch/transformers" in s for s in skipped)
