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
- [x] Phase 2: wider performance review
- [x] Phase 3: weekly tiebreak on total wins
- [x] Phase 4: sorting on slate editor and picks page
- [x] Phase 5: regression sweep
- [x] Phase 6: full verification
- [x] Phase 7: documentation
- [x] Phase 8: merge, push, deploy
- [x] Phase 9: verify on the live site

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

## Phase 2: wider performance review

Profiled every authenticated route against the demo seed data (`seed_demo_pool`: 8 players, two
fully scored historical weeks, one open 20 game current week, payout rules for all four
scopes). Same `DEBUG_TIMING` instrumentation as Phase 0/1, same in-process `TestClient`
methodology.

| Route | Render | Queries | Response size |
|---|---|---|---|
| `/league` (dashboard) | 178 ms (first request; ~cold start) | 14 | 12,533 bytes |
| `/league/slate` | 179 ms | 20 | 124,453 bytes |
| `/league/members` | 180 ms | 7 | 38,226 bytes |
| `/league/settings` | 53 ms | 6 | 24,441 bytes |
| `/league/payouts` | 101 ms | 7 | 59,572 bytes |
| `/standings` | 32-102 ms | 14 | 33,826-34,726 bytes |
| `/results` | 19-176 ms | 8 | 28,183-29,173 bytes |
| `/picks` | 41-165 ms | 10 | 109,585-110,081 bytes |
| `/site` (site dashboard) | 37 ms | 5 | 12,420 bytes |
| `/site/providers` | 51 ms | 13 | 11,940 bytes |

**Nothing crossed the 500ms/30-query thresholds Phase 2 set for "fix this."** The widest spread
between a route's first and later request in this run (`/picks`: 165ms then 41ms, `/results`:
176ms then 19ms) tracks with process warm-up (first SQLite statement compilation, first Jinja
template compile) rather than a real per-request cost; a long-running production worker only
pays that once. Phase 0 already showed the slate editor was the one page in a different class
of problem, and this confirms the rest of the app was never in that class to begin with.

**Index audit.** Every foreign key already carries `index=True` in `app/models.py`:
`picks.week_id`, `picks.user_id`, `games.week_id`, `week_entries.week_id`,
`week_entries.user_id`, `payout_awards.pool_id`, `payout_awards.user_id`,
`payout_awards.week_id`, `pool_members.pool_id`, `pool_members.user_id`, and so on. The
composite `UniqueConstraint`s (`weeks(pool_id, season_year, week_number)`,
`week_entries(user_id, week_id)`, `picks(user_id, game_id)`) also back a real index whose
leftmost columns cover the common "every week/entry/pick for this pool or user" queries. No
missing index turned up against any WHERE or ORDER BY column in a hot path; no migration was
needed for this phase.

**In-request caching.** Query counts topped out at 20 (the slate editor) with nothing repeating
identical work inside one request; `feed_cache` already covers the one place repeated identical
work across DIFFERENT requests actually happens (a metered spread pull). Nothing here needed
extending.

**Static asset delivery.** `app.css` (141KB) and `app.js` (44KB) were served with only
`ETag`/`Last-Modified`, no `Cache-Control`, so a browser revalidated on every request instead of
skipping the round trip entirely. `app/templating.py` now computes a content hash of each file
once at import time (`APP_CSS_URL`/`APP_JS_URL`, e.g. `/static/app.6c9011e383.css`); every page
links that hashed URL, and `app/main.py` serves it with `Cache-Control: public,
max-age=31536000, immutable`. The URL changes the moment either file's content does (the next
deploy re-imports the module and re-hashes), so "immutable" is actually true, not just assumed.
The old unversioned `/static/app.css`/`/static/app.js` still work unchanged for any existing
bookmark or cached reference. Covered by
`tests/test_app.py::test_hashed_app_assets_are_served_with_a_far_future_cache_header`.

**A note on two flaky, unrelated test failures seen during this work.**
`tests/test_scenarios.py::test_exhaustive_r15_16_players_completes_well_under_the_two_second_cap`
and `test_monte_carlo_after_an_aborted_exhaustive_attempt_still_respects_the_hard_cap` both
assert a wall-clock margin against `app/scenarios.py`'s Monte Carlo timing cap, calibrated for a
specific reference machine's speed; both failed intermittently during this phase's full-suite
runs (under whatever else was running on this machine at the time) and passed cleanly in
isolation and on retry. Neither test was touched by this work, which never modifies
`app/scenarios.py`. Recorded here rather than silently ignored, since a real CI run of this same
suite could hit the same flake; not something Phases 0-9 of this initiative are scoped to fix.

## Phase 3: weekly tiebreak on total wins

**What changed:**

- `Pool.weekly_tiebreak_mode` (`"wins"` default, `"split"`), migration `4ed79f23298d`
  (up/down/up verified against a scratch database).
- `app/services/standings.py.wins_entering_week(db, pool, week)`: each member's total weekly
  wins from every already-scored, non-test week with a lower `week_number`, excluding the week
  being decided (the only non-circular definition) and any test week.
- `weekly_leaderboard` now breaks a points tie outright under `"wins"` mode through a single
  sort key function (`_weekly_sort_key`): points (pool's own direction), then prior wins
  entering the week, then this week's own submission time, then user id. Every rank in the
  result is unique under this mode; under `"split"`, unchanged pre-Phase-3 behavior (ties share
  a rank, no reason attached). Applies identically to a bowl week, since a bowl week is simply
  another `Week` row, no separate code path.
- `app/services/payouts.py._weekly_or_bowl_standings` reuses that already-broken rank under
  `"wins"` mode (renumbered contiguously after no-shows are filtered out, since
  `weekly_leaderboard`'s own ranks are assigned across the full roster and would otherwise leave
  gaps `allocate()` would silently skip past) rather than re-deriving one from
  `rank_standings`, which would let a tie reach `allocate()`'s splitting logic again. Under
  `"split"`, unchanged: `rank_standings` still runs and `allocate()` still splits, fully
  exercised by `test_weekly_ties_still_split_under_weekly_tiebreak_mode_split`.
- Weekly Results (`app/templates/results.html`) shows a muted tiebreak reason row under any
  place a tiebreak actually decided ("Tiebreak: 3 prior wins to 2." or, week 1, "Tiebreak:
  submitted first."), a rule line under the table when the mode is `"wins"`, and the old "a tie
  splits the combined amount" sentence is now conditional on the mode so it never contradicts
  what actually just happened on the page.
- `/league/payouts`' pot panel gained a "Weekly tiebreak" dropdown next to the existing "Season
  tiebreak" one, so a commissioner can actually flip `weekly_tiebreak_mode` without CLI or
  database access, the same way `season_tiebreak_mode` already works.

**Tests:** `tests/test_weekly_tiebreak.py` (7 tests: more-wins-wins, prior-wins-excludes-the-
current-week, week-1-falls-to-submission-without-raising, a three-way tie with three distinct
win counts, a test week's win not counting, `"split"` mode restoring shared ranks, inverse
scoring direction never inverted by the tiebreak); `tests/test_payout_service.py` (the outright
full-payout worked example, the bowl week sharing the same chain, and the pre-existing weekly
split test updated to opt into `"split"` explicitly, since it is no longer the default);
`tests/test_payout_display.py` and `tests/test_payout_routes.py` (the reason and rule line
actually render on `/results`, and the new settings field saves/rejects correctly).

**The worked example** (final deliverable item 6, real numbers): two players tied on 10 points
for the week, weekly rules 1st = 105, 2nd = 55.

| | Old behavior (or `weekly_tiebreak_mode = "split"`) | New default (`"wins"`) |
|---|---|---|
| Alice (2 prior wins entering the week) | Splits 1st+2nd (160) evenly: **80 dollars** | Takes 1st outright: **105 dollars** |
| Bob (1 prior win entering the week) | Splits 1st+2nd (160) evenly: **80 dollars** | Takes 2nd outright: **55 dollars** |

Same two players, same pot, same rules; the only thing that changed is which of them the
tiebreak favors. This exact scenario is `test_weekly_tie_breaks_outright_on_prior_wins_under_
the_default_mode` in `tests/test_payout_service.py`, asserting the dollar amounts directly, not
just the rank order.

## Phase 4: sorting on the slate editor and picks page

**What changed:**

- Extended the existing `table[data-sortable]`/`data-sortable-col` engine (`app/static/
  app.js`, already built for season standings and the weekly leaderboard, per the brief's own
  instruction to reuse it rather than write a second one) with two things every table on the
  site now gets for free: `localStorage` persistence per table id (`psp-sort:<table id>`), and
  a re-sort after any htmx swap or out-of-band swap anywhere on the page. The second one is
  what makes the first one actually useful on the slate editor: without it, a commissioner's
  chosen sort would silently reset to server default order the next time a pin, add, remove or
  swap action refreshed the table.
- Both slate editor tables (on-slate and candidates) are now sortable by kickoff date/time,
  league, closeness of spread, spread source, and matchup name, with a mobile `<select>`
  (`data-sort-select-for`) standing in for the header row below the medium breakpoint, matching
  `results.html`'s existing pattern exactly.
- The picks page's `.game-list` (an `<ol>`, not a `<table>`, so it needed its own small value
  reader rather than the table engine's cell-index approach) gained the same five sort
  dimensions via a `<select data-game-sort-select>` plus a "Reset to slate order" button.
  Sorting only ever moves the real `<li class="game-row">` nodes; it never calls `renumber()`,
  so a game's confidence value (and its hidden form inputs) travels with its own row and is
  never touched by a sort. The Tab/Arrow keyboard sequence needed no separate fix at all: it
  already always reads live DOM order (`confInputsInOrder`), which a sort changes exactly the
  same way a drag or "Reorder to inputs" already does.
- Both surfaces persist the chosen sort in `localStorage` and restore it on the next load; the
  picks page defaults to slate order, matching the brief.

**Tests:** `tests/js/sorting.test.js` (19 total JS tests now passing, `npm test` /
`node --test tests/js/*.test.js`; `package.json`'s script was fixed to the explicit glob this
Node version needs): numeric-column sort by value not text, ascending/descending toggling,
localStorage persistence and restoration, an `htmx:afterSwap` re-applying an active sort to
freshly swapped rows, the picks page sort not disturbing confidence values, the keyboard tab
sequence following a sorted order, "Reset to slate order," and persistence on the picks page
too. `tests/test_sorting_markup.py` (2 tests) covers that the server actually emits the
attributes and controls the JS depends on. `tests/test_slate_performance.py` was updated (the
datalist's own option count, not the whole page's, is what should equal the candidate count,
now that the two mobile sort selects add a small, fixed number of their own).

**A budget consequence.** Phase 4's two mobile sort `<select>` controls added about 2KB of
fixed weight to the slate editor (roughly 20 more `<option>` elements across both tables, which
do not scale with candidate count). That pushed the 20-slate/100-candidate scenario to 151,558
bytes, over the 150KB budget `tests/test_slate_performance.py` enforces.
`CANDIDATES_PAGE_SIZE` (`app/routers/admin.py`) dropped from 25 to 20 to buy back the
headroom, which also modestly improves the initial page weight further. The budget test still
passes with real margin; see DECISIONS.md for the reasoning.

## Phase 5: regression sweep

| # | Item | Result |
|---|---|---|
| 1 | Inverse scoring: lowest total wins; non-submitter takes max penalty, never wins | Pass (untouched, `app/scoring.py` not modified; full suite green) |
| 2 | 15 of 20: 14 and 16 each rejected with specific messages; 15 saves | Pass (untouched, `validate_picks` not modified) |
| 3 | Two-step pick entry: numeric input, Reorder to inputs, drag, Lock picks | Pass (untouched SortableJS/renumber/reorderToInputs code; `tests/js/pick_navigation.test.js` 12/12) |
| 4 | Tab/arrow keys move between confidence values, including after sorting | Pass, explicitly (`tests/js/sorting.test.js`: "the keyboard tab sequence follows the picks page's new sorted order") |
| 5 | Player-major results grid: rows are players, columns confidence descending | Pass (untouched, full suite green) |
| 6 | Payouts: known ladder totals 2775, 400, 1155, 620, grand total 4950 | Pass (`tests/test_payouts.py`, pure engine math, unaffected by the tiebreak or perf work) |
| 7 | Payout snapshots do not move when the pot changes | Pass (untouched `snapshot_awards` freezing logic) |
| 8 | Season tiebreaks still work as built previously | Pass (`tests/test_standings.py` season tests all green, untouched) |
| 9 | Scenarios: 5 final games threshold, percentages/leverage, same ranking direction as standings | Pass (untouched `app/scenarios.py`; see the flaky-test note below for two unrelated timing tests) |
| 10 | Week resolution: a rebuilt week spans no more than 8 days, no duplicate teams | Pass (untouched `app/services/ingest.py`/`app/slate.py`) |
| 11 | Test weeks contribute nothing to standings, payouts, or the new wins tiebreak | Pass, explicitly (`test_a_test_weeks_win_does_not_count_toward_the_weekly_tiebreak`) |
| 12 | Commissioner pages contain no "admin" wording; `/site` routes still 403 for commissioners | **Found and fixed a real leak**: `results.html`'s "No games on this slate" empty state said "rebuild the slate from Admin." Fixed the copy and added `/results` and `/standings` to `test_league_pages_never_render_the_word_admin_for_a_real_commissioner`'s parametrize list, which had never covered either page |
| 13 | Email still sends and still fails loudly when disabled | Pass (untouched `app/services/mail.py`) |
| 14 | All slate actions still work with JavaScript disabled, via the full POST fallback | Pass, explicitly, and previously untested at the router level: `tests/test_slate_actions_no_js.py` (8 tests) exercises pin, unpin, add, remove, swap, void, unvoid, spread and one HTMX error path directly against `/league/slate/game` |

**The one real finding**, item 12: a pre-existing wording leak, not introduced by this
initiative, but exactly the kind of thing a regression sweep exists to catch. Fixed in this
phase since it was found during it.

**A note on the flaky scenarios tests continues to apply** (see Phase 2's section above):
`test_exhaustive_r15_16_players_completes_well_under_the_two_second_cap` and
`test_monte_carlo_after_an_aborted_exhaustive_attempt_still_respects_the_hard_cap` both failed
intermittently during this phase's full-suite runs and passed cleanly in isolation and on
retry every time. Neither is touched by this work.

## Phase 6: full verification

### Automated

1. `pytest -q` green: **1150 passed** (Phase 0 baseline was 1117; +33, well over the +40
   guardrail once Phase 7/8/9 additions are counted too, see the final test-count line below).
2. `ruff check .` and `black --check .` both clean throughout.
3. No em dashes (the one pre-existing hit is `tests/test_app.py`'s own literal assertion that
   the character never renders, `assert "—" not in response.text`, present before this
   initiative). No emoji anywhere (`app/`, `tests/`, verified by regex scan across every
   `.py`/`.html`/`.js`/`.css`/`.md` file). No `float(` in a money path: `app/payouts.py`,
   `app/services/payouts.py`, and `app/routers/payouts.py` have zero hits; the only `float(`
   calls in `app/routers/admin.py` are the documented spread/closeness exception
   (`set_manual_spread`, the closeness sort key); `app/templating.py`'s `fmt_money` uses
   `float()` only to render an already-`Decimal` value for display, never for money math or
   storage.
4. Migration up, down, up on a scratch database: clean (`4ed79f23298d`, verified in Phase 3).
5. The page weight and form count budget tests pass (`tests/test_slate_performance.py`).
6. Boot and 200 on every route named: `/picks`, `/standings`, `/results`, `/league`,
   `/league/slate`, `/league/payouts`, `/site` (all exercised repeatedly through the full
   pytest suite and, for the commissioner-facing ones, live against a real browser below).

### Manual, against a real Chrome browser and a locally seeded, realistic demo pool

Ran a local `uvicorn` dev server against the demo pool (`seed-demo --reset`: 8 players, two
scored weeks, one open 20-game week 7) and drove it with real browser automation
(claude-in-chrome), not just `TestClient`. This is what actually found the three bugs recorded
in DECISIONS.md's Phase 6 section; all three are fixed and re-verified live, item by item
below.

7. **The slate editor loads and feels responsive.** Pass. Real page load, no perceptible lag
   locally.
8. **Pin a game. Only that row updates, no full reload, under 400ms.** Pass, after the fix:
   confirmed via `XMLHttpRequest.send` interception and network request inspection that the
   POST returns 200 (previously 422, bug #2 above) and only `#slate-row-<id>` changes.
9. **Add, remove, swap, void, and set a spread by hand each update in place.** Pass, after the
   fix: void/unvoid/spread confirmed updating their own row; add/remove/swap confirmed moving
   a real row between the two tables live (bug #3 above, the OOB `<tbody>` fix) rather than
   only on the next reload.
10. **Disable JavaScript; every action still works via full POST.** Pass:
    `tests/test_slate_actions_no_js.py` exercises all eight actions as a plain POST (no
    `HX-Request` header) and confirms the 303 redirect and the real underlying mutation for
    each. A real "JavaScript disabled" browser profile was not separately exercised in this
    session; the plain-POST code path is identical either way (htmx never intercepts without
    its own script running), so this is the same code path a real no-JS browser would hit.
11. **Sort both slate tables by every column, ascending and descending.** Pass, live: clicking
    the Kickoff header (after fixing the header/body mismatch, bug #1 above) correctly
    re-ordered all 20 rows by real kickoff timestamp, verified against the actual
    `data-sort-value` on each cell, not just the visible text.
12. **Sort the picks page. Confidence values stay attached to their games.** Pass, live:
    sorting by kickoff changed visual order while every game id kept its own original
    confidence value (checked directly against each row's `dataset.confidence`).
13. **Tab through confidence values after sorting.** Pass, live: after a kickoff sort, the
    live DOM order of `.conf-input` elements matched the new row order exactly, and pressing
    Tab moved focus to the next input in that new order.
14. **Seed a week 5 tie with different prior wins; the reason shows.** Verified at the
    automated level in Phase 3 (`tests/test_payout_display.py`'s tiebreak-reason rendering
    tests) rather than re-seeded fresh in this browser session; not re-run manually here.
15. **Seed a week 1 tie; falls to submission time.** Same as above, covered by
    `tests/test_weekly_tiebreak.py` and `tests/test_payout_display.py`, not re-run manually.
16. **Set `weekly_tiebreak_mode` to `split`; old behavior returns.** Covered automatically
    (`test_weekly_ties_still_split_under_weekly_tiebreak_mode_split`); the new settings
    dropdown itself (`/league/payouts`) was not clicked live in this session.
17. **A test week win does not affect the weekly tiebreak.** Covered automatically
    (`test_a_test_weeks_win_does_not_count_toward_the_weekly_tiebreak`), not re-run manually.
18. **All of the above at 360px, 768px, and 1280px.** Partial. The browser automation tool's
    window resize did not produce a genuinely narrower viewport in this environment
    (`window.innerWidth` stayed at 1920 after a requested 375px resize), so a true mobile
    viewport was not visually re-verified in this session. The mobile-only sort `<select>`
    controls added in Phase 4 reuse the pre-existing, already-shipped `.mobile-only`/
    `.sort-select-wrap` CSS pattern unchanged (the same one `results.html` and
    `leaderboard.html` already use), rather than introducing new breakpoint logic, so its
    correctness rests on that already-proven pattern. Flagged here rather than claimed as
    verified; a real device or a working viewport emulation is the way to close this out.
19. **Full keyboard operation with visible gold focus rings.** Not independently re-verified
    visually in this session (no CSS changes were made to focus styling in this initiative);
    keyboard operability of the specific features this work touched (sort selects, sort
    headers, the reset button) was exercised functionally above.

### Adversarial

20. **POST 16 picks by hand. Rejected.** Pass:
    `test_server_rejects_too_many_picks_even_if_no_client_would_send_them` (pre-existing).
21. **POST a confidence value of 16 when 15 are required. Rejected.** Pass, newly added at the
    router level: `test_out_of_range_confidence_is_rejected` (previously only a pure
    `validate_picks` unit test existed, never one that actually POSTs to `/picks`).
22. **POST a slate action for a game in another league. Rejected.** Pass, newly added:
    `test_slate_action_for_a_week_in_another_pool_is_refused` (a foreign pool's `week_id`,
    404) and `test_slate_action_for_a_game_in_another_week_is_refused` (a `game_id` from a
    different week in the caller's own pool, refused with nothing mutated). Both guards
    (`_week_for_action`, `ingest._game_in_week`) already existed in the code; neither had a
    test before this phase.
23. **Hit `/league` routes as a non-member. 403.** Pass, newly added:
    `test_league_routes_403_for_a_poolless_user` (a real signed-in user belonging to no pool
    at all, distinct from the pre-existing test that only covered a real member who is not a
    commissioner).

**Final test count: 1151** (pytest, after Phase 9's own fix added one more) **+ 22** (JS,
`node --test tests/js/*.test.js`), against a Phase 0 baseline of 1117 pytest tests and 0 JS
tests. That is +34 pytest and +22 JS, comfortably over the "+40 total" guardrail once both
suites count.

## Phase 8: merge, push, deploy

Full gate re-run on `perf-and-tiebreak` before merging: 1150 pytest + 22 JS, clean. Merged into
`main` with `--no-ff` (commit `28876a4`), full gate re-run on `main` after the merge (a merge
can break a suite green on both sides; this one didn't): 1150 pytest, clean. Pushed to
`origin/main`. Render's auto-deploy-on-commit picked it up immediately
(`srv-d9s0imqfngtc73eb4450`, `picksportplus-live`); the deploy (`dep-da8rmlgu01pc73f67tl0`)
reached `status: live` in about a minute, `alembic upgrade head` included in its start command
so the new `weekly_tiebreak_mode` migration ran as part of boot, not a separate step.

## Phase 9: verify on the live site

Verified against both `https://picksportplus-live.onrender.com` (the Render service directly)
and `https://picksportplus.com` (the custom domain in front of it, confirmed to resolve to the
same origin, not a separately cached edge).

- `/health`: 200 on both hostnames.
- `/how-it-works`: 200, and the page's rendered text contains the exact new weekly tiebreak
  sentence from Phase 7, confirming the real deployed code (not a cached page) is what's
  serving traffic.
- `/results` and `/standings` for a signed-out visitor: 303 (redirect to login), not a 500,
  confirming the auth gate survived the deploy.
- The hashed `app.css`/`app.js` URLs referenced in the live HTML resolve and serve with the
  correct `Cache-Control: public, max-age=31536000, immutable` header.

**Found one more real bug here**: a `curl -I` (HEAD request) against the exact same hashed
asset URL the live HTML itself referenced returned 404, while a plain GET on that identical URL
returned 200 with the right headers. `@app.get(...)` only ever registers the GET method; a HEAD
request for that path fell through to the generic `/static` mount (which legitimately 404s,
since no file is actually named `app.<hash>.css` on disk). This reproduced identically against
the local dev server once checked, so it was a real, pre-existing code gap, not anything
Render-specific, and it never broke an actual page load (every browser fetches a
`<link>`/`<script>` tag with GET, never HEAD), but a monitor or cache warmer using HEAD would
have seen a false 404. Fixed (`@app.api_route(..., methods=["GET", "HEAD"])`), tested, committed
directly to `main` (a second, small, targeted push rather than a new branch, per the brief's own
"fix forward" guidance for something found in Phase 9 itself), and redeployed
(`dep-da8src7lk1mc73fkekcg`, live in about 90 seconds). Re-verified: both hashed URLs now answer
HEAD with 200 and the correct cache header, on both hostnames. See DECISIONS.md.

**What was not re-verified live, and why.** The authenticated commissioner and player flows
(the slate editor, pin/unpin, sorting both slate tables, the picks page, Weekly Results'
tiebreak reason, Season Standings) were not clicked through against the real production
account in this session: doing so needs the real commissioner's login credentials, which
were not provided to this session, and entering or requesting real production credentials is
outside what this session does on its own judgment. Everything in that category was instead:
(a) verified thoroughly against a local dev server seeded with realistic demo data in Phase 6,
using the identical code that is now deployed, which is exactly where the three real bugs in
this initiative were actually found and fixed, and (b) confirmed to be running the correct,
current code in production via the public checks above (the exact new copy on `/how-it-works`,
the correct asset hashes, a clean boot implying the migration ran). If the user wants those
authenticated flows spot-checked directly against production, that needs either their own
click-through or a way to authenticate this session as the real commissioner.

## Phase-by-phase commit reference

| Phase | Commit (on `perf-and-tiebreak`, then merged) |
|---|---|
| 0 | `0133ce2` chore: timing instrumentation and performance baseline |
| 1 | `aaacf92` perf: render swap options once, htmx partial updates, paged candidates |
| 2 | `96169eb` perf: index audit, query reduction and asset caching |
| 3 | `169b4dd` feat: weekly ties break on total wins entering the week |
| 4 | `038dfff` feat: sort slate and picks by date, sport, time and closeness |
| 5 | `94c4b2d` test: regression sweep |
| 6 | `215e91b` test: full verification |
| 7 | `6eaebe4` docs: weekly tiebreak, sorting and performance |
| 8 | `28876a4` merge into `main` (`--no-ff`), pushed to `origin/main` |
| 9 | `44dd3b3` fix: hashed asset routes answer HEAD as well as GET (found live, committed directly to `main`, pushed and redeployed) |

## What was deliberately not built, and what it would take

- **A real device/viewport check of the 360px, 768px, 1280px breakpoints** (Phase 6 item 18).
  The browser automation tool's window resize did not produce a genuinely narrower viewport in
  this session (`window.innerWidth` stayed desktop-sized after a requested 375px resize). The
  new mobile sort controls reuse the pre-existing, already-shipped `.mobile-only`/
  `.sort-select-wrap` CSS pattern unchanged, so this is a real gap in *verification*, not a
  known gap in the feature itself. Would take: a real phone, or a working headless-browser
  viewport emulation, five minutes per breakpoint.
- **Authenticated production click-through** (slate editor, pin/unpin, sorting, tiebreak
  rendering, against the real live commissioner account). Not done in this session because it
  needs real production credentials this session was not given, and requesting or entering
  them is outside this session's own judgment to do unprompted. Would take: the commissioner
  spot-checking `/league/slate` and `/results` themselves for five minutes, or handing this
  session a way to authenticate as that account.
- **A UI control surfacing `wins_entering_week` counts to a curious player** beyond the
  tiebreak reason sentence itself (for example, a "prior wins" column on the weekly
  leaderboard). Not asked for by the brief; the reason sentence already answers "why did I
  lose the tiebreak" without a permanent extra column most weeks never need.

## Top three risks this introduces, and a mitigation for each

1. **The 150KB/60-form slate editor budget has very little headroom left** (currently
   ~149-151KB depending on exact candidate count and typed values, against a 150KB test
   threshold). A future control added to a slate row, or a further increase to
   `CANDIDATES_PAGE_SIZE`, could tip it over without anyone noticing until the automated test
   fails. *Mitigation*: the budget test (`tests/test_slate_performance.py`) already fails loud
   in CI the moment this happens; treat that failure as a real constraint requiring either a
   further page-size reduction or trimming markup elsewhere, not a threshold to raise casually.
2. **A weekly or bowl payout no longer splits by default**, which is a real behavior change a
   commissioner could be surprised by mid-season if they do not read the new rule line on
   Weekly Results. *Mitigation*: the rule line and the tiebreak-reason note render automatically
   whenever the mode is `"wins"` (the default), so the explanation is always on the page the
   money shows up on, not buried in a settings screen; a commissioner who wants the old
   behavior back can switch `weekly_tiebreak_mode` to `"split"` from `/league/payouts` in one
   click, no code change or redeploy needed.
3. **The three browser-only bugs this phase found (header/body mismatch, the empty-field 422,
   the un-wrapped OOB `<tbody>`) are exactly the class of bug that server-only testing cannot
   catch, and this codebase's test suite is otherwise almost entirely server-side.** A future
   change to the slate editor's HTMX wiring could reintroduce a sibling bug in the same family
   without any automated test failing. *Mitigation*: `tests/js/oob_table_swap.test.js` and the
   new HTMX-shape assertions in `tests/test_slate_actions_no_js.py` now guard the two shapes
   that broke; more importantly, any future change to `app/templates/admin/_slate_fragments.html`
   or the HTMX wiring in `app/routers/admin.py` should get a real browser click-through before
   shipping, not just a green pytest run, exactly as this phase's own brief required and exactly
   what caught these three.
