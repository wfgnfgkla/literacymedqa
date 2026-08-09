"""
Pre-flight check. Run this before ANY generation or eval run.

Its job is to fail loudly on a half-locked config, because the expensive failure mode
on this project is discovering after 2,000 API calls that the rewriter version string
was never pinned and the run is not reproducible.

    python src/validate_config.py config.yaml

Exit code 0 means safe to run. Exit code 1 means stop.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    raise SystemExit("pyyaml is required: pip install pyyaml")


class Check:
    def __init__(self):
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def error(self, msg: str) -> None:
        self.errors.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)

    def report(self) -> int:
        for w in self.warnings:
            print(f"  WARN   {w}")
        for e in self.errors:
            print(f"  BLOCK  {e}")
        print()
        if self.errors:
            print(f"{len(self.errors)} blocking issue(s). Do not run.")
            return 1
        if self.warnings:
            print(f"Config is runnable. {len(self.warnings)} warning(s) above.")
        else:
            print("Config is fully locked. Safe to run.")
        return 0


def validate(path: str | Path) -> int:
    c = Check()
    cfg = yaml.safe_load(open(path))

    root = Path(path).parent

    # --- reproducibility: everything that must be pinned before a run ---
    rw = cfg["models"]["rewriter"]
    if not rw.get("version"):
        c.error("models.rewriter.version is null. Appendix A reproducibility depends on it.")
    if rw.get("temperature") != 0:
        c.error(f"models.rewriter.temperature is {rw.get('temperature')}, must be 0.")

    vf = cfg["models"]["verifier"]
    if not vf.get("version"):
        c.warn("models.verifier.version is null.")
    if vf["name"] == rw["name"]:
        c.error("Verifier and rewriter are the same model. They must differ.")

    evaluated = cfg["models"]["evaluated"]
    names = []
    for m in evaluated:
        tag = m.get("id", "?")
        if not m.get("name"):
            c.error(f"models.evaluated[{tag}].name is null.")
            continue
        names.append(m["name"])
        if not m.get("version"):
            c.warn(f"models.evaluated[{tag}].version is null.")
        if m.get("temperature") != 0:
            c.error(f"models.evaluated[{tag}].temperature must be 0.")

    # --- the disjointness rule: no model grades text it wrote or checks itself ---
    if rw["name"] in names:
        c.error(
            f"Rewriter ({rw['name']}) is also an evaluated model. "
            "No model may be graded on text it generated."
        )
    if vf["name"] in names:
        c.warn(
            f"Verifier ({vf['name']}) is also an evaluated model. "
            "Defensible, but state it explicitly in the paper."
        )

    # --- sample size ---
    n = cfg["dataset"].get("n_items")
    if n is None:
        if cfg["run"].get("pilot_mode"):
            c.warn("dataset.n_items is null. Fine for pilot_mode, blocking for full generation.")
        else:
            c.error("dataset.n_items is null but pilot_mode is false.")
    elif n > 1273:
        c.error(f"dataset.n_items={n} exceeds the MedQA US test split (~1,273 items).")

    # --- cost rates ---
    rates = cfg.get("cost_rates_usd_per_1m") or {}
    for model in {rw["name"], vf["name"], *names}:
        r = rates.get(model)
        if r is None:
            c.warn(f"No cost rate for '{model}'. Calls log tokens but not dollars.")
        elif r.get("input") is None or r.get("output") is None:
            c.warn(f"Cost rate for '{model}' is incomplete. Totals will be a lower bound.")

    # --- prompt files exist, have the right placeholders, and get hashed ---
    required_placeholders = {
        cfg["generation"]["rewriter_prompt"]: {"{stem}"},
    }
    for p in cfg.get("prompt_conditions", []):
        required_placeholders[p["template"]] = {"{stem}", "{options}"}

    for p, needed in required_placeholders.items():
        fp = root / p
        if not fp.exists():
            c.error(f"Prompt file missing: {p}")
            continue
        text = fp.read_text(encoding="utf-8")
        missing = {ph for ph in needed if ph not in text}
        if missing:
            c.error(f"{p} is missing placeholder(s): {', '.join(sorted(missing))}")
        h = hashlib.sha256(fp.read_bytes()).hexdigest()[:12]
        print(f"  hash   {p}  {h}")

    # --- gates ---
    g = cfg["gates"]["realism"]
    if g.get("max_rater_accuracy", 1) > 0.75:
        c.warn(f"Realism gate loosened to {g['max_rater_accuracy']}. Proposal specifies 0.75.")
    if cfg["gates"]["fidelity"].get("min_filter_human_kappa", 0) < 0.6:
        c.warn("Fidelity kappa threshold is below 0.6.")

    print()
    return c.report()


if __name__ == "__main__":
    cfg_path = sys.argv[1] if len(sys.argv) > 1 else "config.yaml"
    sys.exit(validate(cfg_path))
