#!/usr/bin/env python3
"""
Readability gate -- level (c) rewrites against the real corpora, all three metrics.

Scores our level (c) rewrites with the SAME functions in readability.py that
produced askdocs_metrics.csv, meqsum_metrics.csv and medqa_stems_metrics.csv. That
is the point of reusing them: if our side were measured by a second implementation,
any difference between the distributions could be the measurement rather than the
text, and the comparison would prove nothing.

DISTRIBUTIONS, NOT MEANS

Every metric is reported as min / p25 / median / p75 / max / sd on both sides. The
means alone have already misled this project once: the pilot's level (c) FK mean of
7.30 looked healthy while only 50 of 100 items were inside the 6-8 band, 21 below
and 29 above cancelling each other out. A summary statistic that averages two
opposite failures into an apparent success is worse than no summary at all.

TWO DENOMINATORS

The fidelity gate dropped items, so "our level (c) rewrites" is ambiguous:

  all        all 100 generated level (c) rewrites
  surviving  only those that passed fidelity, which is what would actually
             enter the benchmark

Both are reported. The surviving set is the defensible denominator for a claim
about the released benchmark, but the gate threshold was written before anyone
knew there would be drops, so the choice is not ours to make silently -- if the two
diverge, that belongs with Kiran before anyone calls this gate passed or failed.

    python src/readability_gate.py --mesh-xml path/to/desc2026.xml
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics as st
import sys
from pathlib import Path

try:
    import yaml
except ImportError as exc:  # pragma: no cover
    raise SystemExit("pyyaml is required: pip install pyyaml") from exc

sys.path.insert(0, str(Path(__file__).resolve().parent))
import readability as rd  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
METRICS = ("flesch_kincaid_grade", "mean_sentence_length", "med_term_density")
CORPORA = ("askdocs", "meqsum")


def describe(vals: list[float]) -> dict:
    v = sorted(vals)
    n = len(v)
    q = lambda p: v[min(n - 1, int(p * n))]
    return {"n": n, "min": v[0], "p25": q(.25), "median": st.median(v),
            "p75": q(.75), "max": v[-1], "mean": st.mean(v),
            "sd": st.pstdev(v) if n > 1 else 0.0}


def fmt(d: dict) -> str:
    return (f"n={d['n']:<6} min {d['min']:7.2f}  p25 {d['p25']:7.2f}  med {d['median']:7.2f}  "
            f"p75 {d['p75']:7.2f}  max {d['max']:8.2f}  mean {d['mean']:7.2f}  sd {d['sd']:6.2f}")


def load_corpus(name: str) -> dict[str, list[float]]:
    path = ROOT / "data" / "reference" / f"{name}_metrics.csv"
    out = {m: [] for m in METRICS}
    with open(path, encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            for m in METRICS:
                out[m].append(float(row[m]))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mesh-xml", required=True,
                    help="MeSH descriptor XML. Not in the repo by design; pinned by "
                         "sha256 in data/reference/manifest_mesh.json.")
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    ap.add_argument("--rewrites", default=str(ROOT / "data" / "pilot_rewrites.jsonl"))
    ap.add_argument("--verdicts", default=str(ROOT / "data" / "pilot_verdicts.jsonl"))
    ap.add_argument("--out", default=str(ROOT / "data" / "reference" / "level_c_metrics.csv"))
    args = ap.parse_args()

    cfg = yaml.safe_load(open(args.config, encoding="utf-8"))
    threshold = float(cfg["gates"]["realism"]["max_fk_grade_delta"])
    ref_stat = cfg["gates"]["realism"].get("fk_delta_reference_statistic")

    rows = [json.loads(l) for l in open(args.rewrites, encoding="utf-8") if l.strip()]
    verdicts = [json.loads(l) for l in open(args.verdicts, encoding="utf-8") if l.strip()]
    dropped_c = {v["item_id"] for v in verdicts if v["level"] == "c" and v.get("dropped")}

    print(f"  MeSH   {args.mesh_xml}")
    terms = rd.parse_mesh_terms(args.mesh_xml)
    matcher = rd.build_matcher(terms)
    print(f"         {len(terms):,} terms")

    scored = []
    for r in rows:
        if "error" in r or not r.get("level_c"):
            continue
        s = rd.score_item(r["level_c"], matcher)
        scored.append({"item_id": r["item_id"], "surviving": r["item_id"] not in dropped_c,
                       "flesch_kincaid_grade": s.flesch_kincaid_grade,
                       "mean_sentence_length": s.mean_sentence_length,
                       "med_term_density": s.med_term_density,
                       "word_count": s.word_count, "sentence_count": s.sentence_count})

    out = Path(args.out)
    with open(out, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(scored[0].keys()))
        w.writeheader()
        w.writerows(scored)
    print(f"  wrote  {out}  ({len(scored)} level (c) rewrites)")

    corpora = {c: load_corpus(c) for c in CORPORA}
    sets = {"all": scored, "surviving": [s for s in scored if s["surviving"]]}

    for metric in METRICS:
        print()
        print("=" * 108)
        print(f"  {metric}")
        print("=" * 108)
        for c in CORPORA:
            print(f"  {c + ' (real)':22s} {fmt(describe(corpora[c][metric]))}")
        print(f"  {'medqa stems (clinical)':22s} "
              f"{fmt(describe(load_corpus('medqa_stems')[metric]))}")
        for label, subset in sets.items():
            vals = [s[metric] for s in subset]
            print(f"  {'level (c), ' + label:22s} {fmt(describe(vals))}")

    # ---- distribution overlap, not just position --------------------------
    # Matching a corpus median says nothing about matching its shape. If our
    # distribution really resembled the reference, ~50% of our items would fall
    # inside its p25-p75 and ~80% inside its p10-p90. Large departures mean we hit
    # the right centre by a different route than real patients take.
    print()
    print("=" * 108)
    print("  DISTRIBUTION OVERLAP vs askdocs (the real-patient corpus)")
    print("=" * 108)
    print("  expected if our distribution matched: 50% inside p25-p75, 80% inside p10-p90")
    print()
    print(f"  {'metric':24s} {'set':11s} {'in p25-p75':>11s} {'in p10-p90':>11s}  shape")
    for metric in METRICS:
        ref = sorted(corpora["askdocs"][metric])
        n = len(ref)
        q = lambda pp: ref[min(n - 1, int(pp * n))]
        lo50, hi50, lo80, hi80 = q(.25), q(.75), q(.10), q(.90)
        for label, subset in sets.items():
            vals = [s[metric] for s in subset]
            i50 = sum(1 for v in vals if lo50 <= v <= hi50) / len(vals) * 100
            i80 = sum(1 for v in vals if lo80 <= v <= hi80) / len(vals) * 100
            note = "matches" if abs(i50 - 50) <= 15 else ("NARROWER" if i50 > 65 else "SHIFTED/WIDER")
            print(f"  {metric:24s} {label:11s} {i50:10.0f}% {i80:10.0f}%  {note}")

    # ---- FK band, the number a mean can hide ------------------------------
    print()
    print("  FK 6-8 band (the rewriter prompt's own calibration target):")
    for label, subset in sets.items():
        v = [s["flesch_kincaid_grade"] for s in subset]
        below = sum(1 for x in v if x < 6)
        above = sum(1 for x in v if x > 8)
        print(f"    level (c), {label:10s} in band {len(v)-below-above:3d}/{len(v)}   "
              f"below 6: {below:3d}   above 8: {above:3d}")

    # ---- the gate itself -------------------------------------------------
    print()
    print("=" * 108)
    print("  GATE: level (c) FK grade vs real-corpus FK grade")
    print("=" * 108)
    print(f"  threshold max_fk_grade_delta = {threshold}")
    print(f"  config pins the CORPUS statistic as '{ref_stat}' but does NOT pin ours,")
    print("  so both of our statistics are reported against it. They disagree.")
    print()
    header = f"  {'our set':12s} {'our stat':10s} {'value':>7s}   {'corpus':10s} {'median':>7s}   {'delta':>6s}  verdict"
    print(header)
    print("  " + "-" * (len(header) - 2))
    any_fail = False
    for c in CORPORA:
        ref = st.median(corpora[c]["flesch_kincaid_grade"])
        for label, subset in sets.items():
            vals = [s["flesch_kincaid_grade"] for s in subset]
            for stat_name, stat in (("median", st.median(vals)), ("mean", st.mean(vals))):
                delta = abs(stat - ref)
                ok = delta <= threshold
                any_fail |= not ok
                print(f"  {label:12s} {stat_name:10s} {stat:7.2f}   {c:10s} {ref:7.2f}   "
                      f"{delta:6.2f}  {'PASS' if ok else 'FAIL'}")
    print()
    print(f"  ALL COMBINATIONS PASS" if not any_fail else
          "  AT LEAST ONE COMBINATION FAILS -- see above")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
