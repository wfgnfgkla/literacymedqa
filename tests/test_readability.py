"""
Known-answer tests for readability.py, run before touching any real corpus
data -- same principle as the harness sanity check: verify the measuring tool
works on cases with a known right answer before trusting it on real text.
"""
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import readability as rd


# --------------------------------------------------------------------------- #
# Flesch-Kincaid grade
# --------------------------------------------------------------------------- #

def test_fk_grade_simple_sentence_is_low():
    # "The cat sat on the mat." is a textbook-simple sentence -- should land
    # low (below elementary level; textstat gives it a negative grade, which
    # is correct behavior for this formula on very short/simple text, not a bug).
    grade = rd.flesch_kincaid_grade("The cat sat on the mat.")
    assert grade < 3


def test_fk_grade_complex_sentence_is_high():
    complex_sentence = (
        "The mitochondria is the powerhouse of the cell, facilitating "
        "cellular respiration through oxidative phosphorylation."
    )
    grade = rd.flesch_kincaid_grade(complex_sentence)
    assert grade > 12  # college level, matches jargon-heavy scientific text


# --------------------------------------------------------------------------- #
# Mean sentence length -- must share FK's own sentence-count denominator
# --------------------------------------------------------------------------- #

def test_mean_sentence_length_basic():
    text = "This is one sentence. This is another one."
    msl, word_count, sentence_count = rd.mean_sentence_length(text)
    assert sentence_count == 2
    assert word_count == 8  # "This is one sentence This is another one"
    assert msl == pytest.approx(4.0)


def test_mean_sentence_length_unpunctuated_run_on_counts_as_one_sentence():
    # Documented, known weakness (per the plan): unpunctuated text undercounts
    # sentences. This test locks in that the behavior is understood and
    # doesn't crash -- not that it's "correct" in an absolute sense.
    run_on = "i have had a headache for three days and i also feel dizzy and nauseous"
    msl, word_count, sentence_count = rd.mean_sentence_length(run_on)
    assert sentence_count == 1
    assert msl == word_count  # all words attributed to the one counted sentence


def test_mean_sentence_length_never_divides_by_zero():
    # Empty or near-empty text shouldn't crash the pipeline.
    msl, word_count, sentence_count = rd.mean_sentence_length("")
    assert sentence_count >= 1
    assert msl >= 0


# --------------------------------------------------------------------------- #
# MeSH XML parsing -- against a hand-built mock matching the REAL confirmed
# structure (DescriptorRecord > DescriptorName > String; DescriptorRecord >
# ConceptList > Concept > TermList > Term > String), not the real 100MB+ file.
# --------------------------------------------------------------------------- #

MOCK_MESH_XML = """<?xml version="1.0"?>
<DescriptorRecordSet>
  <DescriptorRecord DescriptorClass="1">
    <DescriptorUI>D006973</DescriptorUI>
    <DescriptorName>
      <String>Hypertension</String>
    </DescriptorName>
    <ConceptList>
      <Concept PreferredConceptYN="Y">
        <ConceptUI>M0010789</ConceptUI>
        <ConceptName><String>Hypertension</String></ConceptName>
        <TermList>
          <Term>
            <String>Hypertension</String>
          </Term>
          <Term>
            <String>High Blood Pressure</String>
          </Term>
        </TermList>
      </Concept>
    </ConceptList>
  </DescriptorRecord>
  <DescriptorRecord DescriptorClass="1">
    <DescriptorUI>D003327</DescriptorUI>
    <DescriptorName>
      <String>Coronary Disease</String>
    </DescriptorName>
    <ConceptList>
      <Concept PreferredConceptYN="Y">
        <ConceptUI>M0005433</ConceptUI>
        <ConceptName><String>Coronary Disease</String></ConceptName>
        <TermList>
          <Term>
            <String>Coronary Disease</String>
          </Term>
          <Term>
            <String>Heart Attack</String>
          </Term>
        </TermList>
      </Concept>
    </ConceptList>
  </DescriptorRecord>
  <DescriptorRecord DescriptorClass="1">
    <DescriptorUI>D003907</DescriptorUI>
    <DescriptorName>
      <String>Diabetes Mellitus</String>
    </DescriptorName>
    <ConceptList>
      <Concept PreferredConceptYN="Y">
        <ConceptUI>M0005827</ConceptUI>
        <ConceptName><String>Diabetes Mellitus</String></ConceptName>
        <TermList>
          <Term>
            <String>Diabetes Mellitus</String>
          </Term>
          <Term>
            <String>Diabetes</String>
          </Term>
        </TermList>
      </Concept>
    </ConceptList>
  </DescriptorRecord>
</DescriptorRecordSet>
"""


@pytest.fixture
def mock_mesh_file():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".xml", delete=False) as f:
        f.write(MOCK_MESH_XML)
        path = f.name
    yield path
    Path(path).unlink()


def test_parse_mesh_terms_extracts_descriptor_and_entry_terms(mock_mesh_file):
    terms = rd.parse_mesh_terms(mock_mesh_file)
    # Descriptor names (main headings)
    assert "hypertension" in terms
    assert "coronary disease" in terms
    assert "diabetes mellitus" in terms
    # Entry terms (synonyms) -- this is the whole point of using MeSH over a
    # flat keyword list: "high blood pressure" and "heart attack" are real
    # patient-language synonyms for clinical terms, and both must be captured.
    assert "high blood pressure" in terms
    assert "heart attack" in terms
    assert "diabetes" in terms


def test_parse_mesh_terms_lowercases_everything(mock_mesh_file):
    terms = rd.parse_mesh_terms(mock_mesh_file)
    assert "Hypertension" not in terms  # only lowercase form present
    assert "hypertension" in terms


# --------------------------------------------------------------------------- #
# MeSH matching -- positive controls, negative controls, longest-match-first
# --------------------------------------------------------------------------- #

@pytest.fixture
def matcher(mock_mesh_file):
    terms = rd.parse_mesh_terms(mock_mesh_file)
    return rd.build_matcher(terms, exclusions=set())  # no exclusions for these tests


def test_positive_control_multiword_term_matches(matcher):
    density, matched = rd.med_term_density("I think I had a heart attack yesterday", matcher)
    assert matched == 2  # "heart attack" -- 2 tokens matched as one term


def test_positive_control_entry_term_synonym_matches(matcher):
    # "high blood pressure" is an entry term (synonym) for the Hypertension
    # descriptor, not the descriptor name itself -- confirms synonyms are
    # actually usable for matching, not just present in the parsed set.
    density, matched = rd.med_term_density("My doctor said I have high blood pressure", matcher)
    assert matched == 3  # "high", "blood", "pressure"


def test_longest_match_first_does_not_double_count(matcher):
    # "heart attack" should match ONCE as a 2-token multi-word term, not
    # separately double-counted as if "heart" and "attack" were each
    # independently matched too (they aren't separate single-word MeSH terms
    # in this mock set, but the mechanism must not double-book positions
    # regardless).
    text = "heart attack"
    density, matched = rd.med_term_density(text, matcher)
    assert matched == 2  # not 4


def test_negative_control_common_word_exclusion(mock_mesh_file):
    # Real MeSH includes descriptors like "Cold" (as in Common Cold) that are
    # also everyday English words. Confirm the exclusion list actually
    # excludes them when applied (using the real default exclusions this
    # time, not the empty-set fixture).
    terms = {"diabetes", "cold"}  # pretend "cold" is a MeSH term, like it is
    matched_with_exclusion = rd.build_matcher(terms)  # default exclusions apply
    density, matched = rd.med_term_density("I feel cold today", matched_with_exclusion)
    assert matched == 0  # "cold" correctly excluded as a common word

    matched_without_exclusion = rd.build_matcher(terms, exclusions=set())
    density2, matched2 = rd.med_term_density("I feel cold today", matched_without_exclusion)
    assert matched2 == 1  # without the exclusion, it would incorrectly count


def test_density_calculation_is_per_100_words(matcher):
    # 10-word text with exactly 2 matched tokens ("heart attack") -> 20 per 100
    text = "yesterday I had what felt like a heart attack at work"
    density, matched = rd.med_term_density(text, matcher)
    word_count = len(rd._tokenize(text))
    assert word_count == 11
    assert matched == 2
    assert density == pytest.approx(2 / 11 * 100)


# --------------------------------------------------------------------------- #
# End-to-end: score_item ties everything together
# --------------------------------------------------------------------------- #

def test_score_item_end_to_end(matcher):
    text = "I have had high blood pressure for two years and I am scared."
    scores = rd.score_item(text, matcher)
    assert scores.flesch_kincaid_grade is not None
    assert scores.mean_sentence_length > 0
    assert scores.med_term_density > 0  # "high blood pressure" should match
    assert scores.word_count > 0
    assert scores.sentence_count >= 1
