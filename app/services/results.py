"""Pull finals from ESPN and score a week. Both operations are idempotent.

ESPN is keyless and unmetered, so the hourly cron can refresh scores every run without
touching the credit budget. Nothing in this module calls a metered provider.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Game, Pick, Pool, PoolMember, Week, WeekEntry, utcnow
from app.providers import espn
from app.providers.http import ProviderError
from app.scoring import GameOutcome, PickInput, score_week, weekly_winner_ids

log = logging.getLogger("picksportplus.results")


@dataclass
class ResultsReport:
    week_number: int
    updated: int = 0
    finals: int = 0
    pending: int = 0
    voided: int = 0
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"Week {self.week_number}: {self.finals} final, {self.pending} still to play, "
            f"{self.voided} void. {self.updated} rows updated."
        )


@dataclass
class ScoreReport:
    week_number: int
    players: int = 0
    scored_games: int = 0
    winners: list[str] = field(default_factory=list)
    week_complete: bool = False

    def summary(self) -> str:
        text = (
            f"Week {self.week_number}: scored {self.players} players over "
            f"{self.scored_games} completed games."
        )
        if self.winners:
            text += " Week winner: " + ", ".join(self.winners) + "."
        if self.week_complete:
            text += " The week is complete."
        return text


def _aware(value: dt.datetime | None) -> dt.datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=dt.UTC)


# Fetching finals ------------------------------------------------------------


def fetch_results(db: Session, pool: Pool, week: Week) -> ResultsReport:
    """Refresh status, scores and winners for every game in the week."""
    report = ResultsReport(week_number=week.week_number)
    rows = list(db.scalars(select(Game).where(Game.week_id == week.id)))
    if not rows:
        report.warnings.append("This week has no games yet.")
        return report

    by_event: dict[str, Game] = {g.espn_event_id: g for g in rows}
    leagues = sorted({g.league for g in rows})

    for league in leagues:
        resolved = (week.resolved_weeks or {}).get(league)
        espn_week = resolved["week"] if resolved else week.week_number
        season_type = resolved["season_type"] if resolved else espn.SEASON_TYPE_REGULAR
        try:
            payload = espn.fetch_scoreboard(
                db,
                league,
                week.season_year,
                espn_week,
                season_type=season_type,
                ttl_minutes=5,
            )
        except ProviderError as exc:
            report.warnings.append(
                f"Could not refresh {league.upper()} scores: {exc}. "
                "The last known results are still shown."
            )
            continue

        for game in espn.parse_scoreboard(payload, league):
            row = by_event.get(game.event_id)
            if row is None:
                continue
            changed = False

            # A commissioner void is a deliberate decision and outranks the feed.
            if row.status != "void" and row.status != game.status:
                row.status = game.status
                changed = True
            if game.home.score is not None and row.home_score != game.home.score:
                row.home_score = game.home.score
                changed = True
            if game.away.score is not None and row.away_score != game.away.score:
                row.away_score = game.away.score
                changed = True
            if row.status != "void" and game.winner is not None and row.winner != game.winner:
                row.winner = game.winner
                changed = True
            if changed:
                report.updated += 1

    db.flush()

    for row in rows:
        if row.status == "void":
            report.voided += 1
        elif row.status == "final":
            report.finals += 1
        else:
            report.pending += 1

    # Lock the week the moment the first slate game has kicked off, so the status in the
    # database agrees with what the request-time lock check already enforces.
    if week.status == "open" and week.lock_at and utcnow() >= _aware(week.lock_at):
        week.status = "locked"
        db.flush()

    return report


# Scoring --------------------------------------------------------------------


def score_week_for_pool(db: Session, pool: Pool, week: Week) -> ScoreReport:
    """Recompute every week_entry for the week. Safe to run repeatedly."""
    report = ScoreReport(week_number=week.week_number)

    slate = list(db.scalars(select(Game).where(Game.week_id == week.id, Game.in_slate.is_(True))))
    outcomes = [GameOutcome(game_id=g.id, status=g.status, winner=g.winner) for g in slate]
    report.scored_games = sum(
        1 for o in outcomes if o.status == "final" and o.winner in ("home", "away")
    )

    members = list(db.scalars(select(PoolMember).where(PoolMember.pool_id == pool.id)))
    picks_by_user: dict[int, list[Pick]] = {}
    for pick in db.scalars(select(Pick).where(Pick.week_id == week.id)):
        picks_by_user.setdefault(pick.user_id, []).append(pick)

    existing = {
        e.user_id: e for e in db.scalars(select(WeekEntry).where(WeekEntry.week_id == week.id))
    }

    # picks_required stands in for a real per pool "picks required" field until Phase 3
    # adds one: today every player picks the whole slate, so the slate size is correct.
    picks_required = pool.num_games_per_week

    results = {}
    for member in members:
        picks = picks_by_user.get(member.user_id, [])
        result = score_week(
            [
                PickInput(game_id=p.game_id, picked_team=p.picked_team, confidence=p.confidence)
                for p in picks
            ],
            outcomes,
            mode=pool.scoring_mode,
            picks_required=picks_required,
        )
        results[member.user_id] = result

        entry = existing.get(member.user_id)
        if entry is None:
            entry = WeekEntry(user_id=member.user_id, pool_id=pool.id, week_id=week.id)
            db.add(entry)
        entry.points = result.points
        entry.correct = result.correct
        entry.possible = result.possible
        entry.did_not_submit = result.did_not_submit
        # A player who never submitted keeps a null submitted_at. did_not_submit above is
        # the flag the UI checks; under scoring_mode "inverse" their points are the maximum
        # penalty, not zero, so submitted_at is not a safe stand in for that any more.
        if picks:
            entry.submitted_at = entry.submitted_at or min(
                (_aware(p.created_at) for p in picks), default=utcnow()
            )
        report.players += 1

    winners = weekly_winner_ids(results, mode=pool.scoring_mode)
    for member in members:
        entry = existing.get(member.user_id) or db.scalar(
            select(WeekEntry).where(
                WeekEntry.week_id == week.id, WeekEntry.user_id == member.user_id
            )
        )
        if entry is not None:
            entry.is_winner = member.user_id in winners

    db.flush()

    if winners:
        names = db.scalars(
            select(WeekEntry).where(WeekEntry.week_id == week.id, WeekEntry.user_id.in_(winners))
        )
        report.winners = sorted(e.user.display_name for e in names)

    # The week is complete when every slate game has stopped being playable.
    playable = [g for g in slate if g.status not in ("final", "void")]
    if slate and not playable:
        report.week_complete = True
        if week.status != "scored":
            week.status = "scored"
            week.scored_at = utcnow()
    db.flush()

    return report
