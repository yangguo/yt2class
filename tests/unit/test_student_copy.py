from __future__ import annotations

from yt2class.stages.student_copy import (
    contains_student_meta,
    normalize_headword_display,
    sanitize_student_copy,
)


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


def test_sanitize_preserves_complete_japanese_sentences_containing_tsuki():
    examples = (
        "本日雨天につき、運動会は来週に延期します。",
        "店内改装中につき、今月は臨時休業いたします。",
        "こちらは限定品につき、お一人様一点までとさせていただきます。",
    )
    for sentence in examples:
        assert sanitize_student_copy(sentence) == sentence


def test_headword_normalization_only_changes_standalone_grammar_label():
    assert normalize_headword_display("につき") == "～につき"
    assert sanitize_student_copy("文法項目：につき") == "文法項目：～につき"
    assert sanitize_student_copy("本日雨天につき、延期します。") == "本日雨天につき、延期します。"


def test_sanitize_removes_analysis_labels_without_erasing_learner_meaning():
    raw = (
        "新知识导入（非复习）：老师宣布这不是复习内容。"
        "表示关于时，多数场合使用「について」。"
        "この使い方は使わないでください。"
    )
    cleaned = sanitize_student_copy(raw)
    assert "新知识导入" not in cleaned
    assert "非复习" not in cleaned
    assert "老师宣布" not in cleaned
    assert "多数场合使用「について」" in cleaned
    assert "使わないでください" in cleaned


def test_sanitize_does_not_leak_negation_analysis_heading():
    raw = "——保留否定「使わないで」；关于含义通常用「について」。"
    cleaned = sanitize_student_copy(raw)
    assert "保留否定" not in cleaned
    assert "关于含义通常用「について」" in cleaned


def test_sanitize_keeps_teaching_conclusion_after_analysis_narration():
    raw = (
        "日本語では「について」が普通です；老师指出这里是关于的用法；"
        "保留否定「使わないで」；通常用「について」"
    )
    cleaned = sanitize_student_copy(raw)
    assert "老师指出" not in cleaned
    assert "保留否定" not in cleaned
    assert "「について」が普通です" in cleaned
    assert "通常用「について」" in cleaned


def test_sanitize_removes_condition_edit_label_but_keeps_condition_and_full_examples():
    raw = (
        "数量限定につき、なくなり次第終了します。（保留条件「なくなり次第」）"
        "本日雨天につき、運動会は来週に延期します。（「来週に延期」）"
        "参加条件を満たした方につき、記念品を配ります。"
        "返品は受け付けません。"
    )
    cleaned = sanitize_student_copy(raw)
    assert "保留条件" not in cleaned
    assert "なくなり次第終了します。" in cleaned
    assert "「なくなり次第」" in cleaned
    assert "本日雨天につき、運動会は来週に延期します。（「来週に延期」）" in cleaned
    assert "参加条件を満たした方につき、記念品を配ります。" in cleaned
    assert "返品は受け付けません。" in cleaned


def test_meta_does_not_flag_legitimate_month_usage():
    assert not contains_student_meta("2月1日から改定します。")


def test_sanitize_strips_automation_ids_but_keeps_proportion_examples():
    raw = (
        "返却期限を過ぎた場合、延滞料金として一日につき300円お支払いいただきます。"
        "cap-0148 occ-0012 d344652e6eddd447.webm @ 04:17 语音与画面对应。"
    )
    cleaned = sanitize_student_copy(raw)
    assert "一日につき300円" in cleaned
    assert "cap-" not in cleaned
    assert "occ-" not in cleaned
    assert "webm" not in cleaned.lower()
    assert "1000円分のお買い物につき10ポイント" == sanitize_student_copy(
        "1000円分のお買い物につき10ポイント"
    )


def test_sanitize_strips_timing_and_empty_board_frame():
    raw = (
        "接续：名詞／数量詞＋につき。"
        "：0.033s 的板书帧（）已写出本课词头「～につき」及接续「名詞／数量詞」，"
        "与 5.78–8.45s 的口头开场白相对应。"
    )
    cleaned = sanitize_student_copy(raw)
    assert "接续" in cleaned
    assert "0.033s" not in cleaned
    assert "8.45s" not in cleaned
    assert "板书帧（）" not in cleaned
    assert "板书帧()" not in cleaned
    import re

    assert re.search(r"\d+(?:\.\d+)?s", cleaned) is None


def test_sanitize_strips_board_sync_and_teacher_confirm():
    raw = "板书同步 timed 老师确认用法二"
    cleaned = sanitize_student_copy(raw)
    assert "板书同步" not in cleaned
    assert "老师确认" not in cleaned
    assert "timed" not in cleaned.lower()
