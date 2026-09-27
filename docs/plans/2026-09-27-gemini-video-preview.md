# Gemini Video Preview Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Let users compare Gemini 3.8 Flash's direct understanding of public YouTube videos with yt2class's existing evidence-based lesson output, using one URL or a text file of URLs.

**Architecture:** Add an opt-in `gemini-preview` CLI command that calls Google's `generateContent` endpoint directly with a YouTube `file_uri` and a source-faithful Chinese prompt. Reuse the project's YouTube URL parsing, process each link independently, and write one Markdown response plus a machine-readable run record per video. Keep this experimental preview separate from `build-run`; it does not claim to produce a verified PPTX or switch the main analysis provider.

**Tech Stack:** Python 3.11+, Typer, existing `httpx` and `pydantic`, pytest with `httpx.MockTransport`. No new runtime dependency.

## Design decisions

- Accept repeated `--url` values and/or `--links` pointing to one URL per line. A YouTube playlist URL is not a URL list; reject it using `SourceInput`.
- Use `GEMINI_API_KEY` only from the environment. Do not log or persist it. Do not silently fall back to OpenRouter or a paid model.
- Default to `gemini-3.8-flash`, one API request per public video, without automatic retries that could exhaust a free quota. Provide `--prompt-file` for a controlled comparison prompt.
- Preserve returned text verbatim; save the model ID, source URL, timestamp, token usage when present, and the exact prompt in JSON. No claim that model-generated timestamps are verified against source frames.
- Report per-video failures and continue the list, returning a partial-failure exit code. Never overwrite another video's output; use the normalized video ID in file names.
- The Free Tier belongs to a Google AI Studio project and is not technically enforceable by the client; document that a billed key may incur charges and that public-video/usage limits apply.

## Tasks

### Task 1: Input and service contracts

**Files:** Create `src/yt2class/gemini_video.py`; create `tests/unit/test_gemini_video.py`.

1. Write failing tests for single and list URL normalization, playlist rejection, request JSON with `file_uri`, response text extraction, API errors, and output record paths.
2. Run `uv run pytest -q tests/unit/test_gemini_video.py` and confirm expected failures.
3. Implement the smallest service functions using `SourceInput`, `httpx`, and JSON file writing.
4. Re-run the focused tests and refactor only after green.

### Task 2: CLI and user documentation

**Files:** Modify `src/yt2class/cli.py`, `README.md`; create `tests/integration/test_gemini_preview_cli.py`.

1. Write failing CLI tests for `--url`, `--links`, missing key, and partial batch failure.
2. Run the focused integration tests to confirm the red state.
3. Add `gemini-preview` with a lazily constructed HTTP client and helpful output paths; update README with exact commands, free-tier caveats, and scope.
4. Re-run the focused tests, then the full offline suite and static checks.

### Task 3: Live evaluation boundary

**Files:** None unless a live result exposes a defect.

1. Check for `GEMINI_API_KEY` without printing it. If present, run the provided `VGQ6KuiZKKA` example once and inspect the saved output for coverage and timestamps; otherwise report live behavior unverified.
2. Check `git diff --check`, inspect the final diff, and commit the completed feature.
