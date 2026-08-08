# PickSportPlus. Build specification

This is the authoritative specification for the application. Two rules override
everything if there is ever a conflict: follow the Design System in Section 3
exactly, and follow the Copy Rules in Section 3h exactly.

## 1. What the product is

PickSportPlus is a weekly confidence pick'em pool for NFL and college football (FBS). It recreates a classic confidence pool:

- Each week the app publishes a slate of games. The default is 20 games, split 8 NFL and 12 college (FBS). The total and the per league counts are commissioner settings.
- The slate is chosen to be the closest games that week, meaning the games with the smallest betting point spread, so players pick genuinely competitive matchups instead of blowouts. Closest is judged within each league against that league's target count.
- Each player picks the straight-up winner of exactly `picks_required` of the slate games (the default is 15, out of the default 20 game slate) and ranks their confidence across only the games they picked, as one combined ranking. A player assigns `picks_required` points to their most confident pick down to 1 point for their least confident pick, using each value from 1 to `picks_required` exactly once. A slate game a player does not pick is legal to leave alone; it simply scores nothing for that player. Both the slate size and `picks_required` are commissioner settings, read from the pool, never hard coded.
- When a game goes final, a correct pick earns the points staked on it and a wrong pick earns 0.
- The app maintains leaderboards all season: weekly points, season total points, games correct, and weekly wins.

The point spread is used only to select the slate (pick the closest games). Scoring is straight-up, meaning did the player pick the team that actually won. Do not implement against-the-spread scoring.

## 2. Audience and the private to public design

Launch is a private pool for the owner and friends, but the data model must make opening it to the public later trivial.

- Model a Pool (league) with a unique join code. Users join a pool with its code or an admin adds them.
- Global env flag `OPEN_REGISTRATION` (default false). When false, a new user can register only with a valid pool join code (private mode). When true, anyone can self register and join pools (public mode). Build both paths now and default to private.
- v1 ships with one seeded default pool, but the schema supports many pools and many members per pool.
- Roles: a global `admin` role (the commissioner) and normal `player` roles. A pool also has a commissioner. For v1 the seeded admin is the commissioner of the default pool.

## 3. Design system (this is not optional, match it precisely)

The look is a warm collegiate throwback: a classic varsity sports club feel, light theme, tidy and confident. It must look designed by a person, not generated. Do not build a dark theme or a theme toggle for v1. Build light only, but structure tokens so a dark theme could be added later.

### 3a. Brand and wordmark

The product name is PickSportPlus. Create a simple, classic logo as inline SVG: a rounded varsity shield or pennant badge containing the monogram PSP, drawn in the deep green with a thin gold outline. Next to the badge, set the wordmark PickSportPlus in the display font, with the word Plus tinted gold. Keep it clean and small. No mascots, no clip art.

### 3b. Color palette (exact values, use CSS custom properties)

Author these as CSS variables in `static/app.css`. Verify text contrast meets WCAG AA and darken a token if any pairing falls short.

```
:root {
  --paper:        #F4ECDA;  /* page background, warm cream */
  --surface:      #FFFDF7;  /* cards and panels */
  --surface-2:    #FBF4E6;  /* subtle alternate surface, table stripes */
  --border:       #E3D7BE;  /* warm hairline borders */
  --ink:          #1E1B15;  /* primary text */
  --ink-soft:     #574E40;  /* secondary text, must pass AA on paper */
  --green:        #16432B;  /* primary brand, deep collegiate green */
  --green-strong: #10371F;  /* hover and pressed */
  --green-tint:   #E6EDE4;  /* selected pick background, correct fill */
  --maroon:       #6E1E2A;  /* secondary accent */
  --gold:         #C0942F;  /* antique gold, decorative and accents only */
  --gold-soft:    #E8D28A;  /* ribbons, badges backgrounds */
  --brick:        #9B3B2C;  /* loss or incorrect */
  --focus:        #C0942F;  /* focus ring */
  --shadow:       0 1px 2px rgba(30,27,21,.06), 0 10px 26px rgba(30,27,21,.05);
  --radius:       10px;
  --radius-sm:    8px;
}
```

Rules: green is the primary action and the correct or win state. Maroon is secondary emphasis and can accent the away side. Gold is decorative only (thin rules under labels, badge outlines, winner ribbons, focus ring). Gold never carries small body text. On any gold surface, text is `--ink`. Incorrect or loss uses `--brick`.

### 3c. Typography

Load from Google Fonts with preconnect. Display font is Oswald (condensed, varsity). Body font is Libre Franklin (warm, readable).

```
--font-display: "Oswald", "Arial Narrow", system-ui, sans-serif;
--font-body:    "Libre Franklin", system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
```

Use the display font for the wordmark, page and section headlines, eyebrow labels (uppercase with about 0.06em letter spacing), team abbreviations, scores, and point values. Use the body font for everything else. Apply `font-variant-numeric: tabular-nums` to all scores, point totals, and standings numbers so columns align. Set a comfortable base size (16px minimum on mobile) and a clear type scale. Do not center long text blocks.

### 3d. Spacing, radius, elevation

Use a 4px spacing scale (4, 8, 12, 16, 20, 24, 32, 40, 48, 64). Cards use `--radius`, buttons and inputs use `--radius-sm`. Rank badges may be circular. Do not round everything heavily. Borders are 1px `--border`. Elevation is the single soft `--shadow`, used sparingly. No neon glows, no heavy drop shadows.

### 3e. Iconography

Use a small, consistent line icon set (Lucide) as inline SVG, sized to match the text, used sparingly in navigation and status. Never use emoji anywhere.

### 3f. Components (build these as reusable partials and CSS classes)

- Top bar: badge and wordmark on the left, primary nav, a user menu on the right. Sticky on scroll.
- Bottom tab bar on mobile only: This Week, Standings, Results, and Admin when the user is a commissioner. Icons plus short labels. Hidden on desktop.
- Buttons: primary is solid green with cream text; secondary is a green outline on surface; both have a clear pressed and focus state (gold focus ring). Minimum height 44px.
- Card and panel: surface background, hairline border, soft shadow, tidy 16 to 20px padding.
- Game row (the core pick control): shows away team then home team, each with abbreviation, full name, and optional record. Tapping a team selects it as the winner and fills that side with `--green-tint` and a small check. Shows the informational line in small muted text, for example "Line GB -2.5". Includes a drag handle and a confidence chip showing the current point value.
- Confidence chip: a compact pill in the display font showing the staked points for that game.
- Lock countdown: a slim sticky banner under the top bar on the picks page, for example "Picks lock in 2d 4h. Kickoff Thursday 8:15 PM ET".
- Leaderboard: a real table on desktop (rank, player, points, correct, weekly wins) with the leader row accented by a thin gold ribbon; the same data as stacked cards on mobile.
- Standings rank badge: circular, with 1st in gold, 2nd and 3rd in muted tones.
- Winner ribbon: a small pennant or ribbon motif marking the weekly winner. Tasteful, not loud.
- Empty and pending states: friendly, on brand copy, never a blank screen. Example, before a slate publishes: "The Week 6 slate opens Tuesday. Check back to make your picks."
- Flash and toast messages for save success and errors, in theme.

### 3g. Layout and responsiveness (mobile first, must be excellent on phone and desktop)

Design mobile first, then enhance for wider screens. Content column max width about 1080px on desktop with comfortable gutters. On mobile use full width cards, the bottom tab bar, and large tap targets (minimum 44px). The picks list is a single column on mobile and may use a two column grid on wide desktop. Every table reflows to stacked cards below the medium breakpoint. Dragging to rank must work with touch (SortableJS touch support) and also expose up and down buttons on each game row as an accessible, keyboard operable fallback. Test at 360px, 768px, and 1280px widths and make all three look intentional. Respect `prefers-reduced-motion`.

### 3h. Copy rules (enforce everywhere, in UI text and in this codebase)

- Never use em dashes anywhere. Use periods, commas, colons, or parentheses. Hyphens in scores, ranges, and code are fine.
- Never use emoji.
- No random or inconsistent spacing. Align to the spacing scale.
- Sentence case for body text. Uppercase only via CSS for eyebrow labels and team abbreviations.
- Voice is a confident, plainspoken sports club. Concise, no marketing fluff, no filler.

### 3i. Do not look generated (anti template checklist)

- Do not use default indigo, purple, or blue to purple gradients.
- Do not build a generic full width gradient hero.
- Do not use emoji as icons or decoration.
- Do not center everything or oversize every corner radius.
- Do not use dark sportsbook neon effects, that is the wrong vibe.
- Do use the specified fonts, the warm palette, hairline borders, and consistent 4px spacing.
- Do use real team names and real historical results in the demo data, never Lorem Ipsum or fake teams.
- Aim for a polished, human, collegiate sports club result.

### 3j. Accessibility

Semantic HTML, labeled form controls, visible gold focus rings, AA contrast, full keyboard operation of the ranking (via the up and down fallback), and reduced motion support.

## 4. Tech stack (use exactly this)

- Python 3.11+.
- FastAPI plus Uvicorn.
- Jinja2 server rendered pages. This is a server rendered app, one deployable service, not a separate SPA.
- HTMX for partial updates and SortableJS for the drag to rank UI. Vanilla JS only, no bundler.
- Styling is hand authored CSS in `static/app.css` using the design tokens and semantic component classes above. Do not use Tailwind or any CSS framework, so the look stays custom and there is no build step. Fonts load from Google Fonts.
- SQLAlchemy 2.x plus Alembic migrations.
- SQLite for local dev (default `DATABASE_URL=sqlite:///./picksportplus.db`, zero setup on Windows) and PostgreSQL in production on Render via `DATABASE_URL`. Keep all models and queries database agnostic.
- Auth: email and password, hashed with `passlib[bcrypt]`, sessions via Starlette `SessionMiddleware` (signed cookie using `SECRET_KEY`). No third party auth service.
- `httpx` for calling ESPN, The Odds API, and CFBD, with timeouts and light retry.
- `pydantic-settings` for config from `.env`.
- `typer` CLI at `app/cli.py` for the ingest and scoring commands. Render Cron Jobs call these. Do not rely on an always on in process scheduler.
- `pytest`. Pure logic unit tests for scoring and slate selection are mandatory. Provider tests use recorded JSON fixtures and never hit live APIs.
- `ruff` plus `black`, keep the tree clean.
- Pin versions in `requirements.txt`.

## 5. Data sources and integration

All three providers are free. ESPN is the primary source for schedule, live status, final scores, and the spread when present. The Odds API and CFBD are spread fallbacks. Cache the last good response per call and degrade gracefully on failure.

### 5a. ESPN (primary, no key)

- NFL scoreboard: `https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard?seasontype=2&week={W}&dates={YEAR}`
- College FBS scoreboard: `https://site.api.espn.com/apis/site/v2/sports/football/college-football/scoreboard?groups=80&seasontype=2&week={W}&dates={YEAR}` (`groups=80` is FBS, `seasontype=2` is regular season, `3` is postseason)
- Single game detail if needed: `.../summary?event={espn_event_id}`

NFL and college football week numbers are not aligned: college week 1 starts about three weeks before NFL week 1, they stay that far apart all season, and college has a bowl season the NFL has no equivalent of on the same calendar. So a pool's own week number is never sent to ESPN directly as a week number for both leagues. Instead each pool week carries an `anchor_date` (a calendar Saturday), and each enabled league independently resolves its own ESPN week number and season type by finding the calendar entry whose `[startDate, endDate)` window contains that date, trying the regular season first and the postseason (bowl season) second. See `app/services/calendar.py` and Section 6. From each event read: event id, kickoff datetime, home and away team (name plus abbreviation, optional record), final scores, whether the game is completed, the winner flag per competitor, and when present the odds under `competitions[0].odds[]`.

**Verified field behaviour (recorded 2026-08-02 against the live API):**

- `leagues[0].calendar` is a list of season-type groups, each `{label, value, startDate, endDate, entries: [{label, alternateLabel, detail, value, startDate, endDate}]}`. `value` on an entry is the week number. This is the authoritative source for week detection.
- Top level `season` is `{type, year}` and `week` is `{number, teamsOnBye}`.
- `status.type.completed` is the boolean for "final". `status.type.state` is `pre`, `in` or `post`.
- Each competitor has `homeAway`, `score` (string), `winner` (bool), `team.{abbreviation,displayName,location,name,shortDisplayName}` and `records[]` where `type == "total"` carries the "3-2" summary.
- **The scoreboard omits `competitions[0].odds` for completed games.** Odds are present only for upcoming and in-progress games. Do not expect to backfill a historical week from the scoreboard.
- The odds object carries `details` ("CAR -1.5"), `spread`, `overUnder`, and `homeTeamOdds`/`awayTeamOdds` each with a `favorite` boolean. The `favorite` booleans plus `abs(spread)` are the reliable way to derive the home relative spread. Do not trust the raw sign of `spread`.

### 5a-2. ESPN core API (historical odds, no key)

`https://sports.core.api.espn.com/v2/sports/football/leagues/{nfl|college-football}/events/{id}/competitions/{id}/odds`
returns `{count, items: [...]}` where each item has `provider`, `details`, `spread`, `overUnder`,
`awayTeamOdds.favorite`, `homeTeamOdds.favorite`. This endpoint **does** retain odds for
completed games, which is how a historical demo week gets real spreads. Treated as spread
source `espn_core`.

### 5b. The Odds API (spread source, free key)

- Base: `https://api.the-odds-api.com/v4`
- Sport keys: NFL `americanfootball_nfl`, college `americanfootball_ncaaf`.
- Example spreads request: `https://api.the-odds-api.com/v4/sports/americanfootball_nfl/odds?apiKey={ODDS_API_KEY}&regions=us&markets=spreads&oddsFormat=american&dateFormat=iso`
- Cost is credits = markets times regions, so one spreads plus us request is 1 credit. Free plan is 500 credits per month. Read the `x-requests-remaining` response header, log it, and back off when low.
- The `/odds` endpoint returns upcoming games with no week parameter. Each event has `commence_time`, `home_team`, `away_team`, and `bookmakers[].markets[].outcomes[]` where each outcome has a team `name` and a `point` (the spread). Compute the game spread as the median of the home team point across returned US bookmakers, and `closeness = abs(spread)`.
- Match events to ESPN by kickoff date plus normalized team name (see 5d).

### 5c. CollegeFootballData (college fallback, free key)

- Auth header: `Authorization: Bearer {CFBD_API_KEY}`.
- Games: `https://api.collegefootballdata.com/games?year={YEAR}&week={W}&seasonType=regular`
- Lines (spreads): `https://api.collegefootballdata.com/lines?year={YEAR}&week={W}&seasonType=regular`
- Free tier is 1,000 calls per month. Use as a college only fallback when ESPN lacks a spread and The Odds API did not cover the game.

### 5d. Team matching utility (`app/providers/teams.py`)

Cross provider matching is the main integration risk. Build a normalization plus alias layer: lowercase, strip punctuation, expand or standardize "University" and "State", and map common aliases and mascots to a canonical team key. Match candidate games on normalized home, normalized away, and kickoff date. Store the canonical keys on each game row. When a fallback spread cannot be confidently matched, leave the spread null and surface the game in the admin review screen rather than guessing.

### 5e. Spread resolution order per game

1. ESPN odds if present and parseable.
2. Else ESPN core API odds (this is what covers completed games).
3. Else The Odds API, matched by date plus normalized names.
4. Else, college games only, CFBD lines.
5. Else spread is null, excluded from automatic selection, shown to admin.

## 6. Slate selection, closest spreads

Given a pool week (its own season year and week number, plus its `anchor_date`):

1. For each enabled league, resolve the ESPN week number and season type from `anchor_date` (Section 5a), then pull that league's games from ESPN (FBS games use `groups=80`). A league whose anchor date falls outside both its regular season and its postseason resolves to no games for that week, which is not an error: the slate is built from whichever leagues did resolve. The resolution actually used is recorded on the week (`resolved_weeks`, `is_bowl_week`).
2. Resolve each candidate home relative spread via 5e and compute `closeness = abs(spread)`.
3. Drop candidates with no resolvable spread (still visible to admin) and optionally drop games already kicked off.
4. Sort by closeness ascending (a pick'em at 0 is closest). Break ties by earliest kickoff, then by key so the result is stable.
5. Take the closest `target_nfl` NFL games and the closest `target_ncaaf` college games. Defaults are 8 and 12, for a `num_games_per_week` total of 20. All three are commissioner settings.
6. Shortfall handling. If a league has fewer games with a resolvable spread than its target, fill the gap with the next closest games from the other league so the total still matches, and record that it happened so the commissioner can see it. If the targets add up to more than the total, drop the farthest games until the total is met.
7. Compute `lock_at` as the earliest kickoff among the selected slate games, unless the commissioner set a manual lock time.

Put the selection in `app/slate.py` as a deterministic pure function and unit test it thoroughly (ties, missing spreads, a league short of its target, targets that do not add up to the total, and stability under input shuffling).

### 6a. Commissioner control over the slate

The tool always proposes a slate. The commissioner can override it.

- The admin slate editor shows the proposed slate and the full candidate pool for the week, every NFL and FBS game with its resolved spread, spread source and kickoff time.
- The commissioner can change the total and the per league counts, remove a proposed game, add any candidate game, and swap one game for another. An edited slate overrides the proposal.
- When `auto_publish` is on (the default) the tool builds and opens the proposed slate automatically. The commissioner may still change the size and the games at any time while no player has submitted a pick for that week.
- When `auto_publish` is off the tool builds a draft and the commissioner reviews, edits and publishes it by hand.
- Voiding a game is always available, including after picks exist.
- Once any pick exists for a week the game count is fixed for that week and only voiding remains, so scoring stays consistent.

## 7. Automation and the weekly lifecycle (set and forget)

The pool runs itself. `pool.auto_publish` defaults to true.

- Detect the pool's current or upcoming week automatically. When `pool.week1_anchor_date` is set this is date arithmetic against that anchor, not an ESPN lookup. A pool with no anchor configured falls back to asking ESPN what NFL's current week number is, which the commissioner should replace with a real anchor date before the season NFL and college drift out of step (see Section 5a). Do not require anyone to set the week by hand once an anchor is configured.
- A scheduled job builds the slate for the upcoming week, and because `auto_publish` is true, opens it automatically (status open) and sets `lock_at`. No human action is required to run a normal week.
- The commissioner may still adjust a published slate before `lock_at`, but only non destructive changes once picks exist. Before any picks exist, free edits are allowed. After picks exist, allow voiding a game but not reshuffling the whole slate.
- `pool.current_week` advances automatically as the calendar moves.
- A results job runs frequently on game days, pulls finals, and scores idempotently. When every slate game is final, mark the week scored.
- Net effect: after first time setup, a full season needs zero manual steps. Lock is enforced at request time by comparing now with `lock_at`, so picks stay honest even if a job is delayed.

## 8. Picks and confidence UI

For a week that is open and before `lock_at`:

- The This Week page lists the slate games with matchup, kickoff in the pool timezone, and the informational line. The slate holds `num_games_per_week` games (the default is 20); a player picks exactly `picks_required` of them (the default is 15), both commissioner settings on the pool.
- For each game the player taps a team to select the straight-up winner. The selected side fills with `--green-tint` and shows a check.
- The picked games are ranked as one combined ranking across both leagues to set confidence. Order maps to points, top is `picks_required` down to 1. Each row shows its current point value live as the list reorders. Provide up and down buttons per row as the accessible fallback.
- A summary bar shows progress against `picks_required`, for example "12 of 15 winners chosen", with a Save action over HTMX and a clear saved indicator. Validate on save: exactly `picks_required` picks submitted, every picked game on the slate, and confidence values are a permutation of 1 to `picks_required`. A slate game the player did not pick is not an error.
- Never hard code the slate size or `picks_required` in copy or validation. Always read both from the pool.
- Editable until `lock_at`, then read only for everyone. After lock, all players' picks become visible on the Results page for transparency.
- A player who did not submit by `lock_at` scores 0 for that week.
- The three-stage entry flow (type confidence numbers, a "reorder to inputs" step, then live drag refinement, then a distinct lock confirmation step) is a later phase; the interaction described above is what ships with this phase's `picks_required` rule.

## 9. Scoring and leaderboards

- Read each game outcome from ESPN once completed, winner is home, away, or tie from the final score.
- Each pool runs in one of two scoring modes, set per pool on `Pool.scoring_mode` and switchable by the commissioner in pool settings without a code change:
  - `inverse` (the default, and this pool's real rule): a wrong pick counts its confidence points AGAINST the player, a correct pick earns nothing. Lowest total wins. A player who submits no picks for a week is flagged `did_not_submit` and takes the maximum possible penalty, `sum(1..picks_required)`, so sitting a week out is never the safe play. A no-show is never eligible to win the week, no matter how the arithmetic compares.
  - `standard` (the older rule, kept switchable): if `picked_team == winner` the player earns that pick's confidence points, else 0. Highest total wins. A player who submits no picks scores 0 and is excluded from winning the week the same way.
- `correct` (how many picks matched the winner) is counted the same way in both modes; only the direction points run in is different. `possible` is the count of countable outcomes among that player's own submitted picks, not the whole slate, so players covering different subsets of the slate never affect each other's possible count. A no-show's possible is 0.
- Tie games (possible in NFL) are voided: 0 points for everyone on that game and excluded from the correct and possible counts, in both modes. Same for a game the commissioner voids (cancellation or moved out of week).
- Weekly result per player: points (sum earned or charged, depending on mode), correct (count), possible, `did_not_submit`, stored in a `week_entries` row. Season standings aggregate weekly rows into total points, total correct, and weekly wins (best points that week under the pool's mode, ties share the win, no-shows excluded).
- Put per pick and per week scoring in `app/scoring.py` as pure functions and unit test heavily: all correct, all wrong, mixed, an unsubmitted player (in both modes, including the no-show max penalty), a tie or voided game, and a shorter than N slate.
- Render a weekly leaderboard, season standings, plus a Results page per week showing every game outcome and each player's picks. Standings and leaderboards sort ascending on points under `inverse`, descending under `standard`, and the UI makes the active direction explicit (a "points against" column heading, a rule reminder, "no picks submitted" instead of a bare number) whenever a pool runs `inverse`.
- All ingest and scoring commands are idempotent and safe to re run as scores update through the day.

## 10. Admin and commissioner tools

Every pool has a commissioner, the league admin, who sets it up and controls its settings.
Regular players cannot change any of them. The commissioner controls:

- League name, join code (view and rotate), season year, timezone.
- Auto publish on or off, open registration on or off, lock time.
- The total number of games per week, and the number taken from each league (NFL and college).

Other tools:

- Slate management: view the current and upcoming week, rebuild a slate, review the full candidate pool with resolved spread and source, add, remove or swap games before picks exist, set or override `lock_at`, and publish. See Section 6a.
- Manually trigger fetch results and score now, in addition to the cron.
- Member management: view members, remove, promote to commissioner, and view or rotate the pool join code.
- Void or un-void a game.

## 11. CLI commands and cron

All idempotent, all take `--year` and `--week` where relevant, defaulting to the pool's detected current week.

- `init-db` create schema or run migrations.
- `seed-admin` create the initial admin user, the default pool, and the join code from env.
- `seed-demo` create a demo pool, a few players, and a historical completed week (real past season and week) with picks, so the full UI and scoring can be exercised immediately.
- `sync-week` detect the current or upcoming week, build the slate, and auto publish it when `auto_publish` is true.
- `build-slate --week` build a draft slate for a specific week.
- `publish-week --week` open a drafted week.
- `fetch-results --week` pull finals and status from ESPN.
- `score-week --week` compute pick results, `week_entries`, and standings.
- `run-cron` the set and forget entry point. Safe to run hourly.

## 12. Data model

See `app/models.py`. Season standings are aggregated from `week_entries` on read.

## 17. Testing

- Provider tests run against recorded fixtures only, never live APIs.
- Mandatory pure unit tests for `slate.py` and `scoring.py` covering the cases listed in Sections 6 and 9.
- A smoke test that boots the app and loads the main pages.
- All ingest and scoring commands are idempotent and re runnable.

## 18. Definition of done

Feeds fail soft: timeouts, one or two retries, cache last good data, and clear admin visible warnings when a spread or score cannot be resolved, never a crashed page. Never commit secrets, ship `.env.example`. The README covers what it is, Windows setup, env vars, running a week end to end, the historical demo, and Render deploy with the cost note.

Done when, with `.env` filled and `seed-demo` run, the owner can locally: log in as admin, see the closest spread slate across NFL and FBS auto published for the current week, log in as a player and submit winner plus confidence picks with the drag to rank UI on both a phone width and desktop width screen, lock the week, run fetch results and score week, and see correct weekly and season leaderboards. The same app deploys to Render from GitHub with the hourly `run-cron` handling slate publish, results, and scoring. The interface matches the collegiate design system in Section 3, is fully responsive, and contains no em dashes and no emoji.

## 19. Optional future extensions (not in v1)

- Email reminders before lock and when results post.
- Public mode UI: pool discovery and self serve pool creation.
- Season playoff and bowl support (`seasontype=3`).
- Tiebreaker rules and a commissioner configurable scoring variant.
