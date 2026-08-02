# PickSportPlus

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/jakespencer6596-debug/picksportplus)

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

## Deploy the free demo

The button at the top of this README deploys a **free, fully seeded demo**: one free Render
web service and nothing else. There is nothing to paste. `SECRET_KEY` is generated by Render,
and the demo needs no API keys at all.

**Storage.** The demo runs on SQLite on the instance's own disk, because Render allows only
one free Postgres per workspace and yours may already be in use. That disk is ephemeral: when
the free service sleeps or redeploys, the file is lost and the start command rebuilds the demo
from the recordings in `tests/fixtures`. The seeded week, the standings and both demo logins
are therefore always present, and the demo always looks right. What does not survive a restart
is anything a visitor typed, such as picks they made while clicking around.

To make it permanent and still free, create a database at [Neon](https://neon.tech) and set
`DATABASE_URL` on the service to its connection string. That is the only change, no redeploy
of code needed, and Neon's free tier does not expire the way Render's 30 day free Postgres
does.

The demo runs entirely on the real 2025 week 5 recordings committed in `tests/fixtures`, and
`OFFLINE_MODE` blocks outbound HTTP, so the deployed site never calls ESPN, The Odds API or
CollegeFootballData and never spends a metered credit.

The start command migrates and seeds before serving, and every step is idempotent:

```
alembic upgrade head && python -m app.cli seed-admin && python -m app.cli seed-demo && uvicorn app.main:app --host 0.0.0.0 --port $PORT --proxy-headers --forwarded-allow-ips="*"
```

`seed-admin` does nothing unless you set `ADMIN_EMAIL` and `ADMIN_PASSWORD`, and `seed-demo`
does nothing once the demo pool exists, so a redeploy or a wake from sleep repeats it safely.

### Demo logins

Both accounts use the password `demo-pass-2025`.

| Role | Email | What it shows |
| --- | --- | --- |
| Commissioner | `commissioner@picksportplus.demo` | The admin side: pool settings, the slate editor, members |
| Player | `player@picksportplus.demo` | The ordinary side: picks, standings, results |

Registration is closed on the demo (`OPEN_REGISTRATION=false`), so nobody can sign themselves
up. Add players by sharing the pool join code from the admin Members page.

### What to expect on the free plan

- **The web service sleeps after about 15 minutes idle** and takes roughly a minute to wake on
  the next visit. That is normal on Render's free plan. The first click on a shared link may
  therefore feel slow.
- **The demo database is ephemeral.** See the storage note above. Swap `DATABASE_URL` to a free [Neon](https://neon.tech) database to make it permanent. Neon does not expire, unlike Render's 30 day free Postgres.
  past 30 days, point `DATABASE_URL` at a free external Postgres such as
  [Neon](https://neon.tech), which does not expire. It is a one line swap: replace the
  `fromDatabase` block for `DATABASE_URL` in `render.yaml` with your Neon connection string,
  or just paste the Neon URL into `DATABASE_URL` in the Render dashboard and delete the Render
  database. The app only needs a standard Postgres URL.
- **Live weekly automation is intentionally left out.** The auto slate, live scores and scoring
  are the `run-cron` command, and Render does not offer cron jobs on the free plan. Enable it
  later on a paid plan, or run `python -m app.cli run-cron` hourly from any external scheduler
  against the same `DATABASE_URL`. Everything it does is idempotent.

## Deploying the full app to Render (paid, with live automation)

The committed `render.yaml` is deliberately the **free demo** blueprint: one free web service,
one free database, no cron. To run a real season with live automation you need a paid plan,
because the scheduled job is what builds each week's slate and pulls scores.

1. Deploy the blueprint as above, then upgrade the web service and database off the free plan.
2. Set the real keys in the dashboard: `ODDS_API_KEY`, `CFBD_API_KEY`. Set `ADMIN_EMAIL`,
   `ADMIN_PASSWORD` and `DEFAULT_JOIN_CODE` so `seed-admin` creates your own pool on the next
   deploy. Set `OFFLINE_MODE=false` so the app is allowed to reach the feeds at all.
3. Add the cron service to `render.yaml` and redeploy:

```yaml
  - type: cron
    name: picksportplus-cron
    runtime: python
    plan: starter
    region: oregon
    schedule: "17 * * * *"   # hourly, off the hour
    buildCommand: pip install -r requirements.txt
    startCommand: python -m app.cli run-cron
    envVars:
      - key: DATABASE_URL
        fromDatabase:
          name: picksportplus-db
          property: connectionString
      - key: SECRET_KEY
        sync: false          # paste the web service's generated value
      - key: OFFLINE_MODE
        value: "false"
      - key: ODDS_API_KEY
        sync: false
      - key: CFBD_API_KEY
        sync: false
```

`run-cron` is idempotent, so a repeated or missed run causes no damage. If you would rather not
pay Render for a cron service, run the same command hourly from any external scheduler (a
GitHub Actions schedule, a home server, cron on any box) pointed at the same `DATABASE_URL`.

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
