from __future__ import annotations

from yt2class.stages.student_copy import contains_student_meta, sanitize_student_copy


def test_sanitize_strips_asr_meta_phrases():
    raw = "用法1：改装工事 ASR→误听为2月。一時休業につきご迷惑を。"
    cleaned = sanitize_student_copy(raw)
    assert "ASR" not in cleaned
    assert "误听" not in cleaned
    assert "休業" in cleaned or "につき" in cleaned


def test_sanitize_removes_teacher_transition_markers():
    raw = "老师过渡：接下来看板书对应。"
    assert contains_student_meta(raw)
    cleaned = sanitize_student_copy(raw)
    assert cleaned == "" or "老师过渡" not in cleaned


def test_headword_display_normalizes_tsuki():
    assert "～につき" in sanitize_student_copy("2月の文法、につきの用法")


def test_meta_does_not_flag_legitimate_month_usage():
    assert not contains_student_meta("2月1日から改定します。")


def test_sanitize_strips_board_sync_and_teacher_confirm():
    raw = "板书同步 timed 老师确认用法二"
    cleaned = sanitize_student_copy(raw)
    assert "板书同步" not in cleaned
    assert "老师确认" not in cleaned
    assert "timed" not in cleaned.lower()
