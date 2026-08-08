# Week 1 readiness build: progress

Target: Week 1 slate opens Saturday, September 12, 2026. See `DECISIONS.md` for the
reasoning behind every judgment call made along the way.

- [x] Phase 0. Orientation and baseline
- [x] Phase 1. Fix week resolution (per-league date window)
- [ ] Phase 2. Invert scoring, lowest total wins
- [ ] Phase 3. Pick 15 of 20
- [ ] Phase 4. Two-step pick entry
- [ ] Phase 5. Admin-curated slate with pinned games
- [ ] Phase 6. Navigation, sortable tables, transposed results grid
- [ ] Phase 7. Entry payment and payouts
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
