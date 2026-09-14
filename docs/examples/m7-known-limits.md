# M7 known limits (quantified)

These are explicit gaps versus design targets in `docs/plans/2026-08-11-youtube-to-ppt-design.md` §15.
CI proves contracts and fixture behavior; semantic and visual gates need dated live runs.

| Design target | Shipped / CI status | Limit |
| --- | --- | --- |
| ≥90% must-surface knowledge when page budget allows | Not measured on annotated set | **Unverified** — use `evals/manifest.yaml` + local media |
| 0 serious factual errors on human eval set | No committed gold labels | **Unverified** — dual adjudication schema only |
| ≥95% screenshot readability | Fixture projection in `evals/comparison.py` | **Not human-rated** |
| 100% evidence closure on final slides | Contract tests + bind tests | **Verified in CI** for fixture paths |
| 100% core timeline accounted | M2/M5 integration with fake provider | **Verified offline**; live long-course dated separately |
| Resume after interrupt | Integration tests (`test_resume.py`, cache invalidation) | **Verified offline** for listed fault classes |
| Wall-clock / cost vs realtime video | `evals/performance.py` linear **estimates** | **Not a speed guarantee** — coefficients are fixture-only |
| Hybrid default | Still **opt-in** (`analysis.mode` default `frames`) | Native upload requires explicit config |
| Live YouTube / vendor adapters | `@pytest.mark.live` only | **Excluded** from default `pytest` |

Update this table when live eval scorecards land under `evals/reports/` with `generated_at` dates.
