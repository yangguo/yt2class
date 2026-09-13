# M2 live / opt-in longitudinal sample

This harness is **opt-in**. Ordinary `uv run pytest -q` does not run it.

The licensed 5–10 minute local course used for a real-model longitudinal
sample is **not** committed. Do not add course media, API keys, cookies, or
raw model responses as gold fixtures.

## How to run

1. Place a licensed local video and a sidecar caption file outside the repo
   (for example `~/yt2class-samples/lesson.mp4` and `lesson.zh.vtt`).
2. Copy `tests/fixtures/live/annotations.placeholder.json` and fill in the
   human-annotated topics, examples, procedure steps, and timestamps.
3. Export provider credentials in the environment. The harness never writes
   those values into the repository.
4. Run:

```bash
YT2CLASS_LIVE_SAMPLE=/absolute/path/to/lesson.mp4 \
YT2CLASS_LIVE_CAPTIONS=/absolute/path/to/lesson.zh.vtt \
YT2CLASS_LIVE_ANNOTATIONS=/absolute/path/to/annotations.json \
uv run pytest tests/live -m live -q
```

## What to keep

Save only de-identified provider/model/prompt versions, token usage, coverage
ledgers, and intermediate JSON with evidence IDs. Human review notes become
regression fixtures. Raw model completions are not gold answers.
