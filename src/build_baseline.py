"""
Build the readability baseline: scores every cleaned corpus, writes per-item
metrics + summary distributions + histograms, and runs Gate 3's falsifiable
sanity check (docs/reference_corpora_plan.md D6/Gate 3).

Usage:
    python src/build_baseline.py \\
        --mesh-xml path/to/desc2026.xml \\
        --askdocs data/reference/askdocs_clean.jsonl \\
        --meqsum data/reference/meqsum_clean.jsonl \\
        --medqa-base data/medqa_base.jsonl \\
        --out-dir data/reference

Outputs:
    askdocs_metrics.csv, meqsum_metrics.csv, medqa_stems_metrics.csv
        -- per-item metric rows keyed by content hash, NO raw text (D6/D7)
    baseline_stats.json -- per corpus per metric: n, mean, sd, percentiles
    histograms/*.png
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import readability as rd


def _hash_id(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def score_corpus(items: list[dict], matcher: dict, text_key: str = "text") -> list[dict]:
    rows = []
    for item in items:
        text = item[text_key]
        scores = rd.score_item(text, matcher)
        rows.append({
            "item_id": item.get("item_id", _hash_id(text)),
            "content_hash": _hash_id(text),
            "flesch_kincaid_grade": round(scores.flesch_kincaid_grade, 3),
            "mean_sentence_length": round(scores.mean_sentence_length, 3),
            "med_term_density": round(scores.med_term_density, 3),
            "word_count": scores.word_count,
            "sentence_count": scores.sentence_count,
        })
    return rows


def summarize(rows: list[dict], metric: str) -> dict:
    values = sorted(r[metric] for r in rows)
    n = len(values)
    if n == 0:
        return {"n": 0}

    def pct(p: float) -> float:
        idx = min(n - 1, max(0, round(p * (n - 1))))
        return values[idx]

    return {
        "n": n,
        "mean": round(statistics.mean(values), 3),
        "sd": round(statistics.stdev(values), 3) if n > 1 else 0.0,
        "min": round(values[0], 3),
        "p10": round(pct(0.10), 3),
        "p25": round(pct(0.25), 3),
        "median": round(pct(0.50), 3),
        "p75": round(pct(0.75), 3),
        "p90": round(pct(0.90), 3),
        "max": round(values[-1], 3),
    }


def write_metrics_csv(rows: list[dict], out_path: Path) -> None:
    if not rows:
        out_path.write_text("", encoding="utf-8")
        return
    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def write_histogram(rows: list[dict], metric: str, title: str, out_path: Path) -> None:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print(f"  (matplotlib not available, skipping histogram for {title})")
        return
    values = [r[metric] for r in rows]
    fig, ax = plt.subplots(figsize=(6, 4))
    ax.hist(values, bins=30)
    ax.set_title(title)
    ax.set_xlabel(metric)
    ax.set_ylabel("count")
    fig.tight_layout()
    fig.savefig(out_path, dpi=100)
    plt.close(fig)


def load_jsonl(path: Path) -> list[dict]:
    items = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                items.append(json.loads(line))
    return items


def load_medqa_stems(path: Path) -> list[dict]:
    """MedQA base set items -> {item_id, text} using the clinical stem field."""
    items = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            stem = rec.get("stem") or rec.get("question") or ""
            if stem:
                items.append({"item_id": rec.get("item_id", ""), "text": stem})
    return items


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--mesh-xml", required=True, help="Path to MeSH descriptor XML (e.g. desc2026.xml)")
    ap.add_argument("--askdocs", default="data/reference/askdocs_clean.jsonl")
    ap.add_argument("--meqsum", default="data/reference/meqsum_clean.jsonl")
    ap.add_argument("--medqa-base", default="data/medqa_base.jsonl")
    ap.add_argument("--out-dir", default="data/reference")
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "histograms").mkdir(exist_ok=True)

    print(f"Parsing MeSH terms from {args.mesh_xml} ...")
    mesh_terms = rd.parse_mesh_terms(args.mesh_xml)
    print(f"  {len(mesh_terms)} terms parsed")
    matcher = rd.build_matcher(mesh_terms)

    corpora = {}

    askdocs_path = Path(args.askdocs)
    if askdocs_path.exists():
        print(f"Scoring AskDocs ({askdocs_path}) ...")
        corpora["askdocs"] = score_corpus(load_jsonl(askdocs_path), matcher)
    else:
        print(f"  SKIP AskDocs: {askdocs_path} not found (run clean_askdocs.py first)")

    meqsum_path = Path(args.meqsum)
    if meqsum_path.exists():
        print(f"Scoring MeQSum ({meqsum_path}) ...")
        corpora["meqsum"] = score_corpus(load_jsonl(meqsum_path), matcher)
    else:
        print(f"  SKIP MeQSum: {meqsum_path} not found (run pull_meqsum.py first)")

    medqa_path = Path(args.medqa_base)
    if medqa_path.exists():
        print(f"Scoring MedQA stems ({medqa_path}) -- for Gate 3 sanity check ...")
        corpora["medqa_stems"] = score_corpus(load_medqa_stems(medqa_path), matcher)
    else:
        print(f"  SKIP MedQA stems: {medqa_path} not found")

    baseline_stats = {}
    for name, rows in corpora.items():
        write_metrics_csv(rows, out_dir / f"{name}_metrics.csv")
        baseline_stats[name] = {
            metric: summarize(rows, metric)
            for metric in ("flesch_kincaid_grade", "mean_sentence_length", "med_term_density")
        }
        for metric in ("flesch_kincaid_grade", "mean_sentence_length", "med_term_density"):
            write_histogram(rows, metric, f"{name}: {metric}",
                             out_dir / "histograms" / f"{name}_{metric}.png")

    (out_dir / "baseline_stats.json").write_text(
        json.dumps(baseline_stats, indent=2), encoding="utf-8"
    )
    print(f"\nWrote {out_dir / 'baseline_stats.json'}")

    # Gate 3: falsifiable sanity check. If this fails, the matcher or the
    # cleaning is broken -- stop and debug, don't publish (per the plan).
    if "medqa_stems" in baseline_stats and ("askdocs" in baseline_stats or "meqsum" in baseline_stats):
        print("\n=== Gate 3: falsifiable sanity check ===")
        medqa_density = baseline_stats["medqa_stems"]["med_term_density"]["mean"]
        medqa_fk = baseline_stats["medqa_stems"]["flesch_kincaid_grade"]["mean"]
        gate_pass = True
        for name in ("askdocs", "meqsum"):
            if name not in baseline_stats:
                continue
            patient_density = baseline_stats[name]["med_term_density"]["mean"]
            patient_fk = baseline_stats[name]["flesch_kincaid_grade"]["mean"]
            density_ok = medqa_density > patient_density
            fk_ok = medqa_fk > patient_fk
            print(f"  {name}: density {patient_density} (MedQA: {medqa_density}) "
                  f"-> {'ok' if density_ok else 'FAIL'}")
            print(f"  {name}: FK grade {patient_fk} (MedQA: {medqa_fk}) "
                  f"-> {'ok' if fk_ok else 'FAIL'}")
            gate_pass = gate_pass and density_ok and fk_ok
        print(f"\nGATE {'PASS' if gate_pass else 'FAIL'}")
        if not gate_pass:
            sys.exit(1)
    else:
        print("\n(Gate 3 skipped -- need both medqa_stems and at least one patient corpus scored)")


if __name__ == "__main__":
    main()
