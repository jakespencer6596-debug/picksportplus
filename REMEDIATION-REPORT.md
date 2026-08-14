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
| 0 | Baseline, branch, report | Done | (this commit) |
| 1 | Data persistence (ephemeral SQLite) | Done | (this commit) |
| 2 | Week resolution / anchor date | Pending | |
| 3 | Preseason / test week support | Pending | |
| 4 | Split site admin vs commissioner | Pending | |
| 5 | Move provider controls to site admin | Pending | |
| 6 | Fix slate build interaction | Pending | |
| 7 | Transactional email | Pending | |
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
