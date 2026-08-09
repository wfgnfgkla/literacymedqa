# Running on Kaggle (2x T4)

Everything -- MedGemma inference and the API-based models -- runs from one notebook.
MedGemma is served locally via vLLM; Llama-3.3 (NVIDIA Build) and the closed GPT model
(Azure OpenAI) are called over the network from the same notebook. All three speak the
OpenAI-compatible chat API, so `src/harness.py` has one client code path for all of them.

Right now (per config.yaml) there are no API keys set up yet, and the closed GPT model
hasn't been chosen. That's fine -- everything below still works: `available_models()`
prints a clear skip for whatever isn't configured, and nothing crashes or silently
scores a missing model as 0%.

## Cell order

**Cell 1 -- clone and install**
```bash
!git clone https://github.com/RithikSatarla/literacymedqa.git
%cd literacymedqa
!unzip -oq literacymedqa.zip -d /tmp/unpacked && cp -r /tmp/unpacked/literacymedqa/* .
!pip install -q -r requirements.txt
!pip install -q vllm   # separate: GPU-specific build, not in requirements.txt
```
(The unzip step is a workaround for the repo's current state -- the intended
`prompts/`/`src/`/`data/`/`logs/` layout is inside `literacymedqa.zip`, not yet
committed as real folders on `main`. Once that's fixed upstream, drop this step.)

**Cell 2 -- secrets (once you have them)**
```python
import os
from kaggle_secrets import UserSecretsClient
secrets = UserSecretsClient()

# Only set what you actually have -- available_models() skips the rest gracefully.
try:
    os.environ["AZURE_OPENAI_API_KEY"] = secrets.get_secret("AZURE_OPENAI_API_KEY")
    os.environ["AZURE_OPENAI_ENDPOINT"] = secrets.get_secret("AZURE_OPENAI_ENDPOINT")
except Exception:
    print("Azure secrets not set yet -- gpt_closed will be skipped.")

try:
    os.environ["NVIDIA_API_KEY"] = secrets.get_secret("NVIDIA_API_KEY")
except Exception:
    print("NVIDIA secret not set yet -- llama3 will be skipped.")
```

**Cell 3 -- launch vLLM in the background**
```python
import subprocess, time, requests

vllm_proc = subprocess.Popen([
    "vllm", "serve", "google/medgemma-4b-it",
    "--dtype", "float16",
    "--tensor-parallel-size", "2",
    "--port", "8000",
])

for _ in range(60):  # poll up to ~5 min; first load can be slow
    try:
        if requests.get("http://localhost:8000/v1/models", timeout=3).ok:
            print("vLLM is up.")
            break
    except requests.exceptions.ConnectionError:
        pass
    time.sleep(5)
else:
    raise RuntimeError("vLLM did not come up in time -- check vllm_proc output.")
```

**Cell 4 -- run the sanity check**
```python
!python src/sanity_check.py
```
First time through, or any time you don't want to touch the GPU / burn API calls,
run `!python src/sanity_check.py --mock` instead. Mock mode fakes the model responses
with a deterministic pseudo-random letter, so it's expected to land around chance
accuracy (~25% on 4 options) and print `CHECK`, not `PASS` -- that's correct, not a
bug. Its job is only to prove the code path runs end to end (loads items, calls
run_model, parses, logs cost, writes `data/sanity_check_results.jsonl`) with zero
GPU and zero keys. Only a real (non-mock) run's accuracy numbers mean anything against
the published-number targets.

**Cell 5 -- the actual eval / intervention runs (Patrick's steps 8-9), once the sanity
check passes for real**
```python
from src.harness import run_batch
# items = ... (load from data/literacymedqa_v1.jsonl once steps 1-7 have produced it)
# run_batch(items, model_ids=["llama3", "medgemma", "gpt_closed"],
#           prompt_template="prompts/eval_plain_v1.txt", prompt_condition="plain")
```
This is resume-safe: if the Kaggle session dies mid-run, restarting this cell picks up
exactly where it left off (it skips every (item_id, model, level, prompt_condition)
already in `data/results.jsonl`) rather than re-spending on completed work.

## Before every real run

```bash
python src/validate_config.py config.yaml
```
As of this writing it still reports 2 blocking issues (rewriter version string,
`gpt_closed` model not chosen) -- both need the mentor lock described in README.md
before a full (non-sanity-check) run should happen.
