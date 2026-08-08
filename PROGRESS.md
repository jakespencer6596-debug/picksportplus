# Week 1 readiness build: progress

Target: Week 1 slate opens Saturday, September 12, 2026. See `DECISIONS.md` for the
reasoning behind every judgment call made along the way.

- [x] Phase 0. Orientation and baseline
- [x] Phase 1. Fix week resolution (per-league date window)
- [x] Phase 2. Invert scoring, lowest total wins
- [x] Phase 3. Pick 15 of 20
- [x] Phase 4. Two-step pick entry
- [x] Phase 5. Admin-curated slate with pinned games
- [x] Phase 6. Navigation, sortable tables, transposed results grid
- [x] Phase 7. Entry payment and payouts
- [ ] Phase 8. The scenarios engine
- [ ] Phase 9. Demo data, deploy, and launch readiness

## Phase 0 notes

- Baseline test suite (before any code change): **557 passed**, 0 failed.
- Fixed one pre-existing `ruff` finding in `alembic/env.py` (unsorted imports) as part of
  baseline cleanup; see `DECISIONS.md`.
- Added `python -m app.cli doctor` (`app/cli.py`), a read-only diagnostic: database
  dialect/URL, Alembic migration state, `OFFLINE_MODE`, provider key presence, pool
  settings, and a live ESPN scoreboard probe per enabled league (URL, HTTP status,
  returned season year, returned week number, game count, valid calendar week range).
- Environment repairs required before any of this could run: moved the repo out of
  OneDrive (`C:\dev\PickSportPlus`) and installed Python 3.11 (no interpreter existed on
  this machine). Full detail in `DECISIONS.md`.
- Branch `week1-readiness` created from `main`.

### doctor output, no pool seeded yet (schema only)

```
Database
  dialect     : sqlite
  url         : sqlite:///./picksportplus.db
  migrations  : up to date

OFFLINE_MODE  : False

Provider keys (presence only, values are never printed)
  ODDS_API_KEY : NOT SET
  CFBD_API_KEY : NOT SET

No pool exists yet. Run: python -m app.cli seed-admin
```

Provider keys were NOT SET at the time of this run because `.env` had not yet been copied
into the new working directory (see `DECISIONS.md`, OneDrive section). The live ESPN
probe against the real September 12, 2026 date will be re-run and recorded once Phase 1
lands and a pool exists; see Phase 9b for the mandatory live re-verification.

## Phase 1 notes

Fixed the root bug: `fetch_candidates` sent the pool's own week number straight to ESPN as
the literal week number for both NFL and college, but the two leagues' week numbers are not
aligned (college starts about three weeks before the NFL and has a bowl season the NFL has no
equivalent of). This made a build fail outright the moment the two calendars drifted apart,
including the real launch date: Saturday September 12, 2026 is NFL week 1 and college week 3,
so the old code would have sent week 1 (or whatever number NFL detection produced) to college
too and silently built the wrong college slate, or built nothing.

The fix, in order:

- `app/services/calendar.py` (new). Resolves each of a pool's enabled leagues to an ESPN week
  number and season type from a calendar anchor date, reusing `espn.parse_calendar` and
  `espn.current_week_from_payload` rather than reimplementing date window matching. Tries the
  regular season first, falls back to the postseason (bowl season for college), and returns
  `None` for a league with genuinely no games in either window, never raising.
- `app/models.py`. Added `Pool.week1_anchor_date` (the commissioner-set Saturday pool week 1
  anchors to) and `Week.anchor_date`, `Week.resolved_weeks`, `Week.is_bowl_week`. `Week.week_number`
  keeps its old meaning, the pool's own 1, 2, 3... sequence, and is never again sent to ESPN
  directly.
- `app/services/ingest.py`. `ensure_week` computes `anchor_date` from `pool.week1_anchor_date`
  when creating a new week. `fetch_candidates` resolves each league independently via
  `calendar.py` and records what it found on `Week.resolved_weeks`/`is_bowl_week`. `detect_week`
  uses pure date arithmetic against the pool's anchor when one is configured, and only falls
  back to asking ESPN what NFL's current week is (the old behaviour, with a warning) for a pool
  that has not been configured yet. The old dead end message ("ESPN returned no games for week
  N. Nothing to build yet.") was replaced with one that names the anchor date, every league
  attempted, the resolved week or the reason none resolved, the URL called, the HTTP status
  when a request failed, the game count, and the valid week range from that league's calendar.
- `app/routers/admin.py` / `app/templates/admin/settings.html`. Added a "Week 1 anchor date"
  field to pool settings so a commissioner can actually set `week1_anchor_date`.
- `alembic/versions/d3ed4af188d3_add_per_league_week_resolution_columns.py`. The migration for
  all four new columns, reversible, verified against both a populated and a fresh database.
- `tests/fixtures/espn_cfb_2026_calendar.json` (new). See `DECISIONS.md` for how it was built
  from the real recorded 2025 college calendar.
- `tests/test_calendar.py` (14 tests) and `tests/test_ingest.py` (17 tests), new. Cover the
  concrete September 12, 2026 case (NFL week 1, college week 3), a league outside both its
  regular season and postseason resolving to `None` without raising, a December anchor putting
  college into the postseason and setting `is_bowl_week`, caching (one HTTP call per league per
  season across repeated resolutions), and the rewritten dead end message.

Full judgment calls (the `detect_week` fallback shape, where the shortfall note lives, the
anchor time-of-day handling, and the synthetic college fixture) are recorded in `DECISIONS.md`
under "Phase 1".

Test suite: **588 passed**, 0 failed (557 at the Phase 0 baseline, plus 31 new). `ruff check .`
and `black --check .` both clean.

```
Pool: PickSportPlus Demo (id 1)
  season_year        : 2025
  sports             : ['nfl', 'ncaaf']
  num_games_per_week : 20
  target_nfl/ncaaf   : 8 / 12
  auto_publish       : True
  current_week       : 5
  timezone           : America/New_York
```
(`doctor --no-probe` output against the local demo pool seeded by `seed-demo`; this pool
predates `week1_anchor_date` and has none set, so it is exercising the fallback path on
purpose. The mandatory live re-verification against the real September 12, 2026 date, with an
anchor actually configured, happens in Phase 9b per the note above.)

## Phase 2 notes

Inverted the pool's real scoring rule: wrong picks now count their staked confidence against
the player, correct picks earn nothing, and the lowest weekly total wins. `standard` (the old
behavior) keeps working, switchable per pool from `/admin/settings` without a code change.
`Pool.scoring_mode` defaults to `"inverse"`, so every pool that does not explicitly choose
`standard` runs the real rule starting now.

- `app/scoring.py`. `score_pick` and `score_week` take a `mode` parameter (`"standard"` by
  default at the function level, see `DECISIONS.md` for why that default did not flip even
  though the pool level default did). `score_week` also takes `picks_required`, defaulted to
  `len(outcomes)` until Phase 3 adds a real per-pool field. `WeekResult` gained
  `did_not_submit: bool`. A no-show scores 0 under `standard` (unchanged) and the maximum
  possible penalty, `sum(1..picks_required)`, under `inverse`. `weekly_winner_ids` takes a
  `mode` too, excludes any `did_not_submit` result before picking a winner in either mode
  (highest wins under `standard`, with the old "nobody wins a scoreless week" `<= 0` guard;
  lowest wins under `inverse`, no such guard, since 0 is a perfect and winnable score there).
- `app/models.py`. `Pool.scoring_mode` (new column, default `"inverse"`) and
  `WeekEntry.did_not_submit` (new column), plus a `SCORING_MODES` constant.
- `alembic/versions/6aa4a2f020c6_add_scoring_mode_and_did_not_submit.py`. The migration for
  both columns, reversible, verified upgrade and downgrade against a fresh SQLite database.
- `app/services/results.py`. `score_week_for_pool` threads `pool.scoring_mode` and
  `pool.num_games_per_week` (as `picks_required`) into `score_week`, stores
  `WeekEntry.did_not_submit` from the result, and passes `mode` into `weekly_winner_ids`.
- `app/services/standings.py`. `season_standings` and `weekly_leaderboard` sort ascending on
  points under `inverse`, descending under `standard`, read from `pool.scoring_mode`.
  `_assign_ranks` needed no change, it is direction agnostic by construction (confirmed with a
  comment and dedicated tests). `WeeklyRow` gained `did_not_submit`.
- `app/routers/results.py`. Per pick "earned" value (used only for screen reader text) is now
  computed with `score_pick(..., mode=pool.scoring_mode)` instead of assuming "correct means
  points," and the player column sort direction follows `scoring_mode` too. `PlayerColumn`
  gained `did_not_submit`.
- `app/routers/admin.py` / `app/templates/admin/settings.html`. A new "Scoring" settings card,
  two radio options, following the same pattern Phase 1 used for `week1_anchor_date`.
- Templates (`leaderboard.html`, `results.html`, `picks.html`): "Points" becomes "Points
  against" under inverse, a "low score wins" rule reminder under both standings tables, the
  picks page explains the real stakes of a wrong pick and (this is a genuine behavior fix, not
  just wording) the no-show banner no longer claims "you score 0 points" when the pool is
  actually charging the maximum penalty. Non-submitters read "No picks submitted," driven by
  `did_not_submit`, not inferred from `submitted_at`. See `DECISIONS.md` for the exact copy.
- Audited every template and every `app/` module for a hard coded "highest wins" assumption
  (`desc(`, `reverse=True`, "Correct, N points" style copy); the only real ones were the
  standings/leaderboard sort direction and the results page per-cell copy, both fixed above.
  `app/templates/admin/slate.html` shows no scores at all and needed no change.

Test suite: **624 passed**, 0 failed (588 at the Phase 1 baseline, plus 36 new: most of
`tests/test_scoring.py`'s existing tests were also rewritten to name their mode explicitly
rather than lean on an unstated default, since the pool level default just flipped).
`tests/test_scoring.py` includes `test_inverse_perfect_week_beats_a_no_shows_max_penalty`, the
named regression for the central trap in this phase: a no-show's maximum penalty must never
beat a perfect week. `tests/test_app.py::test_scoring_end_to_end` proves the same fact again
through the real database and router, not just `app/scoring.py` directly, and
`test_scoring_end_to_end_standard_mode_still_works` proves `standard` still works end to end
when a commissioner chooses it. New `tests/test_standings.py` covers sort direction for both
`season_standings` and `weekly_leaderboard`. `ruff check .` and `black .` both clean.
Full judgment calls are recorded in `DECISIONS.md` under "Phase 2".

## Phase 3 notes

The pool's real submission rule: the slate stays 20 games, a player now picks exactly 15 of
them, confidence 1 to 15 assigned only to the games they picked. Both the slate size and the
pick count are commissioner settings (`Pool.num_games_per_week`, new `Pool.picks_required`),
never hard coded.

- `app/scoring.py`. `validate_picks` gained a `picks_required` parameter and now checks
  exactly `picks_required` picks are submitted (one message, `"You have picked N games. Pick
  15."`, for both short and over) instead of requiring one pick per slate game; the old
  per-game "missing a winner" check is gone entirely, since an unpicked slate game is legal
  now. `score_week`'s `possible` changed meaning: it is now the count of countable outcomes
  among a player's own submitted picks, not the whole slate, so two players can cover
  different subsets of the same slate without affecting each other's `possible`. A direct
  consequence, called out explicitly in `DECISIONS.md`: a no-show's `possible` is now 0
  (was the slate's countable count under Phase 2), a deliberate refinement, not a
  regression. The no-show max-penalty math is unchanged.
- `app/models.py`. `Pool.picks_required` (new column, default 15).
- `alembic/versions/7f659398d6cc_add_picks_required_to_pools.py`. The migration, reversible,
  verified upgrade and downgrade against a fresh SQLite database.
- `app/routers/admin.py` / `app/templates/admin/settings.html`. A "Picks required" field in
  the existing Slate size card, validated `1 <= picks_required <= num_games_per_week`,
  following the same pattern `week1_anchor_date` and `scoring_mode` used in Phases 1 and 2.
- `app/routers/picks.py`. `_save_picks` threads `pool.picks_required` into `validate_picks`.
  `picks_page`'s `n` and `has_full_entry` context values now mean `picks_required`, not the
  full slate; the saved-order sort was reworked to put picked games first (by confidence)
  and any unpicked slate games after, since it can no longer assume every slate game has a
  pick.
- `app/services/results.py`. `score_week_for_pool` reads the real `pool.picks_required`
  instead of the Phase 2 stand-in (`pool.num_games_per_week`).
- `app/templates/picks.html` / `components/pick_status.html`. Introduced `slate_size`
  (`games | length`) as its own value, separate from `n` (`picks_required`), for every place
  that actually means the whole published slate (the "N games" pill, the NFL/college
  breakdown) versus the pick target (progress text, the confidence range, the no-show
  penalty formula, the "how this week works" panel). `app/static/app.js`'s live progress
  readout and Save button gating now read `picks_required` from a `data-picks-required`
  attribute instead of the row count; the per-row confidence numbering during drag is
  unchanged (still positional over the whole slate) since building the real "pick 15 of 20"
  interaction is Phase 4's job, not this phase's; see `DECISIONS.md` for the full reasoning
  and what specifically is and is not fixed client side.
- `app/cli.py`. `doctor` now prints `picks_required` alongside `num_games_per_week`.
- `SPEC.md`. Section 1 and Section 8 rewritten for the real rule; Section 9's `possible`
  sentence updated to match the scoring change.

Test suite: **636 passed**, 0 failed (624 at the Phase 2 baseline, plus 12 net new: several
Phase 2 no-show tests were also updated in place for the `possible=0` refinement rather than
counted as new). Explicit coverage for the brief's two named scenarios:
`tests/test_scoring.py::test_a_voided_pick_scores_zero_and_only_reduces_that_players_own_possible`
(a voided pick only shrinks that player's own `possible`, not the slate's) and
`test_no_show_possible_is_zero_not_the_slate_size` (the no-show refinement). Router level:
`tests/test_app.py::test_server_rejects_too_many_picks_even_if_no_client_would_send_them`
pins that `picks_required` is enforced server side regardless of what any client sends.
`ruff check .` and `black .` both clean. Full judgment calls are recorded in `DECISIONS.md`
under "Phase 3".

## Phase 4 notes

Rebuilt the picks page into the real three stage entry flow the brief named: type a
confidence number straight into a row (live duplicate/range validation, `.has-error` reused
rather than a new class), "Reorder to inputs" to snap the list to what was typed, then drag
or use the up/down buttons to refine, confidence now scoped correctly to the picked rows
only (`app/static/app.js`'s `renumber()` no longer numbers the whole slate, the Phase 3
gap). Added a player initiated lock, separate from Save and from the pool wide `lock_at`:
"Lock picks" opens an inline confirmation panel (no HTMX round trip needed, everything it
shows is already on the page) before the real `POST /picks/lock` fires; `POST
/picks/unlock` reverses it, refused once the week's real lock has passed. New
`WeekEntry.locked_at` column, migration `be7a7724eee3`. `app/routers/picks.py`'s
`_save_picks` was split into `_parse_submission` / `_upsert_picks` so `/picks` and the new
`/picks/lock` share exactly one parse and one write path, both still running the same
`validate_picks` Phase 3 already wired up, never weakened or duplicated. `picks.html` gained
a shared `readonly_list` macro and a new state (3b: locked by the player, week still open,
with an "Unlock to edit" escape) ordered so the real time lock always wins over it.

Test suite: **642 passed**, 0 failed (636 at the Phase 3 baseline, 6 net new, all router
level: `_save_picks`/`_lock_picks`/`_unlock_picks` are the only routers touched, and
`app/scoring.py` was not touched at all this phase). `tests/test_app.py::test_picks_page_renders_in_every_state`
walks one player through all five reachable GET `/picks` states end to end, including a
genuinely partial entry (written directly, since a real Save can never leave one). `ruff
check .` and `black .` both clean. Full judgment calls, especially the two-writer confidence
model (typed vs. dragged) and the "Not picked" divider's drag behavior, are recorded in
`DECISIONS.md` under "Phase 4".

## Phase 5 notes

Two changes the group running the pool asked for directly: closest-spread selection alone
was routinely dropping rivalry games (Ohio State vs Michigan, Auburn vs Alabama) the moment
either side was having a lopsided season, and automation building *and opening* a week by
itself was more automatic than the group wanted.

- `app/slate.py`. `Candidate` and `Selected` both gained a `pinned: bool = False` field.
  `select_slate_by_targets` seeds pinned candidates into the first pass before the ordinary
  per league fill runs, so a pin already counts against its own league's target; the
  existing over/under total balancing (drop the farthest, fill from the other league) does
  the rest unchanged. A pinned game is never dropped by the "over total" trim, even as the
  widest spread in the pool. More pins than `num_games_per_week` raises a clear `ValueError`
  rather than silently truncating them. A pin still needs a resolvable spread and to not
  have already kicked off, the same eligibility rule every other candidate follows.
- `app/models.py`. `Pool.auto_publish` default flips to `False`. New `Game.pinned` (Boolean,
  default `False`) and `Pool.rivalries` (JSON, default the curated list below) columns. No
  separate `pin_reason` column: why a game is pinned is computed at render time.
- `alembic/versions/6b6eab096a56_add_pinned_games_and_rivalries.py`. The migration for the
  two new columns, reversible, verified upgrade and downgrade against both a fresh and the
  repo's existing local database. `auto_publish`'s own default flip needed no schema change,
  since the column never carried a database level default; see `DECISIONS.md`.
- `app/cli.py`. `seed_admin`'s `Pool(...)` call no longer hard codes `auto_publish=True`, so
  a freshly seeded pool actually gets the new default.
- `app/services/ingest.py`. `upsert_games` auto-pins a brand new game the moment its two
  teams match one of `pool.rivalries`'s pairs, in either home/away order, but only on
  creation: a commissioner's deliberate un-pin survives every later rebuild of the same
  game. New `set_pinned` (always allowed, including once picks exist, since a pin never
  resizes or reorders the slate that is already live) and `slate_reason` (the "Pinned" /
  "Rivalry" / "Closest (spread X, source Y)" explanation the slate editor shows).
- `app/routers/admin.py` / `app/templates/admin/slate.html`. A pin/unpin control per game,
  on the existing `POST /admin/slate/game` route (`action=pin`/`unpin`), same error handling
  and flash style as `add`/`remove`/`swap`/`void`. A "why it's here" column on the slate
  table, and pinned/missing-spread counts added to the week status summary. `slate_build`
  now catches the new `ValueError` (pins over the total) and flashes it instead of a 500.
- `app/routers/admin.py` / `app/templates/admin/settings.html`. A "Pinned rivalry games"
  card: a plain "Team A vs Team B" per line textarea, parsed through `canonical_key` on
  save, rendered back through `display_name` on load. College matchups only for now.
- Seeded rivalry pairs, every key a real `canonical_key(...)` call, never hand typed: Ohio
  State vs Michigan, Auburn vs Alabama, Army vs Navy, Michigan vs Michigan State, Florida vs
  Georgia, Texas vs Oklahoma, USC vs Notre Dame.
- `SPEC.md`. Section 6 and 6a rewritten for pinned/rivalry games; Section 7 rewritten for
  the new `auto_publish` default and what changes (and does not) when a commissioner turns
  automatic opening back on.

Test suite: **664 passed**, 0 failed (642 at the Phase 4 baseline, 22 net new: 12 in
`tests/test_slate.py`, 8 in `tests/test_ingest.py`, 2 in the new `tests/test_cli.py` for
`seed_admin`'s `auto_publish` default). `ruff check .` and `black .` both clean. Full
judgment calls (eligibility-requires-a-spread-even-for-pins, the rivalry auto-pin
persistence choice, the exact seeded pairs and how they were derived, and why no schema
migration was needed for `auto_publish` itself) are recorded in `DECISIONS.md` under
"Phase 5".

## Phase 6 notes

Group feedback, three items: Standings and Results were redundant (both showed a weekly
leaderboard), no way to sort a table column, and the results pick grid had rows and columns
backwards versus the old platform (rows should be players, columns confidence 20 down to 1).

- `app/routers/leaderboard.py` / `app/templates/leaderboard.html`. `/standings` is season
  standings only now; the `weekly_leaderboard` call and the whole "weekly leaderboard"
  section are gone, not just hidden. A "Weekly results" link in the section head points to
  `/results`.
- `app/routers/results.py` / `app/templates/results.html`. `/results` gained a weekly
  leaderboard section between the scoreboard and the pick grid, built from the existing
  `weekly_leaderboard(db, pool, week=row, viewer_id=user.id)`, passing the page's own
  resolved `row` so it always matches whichever week the switcher has selected. It only
  computes and renders once the week is `revealed` (locked): an entry existing at all before
  lock is itself a "who has submitted" leak, the same reveal rule the pick grid already
  enforced, now extended to the leaderboard too (a real regression a straightforward reading
  of the brief would have introduced, caught by the existing `test_picks_stay_private_until_lock`
  test).
- `app/routers/results.py`. `PlayerColumn` gained `by_confidence: dict[int, tuple[Game,
  PlayerPick]]`, keyed by confidence value, built in `_build_columns` from each player's own
  `Pick` rows (reusing the same per-game loop that already builds the game-major `picks`
  dict, no second scan of the slate) rather than by scanning every slate game, since a
  player's confidence values only exist for the games they actually picked.
- `app/templates/results.html`. The pick grid is rendered twice, once per view, both server
  side: the new by-player grid (rows are players, columns are confidence `picks_required`
  down to 1, each cell "GB over CHI" colour coded by outcome, a genuinely empty cell for a
  confidence value nobody maps to) is the default, the original by-game grid (rows are
  games, columns are players) is kept intact behind a "By player / By game" toggle for
  anyone who preferred it. Both panels always render; `app.js`'s `initViewToggle` only ever
  hides one with the native `hidden` attribute, so a router test can assert the by-game
  table's markup is present regardless of which view JS shows on load, and the toggle keeps
  working if JS is slow to attach (the by-game panel ships server side with `hidden` already
  set, matching the buttons' server side `aria-pressed`, so there is no flash of the wrong
  view).
- `app/static/app.js`. One reusable sort engine, `initSortableTable`/`sortTableRows`, wired
  to both the header click path and a `<select data-sort-select-for="...">` for the stacked
  mobile view (no header row to click there), so neither path duplicates the other's
  ordering logic. Numeric columns read `data-sort-value` off each `<td>` rather than the
  rendered text, since the text can be a placeholder ("No entry", "."). A stable sort keeps
  the server's own tiebreak order (correct, weekly wins, name) intact when a sort ties on the
  clicked column. The caret is an inline SVG built from the same path data as
  `components/icons.html`'s "up"/"down" chevrons, added and removed from the active header
  only. Applied to the Season Standings table and the new weekly leaderboard table; the pick
  grids were left unsorted, sorting a grid whose meaning depends on row/column position
  (confidence order, game order) is not the same feature.
- `app/services/standings.py`. `StandingRow` gained `accuracy_sort`, a numeric twin of the
  existing `accuracy` display property (same rounding, so the two can never disagree), for
  the sortable table's `data-sort-value`. No change to any actual sort or scoring logic.
- `app/templates/base.html`. Desktop nav and the mobile tab bar (still four items) both
  rename "Standings" to "Season"; "This Week", "Results" and "Admin" are unchanged. The
  underlying routes and `active_nav` identifiers did not need to change, only the label.
- `app/static/app.css`. New "Sortable tables" and "By player / by game toggle" rules in
  section 10, a `.pick-rowhead`/`.pick-conf`/`.pick-matchup`/`.pick-void-badge` set in
  section 11 alongside the existing picks grid rules (the by-player grid reuses the existing
  `.picks-grid .pick-cell.is-*` state colours by sharing the `.picks-grid` class, so no
  colour rule is duplicated), and a sticky top header row for `.picks-grid-player` alongside
  the pattern's existing sticky-left first column. No new responsive mechanism: both new
  tables use the same `table-wrap`/`data-label` reflow already documented at the top of
  section 10.

Test suite: **668 passed**, 0 failed (664 at the Phase 5 baseline, 4 net new:
`test_standings_page_has_no_weekly_leaderboard`, `test_results_weekly_leaderboard_matches_the_selected_week`,
`test_results_weekly_leaderboard_stays_private_until_lock`, and
`test_results_grid_is_player_major_with_confidence_columns_and_game_major_toggle`). `ruff
check .` and `black .` both clean. Full judgment calls (gating the weekly leaderboard behind
the same reveal rule as the grid, where the by-confidence lookup lives, and how the toggle
avoids a JS-dependent flash of the wrong view) are recorded in `DECISIONS.md` under
"Phase 6".

## Phase 7 notes

Two pieces of direct feedback: "Will be Venmo only this year. No Venmo, no participation," "1
person to pay, no multiple accounts," and a description of the group's old payout column
(what places 1 through 4 received each week, for Bowl Week, and for end of season awards).
Added a Venmo entry gate in front of picking and a `PayoutRule` system feeding a weekly payout
column, a season awards panel, and a payout summary table, all driven by numbers a
commissioner types in by hand. Zero rows ship seeded anywhere; no dollar figure or Venmo
handle is hard coded anywhere outside a test file. No new Python dependency: Venmo is a deep
link plus manual commissioner reconciliation, not a payment integration.

- `app/models.py`. `Pool` gained `entry_fee`, `venmo_handle`, `payment_required_to_pick`
  (default `True`), `payment_note`. `PoolMember` gained `paid_at`, `paid_marked_by_user_id`,
  `member_venmo_handle`. New `PayoutRule` model (`pool_id`, `scope` of `weekly`/`bowl`/`season`,
  `place`, `amount`, an optional `label`), cascade deleted with its pool, never seeded.
- `alembic/versions/8605b49060e6_add_venmo_entry_gate_and_payout_rules.py`. The migration for
  every new column and the new table, reversible, verified upgrade and downgrade against a
  fresh SQLite database.
- `app/services/payouts.py` (new). `allocate_payouts`, the pure tie-splitting arithmetic
  (integer cents throughout, so a split always reconciles to the combined total exactly).
  `rules_by_place`, `week_payout_scope` (bowl routes to `"bowl"`, never `"weekly"`, even when
  weekly rules also exist), `week_is_complete` (reusing the same "nothing left playable" notion
  `score_week_for_pool` already uses), `weekly_payouts` (tie broken by earliest
  `WeekEntry.submitted_at`), `season_payouts` (tie broken by `season_standings`' own final
  order), and `payout_summary` (per player weekly/bowl/season totals for `/admin/payouts`).
- `app/routers/picks.py`. `payment_gate_blocks(member, pool)` is the one function both
  `picks_page` (rendering the blocking panel) and `_save_picks`/`_lock_picks` (the actual,
  authoritative server side refusal) call, so the page and the enforcement can never disagree.
  A new picks page state, 3a, sits between the player lock state and the open editing state:
  the pool wide time lock and a player's own earlier lock both still win over it, per the
  brief, since the gate only ever blocks *creating or locking new* picks.
- `app/routers/admin.py`. `/admin/settings` gained an "Entry fee and Venmo" card on the main
  settings form, plus a separate payout rules section (three scopes, add/remove only, ships
  empty) and a pot validator banner (collected versus allocated, warn only, never blocks a
  save). `/admin/members` gained a paid/unpaid column with a one click toggle
  (`POST /admin/members/{id}/paid`), a bulk "mark selected paid" action, a per member Venmo
  handle note field for the commissioner's own reconciliation, a duplicate handle warning, and
  a pot summary line. New `/admin/payouts` view, a plain per player summary table.
- `app/routers/results.py` / `app/templates/results.html`. A Payout column on the weekly
  leaderboard, shown once every countable game in the week is final or void and the relevant
  scope has rules configured, with a muted note stating the tie split rounding rule next to it.
- `app/routers/leaderboard.py` / `app/templates/leaderboard.html`. A season awards panel driven
  by `scope="season"` rules, shown once at least one week has been scored, labelled as the
  current standings rather than a final result (see `DECISIONS.md` for why this gate was chosen
  over the alternatives).
- `app/templating.py`. New `money` Jinja filter: a plain number, no currency symbol, no
  trailing `.00`, rounded through integer cents first so a float artifact never reaches the
  page.
- `SPEC.md`. Section 10 gained 10a (the Venmo entry gate) and 10b (payout rules), describing
  the new commissioner surface.

Test suite: **704 passed**, 0 failed (668 at the Phase 6 baseline, 36 net new: 19 in the new
`tests/test_payouts.py`, 17 in `tests/test_app.py`). `ruff check .` and `black .` both clean.
Full judgment calls (the `Float` versus `Numeric` choice, the exact tie split wording, and the
season awards panel gating decision with its rejected alternatives) are recorded in
`DECISIONS.md` under "Phase 7".
