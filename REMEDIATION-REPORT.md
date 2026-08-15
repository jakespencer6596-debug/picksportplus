# August 2026 remediation report

Branch: `remediation-aug-2026`. Tracks the thirteen phase remediation run against defects
found in the August 14, 2026 live walkthrough, ahead of the group's paid season starting
September 12, 2026.

## Baseline (Phase 0)

- `ruff check .`: clean.
- `black --check .`: clean, 60 files.
- `pytest -q`: **939 passed**, 0 failed. This is the baseline test count; Phase 10 requires
  at least 80 more (1019+) after remediation.
- Em dash grep: clean (the one hit in `tests/test_app.py:2370` is an assertion that em dashes
  are absent, not a violation).
- Branch `remediation-aug-2026` created from `main` at commit `c210499`.

## Phase checklist

| Phase | Description | Status | Commit SHA |
|---|---|---|---|
| 0 | Baseline, branch, report | Done | `3005a33` |
| 1 | Data persistence (ephemeral SQLite) | Done | `1079c7b` |
| 2 | Week resolution / anchor date | Done | `f0a0117` |
| 3 | Preseason / test week support | Done | `1a6c199` |
| 4 | Split site admin vs commissioner | Done | `5cb32c4` |
| 5 | Move provider controls to site admin | Done | `957d718` |
| 6 | Fix slate build interaction | Done | `e5bc767` (+ `e862eff` fix) |
| 7 | Transactional email | Done | `c615887` |
| 8 | First-run experience | Pending | |
| 9 | Verify prior fixes against live data | Pending | |
| 10 | Full sweep | Pending | |
| 11 | Documentation | Pending | |
| 12 | Merge, push, deploy, verify | Pending | |

## Phase 1 notes

- Startup log warning added to `app/main.py` (on the `startup` event), logging the database
  dialect always and a loud warning when `settings.is_ephemeral_storage` is true.
- `python -m app.cli doctor` now prints a "Data survived the last restart" section: user,
  pool, pick and payout award counts, and the age of the oldest user row, plus the ephemeral
  storage warning in red.
- Site admin banner (brick, `.lockbar-strong`) shows on every page while `current_user.is_admin`
  and storage is ephemeral; never shown to a commissioner or player.
- `render.yaml`'s `DATABASE_URL` is now `sync: false` with no default value, so Render prompts
  for a real connection string at deploy time.
- `README.md` gained a "Persistent database (Neon)" section under "Deploy the free demo".
- Test count after Phase 1: 949 (+10 over the Phase 0 baseline of 939).

## Phase 2 notes

- `POST /admin/leagues/new` now requires a Saturday week 1 anchor date, prefilled with the
  second Saturday of September; `POST /admin/settings` validates the same Saturday rule.
- `build_slate` refuses outright (a clear flash, no Week row even created) when
  `pool.week1_anchor_date` is None, closing the fallback that produced the 17 day slate.
- `backfill-anchor-dates` CLI command backfills any pool still missing an anchor to the second
  Saturday of September of its own season year; idempotent.
- `select_slate_by_targets` (app/slate.py) refuses to let two candidates sharing a team both
  survive selection; `ingest.duplicate_team_warnings` explains any drop in real team names as
  a build warning.
- `publish_week` refuses (`SlateSpanTooWide`) a slate spanning more than 8 days between its
  earliest and latest kickoff, reporting the span, both kickoffs, and each league's resolved
  ESPN week; wired into both the manual Publish button and `build_slate`'s auto-publish path.
- Slate editor warns when a slate's games span more than 48 hours, since picks lock at the
  first kickoff by default.
- Found and fixed while implementing: `ensure_week` never backfilled an existing week's null
  `anchor_date` even after the pool gained a real one, only a brand new week. See DECISIONS.md.
- **Proof (Phase 10 item 5):** see "Full sweep" below once a live rebuild is exercised in
  Phase 10; the unit tests `test_publish_week_refuses_a_slate_spanning_more_than_eight_days`
  and `test_fetch_candidates_resolves_each_league_independently` (tests/test_ingest.py) pin
  anchor `2026-09-12` resolving NFL to week 1 and college to week 3, and a synthetic 17 day
  span being refused with the span, both kickoffs and both resolved weeks reported.
- Test count after Phase 2: 961 (+12 over Phase 1's 949, +22 over the Phase 0 baseline).

## Phase 3 notes

- `SEASON_TYPE_PRESEASON = 1` added; `resolve_league_week`/`resolve_pool_weeks` gained
  `is_test_week` (default False, opt-in only) to try preseason ahead of regular/postseason.
- `Week.is_test_week` column added (migration `4efccffeb9cc`), reserved `week_number = 0`.
- Commissioner "Create a test week" / "Delete this test week" actions on the slate editor
  (`POST /admin/test-week/create`, `POST /admin/test-week/{id}/delete`).
- Quarantine enforced in code, not just templates: `app/services/standings.py` excludes test
  weeks from season totals/correct counts/weekly wins; `score_week_for_pool` skips the
  payout-freeze hook for a test week; the scenarios panel always reports `visible=False` for
  one; `/results/custom-scenario` refuses a test week with 403.
- Gold "Test week" badge (`.badge-test-week`) on the slate editor, picks page and results page.
- Test count after Phase 3: 978 (+17 over Phase 2's 961, +39 over the Phase 0 baseline).

## Phase 4 notes

- Route split: `admin.py` prefix `/admin` -> `/league`; `payouts.py` `/admin/payouts` ->
  `/league/payouts`; `leagues.py` `/admin/leagues` -> `/site/leagues`; `admin_contacts.py`
  `/admin/contacts` -> `/site/contacts`; new `GET /site` dashboard (`app/routers/site.py`).
  Every route's own suffix and permission dependency unchanged, prefix only.
  Independently verified live: `GET /admin` -> 301 `/league`, `GET /admin/leagues` -> 301
  `/site/leagues`, `GET /site`/`GET /league` -> 303 to `/login?next=...` when signed out.
- 301 legacy redirects for every old bookmarkable GET path (`app/routers/legacy_redirects.py`,
  mounted last so it never shadows a live route). POST-only legacy paths intentionally not
  redirected (a 301 can drop the method/body; nothing in the app itself will ever issue one).
- "Site admin" role pill on the members roster renamed "Platform owner"; the three genuinely
  site-admin-only pages (`leagues.html`, `league_new.html`, `contacts.html`) keep "Site admin"
  wording, per the phase's own exception for pages only a site admin ever reaches.
  Independently verified: `grep -rn "/admin" app/ tests/` after the commit shows zero live
  route/href/redirect hits, only historical file-path prose and the redirect test table.
- No-visible-"admin" rule enforced with a rendered-response integration test (a real
  commissioner's actual HTML), not a static grep, since the `is_site_admin`-gated Provider
  budgets block on `/league` legitimately contains the word in source but never renders for a
  real commissioner.
- Test count after Phase 4: 998 (+20 over Phase 3's 978, +59 over the Phase 0 baseline).

## Phase 5 notes

- New `PlatformSetting` singleton (`espn_only: bool`, migration `b1c4f8a2d5e7`), read fresh via
  `get_platform_settings(db)`, never cached; independently verified upgrade/downgrade/upgrade
  on a scratch database.
- Slate build form is one field again (week number); the `publish`/`no_metered` checkboxes are
  gone from `app/routers/admin.py`'s `slate_build` entirely.
- New `GET /site/providers` (key presence, spend, last call, global ESPN-only toggle) and
  `POST /site/providers/espn-only`, site admin only. Old dashboard "Provider budgets" section
  removed from `/league` entirely, not just re-gated.
- `build_slate` ANDs the global switch into `allow_metered` right before `resolve_spreads`; a
  trusted CLI caller's own `--no-metered` flag still works as a stricter per-run override, it
  just can never bypass the switch when the switch is on.
- **Live-tested in a real browser against a throwaway seeded database** (not just pytest):
  logged in as the demo commissioner, confirmed the build form shows one field and no
  checkboxes; logged in as the site admin, confirmed `/site/providers` renders key
  presence/spend/last-call, toggled "ESPN only" on and back, and confirmed the change was
  immediate; confirmed the commissioner's slate page then showed the neutral note "Some games
  may not have a line yet. You can set one by hand." with no billing language. Also live-
  verified Phase 3's test-week create/delete flow and Phase 4's `/league`/`/site` nav, badges
  and copy while testing this phase. No defects found in any of it.
- Test count after Phase 5: 1008 (+10 over Phase 4's 998, +69 over the Phase 0 baseline).

## Phase 6 notes

- Measured, real build duration (reproduce-it-first): `ingest.build_slate` now logs its own
  elapsed wall clock time on every call (`log.info("slate build finished, pool %s week %s,
  %.2fs elapsed, %s selected", ...)`). A real, throwaway, local run against the live ESPN API
  (a fresh scratch sqlite database, `allow_metered=False` so no Odds API or CFBD credit was
  spent, `is_test_week=True` resolving against 2026-08-14, NFL preseason) measured **6.41
  seconds** for a 16-candidate, 6-core-odds-lookup build: one ESPN scoreboard call for the
  calendar, two for the resolved week (NFL regular and preseason season types), and six
  per-game ESPN core odds lookups. A real season-week build (20 games, up to 60 possible core
  odds lookups plus up to two metered calls) would run well past this, comfortably into the
  "up to a minute" the build form's own progress note now warns about, confirming the original
  bug report ("after more than ten seconds the page still reads Nothing is built") against real
  timings rather than a guess. Log line and script output not committed (a one-off throwaway
  script against a scratch database, not part of the app or the test suite).
- Test count after Phase 6: 1017 (+9 over Phase 5's 1008, +78 over the Phase 0 baseline).
- **Live-tested a real click in a real browser** (the sub-agent had no browser tool available
  and flagged this explicitly, see DECISIONS.md): confirmed a precisely targeted mouse click on
  "Build the slate" sends `POST /league/slate/build` and lands back on the built week, verified
  against both the local server's access log and the browser's own network panel, not a
  screenshot alone. Two false negatives during that testing were self-inflicted (a stale click
  coordinate after scrolling landed on the wrong element), recorded in DECISIONS.md so they are
  not mistaken for a real defect by a future session.
- **Found and fixed a real, separate bug while doing that testing:** the demo pool
  (`app/services/demo.py`) never set `Pool.week1_anchor_date`, so a real "Build the slate"
  click against it always hit Phase 2's refusal. Harmless (confirmed no existing data was
  touched) but confusing, and would have shipped on every deploy since `render.yaml` reseeds
  this exact pool. Fixed in a separate commit `e862eff` (not folded into Phase 6's own commit,
  since it is a Phase 2 gap, not a Phase 6 defect), with a regression test. Test count after
  this fix: 1018.

## Phase 7 notes

- `app/services/mail.py`: Resend's REST API over `httpx`, no new dependency. `send()` returns
  a `MailLog` row only on real success; every other outcome (`MailDisabled`, `MailRateLimited`,
  `MailSendFailed`) raises, so a caller cannot mistake a failure for success.
- Four emails, each next to its existing copy-and-paste path, never replacing it: commissioner
  invite (site admin only), player invite (commissioner only, multiple addresses), password
  reset (single use, one hour expiry, hashed token, anti-enumeration messaging), and an opt-in
  week-published notification (off by default).
- Rate limiting backed by `MailLog` (not memory, survives a restart), per actor per hour.
  New `/site/mail` panel: configuration status, recent sends, a real test-send.
- **Process note:** the first attempt at this phase stalled for hours with zero file changes
  and was killed. A second attempt made correct progress but was itself killed mid-flight,
  before writing tests or its own decisions, once the same stall pattern looked like it might
  recur. The orchestrating session verified the surviving code directly (read every changed
  file, ran the full gate, ran a real migration upgrade/downgrade/upgrade cycle), found it
  well designed, fixed one real defect it found (a stray "admin" word reaching a commissioner's
  screen), and wrote the missing test coverage itself. See DECISIONS.md, Phase 7, for the full
  account, including why this phase's `git status` showed no progress for so long.
- **Live-tested in a real browser against a throwaway seeded database:** the forgot-password
  flow rendering and submitting correctly with the generic anti-enumeration message; `/site/mail`
  showing "Off" configuration status; a real test-send correctly failing loudly ("Email is not
  turned on for this deployment...") rather than silently, with both attempted sends correctly
  logged and visible in the Recent sends table; the commissioner invite email form present and
  wired next to the existing copy-link panel on `/site/leagues`. No defects found beyond the one
  already fixed.
- Test count after Phase 7: 1041 (+23 over Phase 6's 1018, +102 over the Phase 0 baseline).

## Ambiguity decisions

See `DECISIONS.md`, `## Remediation, August 2026`, for every judgment call and its reasoning.

## Phase 10 checklist (33 lines)

Filled in during Phase 10.

## Proof points

- Week resolution span, earliest/latest kickoff, resolved per-league ESPN weeks: filled in
  during Phase 2/10.
- Slate build wall-clock duration: filled in during Phase 6.
- Live deploy status: filled in during Phase 12.

## Setup guides

Commissioner and site admin guides are appended once Phase 12 completes.

## Not built / risks

Filled in at the end.
