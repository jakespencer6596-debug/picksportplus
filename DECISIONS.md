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
than inventing scores. The resulting college week 3 window (2026-09-07 to 2026-09-14) and the
existing, already recorded 2026 NFL calendar fixture's week 1 window (2026-09-09 to
2026-09-16) both contain September 12, 2026, which is what pins the concrete real world case
in the brief: the launch date is NFL week 1 and college week 3.

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
