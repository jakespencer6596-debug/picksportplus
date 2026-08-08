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

## Configuration defaults confirmed as-is

The three decisions raised in Part 4 of the brief were pre-filled with defaults in the
CONFIGURATION block and are being implemented as given, not re-litigated:

- `SLATE_SIZE=20`, `PICKS_REQUIRED=15`, confidence range `1..15` (Decision 1).
- No-show penalty is the maximum, `sum(1..PICKS_REQUIRED)` = 120 at 15 picks (Decision 2).
- Payout rules ship empty; the commissioner enters real dollar figures later (Decision 3).

No dollar amounts, entry fees, or Venmo handles are invented anywhere in this build.
