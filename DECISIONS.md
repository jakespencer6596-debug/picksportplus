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

## Configuration defaults confirmed as-is

The three decisions raised in Part 4 of the brief were pre-filled with defaults in the
CONFIGURATION block and are being implemented as given, not re-litigated:

- `SLATE_SIZE=20`, `PICKS_REQUIRED=15`, confidence range `1..15` (Decision 1).
- No-show penalty is the maximum, `sum(1..PICKS_REQUIRED)` = 120 at 15 picks (Decision 2).
- Payout rules ship empty; the commissioner enters real dollar figures later (Decision 3).

No dollar amounts, entry fees, or Venmo handles are invented anywhere in this build.
