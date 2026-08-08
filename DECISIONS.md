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
