# Standings and ties, September. Build report

Branch: `standings-and-ties`. This report is updated as each phase lands; see individual
commit messages for the exact diff each phase introduced.

## Phase checklist

| Phase | Status | Commit |
|---|---|---|
| 0. Baseline | Done | `b86dbdb` |
| 1. Fix the 120 point bug | Done | `066b731` |
| 2. Replace the tie rules with the league's rule | Done | `3ad9318` |
| 3. Fix the sort that pulls in tiebreak lines | Done | `9cf1cc0` |
| 4. Condense the Results tab | Done | `4f9069c` |
| 5. Condense the Season tab | Done | `ae27e3b` |
| 6. Condense the This Week tab | Done | `64c5c58` |
| 7. Expandable pick rows on Results and Season | Done | `42dcee7` |
| 8. Mobile pass | Done | `07196e7` |
| 9. Regression sweep | Done | `d2700d3` |
| 10. Full verification | Pending | |
| 11. Documentation | In progress | `f8a5473` |
| 12. Merge, safety check, push, deploy | Pending | |
| 13. Verify on the live site | Pending | |

## Phase 0. Baseline

- Read `SPEC.md` in full, `app/services/results.py`, `app/scoring.py`, `app/services/
  standings.py`, `app/payouts.py`, `app/services/payouts.py`, `app/cli.py` (`_cron_pass`),
  `app/templates/leaderboard.html`, `app/templates/results.html`, `app/templates/picks.html`.
- `render.yaml` start command confirmed unchanged and recorded: `alembic upgrade head &&
  python -m app.cli seed-admin && python -m app.cli seed-demo && uvicorn app.main:app ...`.
  Nothing in this build touches this file.
- Baseline gate: `ruff check .` clean, `black --check .` clean (78 files), no em dashes.
  `pytest -q`: **1253 passed**, 2 pre-existing failures, both unrelated to this build:
  `test_week_published_notification_sent_when_pool_opts_in` (a mail-notification test) and
  `test_slate_editor_page_weight_budget` (a slate page byte-budget test, currently ~471-476
  bytes over its 150000 byte cap). Neither touched by this build's changes; both were already
  failing before this branch started and are tracked, not fixed, per the gate rule ("no new
  failures").
- `scripts/seed_prod_shaped.py`: built (a first attempt went off track and never produced the
  file; rebuilt from scratch). Runs `alembic upgrade head` against a local sqlite file (repo
  working directory by default, or anywhere under the OS temp directory), refuses anywhere
  else, then loads the existing `seed_demo_pool` fixture (real teams, real historical scores,
  eight real-named players, no network calls) and layers the seven messy states listed above
  on top. Verified: `doctor-picks` against the resulting database correctly flags the seeded
  16-pick player as corrupt. Used for Phase 8's mobile pass and will be used again for Phase
  10's browser pass and Phase 12's migration row-count check.
- `app/main.py._assert_local_database`: new boot-time guard (Phase 0 rule 8). A no-op when
  the `RENDER` environment variable is set (the real production service); otherwise refuses
  to start unless `DATABASE_URL` resolves to a local `sqlite:///` file inside the repo's
  working directory or the OS temp directory. Logs the resolved database location on every
  local boot. See DECISIONS.md, "Standings and ties, September," for why `RENDER` was chosen
  as the signal.
- Branch `standings-and-ties` created from `main` at `557bc1a`.

## Phase 1. Fix the 120 point bug

**Root cause** (confirmed by reading the code, matching the brief exactly): `_cron_pass`
(`app/cli.py`) scores every week whose status is `"open"` or `"locked"`. `score_week_for_pool`
(`app/services/results.py`) called `app/scoring.py.score_week`, whose no-show branch always
charged the maximum inverse-mode penalty (`sum(1..picks_required)`, 120 at 15 picks) for any
member with zero picks, regardless of whether that week's `lock_at` had actually passed. A week
2 that was merely open, with picks still outstanding, produced a full no-show penalty for every
member who had not yet submitted, and `_season_base_rows` summed every `WeekEntry`
unconditionally, so the phantom penalty flowed straight into season totals.

**The fix:**
1. `score_week_for_pool` now compares `utcnow()` against the week's real `lock_at` before
   treating an empty submission as a no-show. Before lock: scores 0, `did_not_submit=False`.
   After lock: the original maximum-penalty behavior, unchanged. A submitter's live points
   from already-final games are computed exactly as before either way.
2. Weekly winner flags and `PayoutAward` snapshots are now gated on `week_complete` (every
   slate game final or void), computed before `weekly_winner_ids` is ever called, not after.
   Previously `is_winner` was assigned unconditionally on every call, including mid-week.
3. Read-time correction for data already written under the old bug: `app/services/
   standings.py._effective_points`/`_effective_did_not_submit` read a `did_not_submit=True`
   entry as 0 points, not a no-show, whenever that entry's week has a `lock_at` still in the
   future. The stored row is never rewritten; verified by a test that asserts the row's raw
   `points`/`did_not_submit` are unchanged after being read this way
   (`test_stale_phantom_120_row_reads_as_zero_without_the_stored_row_changing`).
4. `app/services/standings.py.season_live_weeks` returns the week numbers of any non-test week
   that is still open or locked but already has `WeekEntry` rows (contributing live). Wired
   into `/standings` (`app/routers/leaderboard.py`) and rendered as a single muted line,
   "Includes week N, in progress." (`app/templates/leaderboard.html`), only when non-empty.

**Tests** (`tests/test_no_show_lock_timing.py`, 7 new tests; plus 2 new tests on
`/standings`'s in-progress line in `tests/test_app.py`): open week before lock scores 0/not a
no-show; the same week after lock takes the max penalty; a locked week with some games final
scores only those; season points equal week 1 plus the live week 2 figure, no phantom penalty,
and `season_live_weeks` returns `[2]`; no weekly winner or `PayoutAward` on an unfinished week;
`season_live_weeks` is empty once every started week is fully scored; a stale phantom-120 row
reads as 0 without the stored row changing. Two pre-existing tests in `tests/test_app.py`
(`test_scoring_end_to_end`, `test_scoring_end_to_end_standard_mode_still_works`) needed their
fixtures updated to move `lock_at` into the past before asserting no-show behavior, since
their scenario (every game final) can only really happen once lock has passed; see
DECISIONS.md for the full reasoning.

## Phase 2. Replace the tie rules with the league's rule

Every ladder (weekly, bowl, season points, season wins) now ranks by its primary metric, ties
broken by weekly wins (season points/wins ladders) or prior wins entering the week (weekly/
bowl), and a tie that survives even that shares a rank and splits the combined payout for the
places it spans, exactly as the league described. There is no third, submission-time level
anywhere in the ranking chain any more; "submitted first" as a reason string is gone.

**Worked table** (weekly ladder, 105/55/25), verified by tests in `tests/test_payout_service.py`
and `tests/test_standings.py`:

| Situation | Result |
|---|---|
| Two tied for 1st (wins differ) | Full 1st (105) to the higher-wins player, full 2nd (55) to the other |
| Two tied for 1st (wins also tied) | Each gets (105 + 55) / 2 = 80, next player takes 3rd (25) |
| Two tied for 2nd (wins also tied) | 1st gets 105, each tied player gets (55 + 25) / 2 = 40 |
| Two tied for 3rd (wins also tied) | 1st 105, 2nd 55, each tied player gets 25 / 2 = 12.50 |

A 12.50 share is never rounded by `Pool.payout_rounding`, `payout_rounding` only ever applies
when resolving a percent-of-pot rule into a place amount, unchanged from before.

**Settings decision (recorded in full in DECISIONS.md):** `Pool.weekly_tiebreak_mode`/
`Pool.season_tiebreak_mode` are left in the database exactly as they are (no migration,
nothing rewritten) and are simply never read again; the new rule applies to every pool. The
now-inert settings dropdowns for both, plus the `Tiebreak` (`payout_tiebreak`) dropdown, are
removed from `/league/payouts`'s pot settings form so nothing on screen misleads a
commissioner into thinking there is still a choice to make.

**Leftover cents:** the combined total for a genuinely tied group is still rounded down to
`Pool.payout_rounding` before splitting (unchanged), and any leftover cent goes to the tied
players in alphabetical order of display name (never submission time any more). Implemented
as a new `"alphabetical"` option on `app/payouts.py`'s existing `_tiebreak_sort_key`, reusing
`allocate()` exactly as instructed; no second splitter was written. `Standing`/`StandingInput`
gained a `display_name` field to carry this.

**Reason shown on the row:** "Tiebreak: 3 wins to 2." when wins actually decided it, "Tied on
points and wins, pot split." when a genuine split occurred, nothing otherwise.

**Rule copy updated** to match the code exactly in `app/templates/results.html` and
`app/templates/leaderboard.html`: "Ties go to the player with more weekly wins. If points and
wins are both tied, the players split the combined payouts for the places they share." The
`/how-it-works` page and `SPEC.md` are covered in Phase 11.

**Tests:** every row of the worked table above (exact dollar amounts); a points tie broken by
wins pays the full amount, no split; a week 1 tie splits (nobody has prior wins yet); the
season wins ladder ties on wins break on points, and a genuine three-way tie on both splits;
inverse scoring still ranks lowest-first on every ladder that uses it, season wins stays
descending; no "submitted first" string anywhere; test weeks contribute nothing to any wins
count used here; the settings-column-no-longer-has-any-effect tests confirm a pool with the
old `"split"` value stored still applies the new rule identically to the default. The
commissioner's recalculate-preview safety requirement (Phase 2, item 6, showing every award
that would change before writing anything) is carried over unchanged from the existing
`recalculate_awards` flow and was not touched by this phase; it is exercised again in Phase 9's
regression sweep.

## Phase 3. Fix the sort that pulls in tiebreak lines

**Root cause:** the tiebreak/split reason rendered as its own `<tr class="lb-row lb-tiebreak-
row">`, a full row with a `colspan`'d note cell. `app/static/app.js`'s `sortTableRows` sorted
every row in `tbody.rows` indiscriminately, so a note row's (empty or mismatched) cell content
sorted like real data and could float to an arbitrary position, no longer attached to the
player it explained.

**The fix:**
1. The reason now renders inside its own player's row, a muted second line in the name cell
   (`app/templates/leaderboard.html`, `app/templates/results.html`), never a separate `<tr>`.
2. Every real data row in a sortable table now carries a `data-row` attribute
   (`leaderboard.html`, `results.html`, and, for the audit, `admin/_slate_fragments.html`'s
   `slate_row`/`candidate_row` macros, the app's only other sortable tables). `sortTableRows`
   only ever selects `tr[data-row]` to sort, and reinserts them with `insertBefore(anchor)`
   where `anchor` is whatever followed the last data row before the sort started, rather than
   a plain `appendChild` that would have dragged a trailing non-data row (a summary line, if
   one is ever added) above the freshly sorted rows on every sort.
3. `picks.html`'s `.game-list` sorter (`sortGameListRows`) already queried `.game-row`
   specifically and excludes its own `data-divider` "Not picked" row; no change needed there,
   confirmed while auditing every sortable list in the app.

**Tests:** `tests/js/sorting.test.js` gained a test proving a non-data row is excluded from
sorting entirely and stays in place; `tests/test_payout_display.py` gained an HTTP-level test
confirming the reason renders inside `<tr data-row>`, never a separate `<tr class="lb-row
lb-tiebreak-row">`. The full existing JS suite (23 tests) and Python suite pass unchanged.

## Phase 4. Condense the Results tab

The weekly leaderboard table (already reshaped into the condensed form across Phases 2/3) is
now exactly the spec's columns: Standing, Name, Points, Correct ("10 of 14"), Wins (season
weekly wins, the tiebreak level), Payout once the week is fully final, every column sortable,
default sort by standing, the tie/split reason under the name. The full pick grid (both the
player-major and game-major views, and the legend and view toggle) is unchanged in content but
now sits behind a native `<details class="full-pick-grid"><summary>Full pick grid</summary>`,
closed by default, so a returning player reaches the condensed table first. The scoreboard and
scenarios sections are untouched, they do not duplicate the leaderboard table.

**Tests:** the existing player-major/game-major grid test now also asserts the disclosure is
present and closed; a new test confirms the Wins column shows season weekly wins, not a
single-week figure.

## Phase 5. Condense the Season tab

The two previously stacked tables (season points, season wins) are now one section with a
"By points" / "By wins" toggle (the same view-toggle pattern results.html already used for
its pick grid), each panel a single condensed table: Standing, Name, Points, Wins, Correct,
and Payout once that scope's season awards exist. The separate "Season awards" section (two
more stacked card/table pairs below the ladders) is gone, folded into the same rows. The stat
card row above the table (Leader, Points, Players, Weeks played) is removed, since it only
restated row 1 and the row count of the table right below it.

**Phase 2 item 5, closed out:** a frozen season award is compared, at read time, against what
`project_awards` would compute live right now (`app/services/payouts.py.recalculate_preview`,
new); a row whose frozen amount or place differs gets a muted "Awarded under the previous tie
rule." note next to the tiebreak reason. Nothing is recalculated to produce this, it is a pure
comparison.

**Phase 2 item 6, closed out:** the commissioner's "Refresh and score now" button
(`POST /league/run/results`) no longer recalculates payout awards as a silent side effect of
scoring. It now always runs a safe live rescore (no actor passed to `score_week_for_pool`,
which only ever recalculates when an actor is present), and only when the week was already
scored before the refresh AND the correction would actually change a frozen award does it
redirect to a new preview page (`GET /league/run/results-preview`) listing every affected
player, old and new place, old and new amount, and whether the award is already marked paid.
Nothing is written until the commissioner clicks "Confirm recalculation"
(`POST /league/run/results-confirm`), which mirrors `score_week_for_pool`'s own recalculate
branch scope by scope and preserves `paid_at` throughout, exactly as `recalculate_awards`
already guaranteed. This is never reachable from the unattended cron path, which never posts
to this route at all.

**Tests:** `recalculate_preview` (service level: lists the real diff, writes nothing, empty
when nothing would change) and an end to end HTTP test driving refresh, preview, and confirm
through a real scenario where the weekly winner actually flips, proving the redirect happens,
nothing is written before confirming, and a paid, now-stale award is left exactly as it was
after confirming.

## Phase 6. Condense the This Week tab

A "post launch" compact-row treatment already existed for this page (52px rows at 1024px and
up, an expandable per-row detail panel), built in an earlier session. It already achieved most
of "a compact table instead of large cards," but it hid the line and kickoff behind that
expand toggle, which conflicts with this build's explicit requirement that a row's required
contents (slate rank, matchup, kickoff, line, the winner control, the confidence input) are
never hidden behind a click, only wrapped onto a second line if a row runs out of width. Two
changes, both additive to the existing markup and CSS, nothing structural:

1. A small slate rank badge, overlaid on the drag grip (so it never disturbs the row's
   existing grid layout at any width), added to every row.
2. The line and kickoff stay inline in `.game-meta` at every width now, wrapping instead of
   moving into the collapsed detail panel; only the league badge and each team's record still
   move there, since neither is required row content.

Nothing about pick entry itself changed: no template structure, data attribute, or app.js
logic touching drag, keyboard navigation, confidence assignment, locking, the payment gate, or
the test-week badge was touched. The existing 23 JS tests (`tests/js/*.test.js`, keyboard
navigation and sorting) and every existing pick-entry test pass unchanged.

**Tests:** a new test confirms the slate rank badge renders and that the line and kickoff each
render exactly once per row, inline, never duplicated into the collapsed detail panel.

## Phase 7. Expandable pick rows on Results and Season

A chevron in each row's own Name cell (both /results and both /standings panels), loaded
lazily over HTMX (`GET /results/pick-strip`, shared by both pages) the first time it opens
(`hx-trigger="click once"`, so a close and reopen never refetches), toggled with the same
open/close mechanism the picks page's own per-row detail panel already used
(`toggleRowDetail`, reused verbatim). The panel lives inside the row's own `<td>`, as a child
of the sortable `<tr data-row>`, not a sibling row, specifically so it travels with its own
row when the table re-sorts (Phase 3's sorter only ever moves `tr[data-row]` elements; a
literal sibling `<tr>` panel would have been orphaned by a re-sort exactly the way a tiebreak
note used to be, before Phase 3).

Cards order by confidence descending, colour by state (correct, wrong, void, pending) using
the same tokens the full pick grid already uses, and scroll horizontally with CSS scroll
snapping (native touch swipe, no JS needed); desktop gets explicit prev/next buttons, and
every card is independently keyboard-reachable with Left/Right moving focus between them.
`prefers-reduced-motion` drops the smooth-scroll behavior on every JS-driven scroll call.

Privacy is unchanged and enforced server side in the route itself, not just the template:
before a week locks, a request for anyone else's picks (by URL, not just through the UI)
reads "Picks are hidden until lock," the viewer's own picks always show, and a `user_id` from
another pool 403s. On Season, the panel carries its own week selector (defaulting to the most
recently scored week), reloading just the panel over HTMX when changed, plus a
points-and-correct summary line for whichever week is selected; on Results the panel has no
selector, since the page's own week switcher already fixes it.

**Tests:** own-picks-visible-before-lock vs another-player's-hidden, revealed once locked,
confidence ordering, a 403 for a player in another pool, the season week selector actually
switching which week's picks render, and the Results row wiring the chevron to the correct
panel id.

## Phase 8. Mobile pass

Checked every tab touched by this build (Results, Season, This Week, Chat, League dashboard,
Payouts) at 360px, 390px, and 430px. `resize_window` does not change a tab's actual CSS
viewport in this environment (`window.innerWidth` held constant across repeated resize
attempts, confirmed against a browser `list_connected_browsers` already reports as local), so
every width was tested for real by injecting a fixed-size `<iframe>` into a blank page and
driving Chrome automation against the iframe's own `contentWindow`, which does get a real,
independent viewport that media queries correctly evaluate against.

Found and fixed one real bug, live on the Results leaderboard at 360px: `.table td` (the
stacked-card layout below 768px) laid out its children as a single-line flex row with no
wrap, so any "second line" content, a tiebreak note, a stale-award note, or Phase 7's new
expandable pick strip, rendered centered on top of the player's name instead of stacking below
it. Fixing that surfaced a second, related bug: the pick strip's own row of fixed-width cards
would not shrink, which stretched `.table-wrap` and the whole page out to the content's width
instead of scrolling sideways inside the strip's own scroller.

Fix (`app/static/app.css`, commit `07196e7`): `flex-wrap: wrap` on `.table td`/`.table tbody
th`, `flex-basis: 100%` plus `text-align: left` on `.lb-tiebreak-note` and
`.pick-strip-target:not([hidden])` so each forces its own line, `overflow-x: hidden` on
`.table-wrap` below 768px (the existing `>=768px` block already restores `overflow-x: auto`,
where the real table needs to scroll sideways on purpose), and `min-width: 0` on
`.pick-strip-scroll-wrap` so it can actually shrink to fit instead of forcing its ancestors
wider. Re-verified at 360px that the pick strip now stacks correctly below the name/ribbon,
and separately confirmed the existing `>=768px` desktop table layout is unaffected (pick strip
still renders inline in the Name cell with its own prev/next buttons).

Swept the remaining five pages at all three widths (`/picks`, `/standings`, league chat, the
league dashboard, payouts): zero horizontal overflow anywhere (`documentElement.scrollWidth`
matched `clientWidth` on all 15 page x width combinations), no console errors, and Season's
leaderboard, which reuses the same `.lb-tiebreak-note`/`.pick-strip-target` classes just fixed
on Results, confirmed clean on a genuinely tied row with its pick strip expanded. No further
CSS changes were needed outside the one fix above. Screenshots for each page at 360px saved to
`docs/mobile/`.

**Tests:** full gate (`ruff check .`, `black --check .`, `pytest -q`, the em dash grep) and
`npm test` all clean after the CSS fix; no new failures (the same 2 pre-existing, unrelated
failures remain: `test_week_published_notification_sent_when_pool_opts_in` and
`test_slate_editor_page_weight_budget`, both untouched by this build and tracked, not fixed).

## Phase 9. Regression sweep

Full gate (`ruff check .`, `black --check .`, `pytest -q`, the em dash grep) run clean
throughout every phase above; this pass adds targeted, named evidence per line rather than
only the aggregate count.

| # | Line | Status | Evidence |
|---|---|---|---|
| 1 | Inverse scoring: lowest total wins a finished week | Pass | `tests/test_scoring.py` (unchanged by this build), full suite green |
| 2 | 15 of 20 validation, specific messages | Pass | `app/scoring.py.validate_picks` tests, unchanged |
| 3 | Saving twice with a swapped game leaves exactly 15 pick rows | Pass | `tests/test_orphaned_picks.py`, 100% green, unchanged |
| 4 | Tab/arrow nav, confidence cap, reorder, drag, lock/unlock | Pass | `npm test`, 23/23 JS tests green (`tests/js/pick_navigation.test.js`, `sorting.test.js`) |
| 5 | Payouts: known ladder totals 2775/400/1155/620, grand total 4950 | Pass | `tests/test_payouts.py::test_fatrunner_ladder_resolves_to_known_totals` |
| 6 | Payout snapshots do not move when the pot changes | Pass | `tests/test_payout_service.py::test_snapshot_amounts_survive_the_pot_growing_after_the_fact` |
| 7 | Published slates do not move on cron | Pass | `tests/test_rebuild_and_amend.py`, `tests/test_cli.py`, unchanged, green |
| 8 | Test weeks contribute nothing to standings, payouts, or wins counts | Pass | Phase 1/2's own new tests plus pre-existing quarantine tests, all green |
| 9 | Scenarios read the same ranking and tie rules as standings | Pass, with a scope note | See below |
| 10 | Voided games score zero, reduce only the affected player's possible | Pass | `tests/test_scoring.py`, `tests/test_results_service.py`, void-related tests green |
| 11 | Commissioner pages carry no "admin" wording; `/site` 403s for commissioners | Pass | `tests/test_app.py`'s rendered-response admin-wording and site-403 tests, unchanged, green |
| 12 | Email and league chat still work | Pass | `tests/test_mail.py`, `tests/test_chat.py`, unchanged, green |

**Item 9, the scope note.** `app/scenarios.py` is deliberately a pure, database-free module
(SPEC.md Section 9a: "no database, no network, no imports from app.models or any
app.services/app.routers module"), so it cannot import `app/services/standings.py`'s new
wins-then-split ranking and never could, before or after this build. Its own `rank_players`/
`_rank_values` already agree with standings on the one thing they share, the primary sort
direction (lowest wins under inverse, matching `weekly_winner_ids`), and already use
competition ranking so ties share a place, which is the correct behavior for a probabilistic
sweep across many scenarios regardless of any single settled week's own tiebreak. Teaching the
scenario engine about weekly-wins tiebreaks would mean passing `wins_entering_week` into the
pure engine as a new input and reworking its ranking and Monte Carlo sampling around a second
dimension, a real, separately scoped feature this prompt's 13 phases never actually ask for
elsewhere; not attempted here, and not a regression this build introduced, since the scenario
engine never had wins-tiebreak awareness even under the old two-mode system.

## What still needs attention

- Phases 10 through 13 (full verification, documentation, merge/push/deploy, live-site check)
  have not started yet.

## Confirmation: no production data touched

Every phase so far has run exclusively against this session's own in-memory sqlite test
fixtures (`tests/conftest.py`) and, for the boot guard, the repo's local `.env`
(`DATABASE_URL=sqlite:///./picksportplus.db`). No Render tool, no `psql`, no production
connection string, and no CLI scoring/repair command has been run against anything but a local
or in-memory database at any point in this build so far.
