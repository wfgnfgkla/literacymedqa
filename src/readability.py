"""
Readability metrics for LiteracyMedQA reference corpora and rewrites.

Computes, per item:
    - flesch_kincaid_grade: US school grade level (textstat)
    - mean_sentence_length: words / textstat.sentence_count -- the SAME
      denominator FK uses internally, so the two metrics share a denominator.
      Known, documented weakness: unpunctuated run-on text undercounts
      sentences, inflating both metrics -- but identically across every corpus
      and every rewrite level, so comparisons stay apples-to-apples.
    - med_term_density: MeSH-matched tokens per 100 words. Deterministic
      (lowercased, longest-match-first) -- NOT scispaCy. See
      docs/reference_corpora_plan.md R2 for why scispaCy was deliberately
      avoided (a real dependency-pinning trap, same failure class as vLLM's).

Reusable by design: the same functions score the reference corpora AND the
level (a)/(b)/(c) rewrites later, so every comparison the realism gate makes
is genuinely apples-to-apples, not two different measurement processes.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

import textstat


@dataclass
class ReadabilityScores:
    flesch_kincaid_grade: float
    mean_sentence_length: float
    med_term_density: float  # matched tokens per 100 words
    word_count: int
    sentence_count: int


def flesch_kincaid_grade(text: str) -> float:
    return textstat.flesch_kincaid_grade(text)


def mean_sentence_length(text: str) -> tuple[float, int, int]:
    """Returns (mean_sentence_length, word_count, sentence_count)."""
    word_count = textstat.lexicon_count(text, removepunct=True)
    sentence_count = max(1, textstat.sentence_count(text))  # never divide by 0
    return word_count / sentence_count, word_count, sentence_count


# --------------------------------------------------------------------------- #
# MeSH term matcher
# --------------------------------------------------------------------------- #

_WORD_RE = re.compile(r"[a-z0-9]+(?:'[a-z]+)?")


def _tokenize(text: str) -> list[str]:
    return _WORD_RE.findall(text.lower())


def parse_mesh_terms(desc_xml_path: str | Path) -> set[str]:
    """
    Parse a MeSH descriptor XML file (e.g. desc2026.xml from NLM) and return
    the full set of lowercased terms: every DescriptorName plus every entry
    term (synonym) nested under it. Uses .iter() rather than a rigid
    exact-depth path, since MeSH's real Concept/Term nesting has more
    structure (preferred vs. non-preferred concepts, multiple terms per
    concept) than is worth hard-coding a brittle exact path for -- any
    <String> that is the text content of a <DescriptorName> or a <Term>
    counts, regardless of exactly how many ConceptList/Concept/TermList
    levels sit above it.
    """
    terms: set[str] = set()
    # iterparse to avoid loading the whole (large) file into memory at once.
    # Clearing happens ONLY at the DescriptorRecord boundary below, once a
    # full record and everything nested inside it has already been read --
    # NOT on every nested element's own "end" event. iterparse's "end" events
    # fire for every element in document order, innermost first, so a naive
    # "clear whatever just ended" clears each <String> before its parent
    # <Term>/<DescriptorName> ever gets to read its .text -- confirmed this
    # was happening via direct debugging (a bare parse with no clearing found
    # every term correctly; adding the naive per-element clear made
    # parse_mesh_terms return nothing at all).
    context = ET.iterparse(str(desc_xml_path), events=("end",))
    for _, elem in context:
        if elem.tag == "DescriptorName":
            s = elem.find("String")
            if s is not None and s.text:
                terms.add(s.text.strip().lower())
        elif elem.tag == "Term":
            s = elem.find("String")
            if s is not None and s.text:
                terms.add(s.text.strip().lower())
        elif elem.tag == "DescriptorRecord":
            elem.clear()  # safe here: this whole record, and everything
                           # nested inside it, has already been read above.
    # Drop single-character terms -- not meaningful for word-level matching
    # and a source of false positives (MeSH has some very short abbreviation
    # entries that would otherwise match almost anything).
    return {t for t in terms if len(t) > 1}


DEFAULT_COMMON_WORD_EXCLUSIONS = {
    # MeSH terms that are also everyday English words -- included here as a
    # STARTING list, meant to be reviewed and extended by a human, not treated
    # as complete. Shipped as a plain, reviewable set rather than buried in
    # matching logic, per the plan's transparency requirement (D5).
    "cold", "pressure", "cell", "cells", "back", "blood", "growth", "fall",
    "fit", "labor", "stress", "shock", "gas", "milk", "salt", "sugar", "iron",
    "run", "walk", "spot", "mole", "pill", "cast", "burn", "gum", "nerve",
    "sound", "smell", "taste", "touch", "weight", "age", "sex", "hearing",
    "vision", "memory", "sleep", "diet", "exercise", "attention",
}


def build_matcher(mesh_terms: set[str], exclusions: set[str] | None = None) -> dict:
    """
    Builds an indexed matcher for longest-match-first matching at real MeSH
    scale (267k+ terms). A naive "scan every multi-word term against every
    document" approach is O(terms x tokens) PER DOCUMENT -- fine against a
    handful of mock terms, but genuinely unworkable at real MeSH scale (this
    was caught by actually running the full pipeline against the real
    267,012-term file, not assumed from the mock-scale tests alone -- the
    real run timed out before this fix).

    Instead, multi-word terms are indexed by their FIRST token: at each token
    position in a document, only the (usually small) set of candidate terms
    that could plausibly start there gets checked, not the entire vocabulary.
    """
    exclusions = exclusions if exclusions is not None else DEFAULT_COMMON_WORD_EXCLUSIONS
    single_word = {t for t in mesh_terms if " " not in t and t not in exclusions}

    multi_by_first: dict[str, list[tuple[str, ...]]] = {}
    for t in mesh_terms:
        if " " in t and t not in exclusions:
            term_tokens = tuple(t.split())
            multi_by_first.setdefault(term_tokens[0], []).append(term_tokens)
    # Longest-first within each bucket, so the first match tried at a given
    # position is the longest one -- true longest-match-first behavior.
    for first_tok in multi_by_first:
        multi_by_first[first_tok].sort(key=len, reverse=True)

    return {"single": single_word, "multi_by_first": multi_by_first}


def med_term_density(text: str, matcher: dict) -> tuple[float, int]:
    """
    Returns (density_per_100_words, matched_token_count).
    Longest-match-first: at each token position, checks only the indexed
    candidate multi-word terms starting with that exact token (fast, even at
    267k-term scale), falling back to single-word matching for positions no
    multi-word term claimed.
    """
    tokens = _tokenize(text)
    matched_positions: set[int] = set()
    multi_by_first = matcher["multi_by_first"]

    for i, tok in enumerate(tokens):
        if i in matched_positions:
            continue
        candidates = multi_by_first.get(tok)
        if not candidates:
            continue
        for term_tokens in candidates:  # already longest-first
            n = len(term_tokens)
            if i + n <= len(tokens) and tuple(tokens[i:i + n]) == term_tokens:
                matched_positions.update(range(i, i + n))
                break

    for i, tok in enumerate(tokens):
        if i in matched_positions:
            continue
        if tok in matcher["single"]:
            matched_positions.add(i)

    word_count = len(tokens)
    matched = len(matched_positions)
    density = (matched / word_count * 100) if word_count else 0.0
    return density, matched


def score_item(text: str, matcher: dict) -> ReadabilityScores:
    fk = flesch_kincaid_grade(text)
    msl, word_count, sentence_count = mean_sentence_length(text)
    density, _ = med_term_density(text, matcher)
    return ReadabilityScores(
        flesch_kincaid_grade=fk,
        mean_sentence_length=msl,
        med_term_density=density,
        word_count=word_count,
        sentence_count=sentence_count,
    )
