from __future__ import annotations

from yt2class.stages.llm_util import text_has_negation


def test_english_negation_requires_word_boundaries():
    assert not text_has_negation("This is a notice about the economy.")
    assert not text_has_negation("annotation and international usage")
    assert text_has_negation("This is not a drill.")
    assert text_has_negation("We never do that.")
    assert text_has_negation("No problem at all.")
    assert text_has_negation("It doesn't work.")


def test_cjk_negation_still_detected():
    assert text_has_negation("这不是自动词。")
