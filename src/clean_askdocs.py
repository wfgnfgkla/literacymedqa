"""
Clean a raw r/AskDocs NDJSON pull (from the Arctic Shift download tool) into
the reference corpus format, per docs/reference_corpora_plan.md D3.

Input: one JSON object per line, Reddit submission fields (id, title,
selftext, author, created_utc -- the standard Arctic Shift/Pushshift-style
submission schema). Only submissions, not comments -- comments are mostly
clinician answers, not patient questions.

Pipeline, in order, each step counted in the manifest:
    1. drop [removed]/[deleted]/empty selftext
    2. drop bot authors (AutoModerator) and mod megathreads
    3. strip URLs, markdown syntax, quoted blocks, u/username mentions
    4. dedupe via canonical-hash (same convention as sample_and_freeze.py)
    5. drop items under 20 words

Text = title + selftext, joined with a newline -- the title is often itself
a compressed statement of the question ("Is this mole normal?"), not just
a label, so dropping it would throw away real signal.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

REMOVED_MARKERS = {"[removed]", "[deleted]", ""}
BOT_AUTHORS = {"AutoModerator", "[deleted]"}

URL_RE = re.compile(r"https?://\S+|www\.\S+")
USERNAME_RE = re.compile(r"/?u/[A-Za-z0-9_-]+")
MARKDOWN_QUOTE_RE = re.compile(r"^>.*$", re.MULTILINE)
MARKDOWN_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")  # [text](url) -> text
MARKDOWN_EMPHASIS_RE = re.compile(r"(\*\*|\*|__|_|~~)")
WHITESPACE_RE = re.compile(r"\n{3,}")


def clean_text(text: str) -> str:
    text = MARKDOWN_QUOTE_RE.sub("", text)          # drop quoted blocks entirely
    text = MARKDOWN_LINK_RE.sub(r"\1", text)          # [text](url) -> text
    text = URL_RE.sub("", text)                        # strip bare URLs
    text = USERNAME_RE.sub("", text)                    # strip u/username mentions
    text = MARKDOWN_EMPHASIS_RE.sub("", text)             # strip *_~ emphasis markers
    text = WHITESPACE_RE.sub("\n\n", text)                 # collapse excess blank lines
    return text.strip()


def canonical_key(text: str) -> str:
    normalized = re.sub(r"\s+", " ", text.strip().lower())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def clean_askdocs_file(raw_path: str | Path, min_words: int = 20) -> dict:
    """
    Returns {"items": [...], "counts": {...}} -- counts track exactly how many
    items were dropped at each pipeline step, for the manifest (same
    transparency convention as Dong's dataset freeze).
    """
    counts = {
        "total_lines": 0,
        "dropped_removed_or_empty": 0,
        "dropped_bot_or_deleted_author": 0,
        "dropped_duplicate": 0,
        "dropped_under_min_words": 0,
        "kept": 0,
    }
    items = []
    seen_keys = set()

    with open(raw_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            counts["total_lines"] += 1
            try:
                post = json.loads(line)
            except json.JSONDecodeError:
                counts["dropped_removed_or_empty"] += 1
                continue

            selftext = (post.get("selftext") or "").strip()
            title = (post.get("title") or "").strip()
            author = post.get("author") or ""

            if selftext in REMOVED_MARKERS and not title:
                counts["dropped_removed_or_empty"] += 1
                continue
            if selftext in REMOVED_MARKERS:
                # Title-only post with no real body text -- still counts as
                # too thin to be useful; treat the same as removed/empty.
                selftext = ""

            if author in BOT_AUTHORS:
                counts["dropped_bot_or_deleted_author"] += 1
                continue

            combined_raw = f"{title}\n{selftext}".strip()
            cleaned = clean_text(combined_raw)

            key = canonical_key(cleaned)
            if key in seen_keys:
                counts["dropped_duplicate"] += 1
                continue

            word_count = len(cleaned.split())
            if word_count < min_words:
                counts["dropped_under_min_words"] += 1
                continue

            seen_keys.add(key)
            items.append({
                "item_id": f"askdocs_{post.get('id', len(items))}",
                "text": cleaned,
                "created_utc": post.get("created_utc"),
            })
            counts["kept"] += 1

    return {"items": items, "counts": counts}


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python clean_askdocs.py <raw_ndjson_path> [output_dir]")
        sys.exit(1)

    raw_path = Path(sys.argv[1])
    out_dir = Path(sys.argv[2] if len(sys.argv) > 2 else "data/reference")
    out_dir.mkdir(parents=True, exist_ok=True)

    result = clean_askdocs_file(raw_path)
    print("Cleaning counts:")
    for k, v in result["counts"].items():
        print(f"  {k}: {v}")

    out_jsonl = out_dir / "askdocs_clean.jsonl"
    with out_jsonl.open("w", encoding="utf-8") as f:
        for item in result["items"]:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")
    print(f"\nWrote {len(result['items'])} items -> {out_jsonl}")

    manifest = {
        "source": "r/AskDocs via Arctic Shift download tool",
        "raw_file": str(raw_path),
        "raw_file_sha256": hashlib.sha256(raw_path.read_bytes()).hexdigest(),
        "cleaning_counts": result["counts"],
    }
    manifest_path = out_dir / "manifest_askdocs.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"Wrote manifest -> {manifest_path}")
