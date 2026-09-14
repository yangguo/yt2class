# M5 MVP end-to-end matrix

Offline CI covers fixture-backed rows. Live network rows stay under `tests/live` with `@pytest.mark.live`.

| # | Scenario | Offline command / test | Live |
| --- | --- | --- | --- |
| 1 | Local video + manual subtitles + vision LLM | `tests/integration/test_resume.py` (fake provider) | `yt2class build-run --video lesson.mp4 --subtitles lesson.vtt --config course.yaml` |
| 2 | Local video, ASR fallback | `tests/integration/test_evidence_bundle.py` | `yt2class build-run --video lesson.mp4 --config course.yaml` |
| 3 | YouTube + manual subtitles | fixture ingest tests | `yt2class build-run --url 'https://www.youtube.com/watch?v=VIDEO' --config course.yaml` |
| 4 | YouTube auto subtitles only | — | same as row 3 without `--subtitles` |
| 5 | Provider timeout / budget pause | `tests/unit/test_runtime_policy.py` | re-run with `yt2class resume --run runs/<id>` |
| 6 | No model, evidence-only | `quality.mode: evidence-only` in config | — |
| 7 | Human review → single-page re-render | `tests/integration/test_cache_invalidation.py` | `yt2class review --apply edits.json --run runs/<run-id>` (bumps revision + M4 bind/render), then `yt2class resume --run runs/<run-id>` |

Exit codes: `0` success, `1` failure, `2` review/budget pause, `3` batch partial failure.
