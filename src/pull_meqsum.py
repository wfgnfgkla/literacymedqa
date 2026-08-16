"""
Load and clean the MeQSum corpus (Ben Abacha & Demner-Fushman, ACL 2019).

Source: https://github.com/abachaa/MeQSum (MeQSum_ACL2019_BenAbacha_Demner-Fushman.xlsx)
1,000 real consumer health questions submitted to the NLM. We use the CHQ
(original question) field only -- not the Summary field, which is the
paper's own abstractive summarization target, not real patient text.

Per docs/reference_corpora_plan.md D2: the raw CHQ field carries website form
scaffolding ("SUBJECT: ...\\nMESSAGE: ...") on SOME but not all rows --
confirmed directly against the real downloaded file, not assumed universal.
Strip the literal labels when present; leave text as-is when they aren't.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import openpyxl

SUBJECT_MESSAGE_RE = re.compile(
    r"^SUBJECT:\s*(?P<subject>.*?)\s*\nMESSAGE:\s*(?P<message>.*)$",
    re.IGNORECASE | re.DOTALL,
)


def strip_form_scaffolding(chq: str) -> str:
    """
    Strips the literal 'SUBJECT:' / 'MESSAGE:' labels when present, joining
    the two parts with a newline (per D2). Text without this scaffolding is
    returned unchanged -- confirmed via the real file that not every row has
    it (e.g. row 2 in the real data is plain unscaffolded text).
    """
    m = SUBJECT_MESSAGE_RE.match(chq.strip())
    if m:
        subject = m.group("subject").strip()
        message = m.group("message").strip()
        return f"{subject}\n{message}" if subject else message
    return chq.strip()


def canonical_key(text: str) -> str:
    """Hash-based dedup key, same convention as sample_and_freeze.py."""
    normalized = re.sub(r"\s+", " ", text.strip().lower())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def load_meqsum(xlsx_path: str | Path) -> list[dict]:
    """
    Returns a list of {item_id, text} dicts, deduplicated, scaffolding
    stripped, in the file's original row order (item_id = row index, stable
    across re-runs on the same file).
    """
    wb = openpyxl.load_workbook(str(xlsx_path))
    ws = wb[wb.sheetnames[0]]

    items = []
    seen_keys = set()
    dupes_dropped = 0
    for i, row in enumerate(ws.iter_rows(min_row=2, values_only=True), start=1):
        file_id, chq, summary = row[0], row[1], row[2]
        if not chq or not str(chq).strip():
            continue
        cleaned = strip_form_scaffolding(str(chq))
        key = canonical_key(cleaned)
        if key in seen_keys:
            dupes_dropped += 1
            continue
        seen_keys.add(key)
        items.append({
            "item_id": f"meqsum_{i:04d}",
            "source_file": file_id,
            "text": cleaned,
        })

    return items, dupes_dropped


def write_manifest(items: list[dict], dupes_dropped: int, source_path: Path,
                    out_path: Path) -> None:
    source_bytes = source_path.read_bytes()
    manifest = {
        "source": "MeQSum (Ben Abacha & Demner-Fushman, ACL 2019)",
        "source_url": "https://github.com/abachaa/MeQSum",
        "citation": (
            "Asma Ben Abacha and Dina Demner-Fushman. 2019. On the "
            "Summarization of Consumer Health Questions. ACL 2019."
        ),
        "source_file_sha256": hashlib.sha256(source_bytes).hexdigest(),
        "total_rows_in_source": 1000,
        "items_kept": len(items),
        "duplicates_dropped": dupes_dropped,
        "field_used": "CHQ (original question) -- NOT Summary",
        "cleaning": "SUBJECT:/MESSAGE: form scaffolding stripped when present "
                    "(not universal across rows -- confirmed empirically)",
    }
    out_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")


if __name__ == "__main__":
    import sys

    xlsx_path = Path(sys.argv[1] if len(sys.argv) > 1 else "data/reference/MeQSum.xlsx")
    out_dir = Path("data/reference")
    out_dir.mkdir(parents=True, exist_ok=True)

    items, dupes_dropped = load_meqsum(xlsx_path)
    print(f"Loaded {len(items)} items ({dupes_dropped} duplicates dropped)")

    out_jsonl = out_dir / "meqsum_clean.jsonl"
    with out_jsonl.open("w", encoding="utf-8") as f:
        for item in items:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")
    print(f"Wrote {out_jsonl}")

    write_manifest(items, dupes_dropped, xlsx_path, out_dir / "manifest_meqsum.json")
    print(f"Wrote {out_dir / 'manifest_meqsum.json'}")
