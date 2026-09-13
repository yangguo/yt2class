# YouTube Course to PPT Design

Updated: 2026-09-13. This updates the original design in place. “Current” means
inspected local code; “target” is planned unless explicitly marked implemented.
The project remains a Python local batch CLI for Chinese-speaking learners,
initially focused on Japanese lessons. No web UI, accounts or cloud storage.

## 1. Current architecture assessment

The useful foundation is already present: original frames remain separate from
model-authored text, Python owns media processing, and Node owns slide layout.
Keep that separation and evolve it into Video → structured knowledge → PPT.

| Area | Inspected implementation | Gap / consequence |
| --- | --- | --- |
| Package | `pyproject.toml`, uv lock, Python 3.11+, Typer, Pydantic, Pillow, PySceneDetect | No project-owned Node package/lock; external tools are not version-recorded |
| Input | `inputs.py`, `cli.py`: YouTube links file only | Local MP4/MKV files in the repository are not a supported CLI input |
| Media | `media.py`: yt-dlp video/subtitle download, separate title request, FFmpeg JPEG scaled to width 1280 | No ffprobe manifest, ASR, audio extraction or robust actual-output-path contract; MP4 fallback assumptions need tests |
| Transcript | `subtitles.py`: VTT parsing, ja/zh preference, ±6 second context | No transcript artifact/IDs, coverage metric, manual-vs-auto provenance or long-range topic context |
| Frames | `scenes.py`: ContentDetector threshold 10, scene starts + 90 second static fill; black/uniform filter; Pillow 8×8 average-hash global dedup | Not AdaptiveDetector, not OpenCV pHash. Cut frames may be transitions; a recurring board can be discarded globally; no blur metric |
| LLM | `llm.py`: individual JPEGs encoded as base64, nearby captions, strict LessonPlan; optional reviewed JSON and fallback | No contact sheet in model request. Candidates are evenly reduced to max_slides BEFORE model analysis, so the model cannot recover omitted important material |
| Contract | `lesson_plan.py` + unversioned dict in `deck.py` | Known frame check exists; duplicate selections, order, requested count and claim-level grounding are not enforced |
| Render | `renderer.py`, `assets/render_deck.mjs`: Artifact Tool, editable text and original image bytes | Hard-coded per-user runtime path; no PptxGenJS package. Summary silently slices to 3; quiz text shares one fixed box |
| Provenance | Frame timestamps/hashes, manifest and source notes | Hash is recorded but not rechecked before embedding; no video hash or claim/caption links; displayed timestamp is not a clickable hyperlink |
| Resume | `_resume_result` trusts PPTX existence + readable manifest | Config/selection changes and corrupted assets can reuse stale results; no independent stage checkpoints |
| Batch | `build_batch` list comprehension | First failure aborts remaining inputs; only some exceptions receive stage wrappers |

At the start of this design increment on 2026-09-13, `uv run pytest -q` gave
**20 passed, 3 failed**. All three failures originated from the old hard-coded
Artifact Tool setup path. The renderer now discovers the helper from the
runtime cache or accepts `YT2CLASS_ARTIFACT_SETUP`; a fresh full run is
**42 passed**. Existing `test-output/` PPTX/previews are historical artifacts,
not proof of a new end-to-end lesson run. Before this initial publication, the
repository had no commits; media and historical output remain intentionally
untracked.

Additional directly reproducible edge case: `_choose_candidates(candidates,
max_slides=1)` divides by zero when more than one candidate exists. This and
validation/resume gaps belong in the first runtime-hardening phase.

## 2. Architecture decision

**Recommend incremental Python orchestration + versioned evidence contract +
PptxGenJS adapter.** It reuses the project's working media/model boundaries and
removes the machine-specific renderer dependency without a language rewrite.

Alternatives considered:

- Wrap summarize CLI as the whole ingestion backend: faster access to its
  extraction modes, but introduces Node runtime/version/output-contract coupling
  and still needs our evidence binding, lesson editing and PPT layer.
- Rewrite around summarize-core in TypeScript: reasonable for a new Node app,
  but wastes this Python implementation; core's published content/prompt entry
  points do not establish a complete scene/keyframe extraction API.

```text
YouTube URL | local video (+ optional sidecar VTT)
  → SourceManifest + ffprobe metadata + immutable media
  → TranscriptDocument + SceneIntervals + FrameCatalogue
  → multimodal segment analysis → KnowledgeUnits
  → chapter/coverage selection → reviewed LessonPlan v2 (IDs + text only)
  → trusted binder + SlideSpec v2 + asset/hash validation
  → PptxGenJS renderer → PPTX → independent preview/QA
```

LLM output is an editorial proposal, not a filesystem manifest. Only the trusted
binder supplies source records, assets, paths, hashes and timestamps. Caption
and OCR content are evidence data, never executable instructions.

Target run artifacts (additive to existing paths):

```text
media/source.<ext>, media/source.info.json, media/audio.wav (if ASR needed)
analysis/source.json, transcript.json, scenes.json, frames.json
analysis/segments/*.json, knowledge.json, selection.json
analysis/slide-spec.v2.json, source-notes.txt, previews/
lesson.pptx, manifest.json
```

The current `analysis/deck.json` remains v1 until the explicit adapter migration.
Use input/config/tool-version hashes for stage cache keys, plus upstream artifact
hashes. Commit each stage via temporary files + atomic rename. Record running,
complete, failed and degraded independently. Changing rendering options must
not redownload media; changing selection must invalidate binding/rendering.

## 3. Ingestion: YouTube, local media and transcript

Introduce `SourceInput` with a discriminant (`youtube` / `local`). Preserve
`build --links`; add mutually exclusive `--video` and optional `--subtitles`.
Local input is copied into the run directory without overwriting the original;
stream a SHA-256 and derive local identity from content, not pathname. Canonical
YouTube video ID drives URL identity; media bytes identify the downloaded version.
Do not normalize away evidence of different actual downloads.

Use yt-dlp with `--no-playlist`, write metadata and obtain its reported final
media path. Preserve a usable downloaded MKV/WebM when MP4 remuxing is not
possible; FFmpeg can consume it. Validate actual streams/duration with ffprobe.
Record download URL, video ID, duration, codecs, tool versions and file hash.
Use argument arrays, bounded timeouts, classified retries and atomic completion;
partial downloads never qualify for a completed source stage.

Transcript preference: supplied sidecar → preferred-language manual captions →
auto captions → optional ASR → explicitly missing. Store original VTT alongside
normalized segments `{id, start_seconds, end_seconds, text, language, origin}`.
Normalize rolling-caption repetitions without removing genuinely repeated speech;
measure coverage and retain uncertainty. Do not relabel subtitle text as ASR.
Timestamps use seconds on the original media timeline, including any audio
extraction offset. Clip/chunk ASR timestamps must be offset back before binding.

ASR extracts audio only when needed (e.g. FFmpeg mono 16 kHz PCM). Define a
`Transcriber` adapter returning the same transcript document. WhisperX is an
optional isolated environment/process: sentence timestamps suffice for MVP;
word alignment and speaker diarization are optional enhancements. Keep model
loads out of the core CLI environment. Record model/alignment versions and
language; missing aligned words remain unaligned, not fabricated timestamps.
No-subtitle mode can still make a clearly labelled visual review deck; it must
not invent lesson explanations or answers.

## 4. Scene and keyframe strategy

Retain PySceneDetect. Choose detector by video profile and measured results:
ContentDetector is a reasonable baseline for slide changes; AdaptiveDetector is
an alternative for moving-camera footage, not universally better for screencasts.
Persist scene intervals rather than just cut timestamps. For each interval,
sample 20/50/80 percent after an edge guard; very short scenes use a midpoint.
For long static scenes also sample at bounded gaps and transcript topic changes.
These proportions/gaps are initial tunables, not demonstrated optimal values.

Filter black/uniform frames, estimate sharpness, and keep quality scores and
rejection reasons. Use local perceptual similarity to cluster candidates; do not
remove every later occurrence of an earlier slide. An identical board paired
with a new explanation is a separate evidence occurrence, even if image bytes
are shared. OCR/text-region changes can protect small but meaningful edits.
Do not use a hash threshold alone as proof of semantic equivalence.

Each retained frame records stable ID, source hash, requested time, actual frame
PTS when available, scene ID, path, JPEG hash, dimensions and quality scores.
Current JPEGs are resized source-derived images, not byte-identical decoded
source frames. Target extraction records that transformation; slide rendering
must preserve the extracted image bytes without model repainting/cropping.

## 5. Multimodal analysis and screenshot selection

Separate **analysis candidate budget** from **final learning slide count**.
Analyze all retained candidates across bounded batches; never globally trim to
12 frames before understanding a long lesson. Batch by scene/topic with overlap,
initially at most 8 images plus bounded transcript context; make image/token/cost
limits configurable and persist usage. Large batches split, rather than silently
truncate unseen parts of the lesson.

Pass each image next to its frame ID/time and relevant timed transcript, with
optional labelled contact sheets for overview. Detailed board text must remain
available as individual images. The segment result (`KnowledgeUnit`) contains:

- unit ID, start/end, topic and lesson role;
- evidence-bound claims, frame/transcript IDs, uncertainty flags;
- candidate frame IDs with selection/rejection reasons;
- novelty, legibility and instructional importance scores (editorial signals).

A second editor selects units under slide/time budgets, rewards topic coverage
and readable screenshots, penalizes repetition, then orders them chronologically.
Use a deterministic coverage heuristic first; do not introduce a vector database
for MVP. Ground every explanation, summary item and quiz answer. A citation
proves traceability, not that the statement is true; semantic review must still
check whether the quoted passage/image supports the claim.

Validate returned IDs against the exact submitted catalogue and enforce counts,
uniqueness and order. Retry invalid JSON once with concise validation errors;
a persistent provider/validation failure is explicit degraded output or a
`require-model` failure. Provider capabilities must be configuration/probe based,
not permanently inferred from a hostname. Reviewed selection JSON follows the
same validation path and contains no paths or image URLs.

## 6. SlideSpec JSON contract — foundation implemented

`src/yt2class/slide_spec.py` is the authoritative Pydantic model for **2.0**.
The generated schema is `docs/schemas/slide-spec.v2.schema.json`; the synthetic
example is `docs/examples/slide-spec.v2.json`. This model is independently usable
but is **not yet connected to build/renderer**. No v1 output is silently upgraded.

| Field | Ownership / meaning |
| --- | --- |
| schema_version | Literal `2.0`; reject unknown major/version |
| source | Binder-owned local/YouTube kind, title, relative media path, SHA-256, duration, optional URL |
| assets | Binder-owned frame IDs, run-relative paths, hashes and timestamps |
| evidence | Frame occurrence or transcript excerpt with interval; transcript origin distinguishes caption/ASR |
| slides | 1–30 chronological learning slides; title, kind, asset ID, 1–4 grounded text points |
| summary | 0–8 grounded statements; empty permitted when evidence is insufficient |
| quiz | 0–8 questions with grounded answers; render answers into speaker notes |
| analysis_mode | model / reviewed / offline / offline-fallback |

All objects forbid extra fields. Structural limits are not layout guarantees.
Pydantic enforces unique IDs, finite/nonnegative times, interval order/duration,
valid reference joins, display-frame citations and chronological slides.
`validate_assets(spec, run_root)` checks resolved path containment (including
symlinks), file existence, and media/frame SHA-256 before use. Neither validates
image decoding, actual capture timestamps or semantic truth; add those checks at
ingestion and QA. JSON Schema alone cannot enforce cross-record joins, range
comparisons, filesystem state or model-generated text truth.

Titles are editorial labels derived from each slide's cited points; reviewers
must reject unsupported titles. `source.title` is metadata, not a grounded claim.
Run manifest owns model/prompt/tool versions and degraded reasons, outside the
portable render contract. Cover/summary/quiz pages are deterministic renderer
compositions, so max_slides means learning pages, not total output pages.

V1 migration must bind old frame IDs using the actual frame catalogue and add
real provenance. Existing free-text summaries/answers with no supporting IDs
require review or omission; never manufacture citations merely to pass v2.

## 7. PptxGenJS renderer target

Keep Python's `render_deck` boundary, introduce explicit `pptxgenjs` backend and
retain Artifact Tool temporarily for comparison. Add a project-owned Node ESM
package, pinned PptxGenJS dependency and committed lockfile. Installed Python
wheel must include renderer assets; document reproducible Node dependency setup.
Avoid copying a renderer into a temp directory that cannot resolve its package.

Renderer receives only bound/validated SlideSpec plus run root and output path;
it does not call a model, download assets, select frames or alter content.
Use 16:9, white background, blue accents and original-frame-first layout from
the existing deck. Map old 1280×720 coordinates to inches consistently. Use
`imageSizingContain`, editable `addText`, `addNotes`, and native hyperlinks.
Frames use full rectangular bounds; rounding must not hide source-edge text.

Template limits: title 2 lines, body up to 4 short points, sensible minimum
font size. Split overflow into continuation pages or return layout errors; never
silently slice summary items or shrink text indefinitely. All summary/quiz
items must appear, paginated as necessary. Page count may exceed learning count
when continuation pages are needed; record the final page-to-evidence mapping.

PptxGenJS writes PPTX; it is not the PNG preview engine. Use a separate optional
LibreOffice/PDF rasterization path, or another configured renderer, and report
preview availability honestly. Verify OOXML package, all expected text/notes,
image byte hashes and hyperlink relationships. Inspect Japanese/Chinese fonts,
long text, image containment and the resulting montage in an actual renderer.
Atomic output promotion prevents a failed export from replacing a good deck.

## 8. Provenance and timestamp back-links

Use one timeline in original-source seconds. Python constructs canonical YouTube
links `https://www.youtube.com/watch?v=<id>&t=<floor(seconds)>s`; preserve precise
fractional time in notes/manifest. Add clickable timestamp text and image link.
Summary and quiz notes list each cited evidence interval rather than just the
course homepage. File paths/hashes never come from LLM output.

Local media does not have a portable web seek URL. MVP shows relative filename
and precise time; notes include source hash/path. Do not advertise file:// links
as reliably seeking in PowerPoint. A future bundled HTML viewer can offer
relative-media seeking when distributed with the video. The PPT itself embeds
screenshots so it remains usable when separated from the source video.

Provenance chain: source bytes → frame/transcript occurrence → knowledge claim
→ slide point → exported page/notes. Persist model name, prompt hash, adapter
version, timestamps, selection reasons and run warnings in manifest. Never log
API keys or credential-bearing request headers.

## 9. Reuse decisions and verified references

Checked upstream documentation 2026-09-13; pin concrete versions/commits at
integration time. Previous conversation star counts are not selection criteria.

- **summarize:** reference caption-first routing, timed frame/text association
  and resumable extraction; optionally prototype CLI adapter against frozen JSON
  fixtures. MIT project. Its npm CLI currently requires Node 24+. Published
  core imports are `/content` and `/prompts`; do not depend on private slide
  internals without a verified public contract. Keep adoption optional until
  output/provenance parity is tested.
  [README](https://github.com/steipete/summarize),
  [core README](https://github.com/steipete/summarize/blob/main/packages/core/README.md),
  [slides guide](https://github.com/steipete/summarize/blob/main/docs/slides.md).
- **PySceneDetect:** directly reuse the installed library's interval detection;
  compare Content/Adaptive profiles on project fixtures. It detects cuts, not
  instructional importance. Verify API against the locked version before using
  examples from latest documentation.
  [Detectors](https://www.scenedetect.com/docs/latest/api/detectors.html).
- **WhisperX:** optional ASR/alignment process, especially useful if sentence
  timing proves insufficient. Its documentation describes CPU execution and
  alignment/diarization limitations; do not assume a CUDA setup suits this Mac.
  Diarization has additional model access requirements and is outside initial MVP.
  [WhisperX](https://github.com/m-bain/whisperX).
- **PptxGenJS:** target deterministic OOXML export; its public types include
  image sizing, notes and hyperlink options. Preview/visual QA remains separate.
  [Public API types](https://github.com/gitbrent/PptxGenJS/blob/master/types/index.d.ts).

## 10. Acceptance and rollout

See the existing [implementation plan](2026-08-11-youtube-to-ppt-implementation.md)
for files, task ordering and verification. Promote defaults only after local
video + supplied captions + reviewed selection yields a verified PPT without
Codex runtime dependencies. Then test YouTube ingestion, missing-caption ASR,
and bounded real multimodal analysis independently.

Use a small corpus: static slide lecture, animated board, software demo, talking
head with few visual teaching points, and a no-caption lesson. Manually annotate
key topics and suitable frame windows before tuning thresholds. Report topic
coverage, unsupported claims, legibility, runtime/cost and degraded stages.
Require zero invented/missing evidence links, no silently dropped content, and
visual acceptance of every exported page. Do not equate passing unit tests with
semantic accuracy or a usable end-to-end product.
