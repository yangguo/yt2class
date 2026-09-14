# Release checklist (M7 gate)

Use this before tagging a release. Do not claim design §15 quality targets without dated live evidence.

## Automated tests

- [ ] `uv run pytest -q` green (unit + contract + integration; excludes `@pytest.mark.live` by default)
- [ ] `uv run pytest tests/contract/test_renderer_packaging.py -q` passes (wheel + bundled Node renderer from clean target dir via `uv pip`)
- [ ] `uv run pytest tests/contract/test_eval_contract.py -q` passes (M7 manifest/scoring contracts)

## Live / manual (date-stamp results in release notes)

- [ ] Optional `uv run pytest -m live` with network credentials — record run date and commit SHA
- [ ] At least one full PPTX visual spot-check (layout, notes, embedded images, sources index)
- [ ] Doctor on a clean machine: `uv run yt2class doctor --json`

## Packaging

- [ ] Build wheel: `uv run python -m hatchling build` (renderer bundle prepared via hatch hook / `scripts/prepare_renderer_bundle.sh`)
- [ ] Install wheel in empty venv/dir and confirm `renderer_root()` resolves bundled `pptxgenjs`
- [ ] Node renderer: `npm ci` in bundled renderer root if testing from sdist checkout

## Compliance inventory (no fake claims)

- [ ] Run `uv run python scripts/dependency_inventory.py` and attach output to release notes
- [ ] Verify `LICENSE` and third-party notices (npm + Python lock/resolution) match shipped artifacts
- [ ] Confirm no secrets, cookies, or copyrighted eval media in the git tag

## Version alignment

- [ ] Remote branch, release tag, and CI workflow run share the **same commit SHA**
- [ ] Release notes separate **shipped** (M0–M6 + M7 harness) vs **roadmap** (live annotated evals, vendor adapters)

## Known limits

Document quantified gaps in [docs/examples/m7-known-limits.md](examples/m7-known-limits.md). Update when live scorecards land in `evals/reports/`.

## Release notes template

```markdown
## Shipped in vX.Y.Z
- M5 CLI: build-run, batch, resume, doctor
- M6 analysis modes: frames (default), native-video, hybrid (opt-in)
- M7: eval manifest, scoring contracts, fixture scorecards, release checklist

## Verified in CI (commit SHA, date)
- pytest summary: …
- packaging smoke: …

## Live / manual (date, operator)
- PPTX visual check: …
- YouTube/provider smoke: …

## Roadmap / not verified
- Annotated ≥90% knowledge coverage on licensed set
- …
```
