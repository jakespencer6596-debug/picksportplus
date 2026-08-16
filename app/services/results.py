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

from app.models import Game, Pick, Pool, PoolMember, User, Week, WeekEntry, utcnow
from app.providers import espn
from app.providers.http import ProviderError
from app.scoring import GameOutcome, PickInput, score_week, weekly_winner_ids
from app.services import payouts as payout_service

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
    payouts_recalculated: bool = False

    def summary(self) -> str:
        text = (
            f"Week {self.week_number}: scored {self.players} players over "
            f"{self.scored_games} completed games."
        )
        if self.winners:
            text += " Week winner: " + ", ".join(self.winners) + "."
        if self.week_complete:
            text += " The week is complete."
        if self.payouts_recalculated:
            text += " Payout amounts were recalculated to match this correction."
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


def score_week_for_pool(
    db: Session, pool: Pool, week: Week, actor: User | None = None
) -> ScoreReport:
    """Recompute every week_entry for the week. Safe to run repeatedly.

    actor identifies who is triggering this specific call and is only ever used for one
    thing: deciding whether a call that lands on a week that was ALREADY "scored" before
    this call started is allowed to recalculate that week's frozen PayoutAward rows (see
    the freeze block below). It has no effect on the first pass that scores a week.
    """
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

    picks_required = pool.picks_required

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
        already_scored = week.status == "scored"
        if not already_scored:
            week.status = "scored"
            week.scored_at = utcnow()

            # A test week (Phase 3, preseason and test week support) scores normally within
            # itself, that is what report.players/scored_games above already reflect, but it
            # must never generate a PayoutAward of any scope, and finishing its own scoring is
            # never the season's real completion signal. Skipping the whole freeze block below
            # is what makes that a computation-level guarantee rather than a template-only one.
            if week.is_test_week:
                db.flush()
                return report

            # Freeze this week's payout awards the instant it finishes scoring, so a pot that
            # grows later (a member pays late) never silently changes a figure the
            # commissioner may already have paid over Venmo. snapshot_awards is itself
            # idempotent and a no-op when there are no configured rules for this scope, so
            # this call is safe even for a pool with no payout rules at all.
            scope = "bowl" if week.is_bowl_week else "weekly"
            payout_service.snapshot_awards(db, pool, scope, week=week)

            # This codebase's real season structure is weeks 1-15 regular season plus week 16
            # as the bowl week, and there is no separate stored "season complete" flag
            # anywhere. The bowl week finishing scoring is therefore treated as the season's
            # natural completion signal, a deliberate, documented choice rather than a
            # discovered fact, and is the trigger for freezing both season-wide scopes here.
            if week.is_bowl_week:
                payout_service.snapshot_awards(db, pool, "season_points", week=None)
                payout_service.snapshot_awards(db, pool, "season_wins", week=None)
        elif actor is not None and not week.is_test_week:
            # A correction landing after this week already finished scoring once (a game
            # voided or a bad final fixed via /league/slate's "Refresh results" button, which
            # is the only caller that ever passes an actor here): standings above were just
            # recomputed to reflect it, but the PayoutAward rows frozen on the first pass are
            # still holding the pre-correction figures and would otherwise stay wrong forever,
            # with no in-product way to fix them. recalculate_awards is this codebase's one
            # deliberate, attributed overwrite path (see its own docstring); requiring a real
            # actor here, rather than recalculating on every repeat call, is what keeps this
            # from firing off the unattended cron path, which never passes one.
            scope = "bowl" if week.is_bowl_week else "weekly"
            payout_service.recalculate_awards(db, pool, scope, week, actor)
            if week.is_bowl_week:
                payout_service.recalculate_awards(db, pool, "season_points", None, actor)
                payout_service.recalculate_awards(db, pool, "season_wins", None, actor)
            report.payouts_recalculated = True
    db.flush()

    return report
