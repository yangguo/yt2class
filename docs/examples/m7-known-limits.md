# M7 known limits (quantified)

Explicit gaps versus design targets in `docs/plans/2026-08-11-youtube-to-ppt-design.md` §15.
CI proves contracts and fixture behavior; semantic and visual gates need dated live runs.

| Design target | Shipped / CI status | Limit |
| --- | --- | --- |
| ≥90% must-surface knowledge when page budget allows | Not measured on annotated set | **Unverified** — use `evals/manifest.yaml` + local media |
| 0 serious factual errors on human eval set | No committed gold labels | **Unverified** — dual adjudication schema only |
| ≥95% screenshot readability | Fixture projection in `evals/comparison.py` | **Not human-rated** |
| 100% evidence closure on final slides | Contract tests + bind tests | **Verified in CI** for fixture paths |
| 100% core timeline accounted | M2/M5 integration with fake provider | **Verified offline**; live long-course dated separately |
| Resume after corrupt analysis cache | `tests/integration/test_resume.py`, `test_m7_recoverability.py` (segment manifest) | **Verified offline** for listed cases |
| HTTP 429 retry | `tests/unit/test_runtime_policy.py`, `test_m7_recoverability.py` (`wrap_provider` + `call_with_retry`) | **Not** wired on fake MVP provider path (retry policy disabled when `provider=fake`) |
| Truncated PPTX cache | `validate_pptx_package`, `test_render_cache_validation.py`, `test_m7_recoverability.py` | **Verified** on delivery validator stack |
| Wall-clock / cost vs realtime video | `evals/performance.py` linear **estimates** | **Not a speed guarantee** |
| Hybrid default | **Opt-in** (`analysis.mode` default `frames`) | Real vendor upload not on shipped CLI |
| Live YouTube / vendor adapters | OpenRouter and Ark Plan adapters plus dated `@pytest.mark.live` runs | **Excluded** from default `pytest`; full gold/visual/human gates remain open |
| Root `LICENSE` file | Not present in repository | **Add before public release**; verify notices manually |
| GitHub Actions / CI workflow | `.github/workflows/ci.yml` runs `uv run pytest -q` on PRs and `master` | **Offline gate only**; live provider calls remain opt-in |

Update this table when live eval scorecards land under `evals/reports/` with `generated_at` dates.
