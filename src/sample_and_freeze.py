#!/usr/bin/env python3
"""
Task 1 -- sample and freeze the LiteracyMedQA base item set.

Draws a stratified sample of N items from the MedQA US 4-option TEST split,
assigns content-derived permanent IDs, and writes data/medqa_base.jsonl plus a
manifest that pins the source hash, seed, and strata counts.

Once the file is committed, `freeze` refuses to overwrite it unless a reason is
supplied, and every such change is appended to data/FREEZE_LOG.md.

Strata are built only from fields that actually exist in MedQA. There are no
specialty labels in the release, so stratification uses:
    exam_step    meta_info: step1 | step2&3
    has_labs     vignette contains a lab/vitals block
    len_tercile  short | mid | long, cut on the population, not the sample
Cells are filled by proportional (largest-remainder) allocation, so the sample
reproduces the population mix on all three axes at once. These are also the
pre-registered subgroup axes for the gap analysis.

Commands
    python src/sample_and_freeze.py freeze --n 500
    python src/sample_and_freeze.py verify
    python src/sample_and_freeze.py freeze --n 600 --reason "PI asked for more power"
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "data" / "medqa_source" / "phrases_no_exclude_test.jsonl"
OUT = ROOT / "data" / "medqa_base.jsonl"
MANIFEST = ROOT / "data" / "medqa_base.manifest.json"
FREEZE_LOG = ROOT / "data" / "FREEZE_LOG.md"

TOOL_VERSION = "sample_and_freeze/1.0.0"
DEFAULT_SEED = 20260811

LAB_CUE = re.compile(
    r"(mg/dL|mEq/L|g/dL|ng/mL|U/L|mm3|mmol/L|mOsmol|/hpf|mmHg|mm Hg|%\s|µg|mcg)", re.I
)
VIGNETTE_CUE = re.compile(r"\b\d{1,3}[- ](year|month|week|day)[- ]old\b", re.I)


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def canonical_key(rec: dict) -> str:
    """Content fingerprint. Independent of file order, so IDs survive a re-release."""
    payload = json.dumps(
        {"q": rec["question"].strip(), "o": {k: v.strip() for k, v in rec["options"].items()}},
        sort_keys=True, ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def item_id(rec: dict) -> str:
    return "LMQ-" + canonical_key(rec)[:12]


def features(rec: dict, tercile_cuts: tuple[int, int]) -> dict:
    q = rec["question"]
    n_words = len(q.split())
    lo, hi = tercile_cuts
    return {
        "exam_step": rec.get("meta_info", "unknown"),
        "has_labs": bool(LAB_CUE.search(q)),
        "is_vignette": bool(VIGNETTE_CUE.search(q)),
        "n_words": n_words,
        "len_tercile": "short" if n_words <= lo else ("mid" if n_words <= hi else "long"),
    }


def stratum_of(f: dict) -> str:
    return f"{f['exam_step']}|labs={int(f['has_labs'])}|{f['len_tercile']}"


def load_population() -> list[dict]:
    if not SOURCE.exists():
        sys.exit(f"source not found: {SOURCE}")
    rows = [json.loads(line) for line in SOURCE.open(encoding="utf-8") if line.strip()]
    lens = sorted(len(r["question"].split()) for r in rows)
    cuts = (lens[len(lens) // 3], lens[2 * len(lens) // 3])

    seen, pop, dupes = {}, [], []
    for idx, r in enumerate(rows):
        k = canonical_key(r)
        if k in seen:
            dupes.append({"source_index": idx, "duplicate_of": seen[k]})
            continue
        seen[k] = idx
        f = features(r, cuts)
        pop.append({
            "item_id": item_id(r),
            "source_index": idx,
            "question": r["question"],
            "options": r["options"],
            "gold_letter": r["answer_idx"],
            "gold_text": r["answer"],
            "exam_step": f["exam_step"],
            "has_labs": f["has_labs"],
            "is_vignette": f["is_vignette"],
            "n_words": f["n_words"],
            "len_tercile": f["len_tercile"],
            "stratum": stratum_of(f),
        })
    return pop, cuts, dupes


def allocate(pop: list[dict], n: int) -> dict[str, int]:
    """Proportional allocation with largest-remainder rounding, so cells sum to exactly n."""
    counts = Counter(p["stratum"] for p in pop)
    total = sum(counts.values())
    exact = {s: n * c / total for s, c in counts.items()}
    alloc = {s: int(v) for s, v in exact.items()}
    short = n - sum(alloc.values())
    for s, _ in sorted(exact.items(), key=lambda kv: kv[1] - int(kv[1]), reverse=True)[:short]:
        alloc[s] += 1
    for s in alloc:  # cannot draw more than the cell holds
        alloc[s] = min(alloc[s], counts[s])
    return alloc


def do_freeze(n: int, seed: int, reason: str | None, exclude_file: str | None = None) -> None:
    pop, cuts, dupes = load_population()
    excluded = []
    if exclude_file:
        ids = {l.strip() for l in Path(exclude_file).read_text(encoding="utf-8").splitlines() if l.strip()}
        excluded = sorted(ids & {p["item_id"] for p in pop})
        pop = [p for p in pop if p["item_id"] not in ids]
        print(f"Excluded {len(excluded)} held-out items listed in {exclude_file}")
    if n > len(pop):
        sys.exit(f"N={n} exceeds the {len(pop)}-item test split. The pool is the pool.")

    by_stratum = defaultdict(list)
    for p in pop:
        by_stratum[p["stratum"]].append(p)

    alloc = allocate(pop, n)
    rng = random.Random(seed)
    sample = []
    for stratum in sorted(by_stratum):
        cell = sorted(by_stratum[stratum], key=lambda p: p["item_id"])  # order-independent
        sample.extend(rng.sample(cell, alloc[stratum]))
    sample.sort(key=lambda p: p["item_id"])

    src_hash = sha256_file(SOURCE)
    for p in sample:
        p["source_sha256"] = src_hash
        p["source_split"] = "medqa/US/4_options/phrases_no_exclude_test"

    payload = "".join(json.dumps(p, ensure_ascii=False, sort_keys=True) + "\n" for p in sample)
    new_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()

    if OUT.exists():
        old_hash = sha256_file(OUT)
        if old_hash == new_hash:
            print("Frozen file already matches this configuration. Nothing to do.")
            return
        if not reason:
            sys.exit(
                "REFUSING to overwrite a frozen item set.\n"
                f"  on disk: {old_hash[:16]}\n  would write: {new_hash[:16]}\n"
                "Re-run with --reason \"...\" if the change is intended. It will be logged."
            )
        log_change(old_hash, new_hash, reason, n, seed)

    OUT.write_text(payload, encoding="utf-8")

    pop_mix = Counter(p["stratum"] for p in pop)
    manifest = {
        "tool_version": TOOL_VERSION,
        "frozen_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": {
            "path": str(SOURCE.relative_to(ROOT)),
            "sha256": src_hash,
            "dataset": "MedQA (Jin et al., 2021), US 4-option, test split",
            "license": "MIT (see data/medqa_source/LICENSE.medqa)",
            "population_size": len(pop),
            "duplicates_dropped": dupes,
            "held_out_excluded": excluded,
            "held_out_source": exclude_file,
        },
        "sampling": {
            "n": len(sample),
            "seed": seed,
            "method": "stratified, proportional allocation, largest-remainder rounding",
            "strata_axes": ["exam_step", "has_labs", "len_tercile"],
            "len_tercile_cuts_words": {"short<=": cuts[0], "mid<=": cuts[1]},
        },
        "output": {"path": str(OUT.relative_to(ROOT)), "sha256": new_hash, "lines": len(sample)},
        "strata": {
            s: {"population": pop_mix[s], "sampled": alloc[s],
                "pop_share": round(pop_mix[s] / len(pop), 4),
                "sample_share": round(alloc[s] / len(sample), 4)}
            for s in sorted(pop_mix)
        },
        "gold_letter_balance": {
            "population": dict(Counter(p["gold_letter"] for p in pop)),
            "sample": dict(Counter(p["gold_letter"] for p in sample)),
        },
    }
    MANIFEST.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")

    if not FREEZE_LOG.exists():
        FREEZE_LOG.write_text(
            "# Freeze log\n\nEvery change to `data/medqa_base.jsonl` after the initial "
            "freeze is recorded here with a reason.\n\n"
            f"- {manifest['frozen_at_utc']} — initial freeze, N={len(sample)}, "
            f"seed={seed}, sha256={new_hash[:16]}\n",
            encoding="utf-8",
        )

    print(f"Froze {len(sample)} items -> {OUT.relative_to(ROOT)}")
    print(f"  sha256 {new_hash}")
    print(f"  strata {len(manifest['strata'])} cells, seed {seed}")
    print(f"  gold letters {manifest['gold_letter_balance']['sample']}")


def log_change(old: str, new: str, reason: str, n: int, seed: int) -> None:
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with FREEZE_LOG.open("a", encoding="utf-8") as f:
        f.write(f"- {stamp} — REFREEZE N={n} seed={seed}: {reason}\n"
                f"  - was `{old[:16]}` now `{new[:16]}`\n")


def do_verify() -> None:
    if not (OUT.exists() and MANIFEST.exists()):
        sys.exit("nothing frozen yet")
    m = json.loads(MANIFEST.read_text(encoding="utf-8"))
    ok = True
    src = sha256_file(SOURCE)
    if src != m["source"]["sha256"]:
        print(f"FAIL source drifted\n  manifest {m['source']['sha256'][:16]}\n  disk     {src[:16]}")
        ok = False
    out = sha256_file(OUT)
    if out != m["output"]["sha256"]:
        print(f"FAIL frozen file edited\n  manifest {m['output']['sha256'][:16]}\n  disk     {out[:16]}")
        ok = False
    rows = [json.loads(l) for l in OUT.open(encoding="utf-8") if l.strip()]
    if len(rows) != m["output"]["lines"]:
        print(f"FAIL line count {len(rows)} != {m['output']['lines']}")
        ok = False
    ids = [r["item_id"] for r in rows]
    if len(set(ids)) != len(ids):
        print("FAIL duplicate item_ids")
        ok = False
    for r in rows:
        if r["gold_letter"] not in r["options"]:
            print(f"FAIL {r['item_id']} gold letter not among options")
            ok = False
    print("OK" if ok else "VERIFY FAILED")
    sys.exit(0 if ok else 1)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("freeze")
    f.add_argument("--n", type=int, default=500)
    f.add_argument("--seed", type=int, default=DEFAULT_SEED)
    f.add_argument("--reason", type=str, default=None)
    f.add_argument("--exclude-file", type=str, default=None,
                   help="path to a file of item_ids to hold out of the benchmark")
    sub.add_parser("verify")
    a = ap.parse_args()
    do_freeze(a.n, a.seed, a.reason, a.exclude_file) if a.cmd == "freeze" else do_verify()


if __name__ == "__main__":
    main()
