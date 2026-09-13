# YouTube Course to PPT Implementation Plan

**Goal:** Evolve the existing prototype into a reproducible, evidence-grounded
Video → structured knowledge → PPT pipeline, preserving original screenshots.

**Architecture:** Python owns ingestion, evidence and editorial validation.
A versioned SlideSpec separates analysis from a project-owned PptxGenJS renderer.
Existing v1 code stays usable during explicit migration.

**Tech Stack:** Python 3.11+, uv, Typer, Pydantic, yt-dlp, FFmpeg/ffprobe,
PySceneDetect/Pillow, optional WhisperX, Node ESM/PptxGenJS, pytest.

Updated in place 2026-09-13. The original eight prototype tasks have source
implementations, but completion is not equivalent to acceptance: the initial
20 passed / 3 renderer-runtime failures were fixed by runtime helper discovery;
the current full suite is 42 passed. This is the initial repository publication.
Do not stage existing media/output/unrelated files wholesale.

## Phase 0 — contract and reproducible baseline

### Task 0.1: Establish the v2 interchange contract — implemented, standalone

Files: `src/yt2class/slide_spec.py`, `tests/test_slide_spec.py`,
`docs/schemas/slide-spec.v2.schema.json`, `docs/examples/slide-spec.v2.json`.

Delivered: strict Pydantic model, generated JSON Schema, synthetic example,
reference/range/order checks, contained file/hash validation. It is not wired to
v1 build. Tests first failed for the missing module, then passed after implementation.

Verify: `uv run pytest tests/test_slide_spec.py -q`.
The example demonstrates shape, not actual lesson evidence or a renderable deck.
When modifying models, regenerate the schema with `SlideSpec.model_json_schema()`;
keep the structural schema synchronized and test cross-field rules in Python.

### Task 0.2: Repair selection invariants and clarify degraded behavior — pending

Modify `llm.py`, `lesson_plan.py`, `tests/test_lesson_plan.py`.

1. Add failing cases: max_slides=1 with multiple frames; duplicate selection;
   requested count exceeded; out-of-order frames; unknown ID from outside the
   submitted batch; malformed model response.
2. Run targeted tests and confirm the observed failure.
3. Handle count=1 explicitly. Extend binding validation with catalogue times and
   requested maximum; reject duplicates/unknown references and enforce ordering.
4. Make fallback/require-model policy explicit. Test that missing captions do not
   produce asserted lesson facts or fabricated answers.
5. Run `uv run pytest tests/test_lesson_plan.py tests/test_deck.py -q`.

### Task 0.3: Make cache reuse evidence-aware — pending

Modify `pipeline.py`, `models.py`, `tests/test_pipeline.py`.

1. Test changed selection/config, corrupt PPTX, deleted frame, changed media and
   incomplete manifest; each must invalidate only dependent stages.
2. Persist stage fingerprints/status atomically; validate referenced files and
   PPTX before resume. Include preview request and renderer version.
3. Preserve valid media while retrying analysis/render; give each batch lesson
   an independent result so one failure does not stop later lessons.
4. Run cache/batch tests with fake adapters, without network/model dependencies.

Exit: selection edge cases and stale-cache tests pass; old renderer failures
remain explicitly separate until Phase 2. No change to user media.

## Phase 1 — auditable ingestion

### Task 1.1: Add local-video entry and normalize source metadata — pending

Modify `inputs.py`, `models.py`, `media.py`, `cli.py`, `pipeline.py`;
add `tests/test_local_input.py`; extend `tests/test_media.py`.

1. Write tests for mutually exclusive --links/--video, optional local VTT, MKV,
   filenames with spaces, identical bytes at different paths, and changed bytes.
2. Add discriminated SourceInput and local copy/hash adapter. Use ffprobe for
   streams/duration; store original-source timebase metadata.
3. Make yt-dlp return its actual final path and metadata; test alternate formats
   and partial/zero-byte outputs. Do not infer source.mp4 from command success.
4. Verify tests plus an offline small local-video ingestion fixture.

### Task 1.2: Persist transcript provenance — pending

Modify `subtitles.py`; add `transcript.py`, `tests/test_transcript.py`.

1. Test manual/auto preference, supplied captions, rolling VTT duplicates,
   silence gaps, Unicode, malformed timing, and missing transcript.
2. Emit stable segment IDs, original ranges/text/language/origin and coverage;
   retain raw source subtitle file and hash. Distinguish failure from absence.
3. Verify frame-local lookup and whole-topic windows use the same source clock.

### Task 1.3: Improve candidate coverage — pending

Modify `scenes.py`, `media.py`, `tests/test_scenes.py`.

1. Add fixture tests for transition at a cut, tiny scene, static long lecture,
   repeated board with new speech, small text edit and blurry candidates.
2. Persist scene intervals; sample guarded interior frames and bounded static
   gaps; keep occurrence IDs when deduplicating image bytes.
3. Record quality scores/rejection reasons; make Content/Adaptive profiles
   configurable. Keep candidate catalogue stable for manual review.
4. Measure annotated-topic frame recall before changing defaults.

Exit: YouTube/local ingestion adapters produce equivalent source/transcript/frame
contracts; local fixtures run without model/network; no timestamp drift or
false completed stages. Live YouTube checks are recorded separately.

## Phase 2 — portable deterministic PPT export

### Task 2.1: Bind reviewed selections to v2 — pending

Modify `deck.py`, `pipeline.py`; add `tests/test_v2_binding.py`.

1. Test that LLM/review JSON cannot supply asset paths, hashes or source records.
2. Build v2 only from trusted source/transcript/frame catalogues and checked
   editorial IDs. Reject unsupported summaries/quiz answers on v1 migration.
3. Call SlideSpec validation then validate_assets immediately before rendering.
4. Check every displayed frame and claim appears in source notes/page mapping.

### Task 2.2: Add PptxGenJS backend — pending

Modify `renderer.py`, `pyproject.toml`; add
`src/yt2class/assets/render_deck_pptxgenjs.mjs`, Node `package.json` + lockfile;
add `tests/test_pptxgenjs_renderer.py` and extend `tests/test_renderer.py`.

1. Write integration tests expecting editable title/body text, byte-preserved
   source images, source notes and clickable YouTube timestamp relationships.
2. Add pinned Node dependency and explicit backend selection; retain Artifact
   Tool during comparison. Ensure module resolution also works from a wheel.
3. Port the white/blue 16:9 templates with contain-fit images and consistent
   coordinate conversion. Render every summary/quiz entry; paginate overflow.
4. Test missing assets, unsupported schema, long CJK text, all 8 summary/quiz
   items, image aspect ratios and atomic failure behavior.
5. Inspect OOXML image hashes/text/notes/links; independently render and inspect
   all fixture pages. Record preview-tool absence as unavailable, not passed.

### Task 2.3: Source back-links and portable notes — pending

Add `provenance.py`, `tests/test_provenance.py`; update renderer and source notes.

1. Test YouTube URL variants/existing t params and fractional seconds; canonical
   links floor playback seconds while precise timestamps stay in notes.
2. Add frame/image hyperlinks, claim-level summary/quiz citations and page map.
3. For local input show filename/time/hash without promising portable seek links.

Exit: a local video + supplied captions + reviewed plan exports a visually
accepted PPT on a clean environment without any Codex plugin runtime. Test
installed package, not just source checkout. Only then switch default renderer.

## Phase 3 — structured knowledge and multimodal editing

### Task 3.1: Analyze bounded segments before selecting slides — pending

Add `knowledge.py`, `analysis.py`, `tests/test_analysis.py`; modify `llm.py`.

1. Test that all retained candidates are scheduled even when final count is 1;
   each batch respects image/token caps and overlap IDs merge deterministically.
2. Emit evidence-bound KnowledgeUnits per segment with uncertainty and reasons.
   Store responses/checkpoints without credentials.
3. Add provider capability configuration, bounded retry/backoff and invalid-JSON
   repair. Test timeout/429/invalid output/unsupported vision independently.

### Task 3.2: Select and review lesson narrative — pending

Modify `lesson_plan.py`, `deck.py`, `pipeline.py`, `cli.py`;
add `tests/test_knowledge_selection.py`.

1. Add deterministic tests for topic coverage, repeated board, chronological
   output, low-confidence exclusion and cited summary/quiz answers.
2. Select KnowledgeUnits under final slide budget, then resolve best legible
   frame. Separate editorial JSON from trusted SlideSpec binding.
3. Expose analyze/review/render checkpoints so editing selection does not repeat
   downloads or paid analysis. Preserve --selection-file compatibility explicitly.
4. Run a small real multimodal lesson; manually check every claim against evidence
   and report cost/coverage. Unit tests alone cannot certify semantic accuracy.

Exit: important topics beyond early/evenly spaced frames survive selection;
unsupported assertions are absent in the reviewed corpus and degraded runs are
visible in deck/manifest.

## Phase 4 — optional ASR and reuse experiments

### Task 4.1: Optional WhisperX adapter — pending

Add `transcription.py`, isolated worker/environment instructions,
`tests/test_transcription.py`; update `pyproject.toml` only if a light extra is
needed, keeping heavyweight ASR isolated.

1. Test chunk offsets, missing aligned words, silence and unavailable backend.
2. Implement normalized transcript protocol; add CPU smoke sample on this Mac.
3. Compare sentence timing to manual timestamps before enabling word alignment.
   Diarization remains opt-in and outside baseline acceptance.

### Task 4.2: summarize compatibility spike — pending, adoption conditional

Add optional adapter only after recording a concrete upstream version and its
license. Freeze sample CLI output and test IDs/times/media cleanup/missing fields.
Compare against native ingestion on the same corpus. Adopt only if it reduces
maintenance while preserving our provenance and failure contracts. Do not import
undocumented slide internals from summarize-core.

## Verification and delivery discipline

For each pending task: first write the behavior test, run and observe failure,
implement the smallest change, rerun the targeted suite, then inspect meaningful
integration output. Commit only scoped changes after verification when repository
boundaries are established; never include local source videos or historical runs
by accident. No automatic push or release is part of this plan.

Final acceptance: local source, YouTube, missing captions and model failure each
have a recorded outcome; source/frame hashes and claim references resolve;
rendered pages have no truncated content; links/notes work; unchanged-stage resume
is deterministic; docs distinguish tested features from proposals.

## Verification recorded for this design increment (2026-09-13)

- Full suite after the additive contract and renderer portability work:
  **42 passed**. The three initial Artifact Tool helper-path failures are fixed
  by runtime helper discovery; no test failures remain in the fresh run.
- New contract coverage: 18 passing cases, including schema/model synchronization
  and transcript/chronological validation. Cross-field/reference/file checks are
  Python validation, not claims made on behalf of JSON Schema.
- Draft 2020-12 schema and synthetic example additionally validated using
  `jsonschema` in a temporary uv dependency environment; project dependencies and
  lockfile were not extended for this check.
- CLI help still runs. Existing multi-candidate max_slides=1 division-by-zero
  reproduced directly and listed in Task 0.2; it is not fixed in this increment.
- No live YouTube download, paid model analysis, new PPT export or visual QA was
  performed. PptxGenJS, local ingress and ASR remain pending as marked above.
