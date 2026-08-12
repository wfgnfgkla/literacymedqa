# Running on Kaggle (2x T4)

MedGemma runs locally via direct `transformers` generation -- no vLLM, no separate
server process. Llama-3.3 (NVIDIA Build) and the closed GPT model (Azure OpenAI) are
called over the network. `src/harness.py` handles all of this behind one interface
(`run_model`), so nothing above it needs to know which provider actually served a
given call.

**Why not vLLM:** it was the original plan, and it didn't work out. vLLM's
exact-pinned dependency chain (`numpy<2.0.0`, `torch==2.5.1`, `transformers>=4.48.2`,
dozens of other exact pins) collided repeatedly with what Kaggle's base image already
has installed -- multiple attempts at reconciling it left the container's `numpy`
installation itself corrupted (`pip show` reporting one version while the actually-
loaded module reported another and was missing core submodules). Direct
`transformers.generate()` has a far shallower dependency tree and sidesteps that
entire class of problem. It's slower per call than vLLM would be at real pipeline
scale (~15k calls for the full run) -- worth revisiting then if throughput becomes a
real bottleneck -- but for a 50-item sanity check the difference is minutes, not hours.

Right now (per config.yaml) there are no API keys set up yet, and the closed GPT model
hasn't been chosen. That's fine -- everything below still works: `available_models()`
prints a clear skip for whatever isn't configured, and nothing crashes or silently
scores a missing model as 0%.

## Cell order

**Cell 1 -- clone and install**
```bash
%cd /kaggle/working
!rm -rf literacymedqa
!git clone https://github.com/RithikSatarla/literacymedqa.git
%cd /kaggle/working/literacymedqa
# Absolute path + delete-then-clone, not a plain `git clone` + `%cd literacymedqa`.
# A plain relative clone re-run from an already-nested working directory (left over
# from a prior attempt in the same session) silently clones INSIDE itself. This
# version is safe to re-run from any starting state.

!pip install -q -r requirements.txt
```
That's the whole install. No version pins to fight -- `transformers` and `torch` are
already in `requirements.txt` (`>=4.44.0` and `>=2.0.0`), and Kaggle's base image
already ships working versions of both; there's no exact-pin dependency chain like
vLLM's to reconcile against what's already there.

Verify with a real import, not just `pip show`:
```python
import transformers
import torch
print("OK:", transformers.__version__, torch.__version__)
print("CUDA available:", torch.cuda.is_available())
```
The last line matters -- if it prints `False`, the GPU accelerator isn't actually
attached to this session (check Settings -> Accelerator -> GPU T4 x2) and generation
will silently fall back to CPU, which will work but be extremely slow.

**Cell 2 -- secrets (once you have them, not required for MedGemma)**
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
Not required to run the sanity check -- MedGemma needs no key at all. Skip this cell
entirely if you just want to confirm the harness works against MedGemma alone.

**Cell 3 -- run the sanity check**
```python
!python src/sanity_check.py
```
First time through, or any time you don't want to touch the GPU / burn API calls, run
`!python src/sanity_check.py --mock` instead. Mock mode fakes the model responses with
a deterministic pseudo-random letter, so it's expected to land around chance accuracy
(~25% on 4 options) and print `CHECK`, not `PASS` -- that's correct, not a bug. Its
job is only to prove the code path runs end to end with zero GPU and zero keys. Only a
real (non-mock) run's accuracy numbers mean anything against the published-number
targets.

The first real (non-mock) call to MedGemma will be slow -- that's the actual model
downloading and loading onto the GPU, a few minutes, one-time per session. Every call
after that reuses the already-loaded model.

**Cell 4 -- the actual eval / intervention runs (Patrick's steps 8-9), once the sanity
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

## Before every real (full-pipeline) run

```bash
python src/validate_config.py config.yaml
```
As of this writing it still reports 2 blocking issues (rewriter version string,
`gpt_closed` model not chosen) -- both need the mentor lock described in README.md
before a full (non-sanity-check) run should happen. Not required before the sanity
check itself.
