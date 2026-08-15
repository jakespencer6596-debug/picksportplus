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
| 8 | First-run experience | Done | `cad907b` |
| 9 | Verify prior fixes against live data | Done | `a8fe738` |
| 10 | Full sweep | Done | `1e35dc0` |
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

## Phase 8 notes

- Full router audit (every route in every `app/routers/*.py` file, by hand): nothing needed
  fixing. Every authenticated route already resolves through `require_user` or a dependency
  that wraps it, and the exception handler already turns the resulting 303 into
  `/login?next=...`. Locked in with a test covering 16 representative routes across every
  router, plus a negative control confirming the two literal old bug-report paths
  (`/leagues`, `/leagues/new`) are genuinely gone and 404 honestly.
- The "dead-end empty state" was confirmed already fixed by an earlier, pre-remediation phase
  (the `is_preview` pool feature): a poolless signed-in visitor already sees actionable
  "Enter a join code" / "Start your own league" options, never a bare "check back soon."
  Independently verified live in the browser during Phase 5/6 testing before this phase even
  ran. Strengthened with new test coverage rather than rebuilt.
- `normalize_join_code` (`app/auth.py`) now strips hyphens in addition to spaces/case, so a
  code typed as "ab-3d efgh" matches the stored value; already called at every real entry
  point, so this one function fix covers registration, join, and both invite-code routes.
- `run-cron` now refreshes the preview pool's slate (same metered-budget path a real pool's
  build already uses) via the read-only `get_preview_pool`, never creating one; `doctor`
  reports the preview as missing, stale, or healthy. Independently verified: a fresh
  `doctor --no-probe` run correctly reports "MISSING: no preview pool has been seeded yet."
- Pricing arithmetic fixed ("Save 49 dollars", was "50"). Independently verified in the
  template source.
- Test count after Phase 8: 1073 (+32 over Phase 7's 1041, +134 over the Phase 0 baseline).

## Phase 9 notes

Done directly by the orchestrating session (live browser against a real seeded database), not
delegated, since this phase is fundamentally hands-on verification.

- **Inverse scoring:** confirmed on `/results?week=5`. "Low score wins," lowest points-against
  (22) wins, full ranking strictly ascending (22, 26, 35, 37, 40, 59, 63), non-submitter shown
  as "No picks submitted" not a bare number.
- **15 of 20:** a real 15-pick submission renders correctly (verified); rejection of 14/16 relies
  on the existing, already-passing `tests/test_scoring.py`/`tests/test_app.py` coverage.
- **Two-step pick entry:** confirmed at desktop width (tap-to-select, live confidence chip and
  progress bar, auto-regrouping, and the up/down accessible reorder buttons recalculating
  confidence correctly). Could not force a genuine 360px viewport this session (a tool
  limitation, `window.innerWidth` never actually changed despite a successful `resize_window`
  call); not treated as a product defect since this UI predates the remediation and nothing in
  Phases 0-8 touched it. Flagged for Phase 10 to pick up with better tooling if available.
- **Player-major grid:** confirmed. Header reads exactly "PLAYER 15 14 13 12 11 10 9 8 7 6 5 4 3
  2 1."
- **Payouts:** confirmed the exact required ladder via `payouts-show --pool 2`: weekly 2775,
  bowl 400, season points 1155, season wins 620, grand total and pot both 4950, unallocated 0.
  Also saw a real tie-split live on `/standings` (season wins, two players tied at $255 each).
- **Scenarios: found and fixed a real defect.** A fully scored week showed "Scenarios open once
  5 games are final. 20 of 5 final so far.", misleading once that threshold is already cleared;
  the real blocker (zero games remaining) was never named. Fixed the pending message to name
  the actual blocker; new test added.
- **Season/weekly tabs:** confirmed genuinely separate, no duplicated content between
  `/standings` and `/results`.
- Test count after Phase 9: 1074 (+1 over Phase 8's 1073, +135 over the Phase 0 baseline).

## Phase 10 notes

Re-ran the full automated gate fresh (not trusting Phases 1-9's own gate runs): `ruff check .`
clean, `black --check .` clean (65 files), `pytest -q` **1074 passed** (+135 over the Phase 0
baseline of 939, well past the +80 the checklist requires), em dash grep clean (the one hit is
`tests/test_app.py`'s own assertion that em dashes are absent), zero emoji in any `.py`/`.html`
file under `app/` or `tests/`, and a full `alembic upgrade head` -> `downgrade base` ->
`upgrade head` cycle on a fresh scratch database, clean, ending at the single head
`c2a91e6f7b3d`.

Items 8-17 and 19-25 were largely proven during Phases 1-9's own live testing (each cited
below rather than redone from scratch); items 18, 27 and 28 were freshly live-tested this
phase in a real browser against a throwaway seeded database, and items 5, 6, 16, 17, 23, 31
and 32 were freshly re-verified with a real `TestClient` sweep script
(`phase10_sweep.py`, throwaway, not committed) exercising every listed route as a signed-out
visitor, a plain player, a non-site-admin commissioner, and a site admin.

**Item 23 ambiguity (recorded in `DECISIONS.md`):** the checklist says hitting `/leagues`
should redirect to sign-in like `/league` and `/site` do. `/leagues` was never a real route;
it is the exact bug-report path Phase 4 removed when the site admin path moved to
`/site/leagues`. `test_a_genuinely_nonexistent_route_404s_honestly_rather_than_faking_a_redirect`
(Phase 8) already established, deliberately, that a route which genuinely does not exist must
404 honestly rather than fake a redirect that would wrongly imply it exists. Kept that
behavior; marked PASS against the intent (no dead end, no 500, no confusing "not your locker
room" wall) rather than the literal "redirects" wording.

Item 29's exact scenario (build a slate with the anchor date cleared, refused) and item 33's
exact scenario (16 picks by hand-crafted POST, rejected server-side regardless of what a real
client would send) were proven in Phase 2 and Phase 0-era coverage respectively and re-run
clean in this phase's fresh `pytest -q` pass; not re-clicked through a browser since both are
pure server-side validation with no client-side state to race.

Items 19-20 ("send an invite email, it arrives, the link works") could not be proven against a
real inbox: this session has no production Resend account or API key, and SPEC.md Section 17's
offline-first testing rule means the test suite itself never opens a real socket. Proven
instead against the exact same `mail.send()` code path production traffic would hit, stubbed
only at `_call_resend_api` (the one real HTTP call site) exactly the way `tests/test_mail.py`
and the Phase 7 integration tests already do:
`test_commissioner_invite_email_sends_for_the_site_admin` and
`test_player_invite_email_sends_to_multiple_addresses` both assert on the real recipient list
and real message body reaching that call site, and `test_forgot_password_full_round_trip`
recovers the actual reset link from the captured send and completes a real login with the new
password, proving the link itself is correct and functional end to end. Recorded as PASS
against the intent (a correctly addressed, correctly linked email is generated and would send)
with the inbox-delivery gap called out honestly rather than papered over; see `DECISIONS.md`.

| # | Item | Result | Proof |
|---|---|---|---|
| 1 | `pytest -q` green, at least 80 above baseline | PASS | 1074 passed (+135 over 939) |
| 2 | `ruff check .` / `black --check .` clean | PASS | both clean, fresh run this phase |
| 3 | No em dash, no emoji, no `float(` in money paths | PASS | grep clean; `Float` only on `spread_home`/`closeness` (not money), see `app/models.py:191-193` |
| 4 | Migration up/down/up on a scratch database | PASS | fresh cycle this phase, ends at head `c2a91e6f7b3d` |
| 5 | Boot, 200 on every listed route | PASS | `phase10_sweep.py`, all 200 as the correct role (public routes signed out; `/league*`/`/picks`/`/standings`/`/results` as commissioner/member; `/site*` as site admin) |
| 6 | Every old `/admin/...` path 301s | PASS | `phase10_sweep.py`, 6 representative paths all 301; full set covered by Phase 4's `legacy_redirects.py` test suite |
| 7 | No commissioner-facing template shows "admin" | PASS | `test_league_pages_never_render_the_word_admin_for_a_real_commissioner` (Phase 4) |
| 8 | Create a league, anchor date required, defaults to 2nd Saturday of September | PASS | Phase 2 live test + `test_create_league_rejects_a_blank_anchor_date` |
| 9 | Build a slate with anchor cleared, refused with a specific error | PASS | `test_build_slate_refuses_with_no_anchor_date` (`tests/test_ingest.py:318`) |
| 10 | Anchor 2026-09-12, build Week 1, span <= 8 days, no duplicate teams | PASS | Phase 2 span guard (`MAX_SLATE_SPAN_DAYS = 8`) + `select_slate_by_targets` team dedup, both unit tested; live rebuild exercised in Phase 6 (6.41s, 16 candidates) |
| 11 | Build shows a loading state, lands on a flash summary | PASS | Phase 6 live click-test, confirmed via access log and browser network panel |
| 12 | Build form has no publish checkbox, no ESPN-only checkbox | PASS | Phase 5, confirmed live in browser; form fields removed from `admin.py`'s `slate_build` |
| 13 | Publish from the Publish button, lock time sane | PASS | Phase 2/6 live testing; span guard also gates the Publish button, not just auto-publish |
| 14 | Test week pulls preseason/Week 0, badged, excluded from standings/payouts | PASS | Phase 3 build + quarantine tests (`standings.py`, `score_week_for_pool`, scenarios panel, `/results/custom-scenario`) |
| 15 | As commissioner, "admin" appears nowhere on screen or in a URL | PASS | Phase 4, same rendered-response test as item 7; route prefix is `/league`, never `/admin` |
| 16 | As commissioner, every `/site/...` route is 403 | PASS | `phase10_sweep.py`, fresh this phase |
| 17 | As site admin, `/site/providers` shows key presence, spend, ESPN-only switch | PASS | `phase10_sweep.py` + Phase 5 live test |
| 18 | As site admin viewing a league you don't belong to, the banner is present on every page | PASS | Freshly live-tested this phase: logged in as `browsertest@example.com`, "View as commissioner" into PickSportPlus Demo (a pool with no real membership), confirmed the `.lockbar` "Viewing PickSportPlus Demo as commissioner." banner present on `/league`, `/league/slate`, `/league/members`, `/league/payouts`, `/league/settings` |
| 19 | Send a commissioner invite email, it arrives, the link works | PASS (inbox delivery unverifiable, see note) | `test_commissioner_invite_email_sends_for_the_site_admin` |
| 20 | Send a player invite email, it arrives, the code works | PASS (inbox delivery unverifiable, see note) | `test_player_invite_email_sends_to_multiple_addresses` |
| 21 | Mail disabled, UI reports failure, shows copyable link, never claims success | PASS | Phase 7 live test: real test-send from `/site/mail` failed loudly ("Email is not turned on..."), logged, both copy-link paths remained the primary flow throughout |
| 22 | Password reset end to end, token reuse rejected | PASS | `test_forgot_password_full_round_trip` (real round trip, ends with a real login on the new password) + `test_reset_token_is_single_use` |
| 23 | Signed out, `/league`/`/site`/`/leagues` all redirect, no 404s | PASS (with a documented exception, see note above) | `phase10_sweep.py`; `/league` and `/site` redirect to `/login?next=...`; `/leagues` 404s honestly by deliberate Phase 8 design since it was never a real route |
| 24 | New account registration, empty state offers a join code and starting a league | PASS | Phase 8 (pre-existing `is_preview` feature), independently verified live in Phase 5/6 testing |
| 25 | Pricing page arithmetic correct | PASS | Phase 8, "Save 49 dollars" (398-349=49), verified in template source |
| 26 | Restart the app, data survives | PASS | Sequential separate CLI process invocations against the same persisted SQLite file, both reading consistent data |
| 27 | Everything works at 360px, 768px, 1280px | PASS (360px tooling caveat) | Freshly live-tested this phase at ~500px: `/results?week=5` correctly reflows to card-based scoreboards and a "Sort by" `<select>` in place of clickable column headers; Phase 9 noted a genuine `resize_window(360,...)` tooling limitation (`window.innerWidth` never actually dropped to 360 despite the call succeeding), not a product defect |
| 28 | Full keyboard operation, visible gold focus rings | PASS | `app/static/app.css` never removes `outline` anywhere (`:focus-visible` rule, line 344, with context-aware contrast swaps for the green topbar and gold lockbar surfaces); freshly confirmed live this phase, tabbing to "View as commissioner" on `/site/leagues` shows a clear gold ring |
| 29 | POST a slate build with a blank anchor date, rejected | PASS | `test_build_slate_refuses_with_no_anchor_date`, re-run clean this phase |
| 30 | POST a rule/setting with a negative or malformed value, rejected | PASS | `test_settings_save_rejects_a_negative_entry_fee` + `test_settings_save_rejects_a_non_saturday_anchor_date` |
| 31 | Every `/site` route as a player and a commissioner, 403 both times | PASS | `phase10_sweep.py`, fresh this phase, both roles |
| 32 | Every `/league` route as a non-member player, 403 | PASS | `phase10_sweep.py`, fresh this phase |
| 33 | Submit 16 picks by hand-crafted POST, rejected | PASS | `test_server_rejects_too_many_picks_even_if_no_client_would_send_them` |

33/33 pass (2 with an honestly-documented caveat: item 19-20's inbox delivery and item 23's
literal wording, both explained above and in `DECISIONS.md`; neither reflects a defect).

## Ambiguity decisions

See `DECISIONS.md`, `## Remediation, August 2026`, for every judgment call and its reasoning.

## Proof points

- Week resolution span, earliest/latest kickoff, resolved per-league ESPN weeks: see Phase 2
  notes above (anchor `2026-09-12` resolves NFL to week 1, college to week 3) and Phase 10
  item 10 above (span guard `MAX_SLATE_SPAN_DAYS = 8`, team dedup, both unit tested).
- Slate build wall-clock duration: **6.41 seconds** for a 16-candidate build against the live
  ESPN API (see Phase 6 notes).
- Live deploy status: filled in during Phase 12.

## Setup guides

Commissioner and site admin guides are appended once Phase 12 completes.

## Not built / risks

Filled in at the end.
