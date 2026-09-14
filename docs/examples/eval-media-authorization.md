# Evaluation media authorization (private)

The repository does **not** store licensed course videos, YouTube cookies, or API keys.

To run live M7 eval rows:

1. Obtain media you are allowed to use (self-produced, purchased license, or explicit permission).
2. Keep files outside git; point `evals/manifest.yaml` `media_slot: local-only` entries at your paths in a private notes file.
3. Record for each asset: title, source URL (if any), license, acquisition date, and retention policy.
4. Run adjudication with two raters (or one rater plus independent re-check) and save scorecards under `evals/reports/` locally or in your artifact store.
5. Optional `@pytest.mark.live` smoke tests live under `tests/live/`; date-stamp results in `docs/release-checklist.md`.

Synthetic CI uses JSON contract fixtures only; see `evals/manifest.yaml`.
