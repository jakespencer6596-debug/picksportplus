# Week 1 readiness build: final report

Branch `week1-readiness`, 11 commits on top of `main`. Every phase gate (`ruff check .`,
`black .`, `pytest -q`) passed before that phase's commit, and was independently re-verified
after the fact. Full test suite at the end of Phase 9: **781 passed, 0 failed**, zero live
network calls during the pytest run. Full reasoning for every judgment call lives in
`DECISIONS.md`; the phase-by-phase build log lives in `PROGRESS.md`. This file is the summary
a human needs to read once.

## 1. Every phase, done, with commit SHA

| Phase | What | Commit |
|---|---|---|
| 0 | `doctor` diagnostic command, build tracking, environment repairs | `a5d22aa` |
| 1 | Per-league ESPN week resolution from a calendar anchor date | `5445769` |
| 1 (follow-up) | `fetch_results` had the same week-number bug, fixed too | `eece5de` |
| 2 | Inverse scoring: lowest total wins, wrong picks earn the staked points, no-show pays the max penalty | `59bb21e` |
| 3 | Pick 15 of 20, confidence 1 to 15 | `894cfb8` |
| 4 | Two-step pick entry: numeric input, reorder, drag refine, explicit lock/unlock | `7e35f04` |
| 5 | Pinned and rivalry games, commissioner-curated slate (`auto_publish` off by default) | `7a4b4b6` |
| 6 | Season Standings / Weekly Results split, sortable tables, player-major pick grid | `b34b462` |
| 7 | Venmo entry gate, payout rules, weekly payout column | `d4b9e93` |
| 8 | Scenario engine: placement odds, leverage table, custom scenarios | `ffa09b3` |
| 9 | Three-week demo seed, Week 1 live wiring, acceptance pass | `906560e` |

Nothing was merged to `main` or pushed by any automated step. That happens after this report,
by hand, with your review in between.

## 2. Decisions made on ambiguity, and why

The full record is in `DECISIONS.md` (organized by phase). The ones most worth knowing about
without reading the whole file:

- **The repo had to move.** It was silently corrupting git (`git status` hung indefinitely)
  because it lived inside OneDrive's Files On-Demand sync. Working copy is now
  `C:\dev\PickSportPlus`, outside any cloud-sync path. Your original OneDrive copy is untouched.
  A local Python 3.11 install and a rebuilt virtual environment were also required; nothing on
  this machine could run `pytest` before that.
- **No-show penalty and possible-count refinement (Phase 2/3).** A no-show pays
  `sum(1..picks_required)` under inverse scoring and is always excluded from winning a week,
  even though 0 points is a perfect score. `possible` (games available to be scored) is scoped
  to each player's own submitted picks, so a no-show's `possible` is honestly 0, and a voided
  pick only shrinks the possible count for the player who picked it, not the whole slate.
- **`auto_publish` really is off by default now** (Phase 5), including for a freshly seeded
  pool, not just in the model's column default. The tool always proposes a slate; nothing opens
  automatically unless you turn that back on in settings.
- **Season awards panel gating (Phase 7).** There is no "the season is officially over" flag
  anywhere in this data model. The panel shows current season-scope payout standings once at
  least one week is scored, labeled as reflecting current standings, not as a final declaration.
  If you want a hard end-of-season gate later, that needs a real decision from you about what
  marks a season complete.
- **The scenario engine's exhaustive path (Phase 8) does not literally call `score_week` once
  per enumerated scenario**, because that measured over 5 seconds at 15 remaining games, well
  past the 2 second budget. It instead computes an exact linear decomposition of the same
  scoring rules, proven equal to brute-force `score_week` output on every one of a scenario set
  in a real test (`test_linearization_matches_brute_force_score_week_on_every_scenario`),
  including the no-show case. The result is identical; only the arithmetic path is faster.

## 3. Live verification, September 12, 2026

Run for real against the live ESPN API, not fixtures, on 2026-08-08 (full output in
`PROGRESS.md` under "Phase 9b live verification"):

```
nfl   LeagueResolution(week=1, season_type=2, label='Week 1', ...)
ncaaf LeagueResolution(week=2, season_type=2, label='Week 2', ...)
```

**September 12, 2026 is NFL week 1 and college week 2.** The original brief guessed college
week 3; that guess was wrong, corrected everywhere it was stated as fact (the Phase 1 unit test
fixture that assumed week 3 was left alone, since it is testing the resolution algorithm
against fixed input, not asserting a real-world fact). This does not matter functionally: the
whole point of Phase 1 is that the app asks ESPN's real calendar instead of hardcoding a number,
so whichever week it actually turns out to be, the build is correct.

A live `build-slate` for that real date also came back with a real 102-game candidate pool and
built a real 20-game slate (7 college, 13 NFL, the college target filled by the NFL cross-league
fill because most college spreads have not posted yet a month out, which is expected). All three
provider keys were confirmed live and working in this session: ESPN (keyless, HTTP 200), The
Odds API (live call, 272 games returned), CollegeFootballData (live call, 95 lines returned).

## 4. What was deliberately not built

- **Team logos and colors.** Explicitly out of scope per the group's own decision (paid API
  rejected, self-hosted logo database rejected as it goes stale). The abbreviation and full-name
  treatment is untouched.
- Email notifications, public pool discovery, mobile apps, against-the-spread scoring: all
  explicitly out of scope from the brief, none of it exists.
- Client-side JS behavior (drag reordering, the numeric-input duplicate highlighting, table
  sorting, touch interaction at 360px) is implemented but **not verified by an automated test in
  this environment**, which has no browser or JS execution tool. 18 of the 21 acceptance-pass
  items are real, named, passing automated tests; 3 (drag/reorder, the 360px touch viewport, and
  client-side table sort clicking) need a human in an actual browser. Full list with test names
  in `PROGRESS.md`, "Phase 9c acceptance pass."
- A hard "season is complete" concept, see Decision 3 above.

## 5. Exactly what you need to do by hand before Week 1

1. **Render cron is not wired up, and this is real, not cosmetic.** `render.yaml` deliberately
   runs no scheduled job, because it was written for a free-tier demo blueprint and Render's
   free plan does not offer cron. Right now nothing will automatically build the Week 1 slate,
   fetch scores, or score the week on its own. Before September 12, either upgrade the Render
   plan and add the `type: cron` block already described in `README.md`, or point any external
   scheduler (a GitHub Actions scheduled workflow, cron-job.org, anything that can hit a
   shell or a webhook once an hour) at `python -m app.cli run-cron`. This is the single most
   important thing on this list.
2. **The demo service runs `OFFLINE_MODE=true`.** That is correct for the free public demo
   Logan's group tests against the week of August 17, but the real Week 1 pool needs a
   deployment (the same service or a separate one) with `OFFLINE_MODE=false` and real
   `ODDS_API_KEY`/`CFBD_API_KEY` values set as Render environment variables, not committed
   anywhere.
3. Enter the real payout amounts in `/admin/payouts` (weekly, bowl, season). Nothing is
   pre-filled; the demo pool has clearly-labeled demo numbers, the real pool has none.
4. Set the real entry fee and Venmo handle in `/admin/settings`.
5. Set (or rotate) the real join code, distribute it to the group, and decide who creates their
   own accounts versus who you create for them.
6. Set `week1_anchor_date` on the real pool to `2026-09-12` if it is not already set from
   whatever `seed-admin` run creates the production pool (the demo pool used a throwaway
   verification pool for the live check in Section 3 above, not your production pool).
7. Review the Week 1 slate once it actually builds (pin any rivalry games you want that the
   auto rivalry list does not already cover, the seeded list is Ohio State/Michigan, Auburn/
   Alabama, Army/Navy, Michigan/Michigan State, Florida/Georgia, Texas/Oklahoma, USC/Notre Dame).
8. Mark members paid as Venmo payments actually arrive; nobody is gated out until you turn
   `payment_required_to_pick` on and it stays off by default until you do.

## 6. Top three risks to a clean September 12 launch

1. **The cron gap (Section 5, item 1).** If nobody wires up scheduled `run-cron` calls, nothing
   builds, publishes, fetches, or scores automatically, and the whole "set and forget" promise
   of this product breaks silently. Mitigation: treat this as a blocking task, not a nice to
   have, and verify it by watching one real hourly run actually happen before September 12,
   not just by reading the config.
2. **Spread coverage a month out.** The live build in Section 3 only resolved 23 of 102
   candidate games' spreads this far from kickoff, and college specifically came up 5 short of
   its target of 12. The cross-league fill already covers for this automatically today, but if
   you want the final Week 1 slate to actually be 8 NFL and 12 college rather than a fill-shifted
   mix, rebuild the slate again closer to game day once more sportsbooks have posted lines,
   rather than locking in whatever the first build produces a month early.
3. **Nobody has driven the live UI in a real browser yet.** The three acceptance items that
   could not be automated (drag reorder, 360px touch, client-side table sort) are exactly the
   interactions the group specifically asked for and will notice first if something is off.
   Mitigation: before the August 17 to 19 group test window, spend fifteen minutes on a real
   phone walking through picks end to end, on the actual demo pool, before strangers do it for
   you.
