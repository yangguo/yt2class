# Gemini Video and Playlist Analysis Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Integrate direct Gemini analysis for one YouTube video, URL lists, and YouTube playlists into the yt2class CLI.

**Architecture:** Keep preview analysis separate from the existing source-faithful PPTX pipeline. Expand playlist metadata with yt-dlp without downloading media, analyze each resulting video separately, and persist per-video Markdown/JSON plus an incrementally updated batch manifest.

**Tech Stack:** Python 3.11+, Typer, httpx, python-dotenv, yt-dlp subprocess, pytest MockTransport.

### Task 1: Input expansion and Gemini API adapter

**Files:**
- Create: `src/yt2class/gemini_video.py`
- Test: `tests/unit/test_gemini_video.py`

Test playlist expansion order, duplicate removal, subprocess errors, API request construction, key redaction, and persisted provenance.

### Task 2: CLI batch execution

**Files:**
- Modify: `src/yt2class/cli.py`
- Create: `tests/integration/test_gemini_preview_cli.py`

Add `gemini-preview` with repeatable `--url`, `--links`, `--prompt-file`, `--model`, partial-failure continuation, and incremental batch manifest writes.

### Task 3: User documentation and verification

**Files:**
- Modify: `README.md`
- Modify: `pyproject.toml`
- Modify: `uv.lock`

Document `.env`, direct video and playlist examples, output files, yt-dlp prerequisite, and experimental-output limitations. Run focused tests and the project test suite.

### Configuration follow-up: provider-specific models

Load the current working directory's `.env` once from the Typer root callback with `override=False`; this makes the existing image-provider settings available to every CLI command while preserving shell-exported values. Keep model selection provider-specific because the image/frame path and direct YouTube-video path use different provider APIs:

- `gemini-preview`: `--model` overrides `GEMINI_MODEL`, which falls back to `gemini-3.8-flash`.
- Legacy `build --links`: keep using `YT2CLASS_MODEL` with its existing endpoint/key settings.
- Product frame analysis: keep the provider's current environment variables and explicit `analysis.model` configuration.

Provide `.env.sample` with provider-neutral image endpoint/key/model settings and document the selection order. Verify `.env` model selection for the product frame path, Gemini video path, and legacy image path, including the CLI override.
