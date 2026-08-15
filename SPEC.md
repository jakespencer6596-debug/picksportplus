# PickSportPlus. Build specification

This is the authoritative specification for the application. Two rules override
everything if there is ever a conflict: follow the Design System in Section 3
exactly, and follow the Copy Rules in Section 3h exactly.

## 1. What the product is

PickSportPlus is a weekly confidence pick'em pool for NFL and college football (FBS). It recreates a classic confidence pool:

- Each week the app publishes a slate of games. The default is 20 games, split 8 NFL and 12 college (FBS). The total and the per league counts are commissioner settings.
- The slate is chosen to be the closest games that week, meaning the games with the smallest betting point spread, so players pick genuinely competitive matchups instead of blowouts. Closest is judged within each league against that league's target count.
- Each player picks the straight-up winner of exactly `picks_required` of the slate games (the default is 15, out of the default 20 game slate) and ranks their confidence across only the games they picked, as one combined ranking. A player assigns `picks_required` points to their most confident pick down to 1 point for their least confident pick, using each value from 1 to `picks_required` exactly once. A slate game a player does not pick is legal to leave alone; it simply scores nothing for that player. Both the slate size and `picks_required` are commissioner settings, read from the pool, never hard coded.
- When a game goes final, each pick is scored per the pool's `scoring_mode` (Section 9). The pool's real, default rule is `inverse`: a wrong pick counts its staked points against the player and a correct pick earns nothing, lowest total wins. `standard` (a correct pick earns its staked points, highest total wins) is kept switchable per pool.
- The app maintains leaderboards all season: weekly points, season total points, games correct, and weekly wins.

The point spread is used only to select the slate (pick the closest games). Scoring is straight-up, meaning did the player pick the team that actually won. Do not implement against-the-spread scoring.

## 2. Audience and the private to public design

Launch is a private pool for the owner and friends, but the data model must make opening it to the public later trivial.

- Model a Pool (league) with a unique join code. Users join a pool with its code or an admin adds them.
- Global env flag `OPEN_REGISTRATION` (default false). When false, a new user can register only with a valid pool join code (private mode). When true, anyone can self register and join pools (public mode). Build both paths now and default to private.
- v1 ships with one seeded default pool, but the schema supports many pools and many members per pool.
- Roles: a global `admin` role (the site admin, sometimes called the platform owner in the UI) and normal `player` roles. A pool also has a commissioner, tracked per member (`PoolMember.role_in_pool`), so a pool can have more than one commissioner or co-commissioner. For v1 the seeded admin is also the commissioner of the default pool.
- Post-launch: a global admin manages every league from `/site/leagues`, the only place a `Pool` is created. There they create a league, attach one or more existing users as its initial commissioner(s) by email (or hand out a commissioner invite link, no email address needed in advance), and can "view as commissioner" to enter any league's own commissioner tools (`/league` and everything under it) exactly as that league's commissioner would see them, with a visible "Viewing X as commissioner" banner on every page and a way back out. A global admin is always treated as a commissioner of every pool; a pool's own commissioner is never a global admin unless their account actually has the global `admin` role. The site admin's own tools (`/site/*`: leagues, provider budgets, mail, contact submissions) are a separate URL namespace from a commissioner's league tools (`/league/*`), and the word "admin" itself never renders on a page a real commissioner reaches, since a pool's own commissioner is not necessarily a site admin. See Section 10 and 10c.

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
- Bottom tab bar on mobile only: This Week, Season, Results, and Admin when the user is a commissioner. Icons plus short labels. Hidden on desktop.
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
- SQLite for local dev (default `DATABASE_URL=sqlite:///./picksportplus.db`, zero setup on Windows) and PostgreSQL in production on Render via `DATABASE_URL`. Keep all models and queries database agnostic. `app.config.is_ephemeral_sqlite_path()`/`Settings.is_ephemeral_storage` detect a SQLite file living on Render's ephemeral local disk (as opposed to a real persistent Postgres `DATABASE_URL`); when true, a loud warning logs on startup, `python -m app.cli doctor` reports it in red, and a brick `.lockbar-strong` banner shows on every page to a site admin. Real player and payment data must never live somewhere a redeploy silently erases it, so the free Render blueprint (`render.yaml`) intentionally has no default `DATABASE_URL`, forcing a real connection string to be set at deploy time.
- Auth: email and password, hashed with `passlib[bcrypt]`, sessions via Starlette `SessionMiddleware` (signed cookie using `SECRET_KEY`). No third party auth service.
- `httpx` for calling ESPN, The Odds API, and CFBD, with timeouts and light retry.
- `pydantic-settings` for config from `.env`.
- `typer` CLI at `app/cli.py` for the ingest and scoring commands. Render Cron Jobs call these. Do not rely on an always on in process scheduler.
- `pytest`. Pure logic unit tests for scoring and slate selection are mandatory. Provider tests use recorded JSON fixtures and never hit live APIs.
- `ruff` plus `black`, keep the tree clean.
- Pin versions in `requirements.txt`.
- Money in the payout system (`app/payouts.py`, `app/services/payouts.py`, `PayoutRule`,
  `PayoutAward`, `Pool.entry_fee`/`pot_override`) is `Decimal` end to end, never `float`.
  Everywhere else in this codebase money uses a plain `Float` column (see `Game.spread_home`
  for the established, older convention), which the payout system deliberately departs from;
  see DECISIONS.md, "Payout system", for the reasoning.

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
5. Else spread stays null. A null spread never excludes a game from selection, it only leaves it unranked against games whose spread is known (Section 6); the game is always shown to admin either way.

## 6. Slate selection, closest spreads

Given a pool week (its own season year and week number, plus its `anchor_date`):

1. For each enabled league, resolve the ESPN week number and season type from `anchor_date` (Section 5a), then pull that league's games from ESPN (FBS games use `groups=80`). A league whose anchor date falls outside both its regular season and its postseason resolves to no games for that week, which is not an error: the slate is built from whichever leagues did resolve. The resolution actually used is recorded on the week (`resolved_weeks`, `is_bowl_week`).
2. Resolve each candidate home relative spread via 5e and compute `closeness = abs(spread)`, or leave it null when no spread is resolvable.
3. A resolvable spread ranks a game, closest first, but is never a requirement for eligibility: a real, scheduled game with no posted line yet is still a legitimate candidate. Optionally drop games already kicked off. This applies even to a pinned candidate (below): a pin guarantees the game survives selection regardless of its spread, known or not.
4. Sort by closeness ascending (a pick'em at 0 is closest); a game with no resolvable spread sorts after every game with a known one. Break ties by earliest kickoff, then by key so the result is stable.
5. Take the closest `target_nfl` NFL games and the closest `target_ncaaf` college games. Defaults are 8 and 12, for a `num_games_per_week` total of 20. All three are commissioner settings. A pinned game already counts against its own league's target here, before the rest of that league's closest games fill in around it.
6. Shortfall handling. If a league does not have enough eligible games to meet its target (too few games scheduled for the window, or the started-game filter removed enough of them), fill the gap with the next closest games from the other league so the total still matches, and record that it happened so the commissioner can see it. If the targets add up to more than the total, drop the farthest games until the total is met, except a pinned game, which is never dropped for being far; a pinned game beyond its own league's target simply expands that league's effective count and shrinks the other, the total unchanged. If more games are pinned than `num_games_per_week` allows, the build fails loudly with a clear message rather than silently dropping a pin.
7. Compute `lock_at` as the earliest kickoff among the selected slate games, unless the commissioner set a manual lock time.

Put the selection in `app/slate.py` as a deterministic pure function and unit test it thoroughly (ties, missing spreads, a league short of its target, targets that do not add up to the total, pinned games, and stability under input shuffling).

### 6a. Commissioner control over the slate

The tool always proposes a slate. The commissioner can override it, and the commissioner alone decides when a proposal actually becomes a published slate (`auto_publish`, Section 7).

- The admin slate editor shows the proposed slate and the full candidate pool for the week, every NFL and FBS game with its resolved spread, spread source and kickoff time.
- The commissioner can change the total and the per league counts, remove a proposed game, add any candidate game, and swap one game for another, whether or not that game has a resolved spread yet. An edited slate overrides the proposal.
- Pinned games. A game can be pinned so it always makes the slate on the next build regardless of how wide its spread runs, because closest-spread selection alone routinely excludes a rivalry game the moment either side is having a lopsided season (Ohio State vs Michigan, Auburn vs Alabama, and similar matchups named below). A pin is set either by hand from the slate editor, or automatically the first time a game matching one of the pool's configured rivalry pairs is created; the commissioner can unpin any single game at any time, and that choice sticks across later rebuilds of the same game. The slate editor shows why each slate game is there: pinned, a rivalry match, or the closest spread. Pinning a game, and editing the rivalry list, are both allowed at any time, including after picks exist: neither resizes or reorders the slate that is already live, they only change what the next rebuild proposes.
- The commissioner curates the rivalry list from the pool settings page: any matchup, one per line. The seeded default covers the games named by the group running the pool plus the other historically lopsided-but-always-relevant rivalries (Army vs Navy, Michigan vs Michigan State, Florida vs Georgia, Texas vs Oklahoma, USC vs Notre Dame).
- Voiding a game is always available, including after picks exist.
- Once any pick exists for a week the game count is fixed for that week and only voiding remains, so scoring stays consistent.

## 7. Automation and the weekly lifecycle (set and forget)

The build side of the pool runs itself; publishing the result to players is a deliberate commissioner action. `pool.auto_publish` defaults to false: a scheduled build always produces a draft, and the commissioner reviews and publishes it by hand from the slate editor. A commissioner who wants the old fully automatic behavior can still switch `auto_publish` on from pool settings, per pool, and everything below runs exactly the same way from that point on except that the last step (opening the week) happens by itself too.

- Detect the pool's current or upcoming week automatically. `pool.week1_anchor_date` (a Saturday) is required, set at league creation and validated as a Saturday on every save; the create-league form and pool settings both prefill and default it to the second Saturday of September. Week detection is date arithmetic against that anchor, never an ESPN lookup. **A build refuses outright, with a clear error and no `Week` row created, when the anchor is missing**, closing off the older, looser fallback that used to ask ESPN what NFL's current week number is (that fallback let NFL and college drift out of step against each other and could quietly produce a slate spanning more than a week). Do not require anyone to set the week by hand once an anchor is configured. `python -m app.cli backfill-anchor-dates` backfills any pool still missing one (an idempotent, one-time migration aid for a pool created before this rule existed) to the second Saturday of September of its own season year.
- **Multi-week span guard.** A build or publish refuses (`SlateSpanTooWide`) when the selected slate's games span more than `MAX_SLATE_SPAN_DAYS` (8) days between the earliest and latest kickoff, since NFL and college resolve to their own ESPN week independently (Section 5a) and can otherwise select a slate that actually spans parts of two different real weeks. The refusal names the span, both kickoffs, and each league's resolved ESPN week, so a commissioner has enough to fix the anchor rather than guess. `select_slate_by_targets` (`app/slate.py`) also refuses to let two candidates sharing a real team both survive selection into the same slate; a dropped duplicate is reported as a build warning in real team names.
- A scheduled job builds the slate for the upcoming week. When `auto_publish` is true it also opens the slate automatically (status open) and sets `lock_at`, no human action required. When `auto_publish` is false (the default) the job stops at a draft, and the week opens only once the commissioner publishes it.
- **Test weeks.** A commissioner can build a low-stakes "test week" (`Week.is_test_week`, reserved `week_number = 0`) from whatever is live right now, NFL preseason and college week 0 included, so a group can try picks and scoring before the real season starts, without needing a real anchor date to resolve one (preseason resolution is tried ahead of regular/postseason specifically for this path). It scores normally on its own, badged `TEST WEEK` everywhere it appears (slate editor, picks page, results page), but is quarantined from everything that counts toward the real season: excluded from season totals, correct counts and weekly-win counts (`app/services/standings.py`), skipped by the payout-freeze hook, invisible to the scenarios panel, and `/results/custom-scenario` refuses one outright. Building it again refreshes it the same way building a real week does. Deleting it (commissioner only) removes it entirely.
- The commissioner may still adjust a published slate before `lock_at`, but only non destructive changes once picks exist. Before any picks exist, free edits are allowed. After picks exist, allow voiding a game but not reshuffling the whole slate.
- `pool.current_week` advances automatically as the calendar moves.
- A results job runs frequently on game days, pulls finals, and scores idempotently. When every slate game is final, mark the week scored.
- Net effect: after first time setup, a full season needs zero manual steps. Lock is enforced at request time by comparing now with `lock_at`, so picks stay honest even if a job is delayed.

## 8. Picks and confidence UI

For a week that is open and before `lock_at`:

- The This Week page lists the slate games with matchup, kickoff in the pool timezone, and the informational line. The slate holds `num_games_per_week` games (the default is 20); a player picks exactly `picks_required` of them (the default is 15), both commissioner settings on the pool.
- For each game the player taps a team to select the straight-up winner. The selected side fills with `--green-tint` and shows a check.
- Entry is a three stage flow, all inside one continuous sortable list, never a hard coded whole-slate ranking:
  1. **Type confidence, tap winners.** Every row carries a small typed number input (1 to `picks_required`) next to the team buttons, live validated as the player types: a value that collides with another row is flagged in `--brick` and the summary line calls it out ("12 of 15 assigned, values 4 and 9 used twice"), without blocking the intermediate, still-invalid state. A row with no team picked never carries a confidence chip or a submitted confidence value, whatever was typed into it.
  2. **Reorder to inputs.** A button snaps the list so picked rows with a valid typed value sort to the top by that value, descending, and any row with no team picked or no value typed drops into a visually separate "Not picked" group below a hairline divider. Confidence is then reassigned cleanly, 1 to `picks_required`, from the new order.
  3. **Drag to refine.** Dragging a row by its grip, or using the up and down buttons (the accessible, keyboard reachable fallback, alt plus an arrow key also works), recalculates every picked row's point value live, `picks_required` at the top down to 1, scoped to the picked rows only, never the whole slate. Confidence is always positional across exactly the picked rows.
- A summary bar shows progress against `picks_required`, for example "12 of 15 winners chosen", with a Save action over HTMX and a clear saved indicator. Validate on save: exactly `picks_required` picks submitted, every picked game on the slate, and confidence values are a permutation of 1 to `picks_required`. A slate game the player did not pick is not an error.
- Never hard code the slate size or `picks_required` in copy or validation. Always read both from the pool.
- **Player lock, distinct from the pool wide `lock_at`.** After ranking, a player may deliberately lock their own picks in early: "Lock picks" opens a confirmation panel summarizing the `picks_required` picks in confidence order before anything is submitted, a second, separate tap from Save so locking cannot happen by accident. Locking saves the entry (the same validation Save runs) and sets `WeekEntry.locked_at`. While `locked_at` is set and the week itself has not reached `lock_at`, the page renders a read only confirmation view for that player alone, with an "Unlock to edit" action. The moment the pool wide `lock_at` passes, the normal read only state takes over for everyone regardless of `locked_at`, and unlocking is refused from then on; a player lock never grants or costs any extra time against the real lock.
- Editable until `lock_at`, then read only for everyone. After lock, all players' picks become visible on the Results page for transparency.
- A player who did not submit by `lock_at` is flagged `did_not_submit` and scored per the pool's `scoring_mode`: 0 under `standard`, the maximum possible penalty (`sum(1..picks_required)`) under `inverse` (the default), and never eligible to win the week either way. See Section 9.

## 9. Scoring and leaderboards

- Read each game outcome from ESPN once completed, winner is home, away, or tie from the final score.
- Each pool runs in one of two scoring modes, set per pool on `Pool.scoring_mode` and switchable by the commissioner in pool settings without a code change:
  - `inverse` (the default, and this pool's real rule): a wrong pick counts its confidence points AGAINST the player, a correct pick earns nothing. Lowest total wins. A player who submits no picks for a week is flagged `did_not_submit` and takes the maximum possible penalty, `sum(1..picks_required)`, so sitting a week out is never the safe play. A no-show is never eligible to win the week, no matter how the arithmetic compares.
  - `standard` (the older rule, kept switchable): if `picked_team == winner` the player earns that pick's confidence points, else 0. Highest total wins. A player who submits no picks scores 0 and is excluded from winning the week the same way.
- `correct` (how many picks matched the winner) is counted the same way in both modes; only the direction points run in is different. `possible` is the count of countable outcomes among that player's own submitted picks, not the whole slate, so players covering different subsets of the slate never affect each other's possible count. A no-show's possible is 0.
- Tie games (possible in NFL) are voided: 0 points for everyone on that game and excluded from the correct and possible counts, in both modes. Same for a game the commissioner voids (cancellation or moved out of week).
- Weekly result per player: points (sum earned or charged, depending on mode), correct (count), possible, `did_not_submit`, stored in a `week_entries` row. Season standings aggregate weekly rows into total points, total correct, and weekly wins (best points that week under the pool's mode, ties share the win, no-shows excluded).
- Put per pick and per week scoring in `app/scoring.py` as pure functions and unit test heavily: all correct, all wrong, mixed, an unsubmitted player (in both modes, including the no-show max penalty), a tie or voided game, and a shorter than N slate.
- Season standings and the weekly leaderboard live on two separate pages, not one combined view: `/standings` is season totals only, `/results` carries the week switcher, that week's scoreboard, that week's leaderboard, and (once the week locks) every player's picks. The weekly leaderboard on `/results` always matches whichever week is selected, and stays hidden until that week locks, the same reveal rule as the pick grid. Standings and leaderboards sort ascending on points under `inverse`, descending under `standard`, and the UI makes the active direction explicit (a "points against" column heading, a rule reminder, "no picks submitted" instead of a bare number) whenever a pool runs `inverse`. Every ranked table's columns are sortable by clicking a header (a `<select>` stands in for that below the medium breakpoint, where there is no header row to click).
- Every player's picks render as a grid with one row per player and one column per confidence value, `picks_required` down to 1, each cell showing the matchup staked at that confidence ("GB over CHI") colour coded by outcome; a toggle switches to the older layout, one row per game and one column per player, for anyone who prefers it.
- All ingest and scoring commands are idempotent and safe to re run as scores update through the day.

### 9a. The scenario engine

The feature the group asked for by name: "Once 5 games were completed, you could see how many different scenarios got you placed for the week. And what those scenarios were (e.g. which teams need to win/lose). As well as your percent chance at 1, 2, 3 (all just a numerator of how many scenarios you have to get you to that position divided by the total number of scenarios remaining)."

**Where it lives.** `app/scenarios.py` is the pure engine: no database, no network, no imports from `app.models` or any `app.services`/`app.routers` module, exactly like `app/scoring.py` and `app/slate.py`. It reuses `app.scoring.score_week` and `score_pick` directly rather than re-deriving how a week is scored, so the scenario sweep can never drift from a real week's result. `app/services/scenarios.py` is the database-touching caller: it pulls a week's `Game` and `Pick` rows, builds the pure module's plain dataclasses, calls into the engine, and hands back something a route can render.

**Enumeration.** Each remaining (not yet final, not void) countable game is a binary home/away outcome, so `R` remaining games have `2**R` scenarios.

- `R <= 20`: an exact, exhaustive sweep of every scenario.
- `R > 20`, or an exhaustive attempt that is on pace to blow the time budget: Monte Carlo, seeded, up to 200,000 samples.
- **Hard cap: 2 seconds for one computation**, exhaustive attempt plus any Monte Carlo fallback combined. Both paths check the wall clock periodically while they run, not only at the end, and degrade to (or further cap) Monte Carlo rather than exceed it. A report exposes `is_estimate` and `method` (`"exhaustive"` or `"monte_carlo"`) so the UI can label an estimate as one. The Monte Carlo sample count is a closed-form target sized from the problem's shape (remaining game count, player count) ahead of time, not a live "stop when the clock says so" loop: watching the clock to decide how many samples to draw made the same seed produce a different sample count from one call to the next under ordinary background load, which broke reproducibility (see DECISIONS.md, Phase 8). A short, still-real periodic wall clock check remains as a safety net against a machine slower than the one that estimate was calibrated against.

**Probability model**, a parameter, never read from a database inside the pure engine:

- `"even"` (default): every remaining game is 50/50, every scenario weighs the same.
- `"moneyline"`: weight each scenario by the product of each remaining game's implied win probability. A real moneyline (American odds) is devigged: implied probability from American odds is `abs(odds) / (abs(odds) + 100)` for a favorite (negative odds) and `100 / (odds + 100)` for an underdog (positive odds), then `p_home = implied_home / (implied_home + implied_away)` so the two sides sum to exactly 1.0. Without a moneyline, win probability is derived from the stored spread through a normal CDF: `p_home_win = Phi(-spread_home / sigma)`, `sigma = 13.5` for NFL and `16.0` for college. Without either, a game falls back to 50/50 and is noted, never an error.
- This is a per user **view** preference, not a pool setting: a query parameter on `/results` (`?model=moneyline`), never a database column. The panel is labelled "Estimated from betting odds" whenever moneyline mode is on.

**Outputs per player**: `scenarios_at_place`/`pct_at_place` for 1st, 2nd and 3rd (ties in a scenario credit every tied player at that place, so summed across every player these do not have to add to 1.0, see the module's tests for the exact identity that does hold), `clinched`/`eliminated` per place (true only when every enumerated or sampled scenario agrees, which is exact under an exhaustive sweep and an approximation under Monte Carlo), and, for whichever player(s) the caller opts in (never all 16 by default, since it is real extra work the placement numbers alone do not need): a **leverage table**, the share of that player's OWN first place scenarios (not all scenarios) in which each remaining game's home or away side won, rendered as plain sentences ("You need Green Bay in 94 percent of your winning scenarios," "Detroit vs Chicago does not matter to you" for a game close to 50/50 within their own winning scenarios), and up to five **representative scenarios** chosen to span the leverage table (a greedy, farthest-first selection over the games with the most leverage, so the five shown differ from each other on what actually matters, rather than being five near-identical scenarios).

**Build your own scenario.** Not a second enumeration engine: the caller (a commissioner or a player, from the panel's three state control per remaining game, home wins / away wins / undecided) fixes some remaining games to a concrete winner and gets back the real standings under that one assumption, computed with a single `score_week` call per player over the combined outcome list (already-final outcomes plus the fixed ones, with any still-undecided game simply omitted, exactly how `score_week` already treats a not-yet-final game) and ranked with the same local competition-ranking helper the sweep itself uses. An HTMX form posts the fixed set and swaps in a recomputed table for every player, not just the poster, since the brief calls this a social, screenshot-friendly feature.

**Wiring into the app.** `Game.home_moneyline`/`Game.away_moneyline` (nullable Integer, American odds) are populated opportunistically wherever `app/providers/espn.py` already parses odds; no ESPN payload recorded in this codebase's fixtures has ever carried one, so this is commonly null in practice and the probability model falls back to the spread-derived CDF (see DECISIONS.md, Phase 8). Two commissioner settings, `Pool.scenarios_min_final_games` (default 5) and `Pool.scenarios_min_remaining_games` (default 1), gate the panel's visibility on Weekly Results; below threshold the panel shows a pending state naming the real configured numbers, never a hard coded "five". A short lived, in process cache (a module level dict) keyed on the week id plus a frozen set of every countable game's `(id, status, winner)`, the scoring mode, `picks_required`, the probability model, and which players' leverage was requested, means a repeated request for the same week in the same state never recomputes; it never evicts within a process lifetime, which is safe because the key itself changes the moment any relevant game's outcome changes, so staleness cannot happen.

## 10. Admin and commissioner tools

Every pool has a commissioner, who sets it up and controls its settings, at `/league` and
everything under it. Regular players cannot change any of them, and a plain commissioner
cannot reach `/site` (Section 10c) at all: hitting it 403s. The commissioner controls:

- League name, join code (view and rotate), season year, timezone, week 1 anchor date (a
  required Saturday, Section 7).
- Auto publish on or off, open registration on or off, lock time.
- The total number of games per week, and the number taken from each league (NFL and college).

Other tools:

- Slate management (`/league/slate`): view the current and upcoming week, rebuild a slate,
  review the full candidate pool with resolved spread and source, add, remove or swap games
  before picks exist, set or override `lock_at`, publish, and build or delete a test week
  (Section 7). The build form is a single field (the week number); it carries no publish
  checkbox and no ESPN-only checkbox, both moved to Section 10c since neither is a
  per-build decision a commissioner should be making. A build in progress is guarded against a
  second concurrent build for the same week and against running past
  `settings.slate_build_timeout_seconds` (default 90s).
- Manually trigger fetch results and score now, in addition to the cron.
- Member management (`/league/members`): view members, remove, promote to commissioner, view
  or rotate the pool join code, mint a commissioner invite link, and (Section 10d) send a
  player invite email to one or more addresses at once.
- Void or un-void a game.

### 10c. Site admin (platform owner) tools

The site admin's own tools live under `/site`, gated on the global `admin` role
(`require_admin`), never under `/league`. A visible "Site admin" wording, and the word "admin"
in general, is reserved for the handful of pages only a site admin ever reaches
(`/site/leagues`, `/site/leagues/new`, `/site/contacts`); it never renders on a page a real
commissioner sees, enforced by a rendered-response test against a real commissioner's own HTML,
not a static grep, since a legitimately admin-gated block can contain the word in source
without ever rendering for a commissioner.

- `/site`: dashboard, an overview across every league.
- `/site/leagues`: the only place a `Pool` is created (Section 2), with "view as commissioner"
  and a plain "league dashboard" link per row, a commissioner invite link generator, and
  (Section 10d) a commissioner invite email form.
- `/site/providers`: API key presence, spend, and last call per provider (ESPN, The Odds API,
  CFBD), and the global ESPN-only switch (`PlatformSetting.espn_only`, Section 5e) that ANDs
  into every build's `allow_metered` right before spread resolution; a trusted CLI caller's own
  `--no-metered` flag remains a stricter per-run override but can never bypass the switch when
  it is on. When the switch is on, a commissioner's slate page shows a neutral note ("Some
  games may not have a line yet. You can set one by hand.") with no billing language, since a
  commissioner never sees spend or budget details.
- `/site/mail` (Section 10d): mail configuration status, recent sends, and a real test-send.
- `/site/contacts`: contact form submissions.
- Every old bookmarkable `GET /admin/...` path 301s to its `/league` or `/site` equivalent
  (`app/routers/legacy_redirects.py`), so an old bookmark or link never dead-ends; POST-only
  legacy paths are not redirected, since a 301 can silently drop the method or body and nothing
  in this app itself issues one.

### 10d. Transactional email

`app/services/mail.py` sends through Resend's REST API over `httpx` (no SDK, no new
dependency), gated on `Settings.mail_enabled`, `resend_api_key`, and `mail_from_address` all
being set; missing any one of them raises `MailDisabled` rather than attempting a call with a
blank credential. `send()` returns a real `MailLog` row only on real success
(`result="sent"`); every other outcome (`MailDisabled`, `MailRateLimited`, `MailSendFailed`)
raises instead of returning something a caller could mistake for success, and every attempt,
success or failure, is logged to `MailLog` (a durable table, not memory, so rate limiting
survives a restart) with its recipient, kind, and actor. Rate limited per actor per hour
(`Settings.mail_rate_limit_per_hour`), scoped so one sender's volume never blocks another's.

Four emails, each living next to its existing copy-and-paste path, never replacing it: a
commissioner invite (`/site/leagues`, site admin only), a player invite
(`/league/members/invite`, commissioner only, one or many addresses), a password reset
(single-use, one-hour expiry, the raw token never stored, only its SHA-256 hash), and an
opt-in week-published notification (`Pool.notify_week_published`, off by default). A UI action
that sends mail must never claim success when the send actually failed: `/site/mail`'s
test-send and every real send path surface `MailSendFailed` as a visible, specific error,
never a generic flash. The one exception is `forgot-password`'s own response, which shows the
identical message and redirect whether or not the address is real and whether or not the send
itself succeeded, deliberately, to avoid confirming or denying an account's existence
(anti-enumeration); the underlying failure is still logged to `MailLog`, just not surfaced to
the visitor who triggered it.

### 10a. The Venmo entry gate

Entry is Venmo only, paid to one collector, "no multiple accounts." `Pool.entry_fee` (dollars, unset until a commissioner types in a real number), `Pool.venmo_handle` (the single collector), `Pool.payment_required_to_pick` (boolean, true by default), and `Pool.payment_note` (free text shown to players) are all set from `/admin/settings`. Nothing here ships with a real number or handle in it; the payout editor and the entry fee field are both blank until a commissioner fills them in by hand.

While `payment_required_to_pick` is on, `GET /picks` shows a blocking panel above the slate for any member whose `PoolMember.paid_at` is still null: the entry amount (when set), the collector's handle, a Venmo deep link (the mobile app link and a `venmo.com/u/...` web fallback, both URL encoded), and "The commissioner confirms payment manually. Message them once you have sent it." The panel blocks interaction the same way the pool wide lock already does (the slate stays visible, read only, no save or lock controls), and the same rule is enforced server side, not just in the template: `POST /picks` and `POST /picks/lock` both refuse an unpaid member with a clear error, exactly as authoritatively as the pick count and confidence validation already is.

The Members page carries a paid/unpaid column, a one click toggle per member (`POST /admin/members/{id}/paid`), a bulk "mark selected paid" action, a note field for the commissioner's own Venmo handle reconciliation (`PoolMember.member_venmo_handle`, never a second place to pay), a duplicate handle warning when two members share one (the group's "no multiple accounts" rule, surfaced, not enforced), and a pot summary reading "N of M paid, X dollars collected of Y dollars," computed from the pool's real `entry_fee`, never a hard coded number.

### 10b. Payout rules

Rebuilt (Payout system rebuild, see DECISIONS.md, "Payout system") from an earlier, simpler
version: this is the current, shipping design. His real ask, verbatim: "if there's a way in
settings to Set Payouts for weekly, bowl week, season points, and season wins... it's a
function of total $$ pool."

**Four scopes**, `PAYOUT_SCOPES = ("weekly", "bowl", "season_points", "season_wins")`. `weekly`
applies to every regular season week (his real structure: weeks 1 to 15), `bowl` to the one
week `Week.is_bowl_week` is true, `season_points` to the season standings ranked by total
points, `season_wins` to the same standings ranked by weekly win count instead, always
descending regardless of the pool's scoring mode. Never hard coded: the pure allocation engine
(`app/payouts.py`) takes ranking direction as an explicit keyword argument at every call site,
specifically so `season_wins` cannot accidentally inherit the pool's scoring direction.

**Two modes per rule**, `PAYOUT_MODES = ("amount", "percent")`. `amount` is a flat dollar
figure. `percent` is a percentage (0-100) of the pot, resolved fresh every time against
`Pool.entry_fee * count(paid members)`, or `Pool.pot_override` when a commissioner has set one
by hand (always wins, for a reserve or a carryover). Every payout figure in this build is a
`Decimal`, never a `float`, end to end.

**Weekly rules are per week, not per season.** A weekly 1st of 105 dollars means 105 dollars
every regular week; a weekly 1st of 2.12 percent means 2.12 percent of the pot every week.
`Pool.weekly_payout_weeks` (default 15) is what multiplies the per-week figure into a season
total for display; every other scope is a one-time payout.

**Ties** split the combined pool of the consecutive places they occupy (two tied for 1st split
1st and 2nd, the next player takes 3rd); a place with no rule contributes zero to that split
rather than raising. Each tied group's combined total is rounded down to `Pool.payout_rounding`
(`cent`, `dollar`, or `five`) before splitting, and the leftover is handed out one unit at a
time in `Pool.payout_tiebreak` order (today, `earliest_submit`: earliest `WeekEntry.submitted_at`
first, a missing submission time sorts last, then `user_id` for full determinism).

**Frozen snapshots.** A percent-mode payout resolves against the pot, and the pot can grow
after a week is already scored (a member pays their entry fee late). Re-resolving a past week
live would silently change a figure the commissioner may already have paid over Venmo, so the
resolved amount is written once, frozen, into `PayoutAward` the instant a week finishes scoring
(`score_week_for_pool`'s own hook), or the instant a bowl week finishes scoring for the two
season scopes (this codebase's real season structure is weeks 1-15 plus a week 16 bowl week,
and there is no separate stored "season complete" flag). All display of a past payout reads
this frozen table; only the current, still unfinished week may show a live, unsaved
projection, and it is always labelled "Projected" wherever it renders.

**Over-allocation warns, never blocks.** The Set Payouts screen (`/admin/payouts`) shows a
banner naming the exact difference and direction whenever the grand total does not equal the
pot, but always lets the commissioner save anyway: he may deliberately hold a reserve, or write
rules before everyone has paid.

The screen has four scope editors in this order (Weekly, Bowl Week, Season: Points, Season:
Wins), a pot panel (entry fee, override, weekly payout weeks, rounding, tiebreak), a live
allocation summary in the shape of the commissioner's own spreadsheet, a "Scale to pot" action
(converts every rule to percent at its current share, so the whole ladder auto-rescales when
the player count changes, which is the entire point of percent mode), and a "Load preset"
action seeding the known ladder (weekly 105/55/25, bowl 250/100/50, season points 600/405/150,
season wins 325/185/110).

Weekly Results gains a Payout column once rules exist for the relevant scope, blank (never
0) for anyone out of the money. Season Standings gains two award panels, Season: Points and
Season: Wins, once the season scope has actually been snapshotted. `/admin/payouts/summary`
is the commissioner's payout summary: one row per player, a running "Paid X of Y, N of M
players settled" line, a Paid checkbox per player (marks or unmarks every one of their
currently unpaid/paid awards in one action), an unpaid-only filter, a copy-as-text export, and
a CSV download.

## 11. CLI commands and cron

All idempotent, all take `--year` and `--week` where relevant, defaulting to the pool's detected current week.

- `init-db` create schema or run migrations.
- `seed-admin` create the initial admin user, the default pool, and the join code from env.
- `seed-demo` create a demo pool, eight players, two fully scored historical weeks (real past season and weeks) with picks, payouts and season standings, and one open current week (a reused real historical slate, no picks, an artificially future `lock_at`), so the full UI, scoring, payouts and scenarios can all be exercised immediately. `--reset` rebuilds it; `--scenario-week` leaves the second historical week partially played instead of fully scored, so the Scenarios panel has a real week to open against. The seeded demo pool always carries a real `week1_anchor_date` (Section 7), since a real "Build the slate" click against it would otherwise hit the same required-anchor refusal a real pool does.
- `backfill-anchor-dates` set `week1_anchor_date` to the second Saturday of September of its own season year on any pool still missing one (Section 7). Idempotent, safe to re-run; a pool that already has an anchor is left untouched.
- `sync-week` detect the current or upcoming week, build the slate, and auto publish it when `auto_publish` is true.
- `build-slate --week` build a draft slate for a specific week.
- `publish-week --week` open a drafted week.
- `fetch-results --week` pull finals and status from ESPN.
- `score-week --week` compute pick results, `week_entries`, and standings.
- `run-cron` the set and forget entry point. Safe to run hourly.
- `payouts-show --scope` print the current ladder and allocation summary as plain text.
- `payouts-preset` load the known payout ladder. Safe to re-run, always clears and reseeds.
- `payouts-snapshot --scope --week` freeze one scope's awards by hand.
- `payouts-summary` print the commissioner payout summary to stdout.

## 12. Data model

See `app/models.py`. Season standings are aggregated from `week_entries` on read.

## 17. Testing

- Provider tests run against recorded fixtures only, never live APIs.
- Mandatory pure unit tests for `slate.py` and `scoring.py` covering the cases listed in Sections 6 and 9.
- Mandatory pure unit tests for `scenarios.py` (Section 9a) covering: a hand computed small case, clinched, eliminated, ties sharing a place with the summed percentages worked out by hand, inverse vs standard picking different winners from identical inputs, moneyline weighting shifting probability toward the favorite, a hand computed leverage table, zero remaining games, Monte Carlo convergence and reproducibility under a fixed seed, and a real timing assertion at 15 remaining games and 16 players well under the 2 second cap.
- A smoke test that boots the app and loads the main pages.
- All ingest and scoring commands are idempotent and re runnable.
- **Offline first.** `tests/conftest.py`'s session-wide `force_offline_mode` fixture ensures
  nothing in the suite ever opens a real socket. This extends to mail (Section 10d): every mail
  test monkeypatches `app.services.mail._call_resend_api`, the one real HTTP call site, rather
  than hitting Resend, the same pattern provider tests already use against ESPN, The Odds API,
  and CFBD.

## 18. Definition of done

Feeds fail soft: timeouts, one or two retries, cache last good data, and clear admin visible warnings when a spread or score cannot be resolved, never a crashed page. Never commit secrets, ship `.env.example`. The README covers what it is, Windows setup, env vars, running a week end to end, the historical demo, and Render deploy with the cost note.

Done when, with `.env` filled and `seed-demo` run, the owner can locally: log in as admin, see the closest spread slate across NFL and FBS built and, once reviewed, published for the current week (`auto_publish` is off by default, Section 7; a commissioner who wants the old fully automatic behavior can switch it on), log in as a player and submit winner plus confidence picks with the drag to rank UI on both a phone width and desktop width screen, lock the week, run fetch results and score week, and see correct weekly and season leaderboards. The same app deploys to Render from GitHub, with the hourly `run-cron` handling slate build, results, and scoring once a cron schedule is actually wired up (a paid Render plan with the cron service from the README, or any external scheduler hitting `run-cron`; the committed `render.yaml` for the free demo intentionally has none, see the README). The interface matches the collegiate design system in Section 3, is fully responsive, and contains no em dashes and no emoji.

## 19. Optional future extensions (not in v1)

- Email reminders before lock and when results post.
- Public mode UI: pool discovery and self serve pool creation.
- Season playoff and bowl support (`seasontype=3`).
- Tiebreaker rules and a commissioner configurable scoring variant.
