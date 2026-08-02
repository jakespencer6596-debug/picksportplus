# PickSportPlus

A weekly confidence pick'em pool for NFL and college football (FBS).

Every week the app publishes a slate of the **closest games**, the ones with the smallest
betting point spread, so you are picking genuine coin flips instead of blowouts. The default
slate is 20 games, 8 NFL and 12 college. You pick the straight-up winner of every game and
rank your confidence across the whole slate: your most confident pick stakes 20 points, your
least confident stakes 1, using each value exactly once. Correct picks earn the points staked,
wrong picks earn nothing.

The point spread only decides **which** games make the slate. Scoring is straight up: did you
pick the team that won.

The pool runs itself. An hourly job detects the current football week, builds the slate,
opens it, pulls final scores, and scores the week. After first time setup a whole season needs
zero manual steps.

---

## Quick start on Windows

From PowerShell in this folder:

```
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
python -m app.cli init-db
python -m app.cli seed-admin
python -m app.cli seed-demo
uvicorn app.main:app --reload
```

Then open http://localhost:8000 and sign in with the `ADMIN_EMAIL` and `ADMIN_PASSWORD` from
your `.env`.

The default local database is SQLite, so there is no Docker and no database server to install.
`.venv/`, `__pycache__/`, `*.db` and `.env` are all gitignored, which matters here because this
folder lives inside OneDrive.

If `python` opens the Microsoft Store instead of running, use `py` or the full venv path
`.\.venv\Scripts\python.exe`.

### The demo

`seed-demo` loads a **real completed week**: NFL and FBS week 5 of the 2025 season, with the
real kickoff times, the real closing point spreads and the real final scores, all replayed from
recordings committed under `tests/fixtures`. It needs no network and no API keys. It creates a
separate demo pool with five players and a full set of picks, already scored, so you can see
the slate, the picks screen, the results grid and both leaderboards straight away.

Sign in as any demo player with password `demo-pass-2025`:

```
dana@picksportplus.demo      marcus@picksportplus.demo    priya@picksportplus.demo
tom@picksportplus.demo       casey@picksportplus.demo
```

Run `python -m app.cli seed-demo --reset` to rebuild it.

---

## API keys

Scores need no key. **ESPN provides schedules, live status and final scores keyless and
unmetered**, and it is the only feed the app touches on a normal cron tick.

Two optional free keys improve spread coverage:

| Provider | Key | Free limit | What it is for |
| --- | --- | --- | --- |
| ESPN | none | none | Schedules, scores, and odds for games that have not finished |
| [The Odds API](https://the-odds-api.com/) | `ODDS_API_KEY` | 500 credits per month | Spread fallback, 1 credit covers a whole league |
| [CollegeFootballData](https://collegefootballdata.com/key) | `CFBD_API_KEY` | 1,000 calls per month | Last resort college only spread fallback |

The app works without either key. It just falls back to ESPN spreads alone.

### How the app stays inside the free limits

This is enforced in code, not left to good intentions.

- ESPN does all high frequency work. It is never budgeted or throttled.
- The Odds API and CFBD are called **only while building or refreshing a slate**, never on a
  page load and never on every cron tick.
- One Odds API spreads request returns an entire league (in testing, 272 upcoming NFL games)
  for **1 credit**, so a full NFL plus college refresh costs 2 credits. That response is cached
  and reused for `SPREAD_CACHE_MINUTES` (default 6 hours).
- Automatic spread refreshes are capped at `MAX_SPREAD_REFRESHES_PER_WEEK` (default 4).
- CFBD is used only when ESPN **and** The Odds API both lack a spread for a college game, at
  most `MAX_CFBD_CALLS_PER_WEEK` (default 1) lines call per week.
- Every response is cached as a last good value, so retries, re-runs and provider outages never
  spend extra credits.
- `x-requests-remaining` is read from every Odds API response and stored.
- A monthly counter per provider is checked against `ODDS_API_MONTHLY_BUDGET` (default 400) and
  `CFBD_MONTHLY_BUDGET` (default 800), both deliberately below the true free limits. When a
  provider hits its budget the app stops calling it, falls back to ESPN, and shows a warning on
  the admin page.

A realistic season costs roughly 2 to 8 Odds API credits per week and about 1 CFBD call per
week, well inside both free tiers.

Check where you stand at any time:

```
python -m app.cli usage
```

---

## Environment variables

Every variable is documented in `.env.example`. The ones that matter most:

| Variable | Default | Meaning |
| --- | --- | --- |
| `SECRET_KEY` | none | Signs the session cookie. Use a long random string in production. |
| `DATABASE_URL` | `sqlite:///./picksportplus.db` | SQLite locally, Postgres on Render. |
| `SEASON_YEAR` | `2026` | The season the pool runs. ESPN uses the year the season starts. |
| `OPEN_REGISTRATION` | `false` | `false` requires a join code to register. `true` lets anyone self register. |
| `NUM_GAMES_PER_WEEK` | `20` | Seed slate size for a new pool. |
| `NFL_GAMES_PER_WEEK` | `8` | Seed NFL target. |
| `NCAAF_GAMES_PER_WEEK` | `12` | Seed college target. |
| `ADMIN_EMAIL` / `ADMIN_PASSWORD` | none | The commissioner account created by `seed-admin`. |
| `DEFAULT_JOIN_CODE` | none | The join code for the default pool. |
| `TIMEZONE` | `America/New_York` | How kickoffs and lock times are displayed. |
| `OFFLINE_MODE` | `false` | Blocks all outbound HTTP. Used by the test suite. |

The slate size variables are only **seeds for a new pool**. Once the pool exists the
commissioner owns those numbers from Admin, Pool settings.

---

## The commissioner

Every pool has a commissioner, the league admin. Players cannot change any of this. From
`/admin` the commissioner controls:

- League name, join code (view, set or rotate), season year, timezone.
- Auto publish on or off, open registration on or off, the lock time.
- The total games per week, and how many come from each league.

### How the slate is chosen

The tool takes the closest `target_nfl` NFL games and the closest `target_ncaaf` college games
by absolute point spread, defaulting to 8 and 12. If one league is short of games with a usable
line that week, the gap is filled with the next closest games from the other league so the
total still lands, and the shortfall is reported on the admin page.

### Editing the slate

The tool always proposes a slate. The commissioner can override it from Admin, Slate:

- Change the total and the per league counts.
- Remove a proposed game, add any other game from that week's candidate pool, or swap one for
  another. The full candidate pool is listed with every game's line, source and kickoff.
- Set a line by hand for a game the feeds could not price, which rescues it for selection.
- Override the lock time, or clear the override to go back to the first kickoff.
- Void a game (a cancellation, or a game moved out of the week). Nobody scores a void and it
  leaves the possible count.

Timing rules:

- With **auto publish on** (the default) the tool builds and opens the slate by itself. The
  commissioner can still change the size and the games at any time until the first player
  submits a pick.
- With **auto publish off** the tool builds a draft and waits for the commissioner to publish.
- **Once any pick exists the game count is fixed for that week** and only voiding remains, so
  scoring stays consistent for everyone.

---

## Running a week end to end

The hourly cron does all of this. These are the manual equivalents.

```
python -m app.cli sync-week                  # detect the week, build it, publish it
python -m app.cli build-slate --week 6       # rebuild one week
python -m app.cli build-slate --week 6 --no-metered   # ESPN only, spends no credits
python -m app.cli publish-week --week 6      # open a draft
python -m app.cli fetch-results --week 6     # pull finals from ESPN
python -m app.cli score-week --week 6        # compute points, entries, standings
python -m app.cli run-cron                   # everything above, safely, in one go
python -m app.cli usage                      # metered API budget report
```

Lock is enforced at request time by comparing the clock against `lock_at`, so a late or missed
cron run can never hand anyone extra time to pick. A player who has not submitted by lock
scores 0 for the week.

Ties are voided: nobody scores them, and they leave both the correct and the possible counts.
Season standings aggregate the weekly rows into total points, total games correct, and weekly
wins, where a tie on points shares the win.

---

## Deploying to Render

1. `git init` if you have not already, then push this folder to a new GitHub repository.
   Check that `.env` and your API key `.txt` files are **not** in the push. They are gitignored.
2. In Render, choose New, then Blueprint, and point it at the repository. `render.yaml` defines
   the web service, the Postgres database and the hourly cron job.
3. Fill in the secret environment variables in the dashboard: `ODDS_API_KEY`, `CFBD_API_KEY`,
   `ADMIN_EMAIL`, `ADMIN_PASSWORD`, `ADMIN_DISPLAY_NAME`, `DEFAULT_JOIN_CODE`. Copy the web
   service's generated `SECRET_KEY` into the cron service so both sign cookies the same way.
4. Deploy. Migrations run automatically on every deploy via the pre-deploy command
   (`python -m app.cli init-db`).
5. Once, after the first deploy, open a shell on the web service and run:
   `python -m app.cli seed-admin`

The cron runs `python -m app.cli run-cron` at 17 minutes past every hour. It is idempotent, so
a repeated or missed run causes no damage.

### Cost note

Render's free web tier sleeps after about 15 minutes idle and its free Postgres expires after
30 days. For an always on season budget about 7 dollars per month for the web Starter plan plus
about 6 dollars per month for Postgres, roughly 13 dollars per month. Or keep the free web
service and point `DATABASE_URL` at a free external Postgres such as Neon or Supabase to stay
near zero. The app must run correctly in either setup, it only needs a standard Postgres
`DATABASE_URL`.

---

## Project layout

```
app/
  main.py            FastAPI app, middleware, routers, error pages
  config.py          settings from .env
  db.py              engine and session
  models.py          SQLAlchemy models
  auth.py            hashing, sessions, current-user and commissioner dependencies
  slate.py           pure closest-spread selection with per league targets (unit tested)
  scoring.py         pure scoring (unit tested)
  templating.py      Jinja environment, date filters, the render helper
  providers/
    http.py          timeouts, retry, last good caching, the credit governor
    espn.py          scoreboard, core API historical odds, week detection
    odds_api.py      The Odds API spreads
    cfbd.py          CollegeFootballData lines
    teams.py         name normalization and cross provider matching
  services/
    ingest.py        candidates, spread resolution, slate build, commissioner edits
    results.py       finals from ESPN, scoring a week
    standings.py     weekly and season leaderboards
    demo.py          the recorded historical week
  routers/           auth, picks, leaderboard, results, admin
  templates/         Jinja pages and partials
  static/            app.css, app.js, vendored htmx and SortableJS
alembic/             migrations
tests/               unit tests plus recorded provider fixtures
```

## Tests

```
.\.venv\Scripts\python.exe -m pytest tests -q
```

Provider tests run against recorded JSON fixtures and never touch the live APIs. A session
wide fixture forces `OFFLINE_MODE` on so no test can make a network call by accident.

## Notes on the data feeds

Two behaviours were confirmed against the live APIs on 2026-08-02 and shaped the design:

- **ESPN drops `odds` from the scoreboard once a game is completed.** Historical spreads
  therefore come from the ESPN core API (`sports.core.api.espn.com`), which retains them. That
  is what lets the demo week carry real closing lines.
- **CollegeFootballData's row `id` is the ESPN event id**, verified 53 out of 53 on a sample
  week. The college fallback is an exact id join, not fuzzy name matching, which removes the
  largest source of cross provider error.

Both CFBD's `spread` and The Odds API's home team `point` are already home relative with a
negative value meaning the home team is favoured, matching the convention used throughout the
app. ESPN's raw `spread` sign is not trustworthy, so it is derived from the explicit
`favorite` flags instead.
