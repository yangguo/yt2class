from __future__ import annotations

from yt2class.stages.llm_util import estimate_tokens, load_prompt


def test_estimate_tokens_counts_cjk_more_aggressively_than_ascii():
    assert estimate_tokens("讲解要点讲解") > estimate_tokens("abcdefgh")


def test_load_prompt_is_cached():
    load_prompt.cache_clear()
    first = load_prompt("segment.md")
    second = load_prompt("segment.md")
    assert first == second
    assert load_prompt.cache_info().hits >= 1
