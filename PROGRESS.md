# Week 1 readiness build: progress

Target: Week 1 slate opens Saturday, September 12, 2026. See `DECISIONS.md` for the
reasoning behind every judgment call made along the way.

- [x] Phase 0. Orientation and baseline
- [ ] Phase 1. Fix week resolution (per-league date window)
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
