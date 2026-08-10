# Decisions

Record of ambiguity resolutions and other build-time judgment calls, most recent first.
Each entry states the decision, why, and where it took effect.

## Phase 0

### Local environment: repo moved out of OneDrive

The working copy lived at `C:\Users\kodak\OneDrive\Desktop\PickSportPlus`. OneDrive's
Files On-Demand feature had turned files inside `.git\` into cloud placeholders
(reparse points), which made `git log` fail with `mmap failed: Invalid argument` and made
`git status` hang indefinitely. Confirmed via `git config --list` (`filter.lfs.required=true`
was a red herring, no `.gitattributes` exists) and via file attributes (`ReparsePoint` on
`.git\index` and object files).

Moved the working copy to `C:\dev\PickSportPlus`, outside any cloud-sync scope. All git
history is intact there (`git log` and `git status` both work instantly). Four local,
gitignored files could not be copied because OneDrive could not hydrate them within the
copy timeout: `.env`, `CFBD_APIKEY.txt`, `OddsAPI_APIKey.txt`, `picksportplus.db`
(+ `picksportplus.db-shm`). None of these are tracked by git, so no repository history or
code was affected. The operator copied the three secret files back in by hand. The local
dev database was not recovered; it is rebuilt by `seed-demo` in Phase 9 and was not needed
for offline test runs.

**Why:** OneDrive was actively corrupting git operations, not just slowing them, so the
build could not safely proceed (branch, many commits, migrations) from the original path.

**How to apply:** All further work in this build happens at `C:\dev\PickSportPlus`. If you
see references to the OneDrive path elsewhere (old shortcuts, IDE workspace files), they
are stale.

### Python environment: no interpreter was installed on this machine

The repository's `.venv` was created on a different machine under a different Windows user
(`C:\Users\Owner\...`, Python 3.13.5) and its interpreter path did not exist here. No other
Python installation was present except the Microsoft Store execution-alias stub. Installed
Python 3.11.9 via `winget install Python.Python.3.11` (matches `pyproject.toml`'s
`black` `target-version = ["py311"]` and `SPEC.md`'s "Python 3.11+"), then rebuilt `.venv`
from `requirements.txt`.

**Why:** every phase's commit gate requires `ruff check .`, `black .`, and `pytest` to
actually run. Without a working interpreter none of the House Rules gates could be enforced.

**How to apply:** use `C:\dev\PickSportPlus\.venv\Scripts\python.exe` for all commands in
this build (`-m pytest`, `-m ruff check .`, `-m black .`, `-m app.cli ...`).

### Pre-existing ruff failure fixed opportunistically

`alembic/env.py` had one pre-existing unsorted-import finding (`I001`). Fixed with
`ruff check . --fix` as part of Phase 0 baseline cleanup, since it is trivial and would
otherwise block the ruff gate on every subsequent phase regardless of what that phase
touches.

**Why:** the House Rules gate is "all three must pass, no exceptions." A pre-existing,
unrelated failure would have made that impossible to honor from Phase 1 onward.

## Phase 1

### detect_week fallback shape when a pool has no week1_anchor_date

The brief asked for judgment on exactly how the fallback should behave. Decision: when
`pool.week1_anchor_date` is unset, `detect_week` reproduces the pre anchor behaviour exactly
(ask ESPN for NFL's current week number, treat that as the pool's own sequence number) and
logs a warning naming the pool. `ensure_week` then leaves the new `Week.anchor_date` column
null for that week, and `fetch_candidates` (in `app/services/ingest.py`) falls back to sending
`week_number` straight to ESPN for every league, exactly like the code did before this phase,
also with a warning.

**Why:** an existing pool (or a brand new one nobody has configured yet) must keep building
something rather than nothing the moment this code ships. Silently guessing a "smarter"
fallback (for example, treating today's date as an implicit anchor) would produce different
per league week numbers than a season that has always run one way, with no warning that
anything had changed. Reproducing the exact old behaviour keeps that migration boring, and
the warning (visible in the CLI and server logs) is what is supposed to prompt the
commissioner to set a real anchor before the two calendars drift apart.

**Where:** `detect_week` and `fetch_candidates` in `app/services/ingest.py`. `Pool.week1_anchor_date`
and `Week.anchor_date` in `app/models.py`, both nullable for this reason. A settings field was
added at `/admin/settings` (`app/templates/admin/settings.html`, `app/routers/admin.py`
`settings_save`) so a commissioner has somewhere to actually set it; leaving it blank clears
it back to the fallback.

### Where the "nothing resolved" shortfall note lives

The dead end message (per league attempt, resolved week or reason, URL, game count, and the
valid calendar range) is built once, as a single string, and appended to `IngestReport.warnings`,
the same list the CLI already prints under `warning:` and the admin slate page already flashes
as an error banner (`app/routers/admin.py` `slate_build`, `app/templates/admin/slate.html`
`warnings-title` section). No new field was added to the `Week` row for this. **Why:** the
warning is about one specific build attempt, not a durable property of the week (a rebuild a
day later, once the anchor is fixed or the season has moved on, may succeed and the old
warning would be stale). `Week.resolved_weeks` and `Week.is_bowl_week` are the durable,
per league facts that are worth keeping on the row; the explanation of why a build came back
empty is transient by nature and belongs in the report for that one run.

### Anchor time of day and pool timezone

`app/services/calendar.py` resolves a date, not a datetime, against ESPN's calendar windows by
converting the anchor date to midday UTC (`calendar.anchor_datetime`) before comparing it
against each `[start, end)` week window. Every recorded calendar window flips over at 07:00Z
or 08:00Z, never at local midnight in any of the pool's timezones, so midday UTC sits safely
inside the correct week regardless of which US timezone the pool runs in. `detect_week`, which
answers a different question (what pool week does "now" fall in, given the pool's own
`week1_anchor_date`), does convert `now` into the pool's configured timezone first
(`ingest._local_date`), because that comparison is against the pool's own week boundaries,
which a commissioner reasonably expects to flip over at a local midnight, not a UTC one.

### 2026 college football calendar fixture

`tests/fixtures/espn_cfb_2026_calendar.json` did not exist and no live capture of it was
taken (Phase 1 has no network access). It was built by taking the real recorded 2025 college
calendar (`espn_cfb_2025_w5.json`, captured 2026-08-02) and shifting every date forward by
exactly 364 days (52 weeks), which preserves weekday alignment (every week window still starts
on the same weekday it did in the real 2025 recording) and keeps the shape, labels and
relative spacing of the real payload. `events` was left as an empty list since these tests only
exercise `leagues[0].calendar`, not game data, and an empty list is honest about that rather
than inventing scores. The resulting synthetic college week window (2026-09-07 to 2026-09-14)
and the existing, already recorded 2026 NFL calendar fixture's week 1 window (2026-09-09 to
2026-09-16) both contain September 12, 2026, which is what pins the concrete real world case
this fixture was built to exercise offline: the launch date falls inside both windows. This
fixture is a synthetic algorithm test only, not a claim about the real 2026 college calendar;
see Phase 9b for the live-verified answer, which can differ (and did: a live probe against the
real ESPN API found college week 2, not week 3, for the real September 12, 2026 date). Do not
update this fixture's numbers to match the live answer, it is testing resolve_league_week's
date-window logic against fixed, known input, not asserting a fact about the real season.

## Phase 1 follow-up: fetch_results had the same bug

While reading `app/services/results.py` in preparation for Phase 2, found that `fetch_results`
called `espn.fetch_scoreboard(db, league, week.season_year, week.week_number, ...)`, the exact
same bug class Phase 1 fixed in `fetch_candidates`: it sent the pool's own week_number straight
to ESPN as the literal week number for every league when refreshing live scores. Phase 1's brief
scoped the fix to the slate-building path and did not catch this. Left unfixed, Phase 1's slate
would have built with the correct per-league games, but score refreshing would have asked ESPN
for the wrong week for whichever league did not match the pool's week_number, silently returning
stale or wrong game state on every scored week.

**Fix:** `fetch_results` now reads `week.resolved_weeks` (populated by `fetch_candidates`) for
each league's own resolved week and season type, falling back to `week.week_number` and
`season_type=2` only when that league has no resolved entry (unanchored pool, or a league that
had no games for the window), mirroring the same fallback Phase 1 already established.

**Why:** consistency with the Phase 1 architecture: `week.week_number` must never be sent to
ESPN directly for a league that has a real resolution on file.

**Where:** `app/services/results.py`, `fetch_results`. Covered by
`tests/test_results_service.py` (new), including a case that would fail if the fix regressed:
the ncaaf cache entry is keyed on its real resolved week (3), not the pool's week_number (1), so
a regression back to the old behaviour would find no cached response and the test would fail
on a missing fixture rather than silently pass.

## Configuration defaults confirmed as-is

The three decisions raised in Part 4 of the brief were pre-filled with defaults in the
CONFIGURATION block and are being implemented as given, not re-litigated:

- `SLATE_SIZE=20`, `PICKS_REQUIRED=15`, confidence range `1..15` (Decision 1).
- No-show penalty is the maximum, `sum(1..PICKS_REQUIRED)` = 120 at 15 picks (Decision 2).
- Payout rules ship empty; the commissioner enters real dollar figures later (Decision 3).

No dollar amounts, entry fees, or Venmo handles are invented anywhere in this build.

## Phase 2

### Where "the default" actually flips: Pool.scoring_mode, not the scoring.py function
### signatures

The brief's Architecture section gives explicit signatures with `mode="standard"` as the
literal default for `score_pick`, `score_week`, and `weekly_winner_ids` ("these decisions are
made, implement them, do not re-derive"). The Tests section separately says to rewrite old
tests to pass `mode="standard"` explicitly "since default is flipping to inverse," which read
literally would contradict the signatures just above it.

**Decision:** implemented the function level defaults exactly as given, `mode="standard"`, so
`app/scoring.py` is unchanged in behavior for any caller that does not pass a mode. The
"default is flipping to inverse" language is about the application's real, effective default:
`Pool.scoring_mode` (new column) defaults to `"inverse"`, and that is what `score_week_for_pool`
threads into `score_week`/`weekly_winner_ids` for every real pool from here on, including the
demo pool and every pool that predates this migration (SQLite back-fills the column via the
migration's server default, see below). `tests/test_scoring.py` still passes `mode="standard"`
explicitly on every old test, per the instruction, purely so each test states which rule it is
proving rather than leaning on an unstated default; it would pass with the mode omitted too.

**Why:** the signatures are marked as decided, not open for re-derivation, and a pure function
defaulting to the old, narrower behavior (no direction flip, no no-show penalty) is the safer
choice for any future caller of `app/scoring.py` that forgets to pass a mode. The dangerous
mistake to guard against is a silent, unintended inversion of real money math; a caller that
forgets `mode=` should get the boring old behavior, not accidentally charge someone 120 points.
The pool level default is where "this pool's real rule is inverse" actually lives, and it is
what `app/routers/admin.py`/`admin/settings.html` expose to the commissioner.

**Where:** `app/scoring.py` (`score_pick`, `score_week`, `weekly_winner_ids` signatures),
`app/models.py` (`Pool.scoring_mode`, default `"inverse"`), `app/services/results.py`
(`score_week_for_pool` reads `pool.scoring_mode`).

### picks_required default: len(outcomes), not a stored field, until Phase 3

`score_week`'s `picks_required` parameter defaults to `len(outcomes)` when the caller omits it,
exactly as specified. `app/services/results.py`'s `score_week_for_pool` does not rely on that
default: it passes `picks_required=pool.num_games_per_week` explicitly, which is correct today
because every player still picks the entire slate (Phase 3 is what introduces "pick 15 of 20").
No number is hard coded anywhere in this phase; the demo pool's real slate size (20) and the
brief's own confirmed `PICKS_REQUIRED=15` example are both just call sites, not constants baked
into `app/scoring.py`. When Phase 3 adds a real `picks_required`-shaped field, only the one
argument at that call site needs to change; `score_week`'s own default keeps working unchanged
for any other caller (a test, a script) that only has a slate to hand it.

### season_totals needs no change, confirmed

Read `season_totals` and its tests before touching anything. It is a plain, unconditional sum
over whatever `points`/`correct`/`possible`/`is_winner` values it is handed; it has no opinion
about which direction is "better." `is_winner` already reflects the correct, mode-aware answer
by the time it reaches `season_totals`, because `weekly_winner_ids` (mode-aware) is what set it
on the `WeekEntry` row in the first place. "Lower is better" under inverse is entirely a sort
direction concern at read time (`app/services/standings.py`), never an aggregation concern, so
`season_totals` and its dataclasses (`SeasonEntryInput`, `SeasonTotals`) are untouched. Added
`test_season_totals_from_scored_weeks_end_to_end_inverse` alongside the existing standard
version specifically to pin this: the same two weeks produce the same season point total under
either mode, and the comment on that test explains why the total alone cannot tell you which
week was the good one, only the sort direction can.

### Exact UI copy landed on

- Column headers: "Points against" (inverse) vs. "Points" (standard), on the season standings
  table, the weekly leaderboard table, and the leader stat tile at the top of `/standings`.
- Rule reminder under both standings tables, inverse only: "Low score wins. You take the
  points you staked on any pick that loses."
- Picks page, unlocked week, inverse: "...Low score wins: a wrong pick counts its staked
  points against you, so stake {{ n }} on your surest pick only if you can afford to be wrong
  about it." (n is the real slate size from the route, never hard coded).
- Picks page, locked week summary, inverse: "A wrong pick counts the points staked on it
  against you. A correct pick costs nothing."
- Picks page, no-show banner, inverse (a real behavior fix, not just wording: the old copy
  said "you score 0 points for the week," which is standard-mode-only and would have been
  actively wrong the moment `inverse` became the default): "You did not submit picks for
  {{ week.label }}, so you take the maximum penalty for the week, {{ (n * (n + 1)) // 2 }}
  points against you." The penalty is computed in the template from `n`, the real slate size,
  matching `score_week`'s own `sum(1..picks_required)` with `picks_required` defaulted to the
  slate size, the same default `score_week_for_pool` overrides with the real value.
- Results page: a non-submitting player's cell/column now reads "No picks submitted," driven
  by the new `did_not_submit` flag (`WeekEntry.did_not_submit`, threaded onto `PlayerColumn`
  and `WeeklyRow`), not by `submitted_at is None`. Player column stats read "{{ points }}
  against, {{ correct }} correct" under inverse instead of "{{ points }} pts, ...". The per
  pick screen-reader text ("Correct, 6 points" / "Wrong, no points") is generated from
  `pick.earned` (which is computed with `score_pick(..., mode=pool.scoring_mode)`, so it is
  already the real, mode-correct number) rather than from a hard coded assumption that only
  "correct" states ever carry points; a wrong pick with a nonzero `earned` reads "Wrong, N
  points against you."
- "No cards to show" / "nobody submitted" empty states on Results and Picks both had a
  pre-existing "a blank week scores zero" claim that is false under inverse (everyone would
  take the same maximum penalty, not zero); both now branch on `pool.scoring_mode`.
- Admin settings: a new "Scoring" card with two radio options, "Inverse: low score wins" and
  "Standard: high score wins," each with a one paragraph explanation of what a correct and a
  wrong pick do under that mode and what a no-show costs, following the existing card/fieldset
  pattern `week1_anchor_date` used in Phase 1.

### Commissioner setting: exposed as a required field, not optional

`settings_save` requires `scoring_mode` as a `Form(...)` field (like `timezone` and
`num_games_per_week` already were) rather than defaulting a missing value server side. The
settings form always renders both radio options with one pre-checked from `pool.scoring_mode`,
so a normal save always sends a value; a request that omits it (a stale form, a bypassed client)
is rejected with a 422 rather than silently coercing to a mode nobody chose. Existing tests that
post to `/admin/settings` were updated to include `scoring_mode` in their form data.

### Existing integration tests in tests/test_app.py encode the old default and had to move

`tests/test_app.py`'s `world` fixture builds a pool without setting `scoring_mode`, so it now
runs under the real default, `"inverse"`. `test_scoring_end_to_end` and
`test_a_voided_game_scores_nobody_and_leaves_the_possible_count` had hand-computed point totals
that assumed the old, correct-picks-earn-points behavior; both were recomputed for inverse
(wrong picks count instead) rather than pinned to `"standard"`, since the whole point of this
phase is that the default pool behavior actually changed and the main end to end test should
prove the real default works, not dodge it. Added `test_scoring_end_to_end_standard_mode_still_works`
alongside it, which explicitly sets `pool.scoring_mode = "standard"` and asserts the old numbers
still hold, so both modes have end to end coverage through the real router and templates, not
just through `app/scoring.py` directly. `test_scoring_end_to_end` also gained assertions on the
commissioner's own `WeekEntry` (who never submits picks in this fixture): `did_not_submit=True`,
`points=10` (the max penalty for the fixture's 4 game slate), and `is_winner=False`, which is the
same no-show-cannot-win fact `test_inverse_perfect_week_beats_a_no_shows_max_penalty` proves in
`app/scoring.py`, now also proved through the real database and service layer.

### Migration: two columns, one revision

`Pool.scoring_mode` (String(16), NOT NULL, server default `'inverse'` for the migration only)
and `WeekEntry.did_not_submit` (Boolean, NOT NULL, server default `false` for the migration
only) landed in one Alembic revision, `6aa4a2f020c6`, on top of Phase 1's head (`d3ed4af188d3`),
following the same pattern Phase 1 used for its four new columns: a server default exists only
to back-fill existing rows, then gets dropped in the same migration so the schema matches
`app/models.py` exactly (new rows always go through the ORM, which always sends an explicit
value). Verified upgrade and downgrade both run cleanly against a throwaway SQLite file.

## Phase 3

### The rule, and what stayed the same

The slate stays `num_games_per_week` games (default 20). A player now submits exactly
`Pool.picks_required` picks (default 15), confidence a permutation of `1..picks_required`,
assigned only to the games they picked. A slate game a player does not pick is legal and
simply is not scored for them. Both numbers are commissioner settings, read from the pool,
never hard coded anywhere (`app/scoring.py`, `app/routers/picks.py`,
`app/services/results.py`, `app/templates/picks.html` all take them as parameters or
context, none of them contain a literal `15` or `20`).

### Exact wording landed on for the count mismatch message

`validate_picks` now has exactly one message for "wrong number of picks," used for both the
short case and the over case, rather than separate phrasing for each direction:

- Short: `"You have picked 14 games. Pick 15."`
- Over: `"You have picked 16 games. Pick 15."`
- Singular is handled (`"You have picked 1 game. Pick 15."`) via a small `_game_word` helper,
  since the old `_number_word` map (a leftover from the deleted "missing a winner" copy) had
  no reason to exist any more and was removed rather than left dead.

One message shape for both directions was chosen over separate "too few"/"too many" copy
because the fix is identical either way (add or remove a pick until the count matches), and
a single terse sentence names the actual problem (the count) without editorializing about
which direction it is wrong in.

### The old "missing a winner" check is gone, not relaxed

`validate_picks` no longer requires one pick per slate game. That check assumed full slate
coverage, which stopped being true the moment `picks_required` could be smaller than the
slate. It is not replaced by a partial version of itself; the single count check above
(`len(picks) != picks_required`) is the only place submission size is validated. A slate
game with no pick is not an error condition at all any more, at any point in
`validate_picks`; it is simply not present in the picks list, and `score_week` already
handles "not present" as "not scored" for that player with no extra code (see below).
`tests/test_scoring.py::test_validate_picks_same_game_picked_twice` is the regression that
pins this: a duplicated pick used to also trigger a separate "One game is missing a winner"
message for the game that lost out to the duplicate; now it only reports the duplicate
itself, because an unpicked game is legal on its own.

### `possible` is scoped to the player's own picks: the no-show refinement

Per the brief, `possible` in `score_week` changed meaning from "countable outcomes on the
whole slate" to "countable outcomes among this player's own submitted picks." This is a
single rule, not a special case for no-shows: a no-show submitted zero picks, so there is
nothing of theirs to count, and `possible=0` falls straight out of the same loop that
computes it for everyone else (`possible` is incremented once per pick that is on the slate
and countable; with zero picks the loop never runs, and the no-show branch returns
`possible=0` explicitly for the same reason). This is a deliberate change from Phase 2's
behavior, where a no-show's `possible` came from the whole slate's countable count. It is
the right call, not a regression, because the alternative is a second, separate rule for
"what does possible mean for a player who picked nothing," which is exactly the kind of
special case Phase 3's own subset-of-the-slate rule was designed to make unnecessary: once
two different players can legitimately cover two different subsets of the same 20 game
slate, "possible" can only mean "counted against outcomes this specific player actually had
a stake in," and a no-show's stake in anything is, correctly, zero. Updated Phase 2's
no-show tests in `tests/test_scoring.py` that asserted the old slate-wide `possible` value
(`test_standard_no_show_scores_zero_and_is_flagged`,
`test_inverse_no_show_takes_the_maximum_penalty_and_is_flagged`,
`test_inverse_no_show_penalty_uses_explicit_picks_required_not_slate_size`), and added
`test_no_show_possible_is_zero_not_the_slate_size` naming the refinement directly. The
no-show max-penalty formula, `sum(1..picks_required)`, is untouched: it already took
`picks_required` as an explicit parameter, never `possible`.

Added `test_a_voided_pick_scores_zero_and_only_reduces_that_players_own_possible` (and an
inverse-mode twin) as the direct test of the brief's central Phase 3 scoring scenario: a
player submits 15 picks, the commissioner voids one of the games they picked after the
fact, and only that player's own `possible` drops (14, not the slate's own count), with
`points`/`correct` on the other 14 picks unaffected.

### `score_week`'s `picks_required` default did not change shape

`picks_required: int | None = None`, defaulting to `len(outcomes)`, is unchanged from Phase
2; it is still only used for the no-show penalty math, never for `possible` (which no
longer has any concept of a slate-wide count to default against). `app/services/results.py`
now passes the real `pool.picks_required` instead of the Phase 2 stand-in
(`pool.num_games_per_week`, with a comment marking it as temporary until this exact phase);
that comment and the stand-in value are both gone.

### `validate_picks` gained a `picks_required` parameter with the same soft-compat shape

`picks_required: int | None = None`, defaulting to `len(slate_game_ids)`. Every real call
site (`app/routers/picks.py`) passes `pool.picks_required` explicitly; the default exists
only so a caller nobody updated does not silently break, matching the pattern already used
for `score_week`. Several pre-existing tests that submitted more picks than the slate
(intentionally, to test the "off slate game" and "duplicate" checks in isolation) now pass
`picks_required` explicitly equal to their own submitted count, so the new count-mismatch
check does not leak an unrelated second error into tests that were not about counts.

### `Pool.picks_required`: new column, one migration, same validation pattern as `scoring_mode`

`Pool.picks_required` (Integer, default 15, NOT NULL). Migration `7f659398d6cc`, on top of
Phase 2's head (`6aa4a2f020c6`), server default `15` for the backfill only, dropped in the
same migration (the ORM always sends an explicit value for new rows), following the exact
pattern the last two migrations used. Verified upgrade and downgrade both run cleanly.
`settings_save` validates `1 <= picks_required <= num_games_per_week` and rejects anything
else with `"Picks required must be between 1 and games per week (N)."`, following the same
required-`Form(...)`-field, flash-and-redirect-on-error pattern `scoring_mode` used in
Phase 2. The settings form field lives in the existing "Slate size" card, right after games
per week, with its own `min`/`max` bounds mirrored from the same rule.

### `app/routers/picks.py`: `n` and `has_full_entry` now mean `picks_required`

Per the brief, `picks_page`'s `n` context key changed from `len(games)` to
`pool.picks_required`, and `has_full_entry` from "every slate game has a pick" to "exactly
`picks_required` picks are in." The row-sort-by-saved-confidence condition in `picks_page`
(which decides whether to show the player's own ranking or fall back to slate order) moved
from `len(picks_by_game) == len(games)` to `== pool.picks_required` for the same reason,
and its sort key was rewritten to put picked games first (by descending confidence) and any
unpicked slate games after, since a `KeyError` was otherwise possible the moment
`picks_by_game` could legitimately be smaller than `games`. `_save_picks` threads
`pool.picks_required` into `validate_picks`, and both `pick_status.html` renders pass
`pool.picks_required` as `n` instead of `len(games)`.

### Template copy: what changed, and what deliberately did not (yet)

`app/templates/picks.html` had two genuinely different numbers hiding under one context
key, `n`: the target pick count (now `picks_required`) and the total published slate size.
Introduced `slate_size` (`games | length`) as its own value in the template for the places
that actually mean "the whole slate" (the page head's "N games" pill, the NFL/college
breakdown), and left every other `n` as `picks_required`, which automatically fixed the
no-show max-penalty formula in the locked view (`(n * (n + 1)) // 2`, unchanged code, now
correct because `n` itself changed meaning to match `score_week`'s own formula) and the
"you staked N points" summary. Reworded "You staked N points on the game at the top and 1
on the game at the bottom" to "...on your top pick and 1 on your last pick," because once
`slate_size` can exceed `picks_required`, the literal last row in the read only list can be
an unpicked slate game, not the confidence-1 pick, so "the bottom of the list" stopped being
a safe way to refer to "the last pick." Reworded the open week "how this week works" panel
to state the rule plainly ("winner for `n` of these `slate_size` games... your most
confident pick stakes `n` points") rather than tying the copy to "the top/bottom of the
list," for the same reason. `pick_status.html`'s error hint and success message both moved
off "every game needs a winner" (no longer true) and "all N games ranked" (no longer
literally "all," since N is now smaller than the slate) to "pick exactly N games" and "N
games ranked."

Known, deliberate limitation, left for Phase 4: `app/static/app.js`'s `renumber()` (the
function that assigns each row's confidence chip while dragging) still numbers every row in
the drag list positionally from the slate size down to 1, unchanged from before this phase,
because Phase 4 is explicitly the phase that rebuilds the picks page into a real "pick N of
the slate" interaction (type numbers, reorder, drag refine, a distinct lock step); building
that selection mechanism here would be exactly the redesign this phase was told not to do,
and the current tap-to-pick control has no way to un-pick a game once tapped, so a truly
correct client side "exactly `picks_required`" flow is not achievable without new controls
Phase 4 is responsible for. What this phase does fix, narrowly: the progress readout
(`updateSummary()` in `app.js`) and the Save button's enable threshold now read a
`data-picks-required` attribute (rendered from `pool.picks_required`, never hard coded)
instead of the total row count, so "X of N winners chosen" and the Save gating are honest
about the real target even while the per row confidence numbering is not yet rebuilt. The
server is the actual authority regardless: `validate_picks` rejects any submission that
does not add up to exactly `picks_required`, whatever the client sent, proven by
`tests/test_app.py::test_server_rejects_too_many_picks_even_if_no_client_would_send_them`.

### `tests/test_app.py`'s `world` fixture: `picks_required` defaults to the fixture's own slate size

`_make_pool` gained a `picks_required` parameter defaulting to `num_games` (the fixture's
own 4 game slate), not the model's real default of 15, so the existing `_valid_submission`
helper (which posts a winner and confidence for every game in `world["game_ids"]`) keeps
producing a complete, valid entry without every existing test needing to know about the new
rule. Tests that want to exercise `picks_required` being smaller than the slate set
`pool.picks_required` explicitly after fetching it from the fixture's pool.
`test_a_missing_winner_is_rejected` was renamed to `test_an_incomplete_submission_is_rejected`
and its assertion moved from the deleted "missing a winner" copy to the new count message,
since dropping one of the world fixture's 4 required picks now produces `"You have picked 3
games. Pick 4."`.

### SPEC.md

Section 1's slate paragraph and Section 8 (Picks and confidence UI) were rewritten to
describe `picks_required` as a real, separate, commissioner-set number from the slate size,
with the default 15-of-20 stated explicitly. Section 8 gained one line naming the later
three-stage entry flow (type numbers, reorder, drag refine, a distinct lock step) as a
future phase, so the spec does not read as though the interaction described in this section
is the final one. Section 9's `possible` sentence was updated to match the scoring change
(scoped to a player's own picks, no-show is 0), a small, adjacent fix since leaving it
stating the old rule would make the spec itself wrong the moment this phase landed; nothing
else in Section 9 changed.

## Phase 4

The gap Phase 3 deliberately left: `app/static/app.js`'s `renumber()` numbered every row in
the drag list positionally from the slate size (20) down to 1, including rows nobody picked.
This phase rebuilds the picks page into the real three stage flow (type numbers, reorder to
inputs, drag to refine) plus a player initiated lock, distinct from Save and from the pool
wide `lock_at`.

### Confidence has two different writers now, on purpose, and they must never fight

The central design problem: a typed number (Stage 1) and a position based drag (Stage 3)
are two different ways of setting the same value, and a naive single `renumber()` that ran
on every interaction would have one silently stomp the other the moment either happened near
the other's row. The fix is two functions with disjoint responsibilities, not one function
with a growing pile of special cases:

- `applyRowConfidence(row, value, opts)` is the only place that writes a row's hidden
  `input[data-conf]`, its `.conf-chip`, and `row.dataset.confidence`, which every other
  function treats as the single source of truth for "what does this row actually submit."
  `opts.syncTyped` additionally overwrites the visible Stage 1 number input; only a position
  based caller passes it.
- `syncRowConfidence(row)` is Stage 1's path: it derives the row's confidence from
  `(team picked ? the typed input's current value : "")` and calls
  `applyRowConfidence(row, that, {})` **without** `syncTyped`, so it never touches what the
  player is mid typing anywhere else on the page. Called from the number input's `input`
  event and from a team button tap (so a value typed ahead of tapping the winner takes
  effect the moment the winner is chosen, without requiring the tap to invent a value for a
  row nobody typed into).
- `renumber(list)` is Stage 3's path: walks the list top to bottom, skips any row with no
  team picked entirely (it is never touched, not even to blank it, since it is already
  blank by construction), and assigns the picked rows `pickedCount` down to 1 in the order
  they appear, overwriting the typed input too (`syncTyped: true`). Called only from an
  actual reordering action: SortableJS `onEnd`, the up/down rank buttons, alt+arrow, and
  "Reorder to inputs" (after that button physically re-sorts the DOM). It is deliberately
  **not** called on page load or on a bare team tap, both of which must leave an
  already-typed or already-saved confidence value alone.

One consequence worth naming: `init()` does not call `renumber()`. It used to (Phase 3's
version renumbered on load too, which is exactly how the whole-slate bug painted every row
on first paint). For a partial entry, the saved rows are not necessarily contiguous at the
top of the list (`picks_page` only reorders games into confidence order once the entry is
*complete*, `len(picks_by_game) == picks_required`; a partial entry falls back to slate rank
order), so renumbering on load by DOM position would silently overwrite genuinely saved
confidence values with position based ones the instant the page rendered. `init()` instead
calls `regroupDivider()` (repositions the "Not picked" divider) and `updateSummary()`
(gating, duplicate check, the meter) and leaves every row's confidence exactly as the server
rendered it.

### The "Not picked" divider: a real, cosmetic, undraggable list item

Per the brief's own framing, the decision to land on: the divider (`<li data-divider>`) is a
genuine sibling inside the same `<ol class="game-list" data-sortable>`, not a second list or
an element positioned outside it. It carries no `.game-grip`, and SortableJS is configured
with `handle: ".game-grip"`, so the divider itself can never be picked up and dragged. A
normal row **can** still be dropped on either side of it by SortableJS, and that is allowed
on purpose: a row's picked/not-picked state is driven only by whether its team button is
tapped, never by which side of the divider it visually ends up sitting on after a drag.
`regroupDivider(list)` repositions the divider (right before the first row with no team
picked, or at the very end if every row is picked, hidden entirely if none are) after every
`renumber()` call and after every `syncRowConfidence()` call, so it tracks reality without
being load bearing for it: if a drag temporarily leaves a picked row below the line or an
unpicked row above it, `renumber()`'s confidence math is completely unaffected, because it
scans by `.team-btn.is-picked`, not by DOM position relative to the divider. This is also
why `move()` (the up/down button and alt+arrow path) needed no special casing for the
divider: swapping a row with the divider as its neighbor just relocates the divider, and the
very next `regroupDivider()` call (inside the `renumber()` that `move()` already calls) puts
it back wherever it actually belongs.

### "Reorder to inputs": duplicates are not an error here, only in `updateSummary()`

`reorderToInputs()` sorts rows with a team picked and a typed value in `1..picks_required`
to the top, by that value descending, stable sort, everything else to the "Not picked"
group. A row typed with a duplicate value is not rejected or skipped at this step, it just
keeps its relative DOM order against its duplicate twin; `renumber()`, which this function
calls immediately after moving rows, then overwrites every picked row with a clean, gap free
`picksRequired..1` sequence based on the new order, which is what "so positions and typed
values agree" (the brief's own phrase) means in practice: the button is exactly what turns a
messy, duplicate-riddled Stage 1 pass into a valid Stage 3 starting point in one tap.

### Stage 1 live validation reuses `updateSummary()`, does not duplicate it

`updateSummary()` already owned the meter, the save button gating, and the "X of N winners
chosen" text (Phase 3). Rather than add a second pass with its own counting logic, it now
also walks the picked rows once for duplicate and out-of-range confidence values, toggles
the existing `.has-error` input state (Section 07 of `app.css`, the same class every other
form field already uses for a bad value, not a new one invented for this) on the offending
`.conf-input` elements, and switches the summary text to "12 of 15 assigned, values 4 and 9
used twice" when a collision exists. The Save button and the new "Lock picks" button share
one `complete` condition (`picked === target && !invalid && !missing && no duplicates`), so
the two buttons can never disagree about whether the entry is postable.

### The lock confirmation: a plain JS panel toggle, not an HTMX round trip

Chose the inline JS panel (`initLockFlow()` in `app.js`, `[data-lock-panel]` starting
`hidden` in the markup) over asking the server for a confirmation fragment first. The
picks_required-picks summary the panel shows is built entirely from state already on the
page (`row.dataset.confidence`, the picked team button's name), so a server round trip would
add latency and a second request for zero new information; the panel's only job is to make
"Lock picks" a two-tap action instead of one, which a client side toggle does for free. The
actual lock is still a real POST: the panel's "Confirm and lock" button is a normal
`hx-post="/picks/lock"` submit inside the same form (`hx-include="closest form"`), so the
server still runs full `validate_picks` and nothing is trusted from the confirmation panel
itself, it is purely a "are you sure" gate in front of the same authoritative POST Save
already uses. On success (`HX-Request` present), the route replies with an `HX-Redirect: /picks`
header instead of a swapped partial: the locked view is a genuinely different page state
(Section 8's read only confirmation branch, not a small save-bar update), so a real
navigation that re-renders `picks_page` from scratch is simpler and more honest than trying
to fake that whole state with a fragment. `/picks/unlock` follows the same pattern. Both
routes also work as plain (non-HTMX) form posts, redirecting 303 back to `/picks`; the
Unlock button in particular needed no `hx-include` at all, since unlocking posts no pick
data, only the action.

### `_save_picks`, `_lock_picks`: one parse, one write, two callers

Refactored the body of the old `_save_picks` into `_parse_submission` (reads the
`winner-{id}`/`confidence-{id}` fields, unchanged behavior: a game with neither present is
skipped, a game with one but not the other reaches `validate_picks` as a real error) and
`_upsert_picks` (writes `Pick` rows and touches `WeekEntry.submitted_at`, never
`locked_at`). `_save_picks` and the new `_lock_picks` both call `_parse_submission` then
`validate_picks(submitted, slate_ids, pool.picks_required)` (the exact same call Phase 3
already made, not weakened, not duplicated) before writing anything; `_lock_picks` is the
only place that ever sets `entry.locked_at`, immediately after `_upsert_picks` returns, so a
rejected lock attempt (wrong count, duplicate confidence, an off-slate game, anything
`validate_picks` catches) writes nothing and never touches `locked_at`, exactly mirroring
what a rejected Save already does. `tests/test_app.py::test_picks_lock_rejects_an_invalid_submission_and_does_not_lock`
pins this. A hand crafted POST to `/picks/lock` is therefore exactly as safe as one to
`/picks`: the same validation function, the same "nothing written on failure" guarantee.

### `WeekEntry.locked_at`: new column, one migration, and how it interacts with the real lock

`WeekEntry.locked_at` (nullable timestamp). Migration `be7a7724eee3`, on top of Phase 3's
head (`7f659398d6cc`), nullable with no backfill needed (an existing row simply has never
been player-locked). Verified upgrade and downgrade both run cleanly against a fresh SQLite
database. `week_is_locked(week)` (the pool wide, clock enforced lock, unchanged by this
phase) is checked **before** `locked_at` everywhere it matters:

- `picks_page`: `locked = week_is_locked(week)`; `player_locked = not locked and
  entry.locked_at is not None`. The instant `locked` flips true, `player_locked` is
  definitionally false, so the template's state 4 branch (read only for everyone, fully
  public on Results) always wins over state 3b (read only for one player, with an unlock
  escape), regardless of what `locked_at` holds. `locked_at` itself is never cleared or
  touched when the real lock arrives; it simply stops being consulted.
- `/picks/unlock`: refuses with a 403 (`"This week is locked. You can no longer unlock your
  picks."`) the moment `week_is_locked(week)` is true, before it ever looks at `locked_at`.
  A time locked week can never be unlocked by the player again, matching the brief's rule
  verbatim. `tests/test_app.py::test_picks_unlock_is_refused_once_the_week_is_time_locked`
  confirms `locked_at` is left exactly as it was (not silently cleared, not silently
  no-op'd into a 200) once that happens.
- `/picks/lock` itself also refuses once `week_is_locked(week)`, same message `_save_picks`
  already used for a locked week, for the same reason Save does: there is nothing left to
  lock once the real lock has already frozen everything.

### `picks.html`: a shared `readonly_list` macro, and the state ordering

Factored the read only slate list (matchup, meta, a confidence chip only on picked games)
out of state 4 into a `readonly_list(games, picks_by_game, tz, label)` macro, since the new
state 3b (player locked, week still open) needed the identical list with only the heading
and surrounding copy different. `{% elif player_locked %}` sits between `{% elif locked %}`
(state 4) and `{% else %}` (state 3, the open editing flow) in the branch chain, which is
what makes the "time lock always wins" rule visible directly in the template, not just in
the Python: Jinja evaluates branches top to bottom, so `locked` is tested and can short
circuit before `player_locked` is ever reached, mirroring `picks_page`'s own
`not locked and ...` guard. Renamed the doc comment at the top of the file from "five
states" to "six," with 3b called out explicitly and a one line pointer to where the
ordering guarantee actually lives.

### Copy

"How this week works" and the "Editable until..." paragraph were rewritten to describe the
three stage flow (type, reorder, drag) and the new lock/unlock language, replacing every
remaining sentence that described the old single-stage "tap then drag the whole list" flow.
Neither `15` nor `20` appears literally anywhere touched; `n` (`picks_required`) and
`slate_size` are threaded through exactly as Phase 3 established.

### Tests

**642 passed**, 0 failed (636 at the Phase 3 baseline, 6 net new, all router level since
`app/scoring.py` is untouched by this phase). New coverage in `tests/test_app.py`:
`test_picks_lock_with_a_valid_submission_saves_and_locks`,
`test_picks_lock_rejects_an_invalid_submission_and_does_not_lock`,
`test_picks_lock_is_refused_once_the_week_is_time_locked`,
`test_picks_unlock_clears_the_lock_while_the_week_is_still_open`,
`test_picks_unlock_is_refused_once_the_week_is_time_locked`, and
`test_picks_page_renders_in_every_state`, which walks one player through all five reachable
GET `/picks` states in order (nothing entered, a genuinely partial entry written directly
since Save cannot produce one, fully entered but unlocked, player locked, then time locked)
and asserts the state-specific markers (`"Reorder to inputs"`, `"Unlock to edit"`,
`"data-sortable"`) appear or disappear exactly where they should, including confirming
`locked_at` survives untouched once the real lock takes over. The existing
`test_member_pages_render` (an authenticated player against an open week with a full slate)
already served as this repo's boot-and-load smoke test for `/picks`; it needed no change
since the open state it exercises still renders under the rebuilt template. `app/scoring.py`
and `validate_picks` are unchanged by this phase, so no new tests were added there, per the
brief.

## Phase 5

Two pieces of pool feedback, both about who gets to decide the slate: closest-spread alone
routinely drops a rivalry game the moment either side is having a lopsided season, and
automation building *and opening* a week by itself was more than the group actually wanted.
`app/slate.py` gained a `pinned` field that guarantees a candidate survives selection no
matter how wide its spread is; `Pool.auto_publish` defaults to `False`, so a build now
always produces a draft the commissioner reviews and publishes by hand, unless they
explicitly turn automatic opening back on.

### Eligibility requires a resolvable spread, even for a pin

A pinned candidate with no resolvable spread, or one that has already kicked off, is
dropped by the same eligibility filter every other candidate goes through, before pins are
even looked at. **Why:** `Selected.closeness` is a real `float`, not optional, and every
downstream consumer (slate rank ordering, the admin "closest (spread X)" reason text) reads
it as one; a pin cannot "always include" a game the rest of the pipeline has no number to
rank. This is not a corner the brief left open to interpretation, it names the exact reason
(`Selected.closeness needs a real number and the rest of the pipeline assumes one`), and it
matches this codebase's existing rule that a game with no spread has no closeness and cannot
be added to the slate at all (`add_to_slate`/`swap_slate_game` already reject a game with
`spread_home is None` for the identical reason). Practically: a rivalry game with no line
yet still auto-pins itself (`Game.pinned=True` is a durable, independent flag, unaffected by
whether a spread has resolved), it simply will not appear on the slate until a spread
exists, exactly like an ordinary unpinned game with no line. Covered by
`tests/test_slate.py::test_a_pin_with_no_resolvable_spread_is_not_eligible_to_force_inclusion`
and the started-game twin.

### `select_slate_by_targets`: pins seeded into the first pass, not a second pass bolted on

Per the brief's own steer, pins are seeded into `chosen_keys`/`first_pass` *before* the
existing per-league fill loop runs, and that loop only takes `max(0, target -
first_pass.get(league, 0))` more games. This is the one place real algorithmic judgment was
needed: the existing "drop the farthest when over total" step (`chosen[:total]`, since
`chosen` is already closest-first) would otherwise cut a pinned game the instant it is the
widest spread in the final set, since a pin can rank anywhere, including dead last. Fixed by
splitting the trim's tail-drop to only ever remove *unpinned* games
(`app/slate.py`, the `if len(chosen) > total:` branch): `excess = len(chosen) - total`
unpinned games are dropped from the tail, which is always possible because the len(pinned) >
total case already raised before this point runs. Verified with
`test_a_pinned_candidate_survives_a_trim_even_as_the_farthest_game`. Everything else
(the fill-from-the-other-league branch, `per_league`, `shortfalls`, `notes`) needed no
change: `first_pass[league]` is updated to `pinned_count + additional_taken` after the loop
(not left at just the pinned count), which is what keeps the shortfall math honest when a
league's pins do not use up its whole target and the rest of its supply genuinely falls
short.

### `Selected.pinned`: added to the dataclass, but `apply_slate` never writes `Game.pinned`

`Selected` gained a `pinned: bool = False` field (mirroring `Candidate.pinned`) as the brief
allowed. It turned out not to be needed for round-tripping state back onto the `Game` row:
`Game.pinned` is the input to selection (read into `Candidate.pinned` in `apply_slate`), and
`select_slate_by_targets` never invents a pin, it only guarantees one that was already set
survives. So there is nothing to write back; a pinned game that missed this build (no
spread yet, already kicked off) simply keeps `Game.pinned=True` on the row, waiting for the
next rebuild, exactly like a commissioner's own manual pin would. `Selected.pinned` is kept
anyway because it is genuinely useful to any future caller of `app/slate.py` that wants to
know which selected games were forced rather than chosen by closeness, and because the brief
explicitly offered it as an acceptable shape; it is simply unused by `apply_slate` today.

### Rivalry auto-pin: fires once, on row creation, never on an update

Chose (a) from the brief's own framing: `upsert_games` (`app/services/ingest.py`) only
checks a game against `pool.rivalries` inside the `if row is None:` (brand new game) branch.
An existing row is never re-checked. **Why:** a commissioner who deliberately unpins a
rivalry game while reviewing a draft has made a real decision; re-applying the auto-pin on
every later rebuild of the same event id would silently overturn it every time the cron
runs, which is worse than the problem this feature is solving (the whole point of Phase 5 is
that automation should defer to the commissioner, not override them). Proven directly by
`tests/test_ingest.py::test_a_commissioners_manual_unpin_of_a_rivalry_game_survives_a_rebuild`
(create, unpin, upsert the identical game again, still unpinned) and by
`test_a_new_rivalry_game_does_not_resize_a_frozen_slate` for the freeze interaction: once
picks exist, `build_slate`'s locked-out branch still calls `upsert_games` (so a genuinely
new rivalry matchup that shows up in that week's fetch still gets pinned for a *future*
build), but it never calls `apply_slate`, so that pin cannot grow or reorder the slate that
players have already picked against.

### The seeded rivalry pairs, and how the canonical keys were derived

Seeded matchups: Ohio State vs Michigan and Auburn vs Alabama (named directly by the group),
plus Army vs Navy, Michigan vs Michigan State, Florida vs Georgia, Texas vs Oklahoma, and USC
vs Notre Dame (the rest of the obviously-same-shape rivalries). Every key is the real return
value of `canonical_key(name, "ncaaf")`, called at import time inside `app/models.py`
(`DEFAULT_RIVALRIES`), never a hand typed slug:

```
canonical_key("Ohio State", "ncaaf")    -> "ncaaf:ohio-state"
canonical_key("Michigan", "ncaaf")      -> "ncaaf:michigan"
canonical_key("Auburn", "ncaaf")        -> "ncaaf:auburn"
canonical_key("Alabama", "ncaaf")       -> "ncaaf:alabama"
canonical_key("Army", "ncaaf")          -> "ncaaf:army"
canonical_key("Navy", "ncaaf")          -> "ncaaf:navy"
canonical_key("Michigan State", "ncaaf")-> "ncaaf:michigan-state"
canonical_key("Florida", "ncaaf")       -> "ncaaf:florida"
canonical_key("Georgia", "ncaaf")       -> "ncaaf:georgia"
canonical_key("Texas", "ncaaf")         -> "ncaaf:texas"
canonical_key("Oklahoma", "ncaaf")      -> "ncaaf:oklahoma"
canonical_key("USC", "ncaaf")           -> "ncaaf:usc"
canonical_key("Notre Dame", "ncaaf")    -> "ncaaf:notre-dame"
```

`tests/test_ingest.py` proves the match logic against these exact values by calling
`canonical_key(...)` itself rather than pasting the strings above, so a future change to
`app/providers/teams.py`'s alias tables cannot silently make this list (or the tests) stop
matching real games without a test failing first.

### Where a fresh pool's rivalry list actually gets set: the model default, not `seed_admin`

`Pool.rivalries`'s `mapped_column` default is `_default_rivalries()` (a fresh copy of
`DEFAULT_RIVALRIES` per row), not an empty list. **Why:** the brief's own phrasing, "wherever
a fresh pool's defaults are established," already describes exactly how every other
opinionated pool default in this codebase works (`sports` defaults to
`["nfl", "ncaaf"]`, `target_nfl`/`target_ncaaf` default to 8/12, not to nothing), so the
curated list living on the column default is consistent with that pattern rather than a
special case bolted onto `seed_admin`. `app/cli.py`'s `seed_admin` needed no new code for
this as a result: it already omits `rivalries=` from its `Pool(...)` call (matching the fix
already required for `auto_publish`), so a freshly seeded pool gets the curated list for
free. The Alembic migration additionally back-fills the same curated list onto any pool row
that predates this column (a real, if small, concern here since this repo's own local/demo
pool already exists), rather than back-filling an empty list and asking the commissioner to
type the whole thing in by hand.

### `auto_publish`'s migration: no schema change needed, and why that is not a shortcut

The brief calls for "a migration ... for the column default." `Pool.auto_publish` has never
carried a `server_default` at the database level (`alembic/versions/139ee0ca88d8_initial_schema.py`
defines it as plain `NOT NULL` with none); the `default=True` that just flipped to
`default=False` in `app/models.py` is a Python-side, ORM-only default, applied only when a
new row is inserted through SQLAlchemy. Alembic's own `--autogenerate` confirms there is
nothing to diff: the column's type, nullability and constraints are byte-for-byte unchanged.
So the actual migration added in this phase (`6b6eab096a56`) touches only the two genuinely
new columns, `games.pinned` and `pools.rivalries`; `auto_publish`'s flip is enforced entirely
by `app/models.py`'s default plus the `app/cli.py` `seed_admin` fix (the hard coded
`auto_publish=True` removed from its `Pool(...)` call), both covered by
`tests/test_cli.py`. This is not a corner cut to avoid writing a migration: there is
genuinely no schema to alter, and inventing a `server_default` this phase never asked for
would only create a second, redundant place for the default to live and drift out of sync
with the model.

### Rivalry list editing: college matchups only, one "Team A vs Team B" per line

The `/admin/settings` textarea (`app/routers/admin.py`'s `_parse_rivalries`) always resolves
both sides through `canonical_key(name, "ncaaf")`. **Why:** every rivalry named in the brief
and every one in the seeded default list is a college matchup; the product does not yet need
a way to pin an NFL rivalry, and a league selector for a feature this small would add a
second form control for a case nobody asked for. A line with no recognisable "vs"/"vs."
separator, or a blank line, is skipped rather than rejected, since a bad line in a rivalry
list is a low stakes typo, not something worth blocking the rest of the settings save over;
`canonical_key` itself never raises, so an unrecognised team name is still stored (as its own
slug), it simply will not match any real game until the spelling is fixed. The reverse
direction, rendering the saved keys back into readable names for the textarea
(`_rivalries_text`), uses `app.providers.teams.display_name`, falling back to the raw key for
anything it does not recognise, so a hand-edited or stale key is still visible rather than
silently disappearing from the form.

### `slate_build`'s new failure mode: pins summing past `num_games_per_week`

`select_slate_by_targets` now raises `ValueError` when more games are pinned than the slate
total allows, per the brief. `POST /admin/slate/build` (`app/routers/admin.py`) did not
previously need a `try/except` around `ingest.build_slate` at all, since nothing in the old
pipeline could raise; added one, following the exact flash-and-redirect pattern
`slate_game_action` already uses for `SlateLocked`/`ValueError`, rather than letting a
misconfigured pin count crash the request with a 500. The CLI paths (`build-slate`,
`sync-week`, `run-cron`) were deliberately left without new exception handling: a pin count
exceeding the total is a real, self-inflicted configuration error a commissioner needs to
notice and fix immediately (reduce pins or raise `num_games_per_week`/a league target), and a
loud CLI traceback naming the exact problem is the correct behavior there, matching how this
codebase's other configuration errors already surface (`_resolve_pool`'s `typer.BadParameter`
for a missing pool, for example). No test pins a specific CLI traceback shape as a result;
the pure-function `ValueError` and the one admin-router catch are what is tested.

### Where "why is this game on the slate" is computed

Per the brief, no `pin_reason` column: `ingest.slate_reason(game, pool)` computes "Pinned" /
"Rivalry" / "Closest (spread X, source Y)" at render time from `Game.pinned` and a live
`canonical_home_key`/`canonical_away_key` match against `pool.rivalries`, so editing the
rivalry list retroactively changes how an already-pinned game explains itself the next time
the slate editor is loaded, with no backfill needed. `app/routers/admin.py`'s `slate_page`
computes one `reasons: dict[game_id, str]` for the games actually on the slate and hands it
to the template; the candidates table (games not yet on the slate) only needs a plain
"Pinned" badge, not the full reason, since "why would this be on the slate" does not apply to
a game that is not there yet.

### Tests

**664 passed**, 0 failed (642 at the Phase 4 baseline, 22 net new: 12 in
`tests/test_slate.py` for pin selection, trimming, league expansion/shrink, the over-total
`ValueError`, the zero-pins regression, and shuffling with pins present; 8 in
`tests/test_ingest.py` for rivalry auto-pin in both home/away order, the no-rivalries-
configured case, the manual-unpin-survives-a-rebuild case, and the two freeze-rule checks;
2 in the new `tests/test_cli.py` for `seed_admin`'s `auto_publish` default end to end,
including that a commissioner's own later choice is not silently reset on a rerun, since
`seed_admin` is designed to run on every boot). `ruff check .`, `black .` and the full suite
all clean. An em dash search across every file touched this phase finds nothing; no emoji
were added anywhere.

## Phase 6

Three pieces of direct feedback from the group actually running the pool: Standings and
Results both showed a weekly leaderboard, which read as the same page twice; there was no
way to sort a table by anything other than the server's default order; and the results pick
grid had rows and columns backwards versus the old platform they were used to (rows should
be players, columns should be confidence 20 down to 1, cells the matchup).

### The reveal rule had to be extended to the weekly leaderboard, not just the pick grid

The brief's architecture section says to reuse `weekly_leaderboard(db, pool, week=row,
viewer_id=user.id)` on `/results` and pass the page's own resolved `row`. Read literally and
implemented without more thought, that call has no gate at all: `weekly_leaderboard` returns
one `WeeklyRow` per pool member regardless of whether the week has locked, because a
`WeekEntry` row is created the moment a player calls Save (`app/routers/picks.py`, `now`
written to `submitted_at`), well before scoring or lock. Wiring the leaderboard section up
unconditionally made `test_picks_stay_private_until_lock` fail: a player who saved (but had
not locked) picks for an still-open week showed up by name in the weekly leaderboard table
on another member's `/results` page, with 0 points and no "did not submit" flag, because
`did_not_submit` itself is only set at scoring time. That is a real information leak the
brief's "picks stay private until lock" rule was written to prevent, just via a side door
("who has an entry at all this week") the pick grid itself never opens. Fixed by computing
`weekly` only inside the same `if revealed:` branch that already gates `_build_columns`, so
the weekly leaderboard section renders its empty state ("opens once this week locks") for
exactly as long as the pick grid stays behind its own lock notice. This is the one place this
phase's implementation deviated from a literal reading of the brief, and it is deviating
toward the existing, tested privacy rule, not away from it.

### Where the per-confidence lookup lives

`PlayerColumn` gained `by_confidence: dict[int, tuple[Game, PlayerPick]]` (`app/routers/
results.py`), built inside the same per-game loop in `_build_columns` that already builds
the game-major `picks` dict keyed by `game_id`: every time that loop constructs a `PlayerPick`
cell for a game the player actually picked (`pick is not None`), it now also writes
`by_confidence[pick.confidence] = (game, cell)`. No second pass over `games` and no query
against the whole slate, since a player's confidence values only exist for the games they
actually picked, exactly as the brief specifies. A player who picked fewer than
`picks_required` games (an incomplete card, still possible if `picks_required` was lowered
after they saved, or they simply never finished) leaves the unused confidence values absent
from the dict; the template's `col.by_confidence.get(c)` returning `None` is what renders the
"genuinely empty cell" the brief asks for, not a sentinel or a placeholder dash (the by-game
grid's "no pick for this game" dash, `.pick-none`, is a different situation, a real absence
during a real game, and keeps its own marker unchanged).

### Both pick grids render server side; the toggle is a pure visibility switch

`results.html` renders the by-player table and the original by-game table in full, every
time the week is revealed, inside two `[data-view-panel]` containers. `app.js`'s
`initViewToggle` does nothing but set the native `hidden` attribute on whichever panel is not
selected and flip `aria-pressed` on the two buttons; there is no conditional server side
render based on a query param or session state. Two consequences worth being explicit about:
a router test can assert the by-game table's markup exists in the response regardless of
which view a real browser would show first (`test_results_grid_is_player_major_with_
confidence_columns_and_game_major_toggle` checks for `data-view-panel="game"` and the
by-game table's `pick-player` header class directly in the HTML), and there is no flash of
the wrong view before JS attaches: the by-game panel ships with `hidden` already set in the
template and the "By game" button already carries `aria-pressed="false"`, so the DOM's resting
state matches what `initViewToggle` would set it to anyway. The cost is doubling the amount
of grid markup in every revealed `/results` response; accepted deliberately, per the brief
("Render both tables server side (simplest, no extra request)"), over a client side
re-render from a JSON payload, which would have meant hand rolling the state/void/pending
color logic twice, once in Jinja and once in JS.

### The sortable table engine: one function, two triggers, and why aria-sort is seeded server side too

`app.js`'s `initSortableTable` is the only place row order or `aria-sort` gets decided; both
a header click and the mobile `<select>`'s change event call the same `render(true)` path, so
there was never a second copy of the comparison or the DOM reorder to keep in sync. Numeric
columns carry `data-sort-value` on every `<td>` precisely because this app's own tables print
non numeric placeholders in numeric columns ("No entry", "."); rather than inventing a
sentinel for those rows, `data-sort-value` is simply set to the same underlying number the
row's rank already reflects (a "No entry" player's `points` is `0`, matching where they
already sort under the server's own order), so no special casing was needed in the template
or the JS.

One thing beyond the brief's literal JS-only description: `aria-sort` and `data-sort-value`
are seeded directly in the template on the default sort column, not left for JS to set on
first paint. Two reasons. First, accessibility should not depend on JS having attached yet;
a screen reader hitting the table before `app.js` runs (or if it fails to load) should still
hear the correct current sort state, since the rows are, in fact, already in that order,
courtesy of `season_standings`/`weekly_leaderboard`'s own server side sort. Second, the test
brief explicitly asks for "the expected `data-sort-value`, `aria-sort` scaffolding" to be
verifiable in server rendered HTML, which a JS-only `aria-sort` cannot satisfy since pytest
never executes `app.js`. `initSortableTable`'s own initial `render(false)` call does not
re-sort the DOM (the server's order already stands and carries tiebreakers, correct/weekly
wins/name, the client's single-column comparator does not know about), it only re-derives the
same `aria-sort`/caret state the template already wrote, which is deliberately redundant
rather than a source of truth living in two places that could drift.

### Tests

**668 passed**, 0 failed (664 at the Phase 5 baseline, 4 net new, all in
`tests/test_app.py`): `test_standings_page_has_no_weekly_leaderboard` (asserts the word
"leaderboard" and the old section's heading id are both actually gone, not hidden);
`test_results_weekly_leaderboard_matches_the_selected_week` (two weeks, two different
`WeekEntry.points`, `?week=5` and `?week=6` each show only their own week's number via
`data-sort-value`); `test_results_weekly_leaderboard_stays_private_until_lock` (the
regression this phase almost introduced, see above); and
`test_results_grid_is_player_major_with_confidence_columns_and_game_major_toggle` (a two
player, four game fixture with a correct, a wrong, a void and a not-yet-final pick, checking
the exact `<strong>H0</strong> over A0` matchup markup at the right confidence column, the
`pick-void-badge` marker, an empty-cell "No pick at confidence 1" for a player who only
picked three of four games, and that the by-game table's markup is still present in the same
response). `ruff check .` and `black .` both clean. An em dash search across every file
touched this phase finds nothing; no emoji were added anywhere; the sort caret is inline SVG
built from the same path data as `components/icons.html`'s existing "up"/"down" chevrons, not
a text glyph.

## Phase 7

Two pieces of direct feedback: "Will be Venmo only this year. No Venmo, no participation,"
and "1 person to pay, no multiple accounts," plus a description of a payout column the group
used on their old platform ("what #1, 2, 3, 4 received each week, for the special Bowl Week,
and end of season awards"). This phase adds a Venmo entry gate in front of picking and a
`PayoutRule` system that drives a payout column on Results, an awards panel on Standings, and
a plain payout summary table, all fed by numbers a commissioner types in by hand. No payment
processor was touched: Venmo is a deep link and manual commissioner reconciliation, exactly as
scoped, so this phase needed zero new Python dependencies.

### The numeric type for money: `Float`, matching `spread_home`/`closeness`, not `Numeric`

`Pool.entry_fee`, `PayoutRule.amount` and every dollar figure downstream use SQLAlchemy
`Float`, the same convention `app/slate.py`'s `spread_home` and `closeness` already established
for a precision sensitive number in this codebase. Rejected `Numeric`: it buys exact decimal
storage, but every place this phase actually needs exactness (splitting a tied payout to the
cent, so the total reconciles exactly) is arithmetic that happens in Python, not a database
comparison or aggregate that would benefit from `Numeric`'s guarantees, and introducing a
second numeric convention alongside the one this codebase already has for money-adjacent
figures would be an inconsistency with no real payoff. The actual precision guarantee lives in
`app/services/payouts.py.allocate_payouts`: every dollar amount is converted to integer cents
(`round(amount * 100)`) before any split or sum, divided with `divmod`, and converted back to
dollars only at the very end, so a float's binary representation never has a chance to drift a
stored or displayed figure. `payout_summary` and the weekly/season payout helpers all round to
2 decimal places at the point a figure is finally added to a running total, for the same
reason. Every place a dollar amount reaches a template goes through the new `money` Jinja
filter (`app/templating.py`), which itself rounds through cents before deciding whether to
show a trailing `.XX`, so a float artifact (`639.9999999999999`) can never leak into what a
player or commissioner reads.

### The tie split rounding rule, exact wording

"Split the combined amount for the tied places evenly, rounded to the cent, with any remainder
cent(s) going to whichever tied player submitted earliest." Implemented literally in
`allocate_payouts`: a tied group at rank R with G members occupies places R through R+G-1
(competition ranking, the same ranks `season_standings`/`weekly_leaderboard` already assign,
never recomputed here); their amounts are summed in integer cents, divided by G with
`divmod`, and the `remainder` leftover cents go one each to the first `remainder` entries in
the caller's own ordering. For a single week that ordering is `WeekEntry.submitted_at`
ascending (`app/services/payouts.py.weekly_payouts`); a player with no submission timestamp at
all sorts last within their tied group, never first, since there is no real "earliest" to
credit them with. Every test in `tests/test_payouts.py` that exercises a tie writes the cent
arithmetic out in a comment and asserts the split reconciles to the combined total exactly, not
just against a pinned magic number, per the brief's own instruction.

Season awards have no per-player submission timestamp to break a tie with (there is no single
instant "the season started" for a player the way there is for one week), so
`season_payouts` reuses `season_standings`' own final tie-broken order (points, then correct,
then weekly wins, then display name) as the remainder-cent tie break instead. This is not
arbitrary: it is the exact order the season standings table itself already displays a tied
group in, so "the leftover cent goes to whoever sits first in the tied group above" reads as
the same rule players can already see on the page, not a second, invisible tiebreak.

### Season awards panel gating: "at least one week has been scored," not a season-complete flag

There is no field anywhere in this data model asserting a season is officially over (no
`Pool.season_ended_at`, no explicit commissioner action that closes a season). The brief named
this ambiguity directly and asked for a documented call. Chosen: the season awards panel on
`/standings` shows whenever at least one member has `weeks_played > 0` in the current season
standings, and its own heading and copy state plainly that it reflects the current standings,
not a final result ("Season awards, as it stands... It will keep moving until the last week is
scored").

Rejected alternatives, and why:

- **Gate on every week being scored.** There is no fixed, known week count anywhere in this
  codebase (`Pool` has no `total_weeks` field, a college season and an NFL season do not even
  share a week count under this pool's own per-league resolution from Phase 1), so "every week"
  has nothing concrete to compare against. Inventing a week count just to gate this one panel
  would be a second, parallel notion of "the season" that nothing else in the product uses.
- **Add a new field, a real season-complete flag or date, and gate on that.** This would be a
  reasonable long term feature, but it is out of this phase's actual scope (a payout gating
  decision, not a new season lifecycle concept), and a flag nobody sets defaults to "never
  complete," which would make the awards panel permanently invisible for every pool until a
  commissioner discovers and flips a switch nothing else in the settings page currently asks
  about. That is worse than showing a clearly labelled in-progress panel.
- **Never show it until manually confirmed by the commissioner.** Same problem as above, plus
  it adds a manual step for a panel whose whole value is to already tell a commissioner "here
  is what everyone is owed" without extra clicking.

"At least one week scored" is the cheapest signal that is actually true today (the standings
themselves already read as more than a blank page once this is true), and pairing it with
honest, prominent copy avoids the actual risk raised by not having a real "season is over"
signal, which is a panel that reads as more final than it is.

### `payment_required_to_pick` defaults to `True`, and what that meant for the existing test fixture

`Pool.payment_required_to_pick` defaults to `True` at the model level, per the brief's literal
architecture section, so a brand new pool requires payment before anyone can pick, exactly as
written, with no fee or handle pre-filled (see the house rule against hard coded dollar
figures). This meant every pre-existing router test that posts real picks through
`tests/test_app.py`'s `world` fixture would otherwise start failing the moment this column
existed, since the fixture's pool never explicitly requires or waives payment. Fixed the same
way Phase 3 handled `picks_required`: `_make_pool` gained a `payment_required_to_pick`
parameter defaulting to `False` (documented inline, mirroring `picks_required`'s own comment),
so the fixture's default behavior is unchanged unless a test deliberately turns the gate on to
prove it works. The model's own real default is untouched; only the test helper's default
differs from it, which is exactly the same shape Phase 3 already established as acceptable for
this codebase's test fixtures.

### Where a member's own Venmo handle gets recorded: a new small route, not specified verbatim by the brief

The brief names `PoolMember.member_venmo_handle` ("optional, for the commissioner's own
reconciliation") but does not name a specific route or form for setting it. Added
`POST /admin/members/{id}/venmo-handle`, a small per-row form on `/admin/members` next to the
paid toggle, following the exact same resolve-verify-act-commit-flash-redirect shape
`member_role`/`member_remove` already established. This is the natural place for it: it is
where a commissioner is already looking at payment status row by row, and it is the same page
the duplicate-handle warning badge renders on, so entering a handle and immediately seeing
whether it collides with someone else's is a single visit, not a hunt across two pages.

### The duplicate Venmo handle check: informational only, case and whitespace insensitive

`_duplicate_venmo_member_ids` (`app/routers/admin.py`) compares `member_venmo_handle` values
trimmed and lowercased, so "PatSmith" and " patsmith " still flag each other, and never treats
an empty handle as a match (two members with nothing on file are not a duplicate account,
they are just two members nobody has noted a handle for yet). Per the brief, this only ever
adds a visible badge next to both flagged rows; it does not block marking either member paid,
removing them, or anything else, since the group's "no multiple accounts" rule is a policy a
commissioner enforces by talking to their players, not something this tool can safely
adjudicate on its own (a family sharing one account legitimately, for example, is a real
possibility this tool has no way to distinguish from a rule violation).

### The payout rule editor: add and remove only, no in place edit

Per the brief's own "keep it simple" steer, `/admin/settings`'s payout editor is a table of
existing rows per scope plus one small add-a-row form; there is no edit-in-place action.
Fixing a typo is a remove followed by a re-add. This keeps the feature to the two routes the
brief actually asks for (`POST /admin/payouts/rule`, `POST /admin/payouts/rule/{id}/remove`)
rather than a third PATCH-shaped route and a second form layout for the same three fields.

### Pot validator: warn only, computed straight from real numbers, never blocking a save

`_pot_totals` (`app/routers/admin.py`) computes collected as `pool.entry_fee (or 0) times the
paid member count`, and allocated as the sum of every `PayoutRule.amount` for the pool across
every scope, and renders a warning banner (`flash-error` styling, reusing the same conditional
flash pattern the existing "league counts add up" banner on the same page already established)
whenever the two disagree by more than half a cent. Settings save itself does not read these
numbers at all, so there is no code path where a mismatch could block a save even
accidentally; the brief was explicit that a commissioner may deliberately hold back a reserve
or write payout rules before everyone has paid, both of which are honest reasons for the two
totals to disagree.

### Tests

**704 passed**, 0 failed (668 at the Phase 6 baseline, 36 net new: 19 in the new
`tests/test_payouts.py` for `allocate_payouts`'s tie-split arithmetic, `week_payout_scope`,
`week_is_complete`, and the database backed helpers including the bowl-routes-to-bowl-not-
weekly case and a full `payout_summary`; 17 in `tests/test_app.py` for the Venmo gate
(unpaid-cannot-save, unpaid-cannot-lock, paid-can-do-both, payment-not-required-means-anyone,
the blocking panel actually rendering), the admin member tools (paid toggle, bulk mark paid,
the duplicate handle warning firing only for a real shared handle), settings persistence for
the new fields, the payout rule add/remove routes, the pot validator banner appearing only on
a real mismatch, the Results payout column (present with rules, absent without, and the named
bowl-scope-not-weekly-scope case), and the Standings season awards panel appearing only once a
week is scored). `ruff check .` and `black .` both clean. An em dash search across every file
touched this phase finds nothing; no emoji were added anywhere; no dollar amount or Venmo
handle is hard coded anywhere outside a test file.

## Phase 8

The feature the group's own feedback named directly: "Once 5 games were completed, you
could see how many different scenarios got you placed for the week... your percent chance
at 1, 2, 3." This phase adds `app/scenarios.py` (the pure scenario engine), a database
touching caller, a Scenarios panel on Weekly Results with a probability model toggle, and
a build-your-own-scenario panel. Zero new Python dependencies, only the standard library
(`itertools`, `random` with an explicit seed, `math.erf`, `time`).

### Where the database-touching caller lives, and why

`app/services/scenarios.py`, a new module, not folded into `app/services/results.py`.
Both are plausible homes (the brief offered either), but `results.py` already owns two
distinct jobs (`fetch_results`, `score_week_for_pool`) that write to the database on every
call; the scenario caller is read only end to end (it never writes a row) and carries its
own, real piece of state (the in process cache). Keeping it separate means the cache's
lifetime and the `week_scenario_panel`/`custom_scenario_standings` entry points are the
whole surface of the new module, easy to reason about in isolation, and it mirrors the
existing pure/impure split exactly: `app/scenarios.py` is to `app/services/scenarios.py`
what `app/scoring.py` is to `app/services/results.py`.

### The moneyline reality: nothing in this codebase's recorded data has ever carried one

Checked directly, twice: the fixture the brief already flagged
(`espn_nfl_upcoming_with_odds.json`, `spread`/`details`/`homeTeamOdds`/`awayTeamOdds` only,
`favorite` boolean, no moneyline anywhere) and every other fixture in `tests/fixtures`. The
only moneyline-shaped keys anywhere in the fixture set (`homeMoneyline`/`awayMoneyline`) are
in `cfbd_lines_2025_w5.json`, CollegeFootballData's own shape, unrelated to `app/providers/espn.py`.
Spec Section 5a documents ESPN's odds object precisely (`details`, `spread`, `overUnder`,
`homeTeamOdds`/`awayTeamOdds.favorite`) and does not mention a moneyline field either.

`espn.parse_moneyline_item`/`moneyline_from_items` read a `moneyLine` key at the same
nesting as `favorite`, a reasonable, best-guess key name for a real ESPN payload that might
carry one, but genuinely unverified: no fixture in this codebase confirms it, and it may
need adjusting once a real payload with a moneyline actually lands. The tests for this path
(`tests/test_providers.py`, the "parse_moneyline_item / moneyline_from_items (Phase 8)"
section) are built on hand constructed, clearly synthetic payloads, commented as such,
following the precedent `test_providers.py`'s own `_synthetic_event` already set for a
payload this codebase has never actually observed. One test
(`test_recorded_upcoming_fixture_has_no_moneyline`) runs the parser against the real
recording and asserts both moneyline fields come back `None`, pinning the documented
reality rather than papering over it. `Game.home_moneyline`/`Game.away_moneyline` are
populated opportunistically in `upsert_games` and never cleared once set (mirroring the
existing spread handling, since Spec 5a's "odds disappear once a game goes final" applies
identically to a moneyline). On real, live traffic today these columns are simply null end
to end, and `app.scenarios.win_probability` falls back to the spread derived normal CDF,
which is the honest, currently-exercised path, not the moneyline one.

### Performance: why `app/scenarios.py` does not call `score_week` once per scenario

The brief's own instruction is "score every scenario with `score_week`... never
reimplement scoring logic." Taken completely literally (one `score_week` call per player
per scenario) this measured 5.45 seconds for 15 remaining games and 16 players alone, on
the build machine, well over the 2 second hard cap, before Monte Carlo, leverage, or
anything else. Scoring a confidence week is a **sum over independent picks**
(`score_pick` has no cross terms between games), so the actual implementation still
computes every scenario's real total through `score_week`/`score_pick`, it just avoids
redundant, identical calls:

1. One `score_week` call per player over the already-final outcomes gives a constant base
   (and correctly handles the no-show penalty, itself scenario-invariant).
2. Two more `score_week` calls per (player, remaining game), one per side, on that single
   pick in isolation, give the exact marginal point delta that game contributes,
   independent of every other remaining game's outcome.
3. Every scenario's total is `base + sum of the deltas for whichever side each remaining
   game lands on`. This is exact, not an approximation of what `score_week` would return:
   it is the identical sum, computed once per (player, game, side) instead of once per
   (player, scenario). `tests/test_scenarios.py::test_linearization_matches_brute_force_score_week_on_every_scenario`
   cross checks this decomposition against the literal brute force per-scenario
   `score_week` call across every one of `2**3` scenarios for three players (one a
   no-show, one who skips some games), so this is verified, not just argued for.

The sweep over `2**R` scenarios (exhaustive) still costs real time even with that
optimization, so `_build_points_array`/`_build_weight_array` build each player's full
scenario array via repeated doubling (`arr = [x + away for x in arr] + [x + home for x in
arr]`, extended once per remaining game) rather than an explicit `itertools.product` loop
with a `score_week`-shaped call inside it: R = 15, 16 players measures around half a
second end to end on the build machine this way, comfortably under the cap; the naive
per-scenario `score_week` approach did not fit at all. R = 20, 16 players (the largest
`MAX_EXHAUSTIVE_REMAINING` case) measured 1.8+ seconds for the array build alone under
this same approach, which is why the exhaustive path also needs a real mid-run time
budget check (see below), not just a fast implementation.

### Monte Carlo: a chunked lookup table, and why sample count is not decided by watching the clock

The naive Monte Carlo loop (draw R random bits, sum R deltas per player, per sample) measured
roughly 11.7 seconds for 200,000 samples at R = 24 with 16 players, again far over budget.
`_build_chunks` groups the remaining games into fixed size bit chunks (12 bits, a 4096 row
table per chunk, the measured knee of the build-cost/lookup-cost tradeoff) and precomputes
each chunk's point contribution per player for every one of that chunk's local bit patterns,
via the same doubling trick as the exhaustive path. A sample then costs O(chunks times
players) table lookups and additions instead of O(remaining games times players) work,
which brought the core sampling loop down to roughly 1.2 seconds for the same 200,000
samples.

The first working version of this still decided how many samples to draw by watching
`time.monotonic()` during the batch loop and stopping once a time budget was nearly spent.
This measurably broke reproducibility: two back to back calls with the identical seed, on
this development machine, under its ordinary background load, sometimes drew a different
number of samples (`test_monte_carlo_is_reproducible_under_a_fixed_seed` failed roughly 1
run in 5 during manual stress testing, sometimes even after tightening the safety margin).
The fix: `_run_monte_carlo` now decides the sample count in closed form, from R and the
player count alone (`_MONTE_CARLO_SECONDS_PER_REMAINING_GAME`, `_MONTE_CARLO_SECONDS_PER_CHUNK_PLAYER`,
both hand calibrated against this exact chunked table on the build machine, times a 3x
`_MONTE_CARLO_SAFETY_MULTIPLIER`), before a single sample is drawn, so the count itself
never depends on live timing. The periodic wall clock check inside the batch loop is still
real and can still stop the loop early with fewer than the deterministic target, but it
checks against the caller's full, original `time_budget_seconds` (never the smaller
sub-budget used only for sizing), so in ordinary operation it is a true last-resort safety
net for a machine meaningfully slower than the calibration machine, not the everyday
mechanism deciding the count. Restressed after the fix: 15 consecutive same-seed pairs, 0
mismatches (previously flaky within 5 to 8 pairs). `MONTE_CARLO_SAMPLES` (200,000) remains
the named target/cap a caller can request; the actual count used on a given call is
honestly documented as a target, not a guarantee, and is reported back on
`ScenarioReport.scenario_count`.

`_EXHAUSTIVE_ABORT_FRACTION` (how much of the total budget the exhaustive attempt gets
before aborting to Monte Carlo) is 0.75, not a tighter number: R = 15/16 players normally
finishes in well under half a second, but a first pass at 0.5 (1.0 second) occasionally
tripped the abort under this same background load, sending the timing test's own case to
Monte Carlo instead of the exact answer it is meant to demonstrate. 0.75 gives real headroom
for a genuinely fast case while still leaving a quarter of the budget for a Monte Carlo
fallback on a case that actually needs one (R = 20 with many players, measured 1.8+ seconds
for the array build alone, comfortably triggers the abort well before the array finishes).

### The representative-scenario selection heuristic

Up to five scenarios, chosen from the candidate pool of a player's own enumerated or
sampled winning scenarios (capped at `_REPRESENTATIVE_CANDIDATE_CAP` = 400 retained
candidates while sweeping, so a player who wins the overwhelming majority of scenarios at
a large R does not force every one of them into memory). Selection: rank the remaining
games by how decisive they are to this player (`abs(leverage_share - 0.5)`, most decisive
first), weight each game by its rank position, then greedily pick candidates via
farthest-first traversal (start with the first candidate, repeatedly add whichever
remaining candidate has the largest weighted Hamming distance, summed only over games
where the two scenarios actually disagree, from every scenario already picked). This is
the literal implementation of the brief's own instruction ("prioritize scenarios that
differ from each other on the games with the most leverage, rather than five near-identical
scenarios"): the weighting means two scenarios that only differ on a game nobody's win
depends on barely register as "different" for this purpose, while a disagreement on the
single most decisive game dominates the distance.

### The cache eviction story

A plain module level dict in `app/services/scenarios.py`
(`_CACHE: dict[tuple, ScenarioReport]`), keyed on `(week_id, frozenset of every countable
game's (id, status, winner), scoring_mode, picks_required, probability_model, sorted
representative_for, seed)`. It never evicts within a process lifetime. This is a
deliberately simple, honest answer rather than an LRU or a TTL: staleness is already
structurally impossible, because the key itself changes the instant any game the report
depends on changes status or winner (a score refresh, a commissioner void), so a stale
entry is never served, it just sits unreachable. The cost is a few dozen unreachable dict
entries across a season in the worst case (one pool, one process, a handful of distinct
"final outcomes" states per week times a couple of probability models), not a correctness
risk, and not worth a dependency (Redis or otherwise) to solve.

### Other choices worth recording

- Leverage "does not matter" band: `_LEVERAGE_INDIFFERENCE_BAND = 0.10` in
  `app/routers/results.py`. A leverage share within 40 to 60 percent reads as "does not
  matter to you" (the brief's own example), rather than naming a team at, say, 52 percent,
  which would read as more decisive than it actually is.
- `rank_players` does not exclude a no-show from 1st place the way
  `app.scoring.weekly_winner_ids` does. Checked against `app/services/standings.py`'s
  `_assign_ranks`, which already ranks a no-show purely by their (penalty) points with no
  special casing, exactly like a "who is actually in what position" table needs to; the
  win-eligibility exclusion is a separate concern (`WeekEntry.is_winner`), not part of
  ranking itself, in the existing code this phase reuses the pattern from.
- `total_weight` is a probability mass, not a scenario tally. Under "even", each
  remaining game's weight is 0.5/0.5, so a 2-remaining-game report's four scenarios sum to
  1.0, not 4; `pct_at_place` divides by this real total either way, so the percentages
  themselves come out identical regardless of which convention the raw counts use, but a
  caller reading `scenarios_at_place`/`total_weight` directly should not assume the second
  is the scenario count (`scenario_count` is the field for that).
- Scenarios panel gated on `revealed` (the same `week_is_locked` rule already hiding the
  pick grid), even though its own final/remaining thresholds cannot really be met before a
  week locks anyway. Keeps every pick-derived surface on `/results` behind one single,
  already-audited rule rather than a second one that happens to be redundant today but
  would not be if the thresholds were ever configured down to zero.
- Build-your-own-scenario is open to any signed in pool member, not commissioner-only. The
  brief calls it "a social, screenshot-friendly feature," which reads as something every
  player uses, not an admin tool; it is still gated on the week having locked, the same
  reveal rule as everything else pick-derived, so it cannot leak picks early.
- Pool defaults: `scenarios_min_final_games = 5`, `scenarios_min_remaining_games = 1`,
  matching the brief's own "once 5 games were completed" example exactly, both editable
  from `/admin/settings` and read at render time, never hard coded in the pending-state copy.

### Verification

`ruff check .` and `black .` both clean. `pytest -q`: 764 passed (28 new in
`tests/test_scenarios.py`, 12 new in `tests/test_scenarios_service.py`, plus new cases
added to `tests/test_providers.py`, `tests/test_ingest.py`, and `tests/test_app.py`). An em
dash search across every file touched this phase finds nothing; no emoji were added
anywhere. Confirmed directly: `app/scenarios.py` has zero imports from `app.models`,
`app.services`, or `app.routers`.

## Phase 9

### The "college week 3" correction

Phase 1's brief guessed "the launch date is NFL week 1 and college week 3" to build a
synthetic offline test fixture. A live probe against the real ESPN API, run for real in Phase
9b (see `PROGRESS.md`), found the real answer for September 12, 2026 is college **week 2**,
not week 3. Corrected every place that stated "college week 3" as a fact about the real 2026
season: `DECISIONS.md` and `PROGRESS.md`'s own Phase 1 sections now say the live-verified
number and point to Phase 9b, rather than asserting the guess as settled. Left
`tests/fixtures/espn_cfb_2026_calendar.json` and its tests completely alone: that fixture is a
synthetic input built by shifting the real 2025 calendar forward 364 days, used only to prove
`resolve_league_week`'s date-window logic picks the right entry from a known, fixed calendar.
It was never a claim about the real season, and the algorithm it tests is exactly what
produced the correct live answer, so "fixing" the fixture's numbers to match live reality
would not make the algorithm test any more correct, only harder to reason about.

### The second historical demo week: real week 6 of 2025, spreads from ESPN core odds, not CFBD

Picked NFL and FBS week 6 of the 2025 season (already fully in the past by the time this ran)
as the second historical week, one past week 5 which the demo already used, still recent
enough that ESPN's feeds still serve it cleanly. Captured live on 2026-08-08 with a small,
one-off script (not part of the app or the test suite): the NFL and FBS scoreboards
(`tests/fixtures/espn_nfl_2025_w6.json`, `espn_cfb_2025_w6.json`, the college one trimmed from
its real 51 games down to a spread-diverse 22 to keep the fixture size reasonable, the same
kind of curation the original week 5 fixture already used, see `app/providers/cfbd.py`'s own
docstring noting its week 5 capture was 24 events, not the full week), and, for every one of
those events, ESPN's core odds endpoint (`espn_core_odds_nfl_2025_w6.json`,
`espn_core_odds_cfb_2025_w6.json`).

**Why ESPN core odds for college, not CFBD:** the build machine had no `CFBD_API_KEY`
configured (confirmed directly: no `.env` file and no relevant environment variable present).
Before assuming CFBD was unusable, checked live whether ESPN's own core odds endpoint (already
used for NFL's historical spreads, unmetered and keyless) also carries college spreads for a
completed game. It does: a live check found a real, resolvable spread for all 51 of week 6's
real college games, not just the 22 kept in the trimmed fixture. So week 6's college spreads
come from the same `espn.fetch_core_odds`/`parse_core_odds` path the NFL side already used,
generalized to `league="ncaaf"`, rather than reaching for CFBD at all. `app/services/demo.py`'s
`WeekSpec.cfb_spread_source` records which path each week's fixture uses ("cfbd" for week 5,
"espn_core" for week 6), so `_load_real_games` can resolve either without a second code path
per source. This is a legitimate, real recording of a real API, not a workaround standing in
for missing data: CFBD remains exactly what it always was, a last resort fallback the live
`resolve_spreads` pipeline still reaches for when ESPN has nothing, and week 5's demo data
still exercises that real path.

### The open week's artificially future `lock_at`: why this is not deceptive

The brief's own framing, restated here since it is easy to misread on a fresh look at
`app/services/demo.py`: week 7 (the demo's open, current week) reuses week 6's real fixture
data for its games, teams and spreads (never invented), but its kickoff timestamps are
therefore real timestamps from October 2025, in the past. `Week.lock_at` is set to
`utcnow() + 7 days` and `lock_at_override=True` is set so no later rebuild silently recomputes
it back to the real (past) earliest kickoff. This is not a bug and not a claim that a 2025 game
is about to happen: `week_is_locked()` (`app/routers/picks.py`) reads only `Week.status` and
`Week.lock_at`, never any individual game's own kickoff time, so this is the one clock that
actually gates picking. The alternative, shifting every reused game's kickoff timestamp
forward to look plausible, would have been *more* misleading, not less: it would print a fake
kickoff time next to a real final score from 2025, actively asserting a false fact about when
that game happened. Leaving the real, past kickoff times alone and only moving the one clock
that matters (the pool-wide lock) keeps every displayed fact about the game itself true, and
is called out in both the module docstring and the code comment at the point `lock_at` is set,
so nobody mistakes the visual oddity (a slate of games that "already happened" sitting on an
"open" week) for a scoring or lock-enforcement bug. `OPEN_WEEK_LOCK_DAYS_AHEAD = 7` was chosen
simply to comfortably outlast a single work session; there is nothing meaningful about the
number 7 itself.

### `--scenario-week`: a CLI flag, not a `--partial` build variant

The brief offered either shape. Chose a boolean flag on the existing `seed-demo` command
(`--scenario-week`) over a separate `--partial` mode because it composes with `--reset`
exactly like every other `seed-demo` flag already does, and because "partial" on its own reads
as an error state (a build that did not finish) rather than the deliberate demo state it
actually is. When passed, `_build_partial_week` replaces `_build_scored_week` for week 6 only:
it builds the real slate and generates real picks for all eight players exactly like the
default path, then reverts every slate game past `pool.scenarios_min_final_games` (read from
the pool, never hard coded 5, so a commissioner who changes that setting sees the demo track
it on the next `--reset --scenario-week`) back to `status="scheduled"` with its score and
winner cleared, and never marks the week `"scored"` (a week with a game still pending is not
complete by this codebase's own definition, `app/services/results.py`'s
`score_week_for_pool`). Week 6's `lock_at`, computed from its real (past) kickoffs, is already
in the past regardless, so `week_is_locked()` is still true and the Scenarios panel's own
reveal gate is satisfied without any special casing. Trade-off named directly: passing this
flag means week 6 no longer has the "fully scored, with a weekly winner" state the plain build
gives it; documented in the flag's own `--help` text, in `app/services/demo.py`'s module
docstring, and in `README.md`.

### Demo payout figures and the entry fee: real numbers, clearly labelled, demo pool only

Phase 7 deliberately ships every real pool, and the real production default pool, with zero
`PayoutRule` rows and no entry fee: no dollar figure is ever hard coded for money someone might
actually owe. This phase's brief explicitly carves out an exception for the demo pool alone,
"since the real point is to let a tester walk the live selection flow," and a payout column
with nothing configured has nothing to show. Every figure this phase adds
(`DEMO_WEEKLY_PAYOUTS`, `DEMO_SEASON_PAYOUTS`, `DEMO_ENTRY_FEE`, `DEMO_VENMO_HANDLE`) lives
only in `app/services/demo.py`, only reaches the database through `seed_demo_pool`, and every
`PayoutRule.label` this phase writes ends in the literal string `"(demo)"` so it is
unmistakable on screen, in `/admin/payouts`, and in `/admin/settings`'s payout rule list. The
demo Venmo handle, `picksportplus-demo`, is not a real Venmo account. Every demo member is
marked paid at seed time (`_mark_everyone_paid`) specifically so the demo pool never triggers
its own Venmo gate; the gate's actual blocking behavior is proven against a fresh, non-demo
pool in `tests/test_app.py` instead, per Phase 7.

### Eight demo players, varied 15-of-20 subsets

Added Jordan Ellis and Sam Okafor to `DEMO_PLAYERS` (six to eight), keeping the existing
"skill level drives pick accuracy, conviction drives confidence ranking" generation approach
unchanged. The one real behavior fix, independent of the player count: `_generate_picks` used
to rank and assign confidence to *every* slate game for every player (`n = len(slate)`,
picking all 20), which predates Phase 3's "pick 15 of 20" rule and silently violated it in the
one place a real tester would actually look. Each non-no-show player now draws a
deterministic, per-player `rng.sample(slate, pool.picks_required)` before ranking, so which
five of the twenty games a player sits out varies from player to player
(`tests/test_demo.py::test_players_pick_a_varied_subset_not_the_identical_games` pins this
directly) rather than either picking everything or, worse, every player mechanically sitting
out the identical five.

### Settings: `week1_anchor_date`, following the existing pattern exactly

`app/config.py` gained `settings.week1_anchor_date: dt.date | None = dt.date(2026, 9, 12)`,
the real, live-confirmed anchor for the 2026 season, mirroring how `settings.season_year`
already carries a real, concrete default (`2026`) rather than an empty placeholder.
`app/cli.py`'s `seed_admin` threads it onto a freshly created pool's own
`Pool.week1_anchor_date` (a column that already existed from Phase 1), the same field a
commissioner can later edit from `/admin/settings`; an existing pool is left alone, matching
every other field `seed_admin` only sets at creation time. Added `WEEK1_ANCHOR_DATE` to
`.env.example`. Checked every other `Settings` field against `.env.example` directly (a small
script diffing field names against the file) rather than assuming nothing else was missing;
found four fields absent (`render_external_hostname`, `render_external_url`,
`http_timeout_seconds`, `http_retries`), all four confirmed via `git log` to predate this
9-phase build entirely (the first two are Render-injected automatically, never meant to be set
by a deployer; the last two are internal HTTP tuning knobs the codebase's own comment says
exist "so tests can tighten them"), so none were added. No `app/models.py` change and no new
Alembic migration this phase.

### `render.yaml` has no cron: confirmed real, flagged as a real launch gap, not silently fixed

Read `render.yaml` and `app/cli.py`'s `run_cron` directly rather than assuming the brief's premise
was true. `run_cron` itself is exactly as described: per pool, `sync_week` (which only
auto-publishes when `pool.auto_publish` is true, off by default since Phase 5), then
`fetch_results` and `score_week_for_pool` for every week still open or locked. The committed
`render.yaml`, however, runs no cron at all, a deliberate, pre-Phase-9 decision documented in
the file's own header comment and in `README.md`'s deploy section: Render's free plan does not
offer scheduled jobs, so the free demo blueprint has none, and `README.md` already gives the
exact `type: cron` block and the external-scheduler alternative for a real, paid deployment.
**Did not invent a workaround for this.** Adding a `type: cron` resource to the committed free
blueprint would fail to deploy on the free plan this blueprint is for, and there is no
credential or account access in this environment to verify a paid-plan deploy actually works.
This is a genuine, human-decision-required launch readiness gap for the real Week 1 pool, not
something this phase can safely code its way past; recorded plainly in `PROGRESS.md`'s Phase
9b section and in the final report, not silently marked done.

### Idempotency test scope: the three real functions, not `sync_week`'s wall-clock-dependent

`detect_week` reads the real wall clock (`dt.datetime.now(dt.UTC)`) by default, and `run_cron`
never lets a caller override it. Driving three runs of the literal `run_cron` function against
a fixture recorded from a fixed past date would make the test's outcome depend on what day it
happens to execute (a stale calendar fixture read against today's real date resolves to
whatever week that calendar considers "last," not the week the fixture's game data is actually
for). `tests/test_ingest.py::test_the_build_fetch_score_pipeline_is_idempotent_across_three_runs`
instead calls `ingest.build_slate` with an explicit week number (the documented pre-anchor
fallback, `pool.week1_anchor_date=None`, so both leagues resolve the same way `run_cron` already
would for such a pool) plus `results.fetch_results` and `results.score_week_for_pool`, the
exact three functions `run_cron` invokes per pool per live week, three times against fully
cached ESPN and CFBD fixture responses (`FeedCache` rows seeded directly, the same pattern
`tests/test_ingest.py`'s other tests already use). `detect_week`'s own date arithmetic and its
ESPN-calendar fallback already have dedicated, deterministic coverage elsewhere in this file
and in `tests/test_calendar.py`, so nothing about detection itself goes untested, it is simply
exercised through a different, explicit-week entry point here so this particular test's
outcome cannot depend on the calendar.

### Verification

`ruff check .` and `black .` both clean. `pytest -q`: 781 passed, 0 failed (764 at the Phase 8
baseline, 17 net new: 16 in the new `tests/test_demo.py`, 1 in `tests/test_ingest.py`). Zero
live network calls during the run: `tests/conftest.py`'s autouse, session scoped
`force_offline_mode` fixture pins `settings.offline_mode = True` for the whole suite regardless
of what any individual test does, and `app/services/demo.py` never calls `fetch_json` at all,
it reads `tests/fixtures` files directly off disk. An em dash search across `app/`, `SPEC.md`
and `README.md` finds nothing; a code-point scan of every `.py`/`.html`/`.js`/`.css` file under
`app/` for emoji finds nothing.

## Post-launch fixes

### Picks page: no color before a pick, sequential auto-numbering, and a hard 15-pick cap

Three related fixes, all in the open-week pick flow, reported directly by the product owner
after trying the demo:

1. **Team buttons showed green/red win-loss coloring before any pick was made.** Root cause:
   `_load_real_games` in `app/services/demo.py` loads a real, already-finished historical game
   for the demo's "open" week (Week 7), and was writing that game's real final `status`,
   `winner`, and scores onto the row, even though the week's `lock_at` is pushed artificially
   into the future so the picks page renders as if it were still open. `team_button` in
   `picks.html` colors a button `.is-winner`/`.is-loser` based purely on `game.is_final` and
   `game.winner`, with no awareness that the week's own lock is what actually gates picking, so
   a demo player saw real historical outcomes leaking through as premature coloring. Fixed by
   adding `_load_real_games(..., unplayed=True)`, used only by `_build_open_week`, which resets
   `status` to `"scheduled"` and clears `winner`/`home_score`/`away_score` while keeping the
   real matchup, spread, and kickoff time. This is a demo-data bug specifically: a real
   production week's games are genuinely not final yet while picking is open, so this never
   affected anything but the reused-historical-data demo path.
2. **A fresh pick now gets an immediate confidence number**, in the order picks are made (first
   pick tapped gets the smallest available value, 1, next gets 2, and so on), rather than
   sitting blank until the player manually types a number or drags. Implemented in
   `app/static/app.js`'s `onClick` (a new `nextAvailableConfidence` helper picks the smallest
   value 1..picks_required not already used by any row, so it never collides even after a pick
   is undone out of order). A value the player already typed ahead of tapping is never
   overwritten. This is explicitly a starting point, not a claim about true confidence:
   "Reorder to inputs," dragging, and manual typing all still fully override it afterward,
   unchanged from Phase 4.
3. **Picking is now hard capped at `picks_required`.** Tapping a team on an unpicked row once
   the cap is already reached is blocked outright (no DOM change, a message replaces the
   picks-summary text: "All 15 picks made. Tap a pick again to undo it before picking
   another."). This required adding a real undo affordance that did not exist before: tapping
   the already-picked team on a row again now unpicks that game entirely (clears the winner and
   the confidence), rather than being a no-op, since a hard cap is only usable if there is a way
   to free a slot back up. Switching which side is picked on an already-picked row still never
   counts against the cap, only a genuinely new pick does.

No automated test coverage for any of this: it is all client-side JavaScript, and this
environment has no browser or JS test runner (same limitation noted in Phase 9's acceptance
pass). Verified by hand: `node --check app/static/app.js` for syntax, tracing the `onClick`
branches by hand for each of new-pick/switch-side/undo/at-cap, and confirming the demo rebuild
(`seed-demo --reset`) produces `status="scheduled"`, `winner=None`, `(None, None)` scores for
every Week 7 game.

### A resolvable spread ranks a game, it never gates eligibility

The product owner's own words: "line is not mandatory, its just a nice added feature." Before
this fix, `select_slate_by_targets` dropped any candidate with no resolvable spread before it
was even considered for the slate, and `add_to_slate`/`swap_slate_game` in
`app/services/ingest.py` raised a `ValueError` if a commissioner tried to add or swap in a game
with no line set. That was backwards: a spread should only affect ranking (closest games
preferred when a line is known), never whether a real, scheduled game is a legitimate
candidate at all.

This was not a theoretical concern. A live build against the real September 12, 2026 slate
(`join_code=WEEK1VERIFY`, `season_year=2026`, `week1_anchor_date=2026-09-12`, pool id 1) came
back skewed 13 NFL / 7 college against a target of 8/12, because only 23 of 102 real candidate
games had a posted spread that early, almost all NFL: college books post lines much closer to
kickoff than NFL books do. The mandatory-spread gate was silently dropping 79 genuine,
scheduled college games from consideration.

**The fix**, entirely in `app/slate.py`:

- `_sort_key` no longer raises when `closeness_of` returns `None`. `float("inf")` stands in as
  the sort-only value, so a spread-less candidate always sorts after every candidate with a
  real spread, but that stand-in never leaks into stored data.
- The eligibility loop in `select_slate_by_targets` no longer calls `closeness_of` at all. The
  only thing that can now drop a candidate is the started-game filter
  (`exclude_started`/`cutoff`). This applies uniformly to `None`, `NaN`, and infinite spreads:
  none of them are a special case for eligibility any more, they simply all sort last.
- `Selected.closeness` is now `float | None`. The `Selected` list is built from the honest
  `closeness_of(candidate.spread_home)`, never from the `float("inf")` sort-only stand-in.
  `Game.closeness` (already a nullable column) needed no change: `upsert_games` already set it
  to `None` whenever `spread_home` is `None`, so it was already consistent with this.
- A pin (or rivalry auto-pin) no longer needs a resolvable spread to force its way onto the
  slate. It only needs to not have already kicked off, the same as any other candidate.
- `_reason()` and the note text in `_build_notes` no longer blame "no resolvable spread" for a
  shortfall, since that can no longer happen. The two remaining, real causes are the
  started-game filter, and a league genuinely not having enough games scheduled for the window
  at all ("were scheduled this week" / "had not kicked off yet").
- `app/services/ingest.py`: removed the two `if game.spread_home is None: raise ValueError(...)`
  gates in `add_to_slate` and `swap_slate_game`. `slate_reason()` now reads "Closest available
  (no line posted yet)" for a spread-less pick instead of a bare "Closest" with nothing to back
  it up. The `resolve_spreads` warning text no longer says a spread-less game was "left off the
  slate," since it no longer is.
- `app/templates/admin/slate.html`: the swap dropdown and the "Add" button in the candidate
  pool no longer require `spread_home is not none`; a spread-less game is exactly as addable or
  swappable as any other now.
- `SPEC.md` Section 5e, 6 and 6a updated to describe the real rule (a resolvable spread ranks,
  it never gates) instead of the old mandatory-spread language.

**Verification.** Re-running the exact build that exposed this
(`python -m app.cli build-slate --week 1 --year 2026 --pool 1`) against the same real, cached
September 12, 2026 candidate pool went from "College 7, NFL 13" to "College 12, NFL 8", the
full 8/12 target, entirely from real games (5 of the 20 slate games have no posted line yet and
are correctly present, ranked after every game with a known spread). `ruff check .`, `black .`,
and `pytest -q` all clean, 786 passed, 0 failed. No schema change, so no Alembic migration.

### Public landing page, pricing, how to use, and contact pages

Before this, `/` always redirected: signed in to `/picks`, signed out to `/login`. There was
no public facing page at all, so a link to the app with nobody signed in just bounced a
visitor straight to a login form with no explanation of what the product even is. Added a
real front door plus three supporting pages, all public, none of them behind `require_user`.

**Routing.** `/` stays in `app/main.py` (it was never a router route). A signed in visitor's
`request.session.get("uid")` check is untouched, still a 303 to `/picks`. A signed out visitor
now renders `app/templates/home.html` instead of redirecting to `/login`. `/pricing`,
`/how-to-use` and `/contact` are a new `app/routers/public.py`, following the exact
`_chrome(db, user)` pattern already established in `app/routers/legal.py` for `/terms` and
`/privacy` (duplicated rather than imported, matching how legal.py itself keeps that helper
private and self contained rather than shared from a common module).

**Header nav, signed out.** The old bare "Sign in" button is now a full row: Pricing, How to
use, Contact as plain `.nav-link` text links, then Login as a new `.btn-gold` button. Gold
(not a second green fill, and not the maroon secondary accent) was picked because the row
already has a green top bar as its background: a second green button would blend into the
bar, and gold is the site's one other high visibility color, already used for the wordmark's
"Plus" and for ribbons/badges, so it reads immediately as "the one action that leaves the
marketing pages" without introducing a new hue. Contrast checked: ink on gold is 6.15:1 (AA
for both normal and large text). `.topbar .btn-gold` overrides `.topbar a`'s cream text the
same way the pre-existing `.topbar .btn-secondary` rule already had to, same specificity
reasoning documented inline in `app.css`.

**Header nav, signed in.** Added a persistent way back to the public site. Desktop: a small
"Home" link (house icon) sits in `.user-menu`, `.desktop-only`, immediately left of the
display name, so it reads as "leave the app" rather than being confused for one of the four
pool-scoped nav tabs (This Week / Season / Results / Admin). Mobile: the bottom tab bar is
already at its four item cap per the brief, so Home does not go there. Instead both the
signed in and signed out headers share one hamburger affordance: a `.mobile-only` toggle
button next to the sign out button (signed in) or next to the desktop nav (signed out), which
opens `#site-menu`, a fixed dropdown panel directly under the top bar. Signed in, that panel
holds exactly one link, Home. Signed out, it holds Pricing / How to use / Contact / Login,
mirroring the desktop row. One shared panel and one shared `initMenuToggle()` in `app.js`
drive both cases, keyed off `[data-menu-toggle]` / `[data-menu-panel]`, rather than two
separate menu implementations.

**Hamburger menu mechanics.** Plain button, not `<details>`: `<details>` gives free keyboard
toggling but not free outside-click-to-close or Escape-to-close, and the brief asked for
both, so a small dedicated `initMenuToggle()` (vanilla JS, no new dependency) owns open,
close, outside click, and Escape (which also returns focus to the toggle button). It reuses
the `reduceMotion` variable already declared at the top of `app.js` (the same one
`initSortable` and the lock flow's `scrollIntoView` already read) rather than adding a second
`matchMedia` check: opening removes `[hidden]` then adds `.is-open` an animation frame later so
the CSS opacity/transform transition has a "before" state to animate from, and `reduceMotion`
skips that frame delay so the panel appears pre-opened instead of visibly sliding in. The
blanket reduced motion rule in `app.css` Section 03 also collapses the transition duration
itself to near zero either way, so this is belt and suspenders, not the only protection.

**Pricing copy.** 199 dollars per season for one league, 350 for two, both hard numbers in
`app/templates/public/pricing.html`, not only in a docstring, per the brief's own test
requirement. The refer-a-friend button is a `mailto:?subject=...&body=...` link built with
Jinja's `urlencode` filter over a `{% set %}...{% endset %} | trim` captured message; there is
no backend referral tracking, no discount code, and no database change, exactly as scoped.
Redemption of the 50 dollar credit is described as a manual step (email us who you referred),
since there is nothing here to automate it against.

**How to use copy.** Describes the real, current default: `inverse` scoring (a correct pick
earns nothing, a wrong pick counts its staked points against you, lowest total wins), pulled
from `app/scoring.py`'s own module docstring and `SPEC.md` Section 9, not the older `standard`
(highest wins) behavior. `standard` is mentioned as a per-league setting a commissioner can
still switch to, since it is a real, live option, not a claim that it is the default.

**New icons.** `menu`, `mail`, `send`, and `home` added to `app/templates/components/icons.html`,
real Lucide path data (the same canonical paths shipped in Lucide's icon set, matching the
existing simple multi-path style already used by `calendar`, `trophy`, etc.), not invented
shapes.

**Verification.** `ruff check .`, `black .`, and `pytest -q` all clean, 791 passed, 0 failed
(786 before this change, plus 6 new tests covering the landing page, the signed-in `/`
redirect regression, and all three new pages rendering for both a signed out and a signed in
visitor). No schema change, so no Alembic migration. `grep -rn "—" app/ tests/` finds nothing
new. No new Python dependency: pricing is informational copy, the referral button and the
contact page are both plain `mailto:` links the visitor's own email client opens, nothing is
sent from the server.

### Compact, expandable game rows on desktop (1024px and up)

The product owner wanted all 20 games of a slate meaningfully closer to fitting on one desktop
screen at once, "super skinny," still readable, with a per row expand for the rest. Scoped
entirely to `.game-list[data-sortable] .game-row`, the open, still-editable pick list rendered
by the `{% else %}` branch (state 3) of `app/templates/picks.html`. The read only slate
(`readonly_list`, states 3a/3b/4) and `results.html`, which both reuse the plain
`.game-row.is-complete` markup, are untouched on purpose, exactly as scoped.

**Always visible in the compact row; never moves.** The drag grip, both team abbreviations and
full names (already ellipsis truncated, so a long name costs width, never height) with their
pick buttons at the unchanged 44px minimum tap target (Spec 3f, non-negotiable, verified by
inspection: `.team-btn`'s `min-height: 44px` is untouched anywhere in this change), the
confidence number input and chip, the up/down rank buttons (kept, shrunk), and the new expand
toggle.

**Moved into the collapsed `.game-detail` panel.** The league badge, `game.line_text`, the
kickoff time, and each team's record. None of these were ever load bearing for the row's
*height* (the row was always exactly as tall as `.team-btn`'s 44px floor plus padding; a badge,
a spread and a kickoff time all sit comfortably under 44px and were never what made the row
tall), only its *width*, so hiding them is what actually buys back real estate. The full team
name stayed inline rather than joining them: moving it would not have shrunk the row by a
single pixel either, and losing it would have made the compact row harder to scan for no
compactness benefit at all.

**How the panel gets its content, and why nothing was duplicated by way of a JS read.** Rather
than teaching `matchup`/`team_button` a `show_record` flag and having the caller reassemble the
suppressed pieces somewhere else in the DOM, `picks.html` renders the badge/line/kickoff/record
block a second time, directly from `game`/`pick`, inside `.game-detail`, and leaves
`matchup`/`team_button`/`league_badge` completely untouched, same signature, same output, every
call site (grep confirms `picks.html` is the only caller of either macro; `results.html` builds
its own row by hand and never calls them). The originals keep rendering in their old spot in
the DOM at every width; app.css only `display: none`s the redundant copies (`.game-meta`'s
direct-child badge/line/kick, and `.team-record` inside `.team-btn`) once the viewport is
1024px and up, and only inside `.game-list[data-sortable] .game-row`. Below that width nothing
in the DOM or the CSS cascade for this list changed at all, which is what makes "mobile is
byte-for-byte the same layout" a fact about the cascade rather than a claim to go re-verify by
hand. The tradeoff is a small amount of duplicated markup (one badge, one line of text, one
kickoff time, two short record strings) per row; the alternative, a macro that conditionally
omits content the caller then has to reconstruct from scratch anyway, duplicates exactly the
same amount of logic one level up for no benefit, so the plain, boring option won.

**The toggle.** A small chevron button, `.row-expand`, reusing the existing `down` icon exactly
the way `.how-it-works-caret` already does (`ic.icon("down", 16, "row-expand-caret")`, rotated
180deg via `[aria-expanded="true"] .row-expand-caret`), no new icon. `aria-expanded` and
`aria-controls="game-detail-{{ game.id }}"` are wired server side; `app.js`'s `onClick` gained
one new branch, `toggleRowDetail`, checked first via `e.target.closest("[data-row-toggle]")`.
It is deliberately a lighter version of `initMenuToggle`'s pattern, not a copy of the whole
thing: no outside-click-to-close and no Escape handling, since the panel is inline content
inside its own row, not a floating overlay covering the page, and the brief was explicit that a
native `<button>`'s own Enter/Space handling is enough. What it does keep from
`initMenuToggle` is the shape: `hidden` removed, `.is-open` added a animation frame later
(skipped under `reduceMotion`, the one module level flag, no second `matchMedia` check) so the
opacity transition has a "before" state, and closing drops `.is-open` and restores `hidden` in
the same tick. `.game-detail:not([hidden])`, not a bare `.game-detail` rule, for the identical
reason `.game-list-divider` already uses that pattern (see the comment above `regroupDivider` in
`app.js`): a plain class rule sits at the same specificity as the UA stylesheet's `[hidden]`
rule and would win on source order, making the panel visible while still hidden.

**Grid mechanics, so a collapsed panel costs zero pixels, not just zero visibility.** The
detail panel is placed with `grid-column: 1 / -1` and only gets `grid-row: 2` inside the
`:not([hidden])` rule, never outside it. An element with `display: none` (what `[hidden]`
resolves to, and what a collapsed panel always has below 1024px) is removed from box generation
entirely and never participates in grid track sizing, so the browser never creates an implicit
second row, and no `gap` is ever reserved for one, while the panel is collapsed. The toggle
button lives in a fifth grid column added only inside `.game-list[data-sortable] .game-row`'s
own rule; `.game-row`'s shared `grid-template-areas: "grip teams meta rank"` (used by every
other page that renders a `.game-row`) is untouched, the fifth column is simply never claimed
by that area string, so the toggle is placed by explicit `grid-column: 5` instead of joining
the named area system, and no other page that reads that shared rule sees any change.

**Row height, and roughly how many rows that buys back.** The one thing that cannot get
shorter, on purpose, is `.team-btn`'s 44px minimum tap target, so that stays the floor. Desktop
row padding-block dropped from `var(--space-2)` (8px) each edge to `var(--space-1)` (4px) each
edge, and the interactive list's own row gap dropped from `var(--space-2)` (8px) to
`var(--space-1)` (4px); the grip and the two rank buttons shrank from the 44px square they
shared with `.team-btn` down to 32px, since the 44px floor was only ever a requirement for team
pick buttons, not for either of those. Net: a compact row is now 52px tall (4 + 44 + 4) with a
4px gap to the next one, 56px per row all in, versus roughly 60px tall with an 8px gap (68px
per row all in) before this change, about an 18% reduction. Twenty rows: about 1,116px of list
height now versus about 1,352px before. That is a real, visible reduction, not a token one, but
it does not get all 20 games onto one ordinary laptop screen at once by itself, since the 44px
tap target floor caps how much further any row can shrink; the expand panel is what makes up
the rest of the "more powerful" ask from here, without trading away the tap target to get
there.

**Dragging.** `SortableJS`'s `handle: ".game-grip"` option was already the only thing that can
start a drag; the new toggle button and detail panel are both plain siblings of the grip inside
the same `<li>`, never nested inside it, so neither one changes what can pick a row up. Verified
by inspection, not by running the JS (this environment cannot run a browser, see Phase 9 and
the earlier post launch notes for the same, already documented, limitation): the toggle button
is a `<button>`, SortableJS only starts a drag from a `pointerdown`/`mousedown` inside the
element matched by `handle`, and the toggle is not inside `.game-grip`'s DOM subtree.

**Confidence cap, auto numbering, and the divider.** None of `renumber`, `regroupDivider`,
`nextAvailableConfidence`, or `updateSummary` read anything new; they all key off
`.game-row`, `.team-btn.is-picked`, `[data-conf-input]`, `.conf-chip`, `input[data-pick]`,
`input[data-conf]`, and `row.dataset.confidence`, none of which changed name, shape, or
position in the DOM. The new elements sit after `.rank-controls` and before the two hidden
inputs, outside every selector any of those four functions ever touch.

**Verification.** `ruff check .`, `black .`, and `pytest -q` all clean, 792 passed, 0 failed
(791 before this change, plus one new test asserting the picks page renders exactly one
`data-row-toggle`/`data-row-detail` pair per slate game, correctly linked by
`aria-controls`/`id`). No schema change, so no Alembic migration. `grep -rn "—" app/` finds
nothing new. No new icon (`down`, rotated, reused from `.how-it-works-caret`'s own pattern), no
emoji, no new dependency, no Tailwind or bundler; the CSS lives in `app/static/app.css` on the
existing 4px spacing scale and the JS addition is one small function plus one new branch in the
existing delegated `onClick` in `app/static/app.js`. This environment cannot run a browser, so
the row height, the grid placement, the animation, and the drag-safety reasoning above are all
verified by reading the CSS cascade and the SortableJS `handle` contract directly rather than by
a screenshot; a human should still eyeball it once on a real desktop browser before this ships.

### Global admin league management, and "view as commissioner"

The product owner wanted a real three tier permission model (player, commissioner, admin),
league creation restricted to a site admin portal, and a way for the admin to enter any
league's commissioner tools and get back out again cleanly. Almost all of the permission
model already existed and was already correct (`User.is_admin`, `PoolMember.role_in_pool`,
`is_commissioner` already treating a global admin as a commissioner everywhere,
`require_commissioner` already gating every route in `app/routers/admin.py`). This entry
covers what was actually missing: the one real bug, and the new admin-only surface.

**The bug: `get_active_pool` silently ignored an admin's own session choice.** Before this
fix, `get_active_pool` (`app/auth.py`) only honored `request.session["pid"]` when a
`PoolMember` row existed for the signed-in user in that exact pool; otherwise it silently fell
back to that user's *first* pool membership by id, with no error, no warning, nothing in the
response that would tip anyone off. For an ordinary player this is fine and was always the
intended behavior (one pool in practice). For a global admin it was a real, silent
mis-routing bug: an admin is not necessarily a `PoolMember` of every league (any league
created before this feature existed, or created for someone else and never personally
joined), so clicking "view as commissioner" on league X would set the session pool id to X,
then the very next request would discover the admin has no `PoolMember` row in X, discard
that choice without a word, and quietly land the admin back in whatever their own first
pool happened to be. The fix (`app/auth.py`, `get_active_pool`): when the signed-in user is
a global admin, a session pool id is honored with a direct `db.get(Pool, pid)`, no
`PoolMember` row required. A non-admin's path through the function is completely untouched,
same membership check, same fallback, same session correction, exactly as it worked before
this change. Proven two ways: `tests/test_app.py::test_get_active_pool_admin_honors_session_pool_with_no_membership_row`
and `test_get_active_pool_non_admin_behavior_is_unchanged_by_a_bogus_session_pool` call
`get_active_pool` directly (a tiny local `_FakeRequest` standing in for `Request`, since the
function only ever touches `request.session`, a plain dict), and
`test_admin_can_view_as_commissioner_of_a_pool_never_joined` proves the same fix end to end
through the real router: an admin who is a member of neither pool (in one variant, a member
of a different pool entirely) posts to `/admin/leagues/{id}/view-as` for a pool they have
never joined, then loads `/admin` and gets that pool's own name back, not the other one's.

**Where the new routes live, and why not `app/routers/admin.py`.** New file,
`app/routers/leagues.py`, mounted at `/admin/leagues` (`GET` list, `GET`/`POST /new` to
create, `POST /{pool_id}/view-as`), everything behind `require_admin`. `app/routers/admin.py`
opens with "Everything here sits behind require_commissioner. A regular player cannot reach
any of it," which is true today; adding a `require_admin` route to that file would either
falsify that statement or force a reader auditing the commissioner boundary to notice one
route quietly uses a stricter gate. A second file keeps the module-level docstring of each
file a complete, accurate description of its own permission boundary, the same reasoning
`app/routers/leaderboard.py`/`results.py`/`picks.py` already split along. `require_admin`
itself (`app/auth.py`) already existed, already correct, wired up nowhere until now; its
error message ("Commissioner access only") was stale copy from before this feature existed
and is fixed to "Site admin access only" alongside wiring it up, since a regular commissioner
now legitimately sees a different message than a site admin only route.

**League creation form fields, and what was deliberately left out.** Name, join code
(pre-filled with `generate_join_code()`, editable), season year, timezone, and an optional
week 1 anchor date, the same set `/admin/settings` already edits for name/season/timezone/
anchor, minus everything that is a slate-shape tuning knob (games per week, NFL/college
targets, picks required, scoring mode, rivalries, payment settings, and so on). Every new
`Pool` gets `num_games_per_week=20`, `target_nfl=8`, `target_ncaaf=12`, matching
`seed_admin` in `app/cli.py` exactly (`DEFAULT_NUM_GAMES_PER_WEEK`/`DEFAULT_TARGET_NFL`/
`DEFAULT_TARGET_NCAAF` in `app/routers/leagues.py`), because the brief was explicit that the
commissioner tunes those later from the pool's own existing settings page and this form
should not duplicate that form. `test_create_league_makes_a_pool_with_seed_admin_defaults`
checks the 20/8/12 defaults land on a real row.

**Existing users only, no new invitation flow (a deliberate scope decision).** The creation
form's "Commissioners" field is a plain textarea, one email per line, parsed the same
line-oriented way `admin.py`'s `_parse_rivalries` already parses its own textarea (skip
blank lines, no hard failure on one bad line). Each email is looked up against `User.email`
(`normalize_email`, already existing); a match becomes a `PoolMember` with
`role_in_pool="commissioner"` immediately. An email with no matching account is reported back
via flash ("No account found for: ...") and otherwise ignored: this feature does not create
user accounts or send an invitation email, on purpose. The brief was explicit that building
new-user invitation is a real scope expansion it does not ask for, and the app already has a
working path for "a player who has since registered becomes a commissioner" --
`POST /admin/members/{member_id}/role`, unmodified here -- so a commissioner named before
they have an account is simply promoted from that pool's own Members page once they join
with the pool's join code, the same as any other promotion.
`test_create_league_attaches_an_existing_user_as_commissioner_by_email` proves the attach
path (case-insensitive email match included); nothing tests account creation because nothing
creates one.

**The viewing-as-commissioner banner, and why it only checks `current_user.is_admin`.** Any
signed-in admin viewing a pool through the existing commissioner routes in
`app/routers/admin.py` (all of which set `active_nav="admin"` via that file's own `_base`
helper) sees a small banner reusing the existing `.lockbar` styling verbatim (`app/templates/
base.html`, no new CSS), "Viewing {pool.name} as commissioner" with an "Exit to leagues" link
back to `/admin/leagues`. It is gated purely on `current_user.is_admin`, not on whether this
particular pool happens to have a real `PoolMember` commissioner row for that same admin
account (the seeded admin from `seed-admin` has exactly such a row for the default pool, and
still sees the banner there): the brief's own wording gates it on `current_user.is_admin`
being true, and functionally every admin session inside `/admin/*` is "viewing as
commissioner" in the sense that matters here, a stance the admin can always exit via the
banner regardless of how they arrived. It is scoped to `active_nav == "admin"` specifically
(not just "any admin page under `/admin/*`"), so it does not render on the `/admin/leagues`
portal itself, which sets its own `active_nav="admin_leagues"`: that portal is not a view of
any single pool, and putting an "exit to leagues" link on the leagues page itself would be
circular. A real commissioner (`role="player"`, `role_in_pool="commissioner"`) never sees it,
since `current_user.is_admin` is false for that account regardless of their pool role.
`test_viewing_as_commissioner_banner_shows_for_admin_and_never_for_a_real_commissioner` builds
one pool with both a real (non-admin) commissioner and the seeded admin-as-commissioner,
logs in as each, and checks the banner text is present only for the admin and absent both for
the real commissioner's own `/admin` page and for `/admin/leagues` itself.

**Nav.** One new conditional link, gated on `current_user.is_admin`, added twice: once in the
desktop `.nav` next to the existing commissioner-only "Admin" link (a second, separate link
rather than folding "Leagues" into "Admin", since they go to genuinely different places --
one pool's dashboard versus every pool -- and conflating them under one label would be more
confusing, not less, for the one account that ever sees both), and once in the mobile
`site-menu` "more" panel (not the bottom `tabbar`, which is a four-item primary nav shared by
every commissioner and would get crowded for the sake of a link only the site admin ever
uses). No new icon needed for the desktop link (plain text, matching the existing "Admin"
link's own styling); the mobile menu entry reuses the existing `flag` icon.

**No schema change, no migration.** Every field this feature touches already exists on
`Pool`, `PoolMember`, or `User`. `PoolMember.role_in_pool` already supports more than one
commissioner per pool (a plain per-row flag, no unique constraint anywhere forcing exactly
one), which is exactly what "commissioners, one per league, and maybe multiple
co-commissioners" needed; nothing new was added to support it.

**Verification.** `ruff check .`, `black .`, and `pytest -q` all clean, 806 passed, 0 failed
(792 at the previous post-launch baseline, 14 net new tests: three parametrized boundary
tests each covering three routes for a regular player and a non-admin pool commissioner (six
cases total), plus one test each for the league list showing every pool, the league list
showing a pool's commissioners, league creation with the 20/8/12 defaults, attaching an
existing user by email, the admin-can-view-a-never-joined-pool end-to-end scenario, the two
direct `get_active_pool` unit tests, and the viewing-as-commissioner banner). `grep -rn "—"
app/` finds nothing new. No new icon beyond the existing `flag`/`settings` icons reused
as-is, no emoji, no new dependency, no Tailwind or bundler, no new CSS (the banner reuses
`.lockbar` exactly as it already renders on `picks.html`).

### Commissioner invite links, and a ready-to-send player invite message

Two distinct invite links the product owner wanted kept firmly apart: one that a site admin
hands a brand new person (no account yet) to become a commissioner of a specific league, and
one that a commissioner already has (yesterday's join code) to hand their own players, now
paired with a written, ready-to-copy message instead of just a bare code.

**Why a second column, `Pool.commissioner_invite_code`, rather than reusing `join_code` with
a flag.** The two codes gate materially different actions: `join_code` only ever creates a
`PoolMember` with `role_in_pool="member"`; the new code creates one with
`role_in_pool="commissioner"`, a real permission upgrade. The product owner's own requirement
was explicit that rotating one must never affect the other, which rules out a shared column
outright (rotating a shared value would always touch both powers at once) and rules out a
"this join code is currently in commissioner mode" flag too (a stray flag flip, or a future
bug, could silently turn a player join link into a commissioner mint). A second, independent,
nullable `String(40)` column with its own unique index is the only shape where "rotate one,
the other is untouched" is true by construction, not by careful bookkeeping.
`test_rotating_commissioner_invite_code_never_touches_join_code_and_vice_versa` proves both
directions in one test.

**Migration `176d4f464857_add_commissioner_invite_code_to_pools.py`.** Adds the column
nullable with no server default (a server default would hand every existing pool the *same*
code, which is useless: two pools cannot share a commissioner invite code once the unique
index is in place). Instead the migration backfills every existing pool by hand in a small
loop, reusing `app.auth.generate_join_code` directly (imported into the migration, the same
precedent `6b6eab096a56` already set for reaching into `app/` from a migration, there for a
static constant, here for a real function so the exact alphabet and ambiguous-character
exclusions never drift into a second implementation) and tracking codes already handed out
in this same pass to avoid a collision, the same shape `rotate_join_code` already uses
against the live table. Applied cleanly against both the existing dev database (two pools,
both backfilled, zero left null) and a brand new database built from the very first migration
forward (zero existing pools, loop body never runs, still ends at head cleanly). `downgrade`
drops the column and its index. Every pool created after this migration, via
`POST /admin/leagues/new`, also gets a fresh code at creation time
(`_fresh_commissioner_invite_code` in `app/routers/leagues.py`, the same 10-try
collision-avoidance shape as `rotate_join_code`), so in practice no pool is ever left without
one, even though the column stays nullable at the type level rather than forcing a synthetic
NOT NULL default (matching `Pool.venmo_handle`/`Pool.entry_fee`'s own "nullable until someone
sets a real value" convention, since nothing structurally requires the value to exist, unlike
`join_code`, which the login/registration path depends on for every pool, always).

**Query param: `?commissioner_code=`, never `?code=`.** `?code=` already means "the player
join code" everywhere in this codebase (`GET`/`POST /register`, and the docs and copy that
already reference it); reusing it for a second, more powerful meaning would make every
existing join link ambiguous about which power it grants depending on invisible database
state. `?commissioner_code=` is unambiguous on sight, greppable, and reads naturally next to
the existing `?code=`. `app/routers/auth.py` adds `_find_pool_by_commissioner_code`,
deliberately a full sibling of `_find_pool_by_code` rather than one function branching on a
"which column" argument, so there is no code path where the two could be crossed by a typo in
a conditional. In `register_submit`, the two are mutually exclusive by construction: a
non-blank `commissioner_code` is checked first and is authoritative (an invalid one is a hard
error, exactly like an invalid `join_code` already is, and it never silently falls through to
try `join_code` instead); only when `commissioner_code` is entirely absent does the form fall
back to today's exact `join_code` handling, unchanged. `User.role` is always `"player"`
regardless of which branch runs; only `PoolMember.role_in_pool` ever varies, so a commissioner
invite link can never produce a global admin no matter what code is used or how the form is
manipulated, by construction, not by a runtime check somewhere that could be bypassed or
forgotten. `register_form` (`GET`) resolves the code up front too, purely for a better
greeting ("You are creating a commissioner account for {pool.name}") and to swap the plain
join-code field for a hidden field carrying the same code through to the `POST`; an unknown
or missing `commissioner_code` at `GET` renders the ordinary form, no error shown, no
regression.

**Rotate and set routes: `POST /admin/leagues/{pool_id}/commissioner-code` and
`.../commissioner-code/set`, in `app/routers/leagues.py`, gated `require_admin`.** Mirrors
`rotate_join_code`/`set_join_code` in `app/routers/admin.py` almost exactly (10-try
collision loop for the generated case, a 4-character minimum plus a clash check for the
hand-set case, `normalize_join_code` for uppercasing), but lives in the admin-only leagues
file and is gated `require_admin` rather than `require_commissioner`, matching today's
separate decision (already reflected in `app/routers/admin.py`'s `member_role`) that only the
site admin may ever create a commissioner. A pool's own real commissioner, even of their own
league, cannot rotate or set this code, proven by extending the existing
`test_leagues_admin_routes_refused_for_a_regular_player` and
`test_leagues_admin_routes_refused_for_a_pool_commissioner_who_is_not_a_site_admin`
parametrized boundary tests with the two new routes rather than writing a parallel pair of
tests, since the boundary being proven is identical to every other route already in that
list.

**UI: an expandable `<details>` per league row on `/admin/leagues`, not a separate page.**
Keeps the existing table from growing a wall of always-visible URLs; a native
`<details>`/`<summary>` disclosure needs no JavaScript to open (the same pattern
`how-it-works.html` and the scenario engine's representative-scenario panel already use), so
this stayed plain HTML. A pool with no code yet (only possible for one created by
`seed_admin`/the CLI after this migration, since both the backfill and `POST
/admin/leagues/new` always populate it going forward) shows a single "Create a commissioner
invite link" button instead of a blank link, which doubles as the first-time generation
action, no separate code path needed for "create" versus "rotate."

**Copy-to-clipboard: one small, generic helper added to `app/static/app.js`, not two.** No
copy-to-clipboard control existed anywhere in the app before this (checked: no
`clipboard`/`data-copy` hits anywhere in `app/` beforehand). Rather than writing one for the
commissioner link and a second for the player invite message, a single `data-copy="<css
selector>"` attribute plus one delegated click handler (`handleCopyClick`, wired into the
existing document-level `onClick`) serves both: it reads `.value` when the target is a form
control (the invite link `<input readonly>`, the invite message `<textarea readonly>`) or
`.textContent` otherwise, and falls back to a hidden textarea plus `execCommand("copy")` when
`navigator.clipboard`/`isSecureContext` are unavailable (a plain `http://localhost` dev
server, for one), so the button always does something instead of throwing. No new dependency.

**Player invite message on `/admin/members`, regenerated live from the pool's current join
code.** Sits in its own "Invite players" section right below the existing "Join code" panel.
Built the exact same way `pricing.html`'s existing refer-a-friend `mailto:` link already is
(`{% set %}` blocks for the subject and body, `| urlencode` on both, a plain `mailto:?subject=
...&body=...` href), so this introduces no new templating pattern, just a second use of one
already in the codebase. The message text and the `mailto:` body both read `pool.join_code`
directly at render time, never a value captured at some earlier point, so rotating the join
code and reloading the page is the only "update" step needed, nothing can go stale. Copy
reviewed for sentence case, plain language, and no em dash, matching the pricing page's own
refer-a-friend tone; `test_members_page_shows_the_invite_link_and_a_working_mailto` asserts
both the link and the code appear in the rendered `mailto:` href and body, and that no em dash
snuck into the generated copy.

**What this does not touch.** `POST /admin/leagues/new`'s existing "attach an existing user by
email" textarea is completely unmodified; that remains the path for a person the admin already
knows has an account. Nothing here sends an email or a text message on anyone's behalf: every
"invite" is a link plus a copy-paste template or a `mailto:` link that opens the commissioner's
own mail client, exactly like the pre-existing refer-a-friend feature, matching the standing
rule against ever sending on a user's behalf without their own explicit action.

**Verification.** `ruff check .`, `black .`, and `pytest -q` all clean, 818 passed, 0 failed
(806 at the previous post-launch baseline, 12 net new tests: the register/commissioner-code
happy path, the greeting on `GET /register` with a valid code, an invalid code rejected, a
missing code proven unchanged from today's behavior, the two-direction rotation-independence
test, hand-setting a commissioner invite code, a fresh league getting its own code distinct
from its join code, the player-invite-panel rendering check, plus the two new routes folded
into the existing admin-only boundary parametrizations rather than counted as wholly separate
tests). Migration applied cleanly against both the live dev database (backfilling two existing
pools, zero left null afterward) and a from-scratch database built through the full chain.
`grep -rn "—" app/ tests/ alembic/` finds only the literal em dash inside the new test's own
assertion string that checks for its absence, nothing in any real copy. No emoji anywhere
touched. No new dependency, no Tailwind or bundler, no SPA; the only new CSS is the small
`.invite-details`/`.invite-panel`/`.invite-message` block in `app/static/app.css`, built from
existing design tokens and reusing `.card`, `.field-label`, `.input`, `.row`, and `.form-hint`
rather than inventing parallel classes for the same shapes.
