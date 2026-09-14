# yt2class

Turn course videos into source-faithful PPTX lecture notes: transcript and visual evidence, full-timeline LLM understanding, claim verification, human review, and SlideSpec 3.0 rendering via PptxGenJS.

**Shipped (M0–M6):** versioned contracts, evidence extraction, analysis, editorial/verify/review, bind/render, `build-run` / `batch` / `resume` / `doctor`, and opt-in `native-video` / `hybrid` analysis modes (with **fake** transports in CI).  
**M7:** evaluation harness, fixture scorecards, release checklist — annotated live quality gates are **not** claimed met ([docs/examples/m7-known-limits.md](docs/examples/m7-known-limits.md)).

**Roadmap:** wire real HTTP vision providers on the M5 path, dated live evals, vendor adapters — see [implementation plan](docs/plans/2026-08-11-youtube-to-ppt-implementation.md).

```text
ingest → evidence → analyze → plan → verify → review → bind → render
```

Default analysis mode is **frames**. Hybrid and native-video are **opt-in**; offline CLI uses `FakeProvider` / `FakeNativeVideoBackend` unless you run `@pytest.mark.live` tests.

## Requirements

- Python 3.11+
- [uv](https://github.com/astral-sh/uv)
- Node.js (PptxGenJS renderer)
- `ffmpeg`, `ffprobe`
- `yt-dlp` (for YouTube ingest)

```bash
uv sync
uv run yt2class doctor
```

Build the bundled renderer before packaging wheels (hatch build hook or `scripts/prepare_renderer_bundle.sh`).

## Quick start (M5 product CLI)

The product path (`build-run`, `batch`, `resume`, and stage commands below) currently wires **`analysis.provider: fake` only**. Requests for any other provider fail closed with `PipelineError` until real adapters are integrated on this path. Use `tests/live/` for opt-in real-model runs.

Example config: [docs/examples/course.fixture.json](docs/examples/course.fixture.json) (keep `"provider": "fake"` for offline/CI).

**Local video + subtitles**

```bash
uv run yt2class build-run \
  --video path/to/lesson.mp4 \
  --subtitles path/to/lesson.vtt \
  --config docs/examples/course.fixture.json \
  --output runs
```

**YouTube URL** (needs `yt-dlp`; still uses fake analysis unless you extend the pipeline)

```bash
uv run yt2class build-run \
  --url 'https://www.youtube.com/watch?v=VIDEO_ID' \
  --config docs/examples/course.fixture.json \
  --output runs
```

**Resume** after interrupt or budget pause (exit code `2`)

```bash
uv run yt2class resume --run runs/<run-id>
```

**Batch** (exit code `3` on partial failure)

```bash
uv run yt2class batch --inputs courses.txt --output runs --config docs/examples/course.fixture.json
```

Exit codes: `0` success, `1` failure, `2` review/budget pause, `3` batch partial failure.

### Stage-level commands (fake provider)

```bash
uv run yt2class analyze \
  --evidence runs/<run-id>/evidence/evidence-bundle.json \
  --output runs/<run-id>/analysis \
  --provider fake \
  --mode frames

uv run yt2class plan --knowledge ... --transcript ... --visual ... --output ... --provider fake
uv run yt2class verify --knowledge ... --plan ... --transcript ... --visual ... --output ... --mode draft --provider fake
uv run yt2class review --knowledge ... --plan ... --report ... --transcript ... --visual ... --output ... --provider fake

uv run yt2class render --run runs/<run-id>
```

**Analysis modes:** `frames` (default), `native-video`, `hybrid`. In the shipped CLI, native/hybrid use the **fake** native video backend when `provider` is `fake`; no real vendor upload occurs. A `media-privacy-audit.json` is still written for mode auditing. Fixture-only mode comparison numbers: [docs/examples/m6-mode-comparison.json](docs/examples/m6-mode-comparison.json) (not measured hybrid gain).

## Copyright-safe fixtures

Synthetic JSON under `tests/fixtures/contracts/` and helpers in `tests/helpers/`. No licensed video, cookies, or API keys in git. Eval slots: [evals/manifest.yaml](evals/manifest.yaml), [docs/examples/eval-media-authorization.md](docs/examples/eval-media-authorization.md).

## Run layout

```text
runs/<run-id>/
  manifest.json
  media/
  evidence/
  analysis/
  editorial/
  delivery/lesson.pptx
  delivery/slide-spec.v3.json
  previews/
```

`doctor --json` checks tools and renderer bundle without printing secrets.

## Legacy prototype: `build --links`

The original YouTube batch prototype uses a separate code path (`llm.py`) and **does not** run the M5 stage DAG or SlideSpec 3.0 product pipeline.

Optional vision HTTP for that legacy path only (not `build-run`):

```bash
export YT2CLASS_MODEL_URL="https://your-endpoint/v1/chat/completions"
export YT2CLASS_MODEL_KEY="..."
export YT2CLASS_MODEL="your-vision-model"
```

Also supported on the legacy path: `OPENAI_API_KEY` (optional `OPENAI_BASE_URL`) and Anthropic-compatible variables. DeepSeek routes are treated as text-only with offline fallback.

```bash
uv run yt2class build --links links.txt --output output --max-slides 12 --preview
```

Prefer `build-run` / `batch` for new work.

## Tests

```bash
uv run pytest -q
uv run pytest tests/contract/test_eval_contract.py -q
```

Live: `tests/live/` (`@pytest.mark.live`). M5 matrix: [docs/examples/m5-mvp-matrix.md](docs/examples/m5-mvp-matrix.md). Recoverability coverage is split across `tests/integration/test_resume.py`, `tests/integration/test_render_cache_validation.py`, `tests/unit/test_runtime_policy.py`, and incremental checks in `tests/integration/test_m7_recoverability.py`.

## M7 evaluation

- Manifest: [evals/manifest.yaml](evals/manifest.yaml) (10 short segments + 1 long-course slot)
- Scoring: [evals/scoring.py](evals/scoring.py) — `evals/comparison.py` scores are **fixture projections**
- Example scorecard: [evals/reports/scorecard.fixture.json](evals/reports/scorecard.fixture.json)
- Release gate: [docs/release-checklist.md](docs/release-checklist.md)

## Design docs

- [Architecture](docs/plans/2026-08-11-youtube-to-ppt-design.md)
- [Implementation plan](docs/plans/2026-08-11-youtube-to-ppt-implementation.md)
- Draft 2020-12 schemas in `schemas/`
