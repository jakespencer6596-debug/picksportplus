# PickSportPlus

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/jakespencer6596-debug/picksportplus)

A weekly confidence pick'em pool for NFL and college football (FBS).

Every week the app publishes a slate of the **closest games**, the ones with the smallest
betting point spread, so you are picking genuine coin flips instead of blowouts. The default
slate is 20 games, 8 NFL and 12 college. You pick the straight-up winner of exactly 15 of them
(both numbers are commissioner settings) and rank your confidence across only the games you
picked: your most confident pick stakes 15 points, your least confident stakes 1, using each
value exactly once. The pool's real, default rule is **inverse scoring**: a wrong pick counts
its staked points against you, a correct pick costs nothing, and the lowest weekly total wins.
The older rule (correct picks earn their staked points, highest total wins) is still available
per pool from Admin, Pool settings.

The point spread only decides **which** games make the slate. Scoring is straight up: did you
pick the team that won.

The pool mostly runs itself. An hourly job detects the current football week and builds a
draft slate; a commissioner reviews and publishes it by hand by default (a pool can switch
back to fully automatic publishing from Pool settings). Once published, the same job pulls
final scores and scores the week with no further action needed.

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

`seed-demo` loads **three real weeks** into a separate demo pool: NFL and FBS weeks 5 and 6 of
the 2025 season, real kickoff times, real closing point spreads and real final scores, all
replayed from recordings committed under `tests/fixtures`, plus an open current week (week 7)
with no picks yet, published from a reused real historical slate with an artificially future
lock time so you can walk the live pick flow. It needs no network and no API keys. Eight demo
players pick exactly 15 of the 20 published games each (not the whole slate), both historical
weeks are fully scored by default with weekly winners, season standings and a labelled demo
payout structure, one player sits out week 5 entirely (the no-show rule), and one week 5 game
is voided (the void scoring rule), so those edge cases are visible on screen, not just in
tests. Pass `--scenario-week` to leave week 6 partially played instead of fully scored, so the
Scenarios panel (Weekly Results) has a real week to open against.

Sign in as any demo player with password `demo-pass-2025`:

```
dana@picksportplus.demo      marcus@picksportplus.demo    priya@picksportplus.demo
tom@picksportplus.demo       casey@picksportplus.demo     jordan@picksportplus.demo
sam@picksportplus.demo
```

Run `python -m app.cli seed-demo --reset` to rebuild it, or
`python -m app.cli seed-demo --reset --scenario-week` for the partially played scenario-engine
variant of week 6.

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
| `WEEK1_ANCHOR_DATE` | `2026-09-12` | The Saturday pool week 1 anchors to. Each league resolves its own ESPN week from this date; see "How the slate is chosen." |
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

- League name, join code (view, set or rotate), season year, week 1 anchor date, timezone.
- Auto publish on or off (off by default), open registration on or off, the lock time.
- The total games per week, how many come from each league, and the pinned rivalry list.

### How the slate is chosen

The tool takes the closest `target_nfl` NFL games and the closest `target_ncaaf` college games
by absolute point spread, defaulting to 8 and 12. If one league is short of games with a usable
line that week, the gap is filled with the next closest games from the other league so the
total still lands, and the shortfall is reported on the admin page.

A game can also be **pinned** so it always makes the slate regardless of its spread, because
closest-spread selection alone tends to drop a rivalry game the moment either side is having a
lopsided season. Pins are set by hand from the slate editor, or automatically the first time a
game matching one of the pool's configured rivalry pairs (Admin, Pool settings) is created; an
unpin sticks across later rebuilds. The slate editor always shows why a game is on the slate:
pinned, a rivalry match, or the closest spread.

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

- With **auto publish off** (the default) the tool builds a draft and waits for the
  commissioner to review and publish it by hand.
- With **auto publish on** the tool builds and opens the slate by itself. The commissioner can
  still change the size and the games at any time until the first player submits a pick.
- **Once any pick exists the game count is fixed for that week** and only voiding remains, so
  scoring stays consistent for everyone.

### Payouts

From Admin, Payouts the commissioner sets how the pool's money moves. Set an entry fee per
member, paid status tracked from the Members page, or type in a flat pot override that always
wins over the computed figure. Configure each of the four payout ladders (weekly, bowl, season
points, season wins) place by place, or click "Load preset" to seed a known dollar ladder for
all four at once, safe to click again later to reset back to it. A payout amount freezes the
instant the week (or the season) it belongs to finishes scoring: what a player was owed for a
past week never quietly changes later just because more members pay their entry fee after the
fact. The Payouts summary page lists what every player is owed across all four scopes, with a
running paid and unpaid total and a plain text or CSV export for bookkeeping outside the app.

---

## Running a week end to end

The hourly cron does all of this. These are the manual equivalents.

```
python -m app.cli sync-week                  # detect the week, build it, publish only if auto publish is on
python -m app.cli build-slate --week 6       # rebuild one week
python -m app.cli build-slate --week 6 --no-metered   # ESPN only, spends no credits
python -m app.cli publish-week --week 6      # open a draft
python -m app.cli fetch-results --week 6     # pull finals from ESPN
python -m app.cli score-week --week 6        # compute points, entries, standings
python -m app.cli run-cron                   # everything above, safely, in one go
python -m app.cli usage                      # metered API budget report
python -m app.cli payouts-show               # print the payout ladder and allocation summary
python -m app.cli payouts-preset             # load the known preset ladder for all four scopes
python -m app.cli payouts-snapshot --scope weekly --week 6   # freeze one scope's awards by hand
python -m app.cli payouts-summary            # print what every player is owed, paid and unpaid
```

Lock is enforced at request time by comparing the clock against `lock_at`, so a late or missed
cron run can never hand anyone extra time to pick. A player who has not submitted by lock is
flagged as a no-show: 0 for the week under `standard` scoring, or the maximum possible penalty
under `inverse` (the pool's real, default rule), and never eligible to win the week either way.

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
is anything a visitor typed, such as picks they made while clicking around, or a payout ladder
a commissioner configured by hand from `/admin/payouts`: `seed-demo` reseeds its own known
payout ladder on every restart, but any change made on top of that by hand in the hosted demo
is gone the next time the free service sleeps or redeploys.

### Persistent database (Neon), required before real players join

Do this before inviting anyone who is going to play for money. SQLite on the instance disk
is demo-only: it is wiped every time the free service sleeps or redeploys, taking every
account, league, pick, payout rule and award with it.

1. Create a free database at [Neon](https://neon.tech). Its free tier does not expire, unlike
   Render's 30 day free Postgres.
2. Copy its connection string and set it as `DATABASE_URL` on the Render service (Render
   dashboard, Environment tab). `render.yaml` leaves `DATABASE_URL` for you to paste in at
   deploy time for exactly this reason, rather than defaulting to the disposable file.
3. Redeploy. No code change needed; the app only needs a standard Postgres URL and already
   runs `alembic upgrade head` on every boot.
4. Confirm it stuck: run `python -m app.cli doctor` (or check the site admin dashboard,
   which shows the same warning) and confirm the storage line no longer reads ephemeral.

The app also warns you if you skip this: a loud line in the server log at boot, a persistent
brick-colored banner on every page the site admin views, and a check in `python -m app.cli
doctor` that reports row counts and the age of the oldest row, so you can see at a glance
whether the last restart actually kept the data.

The demo runs entirely on the real 2025 weeks 5 and 6 recordings committed in `tests/fixtures`,
and `OFFLINE_MODE` blocks outbound HTTP, so the deployed site never calls ESPN, The Odds API or
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
- **The demo database is ephemeral.** See "Persistent database (Neon)" above. Paste a Neon
  connection string into `DATABASE_URL` in the Render dashboard to make it permanent; the app
  only needs a standard Postgres URL.
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
