# PR #15 Lesson Quality Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Improve source-faithful ～につき lesson output so supported cause examples survive selection, summary and learner text are concise, and source times remain accurate after pedagogical reordering.

**Architecture:** Preserve the existing `KnowledgeDocument → EditorialPlan → SlideSpec → PptxGenJS` pipeline. Add evidence-aware example aggregation to the existing editorial stage, constrain learner-visible copy at a single output boundary, derive summary text from sense roles rather than long claims, and let the renderer use each page's own evidence time.

**Tech Stack:** Python 3.11+, Pydantic, pytest, Node ESM, PptxGenJS, uv, npm.

**Spec:** [PR #15 ～につき讲义质量补强设计](../../plans/2026-09-24-pr15-lesson-quality-design.md)

## Global Constraints

- Base PR #15 head: `6009e584aa928ed19cb02996f31935822d04059f`; stay isolated from the `master` checkout.
- Preserve original frames and their hashes; do not generate replacement images.
- Never add an example absent from input claims and valid source evidence; the user reference is an acceptance target, not a production fixture to hardcode.
- Retain one 接续 page, at most two 用法一 pages, one 用法二 page with three rate kinds when present, one 用法三 page, and the current page budget.
- Keep `PageIntent.body_points` at at most four entries of at most 200 characters and `claim_ids` within the existing schema limits, or explicitly update schema and its contracts before changing those limits.
- Keep negation, Japanese sentence text, quantities, conditions, source links, and quality labels intact.
- CI validation is offline; real Ark/YouTube acceptance and visual review must be reported separately.

## Review Focus

1. Four supported cause examples across separate topics: all four appear within two cause pages without displacing rate/about, including a seven-page boundary fixture. Seven is a test input, not a universal total-page cap. Task 2 owns this test.
2. One Japanese claim plus Chinese translation of the same example: no duplicate page or split orphan bullet. Task 2 owns this test.
3. Unsupported target example or truncated ASR: never synthesize its missing ending; retain an omission/review trail. Task 2 owns this test.
4. Long semicolon-separated 用法三 analysis: summary remains short while preserving preference for 「について」. Task 3 owns this test.
5. Pedagogical page order with nonmonotonic source times and no timestamp: content footers use own evidence, summary has no false single time. Task 4 owns this test.

---

### Task 1: Preserve Japanese examples and remove analysis prose

**Files:** Modify `src/yt2class/stages/student_copy.py`; test `tests/unit/test_student_copy.py` and `tests/unit/test_plan13_learner_quality.py`.

**Interfaces:** `normalize_headword_display(text: str) -> str` changes only standalone headword contexts; `sanitize_student_copy(text: str) -> str` preserves source-language sentences while removing internal analysis scaffolding. Do not change its public signature.

- [ ] Write tests that first fail for `本日雨天につき、運動会は来週に延期します。` and `店内改装中につき、今月は臨時休業いたします。`: output must equal the input, while standalone `につき` may become `～につき`.
- [ ] Write tests that first fail for the PDF-like strings `新知识导入（非复习）：老师宣布…` and `——保留否定「使わないで」`: student copy omits the diagnostic narration yet preserves the grammatical conclusion “通常用「について」” and genuine negation.
- [ ] Run only those tests to observe the intended failures; then implement contextual headword normalization and bounded, semantic learner-copy cleanup. Do not delete all occurrences of `老师` or `使わないで` blindly.
- [ ] Run the focused tests, `tests/unit/test_plan13_learner_quality.py`, and `git diff --check`. Commit the isolated change once green.

### Task 2: Fill two cause pages with supported, distinct examples

**Files:** Modify `src/yt2class/stages/edit_deck.py`; possibly `src/yt2class/stages/grammar_sense.py` for role classification; test `tests/unit/test_plan11_connective_cause_cap.py`, `tests/unit/test_plan14_rate_snippets.py`, and a new `tests/unit/test_plan15_cause_coverage.py`.

**Interfaces:** Input remains `KnowledgeDocument`, `CourseMap`, `VisualCatalogue`, and `TranscriptDocument`; output remains `EditorialPlan`. Reuse `PageCandidate`, `PageIntent`, `KnowledgeClaim`, and existing `Omission`. Add a private helper for source-backed distinct cause examples rather than a VGQ6/video-ID branch. A selected example contributes its real `claim_id` and source text to one cause page.

- [ ] Create a fixture with distinct supported examples for 店内改装／工事中／雨天／限定品 in separate `KnowledgeUnit`s, each with evidence IDs; add a fifth duplicate translation, a rate example, and a target example with no source evidence. The four supported examples are synthetic test data and must not be copied into production rules.
- [ ] Assert two or fewer 用法一 pages display the four supported Japanese sentences (plus available translations) with `target_pages=max_pages=7` as one compact-course boundary; each sentence remains one logical bullet, all displayed examples retain their real `claim_id`s, rate/about pages remain, and this fixture stays within its requested budget. Assert unsupported text does not appear and omissions explain capacity/evidence gaps. Run and observe failure on PR #15 baseline.
- [ ] Adjust example selection and grouping so page caps count pages, not individual examples. Preserve the mandatory-sense locks and `max_pages`. Prefer source-backed, distinct examples; group up to two examples per cause page, and keep evidence IDs and claim IDs synchronized. If fewer than four are supported, output only those with source evidence.
- [ ] Run the new test, existing plan11–plan14 tests, and binder contract/integration tests. Inspect generated `PageIntent` to ensure no visible bullet exists without a matching claim. Commit once green.

### Task 3: Produce concise sense comparison summary

**Files:** Modify `src/yt2class/stages/edit_deck.py` and, if needed, `src/yt2class/stages/grammar_sense.py`; test `tests/unit/test_plan15_summary_copy.py` and `tests/integration/test_pptx_renderer.py`.

**Interfaces:** `_summary_page(...) -> PageIntent` and `_polish_learner_plan(...) -> EditorialPlan` retain signatures. Summary `body_points` must be three short role statements when all three supported senses exist; `claim_ids` remain real cited claims. `bind_spec._bind_summary` must receive matching counts so it cannot fall back to raw claim text.

- [ ] Add a failing test with the PDF-like semicolon-separated 用法三 paragraph. Assert summary has one short line per supported sense, no `对比点` / `保留否定` / `老师指出`, contains “通常用「について」”, and does not repeat a long content-page sentence. Include a missing-sense variant that does not claim full three-sense coverage.
- [ ] Replace raw-claim summary copying with a bounded, sense-aware explanation built from supported roles and evidence. Keep the existing rate unit examples in the main 用法二 page; the summary should compare meaning, not duplicate all three rate snippets. Ensure `body_points` and `claim_ids` align by index in binder.
- [ ] Run focused tests and a bound SlideSpec test to confirm visible summary bullets are exactly the compact body points. Commit once green.

### Task 4: Correct source time after pedagogical reordering

**Files:** Modify `renderer/src/render.mjs` and `renderer/src/layouts/links.mjs` if necessary; test `tests/unit/test_renderer_links.py` and an integration render test.

**Interfaces:** `primarySeekSeconds(page, assets, evidenceByAsset, evidenceById)` should return a valid page-evidence timestamp or `null`, and `buildSeekLink(...)` should produce no link for an unanchored summary. Do not infer content timestamps from previous pages. Keep valid YouTube links and local `来源 mm:ss` labels.

- [ ] Add a failing Node-backed test with teaching-order content at 257s followed by content at 99s: expected links 04:17 and 01:39, respectively. Add a summary with multiple claims but no single anchor; assert no `04:17` footer is shown merely because it followed the first page. Add a page with no valid evidence and expect no hyperlink.
- [ ] Remove `deckSeekSeconds` as a content-time floor. Define the summary policy explicitly (omit a single-time footer unless a dedicated anchor exists), and use each content page’s own frame/claim evidence. Preserve original URLs and parseable timestamps.
- [ ] Run Node link tests and `tests/integration/test_pptx_renderer.py`; inspect PPTX package XML for footer/link destinations. Commit once green.

### Task 5: Cross-stage regression and review

**Files:** Test `tests/integration/test_pptx_renderer.py`, `tests/contract/test_domain_contracts.py`, and relevant plan11–plan15 tests. Update `docs/examples/m7-known-limits.md` only if a verified limitation changes; keep PR #15 draft until real acceptance.

- [ ] Run `uv run pytest -q`, `npm ci --prefix renderer`, and `git diff --check` on the final branch; record exact counts.
- [ ] Build a PPTX from the synthetic regression fixture. Inspect SlideSpec text/citation mapping and convert PPTX to PDF or preview images; check pages for orphan bullets, overflow, frame fidelity, repeated summary text, and misleading source times.
- [ ] Compare this output with the user-supplied `lesson.pdf` as a failure example. If the historical intermediate artifacts are absent, state that true VGQ6/Ark reproduction is unverified; do not claim the supplied PDF was regenerated.
- [ ] Review the full PR diff against the global constraints, push the PR #15 follow-up branch or update PR #15 only after local verification, and inspect remote CI for the exact final SHA. Do not merge while PR remains draft or until live acceptance is explicitly closed.

### Task 6: Preserve original screenshot aspect ratio (2026-09-25 follow-up)

**Files:** Modify `renderer/src/layouts/common.mjs` and renderer dependencies only if image dimension parsing requires one; test with `tests/integration/test_pptx_renderer.py` or a focused renderer package test.

- [ ] Reproduce the new six-page PDF's image distortion with a 16:9 source in the image-text box: its PPTX shape currently has the 4.2:3.8 box ratio. Assert the exported shape or PDF placement keeps the source's aspect ratio within a small tolerance.
- [ ] Compute a centered fit rectangle from intrinsic image width/height for cover, image-text, comparison, and sequence layouts. Keep original image bytes, provenance, hyperlinks, and the surrounding layout box unchanged.
- [ ] Render PPTX and export PDF locally; inspect the image-text pages and verify no unintended crop, stretch, overlap, or footer change. Run the full offline suite.

### Task 7: Preserve four supported cause examples in one topic (2026-09-25 follow-up)

**Files:** Modify `src/yt2class/stages/edit_deck.py` and a focused editor test only after checking the new run's source artifacts if available.

- [ ] Add a failing fixture with four distinct supported cause examples in the same `CourseMap` topic, plus a separate rate and about sense. Give enough configurable page budget for a second cause page; do not treat the six-page PDF as a cap.
- [ ] Reconcile `_example_keep`'s per-topic two-example limit with the existing four-example cause capacity. Keep deduplication, real claim/evidence IDs, two examples per readable cause page, and an omission for missing or unsupported claims.
- [ ] Test the full editorial and bind path; do not synthesize the two missing sentences from this document. Compare against the actual new run when its artifacts are available.
