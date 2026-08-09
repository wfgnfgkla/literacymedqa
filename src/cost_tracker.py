"""
Shared cost tracker for LiteracyMedQA.

Every API call in this project gets logged here, from day one. Wrap the call site,
not the reporting step, or the numbers will be incomplete exactly when you need them.

Usage:

    from cost_tracker import CostTracker

    tracker = CostTracker.from_config("config.yaml")

    resp = client.chat.completions.create(...)
    tracker.log(
        stage="rewrite",           # rewrite | verify | eval | intervention | pilot
        model="gpt-4o-mini",
        input_tokens=resp.usage.prompt_tokens,
        output_tokens=resp.usage.completion_tokens,
        item_id="medqa_00412",
        level="c",
    )

    tracker.summary()              # prints per-stage and per-model totals
    tracker.project(scale=12.7)    # projects the full run from a pilot

Cost rates live in config.yaml under `cost_rates_usd_per_1m`. If a rate is null,
the call is still logged with its token counts and cost is recorded as None, so you
can back-fill the dollar figure later without losing the usage data.
"""

from __future__ import annotations

import json
import os
import threading
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError as exc:  # pragma: no cover
    raise SystemExit("pyyaml is required: pip install pyyaml") from exc


VALID_STAGES = {"rewrite", "verify", "eval", "intervention", "pilot", "other"}


class CostTracker:
    def __init__(
        self,
        log_path: str | Path,
        rates: dict[str, dict[str, float | None]] | None = None,
        config_version: int | None = None,
    ):
        self.log_path = Path(log_path)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.rates = rates or {}
        self.config_version = config_version
        self._lock = threading.Lock()

    @classmethod
    def from_config(cls, config_path: str | Path = "config.yaml") -> "CostTracker":
        with open(config_path) as fh:
            cfg = yaml.safe_load(fh)
        return cls(
            log_path=cfg["logging"]["cost_log"],
            rates=cfg.get("cost_rates_usd_per_1m", {}) or {},
            config_version=cfg.get("run", {}).get("config_version"),
        )

    # ---------- core ----------

    def _cost(self, model: str, input_tokens: int, output_tokens: int) -> float | None:
        rate = self.rates.get(model)
        if not rate:
            return None
        rin, rout = rate.get("input"), rate.get("output")
        if rin is None or rout is None:
            return None
        return (input_tokens / 1_000_000) * rin + (output_tokens / 1_000_000) * rout

    def log(
        self,
        stage: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        **meta: Any,
    ) -> dict[str, Any]:
        """Append one call to the log. Returns the written record."""
        if stage not in VALID_STAGES:
            raise ValueError(f"stage must be one of {sorted(VALID_STAGES)}, got {stage!r}")

        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "stage": stage,
            "model": model,
            "input_tokens": int(input_tokens),
            "output_tokens": int(output_tokens),
            "cost_usd": self._cost(model, input_tokens, output_tokens),
            "config_version": self.config_version,
            **meta,
        }
        line = json.dumps(record, ensure_ascii=False)
        with self._lock:
            with open(self.log_path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
                fh.flush()
                os.fsync(fh.fileno())  # a crash mid-run must not lose the tail
        return record

    def _read(self) -> list[dict[str, Any]]:
        if not self.log_path.exists():
            return []
        rows = []
        with open(self.log_path, encoding="utf-8") as fh:
            for i, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    print(f"  warning: skipping malformed log line {i}")
        return rows

    # ---------- reporting ----------

    def summary(self, printout: bool = True) -> dict[str, Any]:
        rows = self._read()
        if not rows:
            if printout:
                print("No calls logged yet.")
            return {}

        by_stage: dict[str, dict] = defaultdict(
            lambda: {"calls": 0, "in": 0, "out": 0, "usd": 0.0, "unpriced": 0}
        )
        by_model: dict[str, dict] = defaultdict(
            lambda: {"calls": 0, "in": 0, "out": 0, "usd": 0.0, "unpriced": 0}
        )

        for r in rows:
            for bucket, key in ((by_stage, r["stage"]), (by_model, r["model"])):
                b = bucket[key]
                b["calls"] += 1
                b["in"] += r["input_tokens"]
                b["out"] += r["output_tokens"]
                if r["cost_usd"] is None:
                    b["unpriced"] += 1
                else:
                    b["usd"] += r["cost_usd"]

        total_usd = sum(b["usd"] for b in by_stage.values())
        total_unpriced = sum(b["unpriced"] for b in by_stage.values())

        if printout:
            print(f"\n{'stage':<14}{'calls':>8}{'in tok':>12}{'out tok':>12}{'USD':>10}")
            print("-" * 56)
            for k in sorted(by_stage):
                b = by_stage[k]
                print(f"{k:<14}{b['calls']:>8,}{b['in']:>12,}{b['out']:>12,}{b['usd']:>10.2f}")
            print("-" * 56)
            print(f"{'TOTAL':<14}{len(rows):>8,}{'':>12}{'':>12}{total_usd:>10.2f}")

            print(f"\n{'model':<40}{'calls':>8}{'USD':>10}")
            print("-" * 58)
            for k in sorted(by_model):
                b = by_model[k]
                print(f"{k:<40}{b['calls']:>8,}{b['usd']:>10.2f}")

            if total_unpriced:
                print(
                    f"\n  {total_unpriced:,} call(s) have no price in config.yaml. "
                    "Token counts are recorded; the dollar total above is a LOWER BOUND."
                )

        return {
            "total_calls": len(rows),
            "total_usd": total_usd,
            "unpriced_calls": total_unpriced,
            "by_stage": dict(by_stage),
            "by_model": dict(by_model),
        }

    def project(self, scale: float, printout: bool = True) -> float:
        """
        Project full-run cost from what has been spent so far.

        `scale` is the multiplier from current volume to full volume. If the pilot
        ran 100 items and N is locked at 1273, scale = 12.73.

        This is the number Patrick's Week 2 task asks you to bring to Kiran BEFORE
        committing to N. Do not commit to N without running this.
        """
        s = self.summary(printout=False)
        if not s:
            if printout:
                print("Nothing logged yet, cannot project.")
            return 0.0
        projected = s["total_usd"] * scale
        if printout:
            print(f"\nSpent so far:  ${s['total_usd']:.2f}  ({s['total_calls']:,} calls)")
            print(f"Scale factor:  {scale}x")
            print(f"PROJECTED:     ${projected:.2f}")
            if s["unpriced_calls"]:
                print(
                    f"  {s['unpriced_calls']:,} unpriced call(s) excluded. "
                    "Fill in config rates before quoting this to anyone."
                )
        return projected


if __name__ == "__main__":
    import sys

    cfg = sys.argv[1] if len(sys.argv) > 1 else "config.yaml"
    t = CostTracker.from_config(cfg)
    t.summary()
