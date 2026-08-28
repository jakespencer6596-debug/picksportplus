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
- [x] Phase 1: slate editor page weight
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

## Phase 1: slate editor page weight

**What changed** (`app/routers/admin.py`, `app/templates/admin/slate.html`, new
`app/templates/admin/_slate_fragments.html`, new `tests/test_slate_performance.py`):

1. The swap "for" list is now a single shared `<datalist id="swap-candidates">`, rendered once
   for the whole page and covering every real candidate (not the old top-40 cap), referenced by
   every on-slate row's swap `<input list="swap-candidates">`. This is one of the two approaches
   the brief names directly, chosen over an HTMX-fetched-on-open control because a `<datalist>`
   needs no JavaScript at all to work, so it satisfies "keep a no-JavaScript fallback" for the
   swap control specifically, not just the rest of the row's actions.
2. Every row collapsed to exactly one `<form>` (previously up to three: pin/void/spread, swap,
   remove). Each button inside carries its own `hx-target`/`hx-swap`, which htmx honors per
   element regardless of any other button in the same form, so pin/void/spread partial-swap just
   their own row while add/remove/swap (which change which table a game belongs to) rely on
   out-of-band fragments in the same response. A no-JavaScript browser ignores every `hx-*`
   attribute and submits the form's own `method`/`action` normally, landing on the unchanged
   flash-and-redirect full page load. This consolidation is also most of what makes the 60-form
   budget achievable: 20 on-slate rows, one form each, cost 20 of it.
3. The candidates table now loads its first 25 rows only (`CANDIDATES_PAGE_SIZE`); a search box
   (`GET /league/slate/candidates`, debounced 300ms) and a "Load more" button fetch further pages
   over HTMX, both returning small HTML fragments, never a page reload. The datalist above still
   covers every candidate regardless of pagination state.
4. Query count was already flat at 17 for this route in Phase 0 (one query for the week's games,
   a handful more for pick counts and slate span), so no N+1 fix was needed here specifically;
   Phase 2 covers the rest of the app.
5. `tests/test_slate_performance.py` asserts the budget directly against a synthetic 20 game, 100
   candidate week and fails loud if either number regresses, plus a second test proving a pin
   action's HTMX response never carries the two full-table refreshes only add/remove/swap need.

**Before and after** (same 20 on-slate / 100 candidate scenario as the Phase 0 baseline):

| Metric | Before | After | Target | Met |
|---|---|---|---|---|
| Response size | 518,028 bytes | 149,448 bytes | < 150,000 bytes | Yes |
| Form count | 405 | 51 | <= 60 | Yes |
| Option count | 820 | 100 | < 200 | Yes |
| DOM nodes (approx) | ~6,048 | ~1,848 | n/a | - |
| Server render | 185-213 ms | ~142 ms | < 300 ms | Yes |
| Query count | 17 | 17 (unchanged, already flat) | constant vs. candidate count | Yes |
| Pin action, full POST fallback (no JS) | 518,057 bytes, full reload | 149,477 bytes, full reload | n/a (fallback path) | - |
| Pin action, HTMX partial swap | n/a (did not exist) | 3,695 bytes, 16 ms in-process | < 400ms, no full reload | Yes |
| Load 25 more candidates, HTMX | n/a | 57,662 bytes, 16 ms in-process | HTMX request, not a reload | Yes |

The response size target was the tightest one: 149,448 bytes leaves under 1KB of headroom
against the 150KB budget at exactly 20 on-slate rows and 100 candidates. A pool that raises
`num_games_per_week` well past 20, or a commissioner who leaves every on-slate row's swap box
focused with a long typed value, could push a specific render over the line; the automated test
in `tests/test_slate_performance.py` is what will catch that the moment it happens rather than
letting it drift back to 500KB unnoticed.
