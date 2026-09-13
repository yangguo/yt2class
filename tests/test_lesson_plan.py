import pytest
from pathlib import Path

from yt2class import lesson_plan, llm, scenes, subtitles


def test_subtitles_for_timestamp_returns_nearby_cue():
    assert hasattr(subtitles, "parse_vtt")
    assert hasattr(subtitles, "nearby_text")

    cues = subtitles.parse_vtt(
        """WEBVTT

00:00:10.000 --> 00:00:14.000
これは例です。
"""
    )

    assert "例" in subtitles.nearby_text(cues, 12.0)


def test_lesson_plan_rejects_frame_not_in_candidates():
    assert hasattr(lesson_plan, "validate_plan")
    assert hasattr(lesson_plan, "PlanValidationError")

    with pytest.raises(lesson_plan.PlanValidationError, match="unknown frame"):
        lesson_plan.validate_plan(
            {
                "title": "辞書形",
                "slides": [
                    {
                        "frame_id": "missing",
                        "kind": "grammar",
                        "title": "重点",
                        "explanation_zh": "解释",
                    }
                ],
                "summary": ["总结"],
                "quiz": [{"prompt": "填空", "answer": "答案"}],
            },
            {"known"},
        )


def test_parse_model_json_accepts_markdown_fenced_json():
    assert hasattr(llm, "parse_model_json")

    assert llm.parse_model_json("```json\n{\"title\": \"辞書形\"}\n```") == {
        "title": "辞書形"
    }


def test_offline_plan_stays_bound_to_known_frames(tmp_path: Path):
    assert hasattr(llm, "offline_plan")
    frame = scenes.FrameCandidate("known", 12.0, tmp_path / "frame.jpg")

    plan = llm.offline_plan("辞書形", [frame], [])

    assert plan.slides[0].frame_id == "known"
    assert plan.quiz


def test_deepseek_anthropic_compat_endpoint_uses_openai_protocol(monkeypatch):
    assert hasattr(llm, "ModelConfig")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "test-token")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://api.deepseek.com")
    monkeypatch.setenv("ANTHROPIC_MODEL", "deepseek-v4-flash[1M]")
    monkeypatch.delenv("YT2CLASS_MODEL_URL", raising=False)
    monkeypatch.delenv("YT2CLASS_MODEL_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    config = llm.ModelConfig.from_environment()

    assert config is not None
    assert config.provider == "openai"
    assert config.endpoint == "https://api.deepseek.com/v1/chat/completions"
    assert config.model == "deepseek-v4-flash"


def test_select_plan_with_mode_falls_back_when_model_rejects_images(tmp_path, monkeypatch):
    assert hasattr(llm, "select_plan_with_mode")
    frame = scenes.FrameCandidate("known", 12.0, tmp_path / "frame.jpg")
    config = llm.ModelConfig(
        endpoint="https://model.invalid/v1/chat/completions",
        api_key="test-token",
        model="vision-model",
    )

    def reject_images(*args, **kwargs):
        raise llm.ModelError("vision unsupported")

    monkeypatch.setattr(llm, "_call_model", reject_images)
    decision = llm.select_plan_with_mode("辞書形", [frame], [], config=config)

    assert decision.mode == "offline-fallback"
    assert decision.plan.slides[0].frame_id == "known"
    assert "vision unsupported" in (decision.note or "")


def test_deepseek_text_only_endpoint_is_skipped_for_frame_selection(tmp_path, monkeypatch):
    frame = scenes.FrameCandidate("known", 12.0, tmp_path / "frame.jpg")
    config = llm.ModelConfig(
        endpoint="https://api.deepseek.com/v1/chat/completions",
        api_key="test-token",
        model="deepseek-v4-flash",
    )

    def should_not_call(*args, **kwargs):
        raise AssertionError("text-only model should not receive image payloads")

    monkeypatch.setattr(llm, "_call_model", should_not_call)
    decision = llm.select_plan_with_mode("辞書形", [frame], [], config=config)

    assert decision.mode == "offline-fallback"
    assert "text-only" in (decision.note or "")
