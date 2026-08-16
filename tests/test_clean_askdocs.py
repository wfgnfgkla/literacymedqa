"""
Tests for clean_askdocs.py. Run against tests/fixtures/mock_askdocs_raw.jsonl,
built to exercise every real cleaning rule -- including cases found by
actually running the cleaner against it, not just written from the spec
up front (the quote-stripping test below exists because an earlier draft
of the fixture had a bug -- '>' placed mid-line instead of at a real line
start -- that made it look like the cleaner was broken when the fixture was
actually unrealistic. Fixed the fixture, then locked in both the corrected
behavior AND the inline '>' preservation case as permanent tests.)
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import clean_askdocs as ca

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "mock_askdocs_raw.jsonl"


def test_full_pipeline_counts():
    result = ca.clean_askdocs_file(FIXTURE)
    c = result["counts"]
    assert c["total_lines"] == 10
    assert c["dropped_removed_or_empty"] == 1   # abc114: empty title + [deleted]
    assert c["dropped_bot_or_deleted_author"] == 1  # abc115: AutoModerator
    assert c["dropped_duplicate"] == 1          # abc118: exact repost of abc117
    assert c["dropped_under_min_words"] == 2    # abc113 (title-only), abc116 ("hi")
    assert c["kept"] == 5
    drop_reasons_sum = (c["dropped_removed_or_empty"] + c["dropped_bot_or_deleted_author"]
                         + c["dropped_duplicate"] + c["dropped_under_min_words"])
    assert c["total_lines"] == drop_reasons_sum + c["kept"]  # every line accounted for


def _get_item(items, item_id):
    return next(i for i in items if i["item_id"] == item_id)


def test_markdown_link_becomes_link_text_url_stripped():
    result = ca.clean_askdocs_file(FIXTURE)
    item = _get_item(result["items"], "askdocs_abc111")
    assert "http" not in item["text"]
    assert "photo" in item["text"]  # link text preserved, URL itself gone


def test_username_mention_stripped():
    result = ca.clean_askdocs_file(FIXTURE)
    item = _get_item(result["items"], "askdocs_abc117")
    assert "u/somedoctor" not in item["text"]
    assert "/u/" not in item["text"]


def test_multiline_quote_block_fully_stripped():
    result = ca.clean_askdocs_file(FIXTURE)
    item = _get_item(result["items"], "askdocs_abc117")
    assert "quoted text from a reply" not in item["text"]
    assert "second line of the quote" not in item["text"]
    # surrounding real content must survive the strip
    assert "chest" in item["text"].lower()
    assert "out of shape" in item["text"]


def test_inline_comparison_symbols_preserved_not_stripped():
    # Real clinical content ("blood sugar >200") must NOT be mistaken for a
    # markdown quote just because it contains '>' -- only an actual line-start
    # '>' (a real blockquote) should be stripped.
    result = ca.clean_askdocs_file(FIXTURE)
    item = _get_item(result["items"], "askdocs_abc120")
    assert ">200" in item["text"]
    assert "<100" in item["text"]


def test_markdown_emphasis_markers_stripped():
    result = ca.clean_askdocs_file(FIXTURE)
    item = _get_item(result["items"], "askdocs_abc119")
    assert "*" not in item["text"]
    assert "_" not in item["text"]
    assert "ibuprofen" in item["text"]
    assert "acetaminophen" in item["text"]


def test_duplicate_detection_is_content_based_not_id_based():
    # abc117 and abc118 have different ids/authors but identical cleaned text
    # -- the second must be dropped as a duplicate, proving dedup keys off
    # content, not off Reddit's own post id.
    result = ca.clean_askdocs_file(FIXTURE)
    ids_kept = {i["item_id"] for i in result["items"]}
    assert "askdocs_abc117" in ids_kept
    assert "askdocs_abc118" not in ids_kept


def test_removed_post_with_no_title_is_dropped():
    result = ca.clean_askdocs_file(FIXTURE)
    ids_kept = {i["item_id"] for i in result["items"]}
    assert "askdocs_abc114" not in ids_kept


def test_bot_author_dropped_even_with_real_looking_text():
    result = ca.clean_askdocs_file(FIXTURE)
    ids_kept = {i["item_id"] for i in result["items"]}
    assert "askdocs_abc115" not in ids_kept


def test_short_fragment_dropped():
    result = ca.clean_askdocs_file(FIXTURE)
    ids_kept = {i["item_id"] for i in result["items"]}
    assert "askdocs_abc116" not in ids_kept  # "help / hi" -- 2 words


def test_canonical_key_is_whitespace_and_case_insensitive():
    a = ca.canonical_key("Hello   World")
    b = ca.canonical_key("hello world")
    assert a == b
