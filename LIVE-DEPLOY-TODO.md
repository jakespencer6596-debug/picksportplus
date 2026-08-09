# Setting up the real Week 1 production service

Not done yet. This is separate from the free demo at `picksportplus.onrender.com`, which
stays as is for Logan and the group to keep testing against. Revisit this when ready to go
live for the real pool.

## 1. Create a new Render web service

- Render dashboard: New + -> Web Service.
- Connect the same repo, `jakespencer6596-debug/picksportplus`, branch `main`.
- Name it something distinct from the demo, for example `picksportplus-live`.
- Build command: `pip install -r requirements.txt`
- Start command:
  ```
  alembic upgrade head && python -m app.cli seed-admin && uvicorn app.main:app --host 0.0.0.0 --port $PORT --proxy-headers --forwarded-allow-ips="*"
  ```
- Plan: start on Free (no cost). Upgrade later only if you want Render's own cron instead of
  an external scheduler, see step 3.

## 2. Environment variables on the new service

| Key | Value |
|---|---|
| `OFFLINE_MODE` | `false` |
| `SEASON_YEAR` | `2026` |
| `DATABASE_URL` | `sqlite:////tmp/picksportplus.db` to start (ephemeral disk, same as the demo; real persistence needs a Postgres add on, a separate decision) |
| `SECRET_KEY` | click Generate in the Render dashboard |
| `ADMIN_EMAIL` | your real email |
| `ADMIN_PASSWORD` | type it directly into Render's dashboard yourself |
| `DEFAULT_JOIN_CODE` | whatever code your group should use to join |
| `ODDS_API_KEY` | type it directly into Render's dashboard yourself, from your local `OddsAPI_APIKey.txt` |
| `CFBD_API_KEY` | type it directly into Render's dashboard yourself, from your local `CFBD_APIKEY.txt` |

`week1_anchor_date` needs no env var, it already defaults to `2026-09-12` in `app/config.py`.

## 3. Decide how weeks actually get built and scored automatically

Nothing runs on a schedule until this is set up. Two options:

- **Render cron (paid)**: upgrade the web service plan, add the `type: cron` block already
  written out in `README.md` under "Deploying the full app to Render," running
  `python -m app.cli run-cron` hourly. Roughly 13 dollars a month all in.
- **External scheduler (free)**: point any free scheduler (a GitHub Actions scheduled
  workflow is the easiest) at `python -m app.cli run-cron` hourly, against the same
  `DATABASE_URL`. No Render cost beyond the free web service.

## 4. Once it is deployed

- Log in as the real admin, confirm the Week 1 slate looks right, pin any rivalry games the
  seeded list does not already cover (Ohio State/Michigan, Auburn/Alabama, Army/Navy,
  Michigan/Michigan State, Florida/Georgia, Texas/Oklahoma, USC/Notre Dame are already in
  there).
- Set the real entry fee and Venmo handle in `/admin/settings`.
- Enter real payout amounts in `/admin/payouts`.
- Distribute the real join code to the group.

Everything else (scoring rules, pick flow, payouts, scenarios) is already live and tested,
see `WEEK1-REPORT.md` for the full build record.
