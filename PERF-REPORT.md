# Performance and weekly tiebreak report

Branch `perf-and-tiebreak`. This report is built up phase by phase; see the checklist below for
status and the tables further down for before/after numbers.

## Methodology note

All numbers in this report come from `TestClient` (FastAPI's in-process test client) against an
in-memory SQLite database with a synthetic but realistic week: 20 games on the slate (8 NFL, 12
college, matching the pool defaults) and 100 more candidate games, built by
`tests/../baseline.py` (a one-off measurement script, not part of the app or the pytest suite).
This isolates server-side render time and query count from network latency and real Postgres
round trips, which is the right isolation for finding and fixing the page-weight and N+1
problems Phases 0-2 are about. DOM node counts are a regex approximation (`<tag ` / `<tag>` open
tags in the rendered HTML), not a real browser DOM parse; it is consistent before and after, so
the relative change is trustworthy even if the absolute count is not exactly what a browser's
`document.querySelectorAll('*').length` would report. `X-Render-Time-Ms` and `X-Query-Count`
response headers come from the `DEBUG_TIMING` instrumentation added in this phase
(`app/main.py`), off by default and never enabled in production. Phase 9 supplements this with
real measurements against the live production site.

## Phase checklist

- [x] Phase 0: timing instrumentation and performance baseline
- [ ] Phase 1: slate editor page weight
- [ ] Phase 2: wider performance review
- [ ] Phase 3: weekly tiebreak on total wins
- [ ] Phase 4: sorting on slate editor and picks page
- [ ] Phase 5: regression sweep
- [ ] Phase 6: full verification
- [ ] Phase 7: documentation
- [ ] Phase 8: merge, push, deploy
- [ ] Phase 9: verify on the live site

## Phase 0 baseline (20 on-slate games, 100 candidates)

| Page | Response size | Server render | Query count | DOM nodes (approx) | Form count | Option count |
|---|---|---|---|---|---|---|
| Slate editor (`/league/slate`) | 518,028 bytes | 185-213 ms | 17 | ~6,048 | 405 | 820 |
| Picks (`/picks`) | 106,649 bytes | 89-194 ms | 10 | ~1,247 | 2 | 0 |
| Results (`/results`) | 29,120 bytes | 96-148 ms | 6 | ~425 | 1 | 0 |
| Pin action round trip (POST + full page reload, current behavior) | 518,057 bytes | 72 ms (in-process; excludes real network/browser paint) | n/a | n/a | n/a | n/a |

This confirms the diagnosis: the slate editor's 518KB response is in the same range as the
515,337 bytes measured against the live site, and the 405 forms / 820 options land almost
exactly where the diagnosis in Phase 1's brief predicts (roughly 20 rows times a ~40-option swap
dropdown, plus pin/remove/swap/void/spread forms per row). Picks and results are not in the same
class of problem: this is slate-specific, not systemic, so Phase 2's wider review is about
individual N+1s and index gaps on other routes, not a second copy of Phase 1's fix.
