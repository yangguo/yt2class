# yt2class

Turn course videos into source-faithful PPTX lecture notes: transcript and visual evidence, full-timeline LLM understanding, claim verification, human review, and SlideSpec 3.0 rendering via PptxGenJS.

**Shipped (M0–M6):** versioned contracts, evidence extraction, analysis, editorial/verify/review, bind/render, `build-run` / `batch` / `resume` / `doctor`, and opt-in `native-video` / `hybrid` analysis.  
**M7 (this milestone):** evaluation harness, fixture scorecards, performance/recoverability tests, release checklist — not a claim that annotated live eval gates are met (see [docs/examples/m7-known-limits.md](docs/examples/m7-known-limits.md)).

**Roadmap:** dated live YouTube/provider eval rows, additional vendor adapters — see [implementation plan](docs/plans/2026-08-11-youtube-to-ppt-implementation.md).

```text
ingest → evidence → analyze → plan → verify → review → bind → render
```

Default analysis mode is **frames** (no remote video upload). Hybrid and native-video are **opt-in**.

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

Build the bundled renderer before packaging wheels (handled automatically by the hatch build hook when `npm` is available):

```bash
./scripts/prepare_renderer_bundle.sh
```

## Quick start (M5 product CLI)

Example config (JSON): [docs/examples/course.fixture.json](docs/examples/course.fixture.json).

**Local video + subtitles**

```bash
uv run yt2class build-run \
  --video path/to/lesson.mp4 \
  --subtitles path/to/lesson.vtt \
  --config docs/examples/course.fixture.json \
  --output runs
```

**YouTube URL**

```bash
uv run yt2class build-run \
  --url 'https://www.youtube.com/watch?v=VIDEO_ID' \
  --config docs/examples/course.fixture.json \
  --output runs
```

**Resume after interrupt or budget pause** (exit code `2`)

```bash
uv run yt2class resume --run runs/<run-id>
```

**Batch** (one URL or local path per line; exit code `3` on partial failure)

```bash
uv run yt2class batch --inputs courses.txt --output runs --config docs/examples/course.fixture.json
```

Exit codes: `0` success, `1` failure, `2` review/budget pause, `3` batch partial failure.

### Stage-level commands

Useful for debugging or partial reruns (offline tests use `--provider fake`):

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

Analysis modes: `frames` (default), `native-video`, `hybrid`. Privacy summary: `analysis/media-privacy-audit.json` and review HTML when enabled. See [docs/examples/m6-mode-comparison.json](docs/examples/m6-mode-comparison.json).

## Copyright-safe fixtures

CI and docs use synthetic JSON under `tests/fixtures/contracts/` and helpers in `tests/helpers/`. Do not commit licensed course video, cookies, or API keys. For eval slots see [evals/manifest.yaml](evals/manifest.yaml) and [docs/examples/eval-media-authorization.md](docs/examples/eval-media-authorization.md).

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

`doctor --json` reports ffmpeg, yt-dlp, Node, renderer bundle, fonts, and preview backend without printing secrets.

## Provider configuration

Configure a vision-capable HTTP endpoint (or use `fake` in tests). Keys via environment variables only — never in git.

```bash
export YT2CLASS_MODEL_URL="https://your-endpoint/v1/chat/completions"
export YT2CLASS_MODEL_KEY="..."
export YT2CLASS_MODEL="your-vision-model"
```

Frames mode sends images to the configured provider; native/hybrid may upload capped video clips when explicitly enabled. Review `media-privacy-audit.json` before enabling uploads.

## Legacy prototype: `build --links`

The original YouTube batch prototype remains for compatibility. It does **not** run the full M5 stage DAG or SlideSpec 3.0 product path.

```bash
uv run yt2class build --links links.txt --output output --max-slides 12 --preview
```

Prefer `build-run` / `batch` for new work.

## Tests

```bash
uv run pytest -q
uv run pytest tests/contract/test_eval_contract.py -q
```

Live network tests: `tests/live/` (`@pytest.mark.live`). M5 scenario matrix: [docs/examples/m5-mvp-matrix.md](docs/examples/m5-mvp-matrix.md).

## M7 evaluation

- Manifest: [evals/manifest.yaml](evals/manifest.yaml)
- Scoring: [evals/scoring.py](evals/scoring.py)
- Example scorecard: [evals/reports/scorecard.fixture.json](evals/reports/scorecard.fixture.json)
- Release gate: [docs/release-checklist.md](docs/release-checklist.md)

## Design docs

- [Architecture](docs/plans/2026-08-11-youtube-to-ppt-design.md)
- [Implementation plan](docs/plans/2026-08-11-youtube-to-ppt-implementation.md)
- Draft 2020-12 schemas in `schemas/`
