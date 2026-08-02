"""Weekly leaderboard and season standings, aggregated from week_entries on read."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Pool, PoolMember, User, Week, WeekEntry
from app.scoring import SeasonEntryInput, season_totals


@dataclass
class StandingRow:
    user_id: int
    display_name: str
    points: int
    correct: int
    possible: int
    weeks_played: int
    weekly_wins: int
    rank: int = 0
    is_you: bool = False

    @property
    def accuracy(self) -> str:
        if not self.possible:
            return "."
        return f"{round(100 * self.correct / self.possible)}%"


@dataclass
class WeeklyRow:
    user_id: int
    display_name: str
    points: int
    correct: int
    possible: int
    submitted: bool
    is_winner: bool
    rank: int = 0
    is_you: bool = False


def _members(db: Session, pool: Pool) -> list[User]:
    return list(
        db.scalars(
            select(User)
            .join(PoolMember, PoolMember.user_id == User.id)
            .where(PoolMember.pool_id == pool.id)
            .order_by(User.display_name)
        )
    )


def _assign_ranks(rows: list) -> None:
    """Competition ranking. Equal points share a rank and the next rank skips ahead."""
    last_points: int | None = None
    last_rank = 0
    for index, row in enumerate(rows, start=1):
        if last_points is not None and row.points == last_points:
            row.rank = last_rank
        else:
            row.rank = index
            last_rank = index
            last_points = row.points


def season_standings(db: Session, pool: Pool, viewer_id: int | None = None) -> list[StandingRow]:
    """Every member of the pool, including players who have not scored yet."""
    members = _members(db, pool)
    entries_by_user: dict[int, list[WeekEntry]] = {m.id: [] for m in members}
    entries = db.scalars(
        select(WeekEntry)
        .join(Week, Week.id == WeekEntry.week_id)
        .where(WeekEntry.pool_id == pool.id, Week.season_year == pool.season_year)
    )
    for entry in entries:
        entries_by_user.setdefault(entry.user_id, []).append(entry)

    rows: list[StandingRow] = []
    for member in members:
        totals = season_totals(
            SeasonEntryInput(
                points=e.points, correct=e.correct, possible=e.possible, is_winner=e.is_winner
            )
            for e in entries_by_user.get(member.id, [])
        )
        rows.append(
            StandingRow(
                user_id=member.id,
                display_name=member.display_name,
                points=totals.points,
                correct=totals.correct,
                possible=totals.possible,
                weeks_played=totals.weeks_played,
                weekly_wins=totals.weekly_wins,
                is_you=member.id == viewer_id,
            )
        )

    rows.sort(key=lambda r: (-r.points, -r.correct, -r.weekly_wins, r.display_name.lower()))
    _assign_ranks(rows)
    return rows


def latest_scored_week(db: Session, pool: Pool) -> Week | None:
    """The most recent week worth showing a leaderboard for."""
    week = db.scalar(
        select(Week)
        .where(
            Week.pool_id == pool.id,
            Week.season_year == pool.season_year,
            Week.status == "scored",
        )
        .order_by(Week.week_number.desc())
    )
    if week:
        return week
    return db.scalar(
        select(Week)
        .where(
            Week.pool_id == pool.id,
            Week.season_year == pool.season_year,
            Week.status == "locked",
        )
        .order_by(Week.week_number.desc())
    )


def weekly_leaderboard(
    db: Session,
    pool: Pool,
    week: Week | None = None,
    viewer_id: int | None = None,
) -> tuple[list[WeeklyRow], Week | None]:
    """Leaderboard for one week. Defaults to the most recent scored or locked week."""
    week = week or latest_scored_week(db, pool)
    if week is None:
        return [], None

    members = _members(db, pool)
    entries = {
        e.user_id: e for e in db.scalars(select(WeekEntry).where(WeekEntry.week_id == week.id))
    }

    rows: list[WeeklyRow] = []
    for member in members:
        entry = entries.get(member.id)
        rows.append(
            WeeklyRow(
                user_id=member.id,
                display_name=member.display_name,
                points=entry.points if entry else 0,
                correct=entry.correct if entry else 0,
                possible=entry.possible if entry else 0,
                submitted=bool(entry and entry.submitted_at),
                is_winner=bool(entry and entry.is_winner),
                is_you=member.id == viewer_id,
            )
        )

    rows.sort(key=lambda r: (-r.points, -r.correct, r.display_name.lower()))
    _assign_ranks(rows)
    return rows, week
