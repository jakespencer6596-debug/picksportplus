"""Weekly leaderboard and season standings, aggregated from week_entries on read.

Sort direction follows pool.scoring_mode: "standard" ranks highest points first,
"inverse" ranks lowest points first, since under inverse a low total is the win.
"""

from __future__ import annotations

import datetime as dt
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
    # Set only when this row's place in a season ladder (season_points_ranking or
    # season_wins_ranking, pool.season_tiebreak_mode == "wins") was actually decided by a
    # tiebreak rather than the ladder's own primary metric alone: a muted, plain sentence
    # naming what broke the tie, for example "Tiebreak: 4 weekly wins to 3." None on every
    # other row, and always None on a plain season_standings()/weekly_leaderboard() row.
    tiebreak_reason: str | None = None

    @property
    def accuracy(self) -> str:
        if not self.possible:
            return "."
        return f"{round(100 * self.correct / self.possible)}%"

    @property
    def accuracy_sort(self) -> int:
        """Numeric twin of accuracy, for the sortable table's data-sort-value.

        Same rounding as accuracy so the two never disagree; -1 for "no possible picks yet"
        so it sorts to the low end regardless of direction, since it is not a real accuracy.
        """
        if not self.possible:
            return -1
        return round(100 * self.correct / self.possible)


@dataclass
class WeeklyRow:
    user_id: int
    display_name: str
    points: int
    correct: int
    possible: int
    submitted: bool
    did_not_submit: bool
    is_winner: bool
    rank: int = 0
    is_you: bool = False
    # Set only when pool.weekly_tiebreak_mode == "wins" and this row's place was actually
    # decided by a tiebreak rather than points alone (Phase 3, weekly wins tiebreak): a muted,
    # plain sentence naming what broke the tie, for example "Tiebreak: 3 prior wins to 2." or,
    # in a week with no prior wins yet (week 1), "Tiebreak: submitted first." None otherwise,
    # including always under "split" mode, where a tie is never broken at all.
    tiebreak_reason: str | None = None


def _members(db: Session, pool: Pool) -> list[User]:
    return list(
        db.scalars(
            select(User)
            .join(PoolMember, PoolMember.user_id == User.id)
            .where(PoolMember.pool_id == pool.id)
            .order_by(User.display_name)
        )
    )


def _assign_ranks(rows: list, metric=lambda row: row.points) -> None:
    """Competition ranking. Equal metric values share a rank and the next rank skips ahead.

    Direction agnostic: it only compares each row to the one before it in the list, so it
    is correct whether the list arrives sorted best-first (descending) or worst-first
    (ascending). The sort itself is what decides which end is "best"; this function just
    walks whatever order it is handed. metric defaults to points, the only dimension every
    pre-Phase-2 caller (season_standings, weekly_leaderboard) ever ranked by; the season wins
    ladder's own "split" mode (app/services/standings.py._season_ranking) is the one caller
    that passes weekly_wins instead, since sharing a rank on equal points there would be
    exactly wrong, it needs to share on equal wins.
    """
    last_value = None
    last_rank = 0
    for index, row in enumerate(rows, start=1):
        value = metric(row)
        if last_value is not None and value == last_value:
            row.rank = last_rank
        else:
            row.rank = index
            last_rank = index
            last_value = value


def _points_sort_key(pool: Pool):
    """Sign multiplier so a plain ascending sort lands best-first for either mode.

    "standard": highest points is best, so points is negated. "inverse": lowest points is
    best, so points sorts as is.
    """
    return 1 if pool.scoring_mode == "inverse" else -1


def _season_base_rows(db: Session, pool: Pool, viewer_id: int | None = None) -> list[StandingRow]:
    """Every member of the pool, including players who have not scored yet, unranked and
    unsorted (rank stays 0). A test week's WeekEntry rows are excluded outright (Phase 3,
    preseason and test week support): a test week scores normally within itself, but must
    contribute zero to season totals, correct counts, and weekly-win counts. This is the one
    place that filter needs to live, since every season aggregate reads from this function.
    """
    members = _members(db, pool)
    entries_by_user: dict[int, list[WeekEntry]] = {m.id: [] for m in members}
    entries = db.scalars(
        select(WeekEntry)
        .join(Week, Week.id == WeekEntry.week_id)
        .where(
            WeekEntry.pool_id == pool.id,
            Week.season_year == pool.season_year,
            Week.is_test_week.is_(False),
        )
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
    return rows


def season_standings(db: Session, pool: Pool, viewer_id: int | None = None) -> list[StandingRow]:
    """Every member of the pool, ranked by season points alone (competition ranking: a tied
    pair shares a rank, the next distinct score skips ahead). This is the pool's season
    points order regardless of pool.season_tiebreak_mode; season_points_ranking below is what
    actually applies the outright tiebreak chain when the pool is set to break ties. Kept as
    its own function, unchanged, because it is still exactly what "split" mode needs and what
    the existing test suite already pins down.
    """
    rows = _season_base_rows(db, pool, viewer_id)
    sign = _points_sort_key(pool)
    rows.sort(key=lambda r: (sign * r.points, -r.correct, -r.weekly_wins, r.display_name.lower()))
    _assign_ranks(rows)
    return rows


def _season_final_week(db: Session, pool: Pool) -> Week | None:
    """The most recently scored real (non test) week of the season: the reference point for
    the season tiebreak submission rule (Phase 3, "Tab entry and season tiebreak"). A bowl
    week counts, it is a real, scored week of this season's structure. None when no real week
    has been scored yet, which every caller here treats as "nobody has a submission time,"
    never an error.
    """
    return db.scalar(
        select(Week)
        .where(
            Week.pool_id == pool.id,
            Week.season_year == pool.season_year,
            Week.status == "scored",
            Week.is_test_week.is_(False),
        )
        .order_by(Week.week_number.desc())
    )


def _season_submission_times(
    db: Session, pool: Pool, rows: list[StandingRow]
) -> tuple[dict[int, dt.datetime | None], Week | None]:
    """Each player's WeekEntry.submitted_at for the season's final scored week (see
    _season_final_week's docstring for why that week specifically). A player with no entry
    for that week, or no final week at all, maps to None, which the tiebreak sort below
    always sorts last: "did not submit the deciding week" must never look like "submitted
    first." Returns the final week alongside the map purely so a caller building a reason
    string has its week_number without a second query.
    """
    final_week = _season_final_week(db, pool)
    if final_week is None:
        return {row.user_id: None for row in rows}, None
    submitted = {
        e.user_id: e.submitted_at
        for e in db.scalars(select(WeekEntry).where(WeekEntry.week_id == final_week.id))
    }
    return {row.user_id: submitted.get(row.user_id) for row in rows}, final_week


@dataclass(frozen=True)
class _SeasonComponents:
    """One player's position on every level of the season tiebreak chain, each already
    oriented so a plain ascending comparison means "better." points is the pool's own scoring
    direction (see _points_sort_key); wins is always -weekly_wins, more wins is always
    better, regardless of scoring mode, since season_wins can never inherit the pool's
    scoring direction (SPEC.md Section 10b, app/payouts.py's own module docstring says the
    same about the payout engine). submission is (missing, timestamp), so a real, earlier
    timestamp always sorts ahead of a later one and both sort ahead of a missing one.
    """

    points: int
    wins: int
    submission: tuple[bool, dt.datetime | None]
    user_id: int


def _season_components(
    row: StandingRow, pool: Pool, submitted_at: dt.datetime | None
) -> _SeasonComponents:
    return _SeasonComponents(
        points=_points_sort_key(pool) * row.points,
        wins=-row.weekly_wins,
        submission=(submitted_at is None, submitted_at),
        user_id=row.user_id,
    )


def _season_sort_key(components: _SeasonComponents, ladder: str):
    """The one place both season ladders' tiebreak order is stated (Phase 2: "Write it as a
    single sort key function so the order is stated in one place and cannot drift between the
    two ladders"). "points": points first, then wins, then submission, then user id.
    "wins": the same four levels, wins and points simply swap first and second place. Either
    way user_id is the final level, so two distinct players are never fully tied: a strict,
    total order, every real rank in the result is therefore unique.
    """
    if ladder == "wins":
        return (components.wins, components.points, components.submission, components.user_id)
    return (components.points, components.wins, components.submission, components.user_id)


def _primary_component(components: _SeasonComponents, ladder: str):
    return components.wins if ladder == "wins" else components.points


def _secondary_component(components: _SeasonComponents, ladder: str):
    return components.points if ladder == "wins" else components.wins


def _tiebreak_reason(
    mine: tuple[StandingRow, _SeasonComponents],
    other: tuple[StandingRow, _SeasonComponents],
    ladder: str,
    final_week: Week | None,
) -> str:
    """A plain sentence naming whichever level of the chain actually separated these two
    players, compared from mine's own row (see _attach_tiebreak_reasons: mine and other are
    always adjacent in the final ranked order, sharing the ladder's primary metric). Checked
    in the same order the chain itself applies: secondary metric, then submission time, then
    user id, so the reason named is always the level that actually decided, never an earlier
    level both players already agreed on.
    """
    mine_row, mine_c = mine
    other_row, other_c = other
    if _secondary_component(mine_c, ladder) != _secondary_component(other_c, ladder):
        if ladder == "wins":
            return f"Tiebreak: {mine_row.points} points to {other_row.points}."
        return f"Tiebreak: {mine_row.weekly_wins} weekly wins to {other_row.weekly_wins}."

    if mine_c.submission != other_c.submission:
        week_number = final_week.week_number if final_week else None
        mine_missing, other_missing = mine_c.submission[0], other_c.submission[0]
        if mine_missing and not other_missing:
            return f"Tiebreak: did not submit week {week_number}."
        if other_missing and not mine_missing:
            return f"Tiebreak: submitted week {week_number}, the other player did not."
        # Neither is missing, so submission is a real (earlier_wins, timestamp) comparison:
        # only the earlier submitter's own row gets to say "first", the later one names the
        # other player instead, since both rows sharing the identical "submitted first"
        # sentence would be true for one of them and false for the other.
        if mine_c.submission[1] < other_c.submission[1]:
            return f"Tiebreak: submitted week {week_number} first."
        return f"Tiebreak: the other player submitted week {week_number} first."

    return "Tiebreak: entry order."


def _attach_tiebreak_reasons(
    ranked: list[tuple[StandingRow, _SeasonComponents]], ladder: str, final_week: Week | None
) -> None:
    """Walk the already fully ranked list and, for every consecutive run of rows sharing the
    ladder's primary metric (a real tie the chain had to break), set each row's
    tiebreak_reason by comparing it to its nearest neighbor in that run. A run of size one
    (nobody else shared the primary metric) is left with tiebreak_reason=None: no tiebreak
    was needed, nothing to explain.
    """
    n = len(ranked)
    i = 0
    while i < n:
        j = i
        primary = _primary_component(ranked[i][1], ladder)
        while j + 1 < n and _primary_component(ranked[j + 1][1], ladder) == primary:
            j += 1
        if j > i:
            for k in range(i, j + 1):
                neighbor = k - 1 if k > i else k + 1
                ranked[k][0].tiebreak_reason = _tiebreak_reason(
                    ranked[k], ranked[neighbor], ladder, final_week
                )
        i = j + 1


def _season_ranking(
    db: Session, pool: Pool, ladder: str, viewer_id: int | None
) -> list[StandingRow]:
    """Shared implementation for season_points_ranking/season_wins_ranking. When
    pool.season_tiebreak_mode is "split", this is just the old, unbroken-tie ranking (the
    exact behavior season_standings has always had for the points ladder; the wins ladder's
    "split" order is the wins-only mirror of it, sharing a rank on equal wins with no further
    tiebreak). When "wins" (the default), every rank in the result is unique: the tiebreak
    chain always resolves down to user_id, so a season place never splits any more.
    """
    rows = _season_base_rows(db, pool, viewer_id)

    if pool.season_tiebreak_mode != "wins":
        if ladder == "wins":
            rows.sort(key=lambda r: (-r.weekly_wins, r.display_name.lower()))
            _assign_ranks(rows, metric=lambda r: r.weekly_wins)
        else:
            sign = _points_sort_key(pool)
            rows.sort(
                key=lambda r: (sign * r.points, -r.correct, -r.weekly_wins, r.display_name.lower())
            )
            _assign_ranks(rows)
        return rows

    submitted_at_by_user, final_week = _season_submission_times(db, pool, rows)
    keyed = [
        (row, _season_components(row, pool, submitted_at_by_user.get(row.user_id))) for row in rows
    ]
    keyed.sort(key=lambda pair: _season_sort_key(pair[1], ladder))
    for index, (row, _components) in enumerate(keyed, start=1):
        row.rank = index
    _attach_tiebreak_reasons(keyed, ladder, final_week)
    return [row for row, _components in keyed]


def season_points_ranking(
    db: Session, pool: Pool, viewer_id: int | None = None
) -> list[StandingRow]:
    """The Season: Points ladder (SPEC.md Section 10b): ranked by season points first. Under
    pool.season_tiebreak_mode "wins" (the default), ties break outright through the full
    chain (weekly wins, then submission time, then user id) and tiebreak_reason explains any
    row a tiebreak actually decided; under "split", identical to season_standings.
    """
    return _season_ranking(db, pool, "points", viewer_id)


def season_wins_ranking(db: Session, pool: Pool, viewer_id: int | None = None) -> list[StandingRow]:
    """The Season: Wins ladder (SPEC.md Section 10b): ranked by total weekly wins first,
    always descending regardless of pool.scoring_mode (the same rule app/payouts.py's
    season_wins scope has always followed). Under "wins" mode ties break outright (season
    points, then submission time, then user id); under "split", a shared rank on equal wins
    with no further tiebreak, the mirror of season_standings for the points ladder.
    """
    return _season_ranking(db, pool, "wins", viewer_id)


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


def wins_entering_week(db: Session, pool: Pool, week: Week) -> dict[int, int]:
    """Each member's total weekly wins from every already-scored, non-test week of this
    pool's season with a week_number strictly lower than `week`'s own (Phase 3, weekly ties
    break on total wins). A member with no such row maps to 0, never a missing key.

    Deliberately excludes `week` itself: using week N's own win to decide a tie in week N is
    circular, the tie exists precisely because it is not yet known who won week N. A test
    week never contributes here, matching every other season-wide aggregate in this module
    (_season_base_rows filters the same way); this matters more than usual here, since a test
    week's own week_number is 0 and would otherwise silently count as "entering" week 1.
    Applies uniformly to the bowl week too (its week_number is simply the highest of the
    season, so this sums every regular week that preceded it): there is no separate bowl
    branch anywhere in this function.
    """
    members = _members(db, pool)
    counts = {m.id: 0 for m in members}
    entries = db.scalars(
        select(WeekEntry)
        .join(Week, Week.id == WeekEntry.week_id)
        .where(
            WeekEntry.pool_id == pool.id,
            Week.season_year == pool.season_year,
            Week.week_number < week.week_number,
            Week.is_test_week.is_(False),
            Week.status == "scored",
            WeekEntry.is_winner.is_(True),
        )
    )
    for entry in entries:
        counts[entry.user_id] = counts.get(entry.user_id, 0) + 1
    return counts


@dataclass(frozen=True)
class _WeeklyComponents:
    """One player's position on every level of the weekly tiebreak chain (Phase 3), each
    already oriented so a plain ascending comparison means "better." points is the pool's own
    scoring direction (see _points_sort_key). wins is -wins_entering_week, so more prior wins
    always sorts as better, mirroring _SeasonComponents' identical convention for the season
    wins level. submission is (missing, timestamp): a real, earlier submission for THIS week
    always sorts ahead of a later one, and both sort ahead of a missing one.
    """

    points: int
    wins: int
    submission: tuple[bool, dt.datetime | None]
    user_id: int


def _weekly_sort_key(components: _WeeklyComponents):
    """The one place the weekly (and, since a bowl week is just another week, bowl) tiebreak
    order is stated (Phase 3: "Write it as a single sort key function so weekly and bowl
    share one definition and cannot drift"): points, then prior wins, then this week's own
    submission time, then user id. user_id is the final level, so two distinct players are
    never fully tied: every real rank this produces is unique.
    """
    return (components.points, components.wins, components.submission, components.user_id)


def _weekly_tiebreak_reason(
    mine: tuple[WeeklyRow, _WeeklyComponents],
    other: tuple[WeeklyRow, _WeeklyComponents],
) -> str:
    """A plain sentence naming whichever level of the chain actually separated these two
    players, checked in the same order the chain itself applies (prior wins, then submission
    time, then user id) so the reason named is always the level that actually decided."""
    mine_row, mine_c = mine
    other_row, other_c = other
    if mine_c.wins != other_c.wins:
        mine_wins, other_wins = -mine_c.wins, -other_c.wins
        return f"Tiebreak: {mine_wins} prior {_wins_word(mine_wins)} to {other_wins}."

    if mine_c.submission != other_c.submission:
        mine_missing, other_missing = mine_c.submission[0], other_c.submission[0]
        if mine_missing and not other_missing:
            return "Tiebreak: did not submit."
        if other_missing and not mine_missing:
            return "Tiebreak: the other player did not submit."
        if mine_c.submission[1] < other_c.submission[1]:
            return "Tiebreak: submitted first."
        return "Tiebreak: the other player submitted first."

    return "Tiebreak: entry order."


def _wins_word(count: int) -> str:
    return "win" if count == 1 else "wins"


def _submission_key(entry: WeekEntry | None) -> tuple[bool, dt.datetime | None]:
    """(missing, timestamp) so a real, earlier submission always sorts ahead of a later one,
    and both sort ahead of a missing entry or a missing submitted_at."""
    submitted_at = entry.submitted_at if entry else None
    return (submitted_at is None, submitted_at)


def _attach_weekly_tiebreak_reasons(ranked: list[tuple[WeeklyRow, _WeeklyComponents]]) -> None:
    """Walk the already fully ranked list and, for every consecutive run of rows sharing the
    same points (a real tie the chain had to break), set each row's tiebreak_reason by
    comparing it to its nearest neighbor in that run. A run of size one is left with
    tiebreak_reason=None: no tiebreak was needed, nothing to explain. Mirrors
    _attach_tiebreak_reasons' identical shape for the season ladders."""
    n = len(ranked)
    i = 0
    while i < n:
        j = i
        points = ranked[i][1].points
        while j + 1 < n and ranked[j + 1][1].points == points:
            j += 1
        if j > i:
            for k in range(i, j + 1):
                neighbor = k - 1 if k > i else k + 1
                ranked[k][0].tiebreak_reason = _weekly_tiebreak_reason(ranked[k], ranked[neighbor])
        i = j + 1


def weekly_leaderboard(
    db: Session,
    pool: Pool,
    week: Week | None = None,
    viewer_id: int | None = None,
) -> tuple[list[WeeklyRow], Week | None]:
    """Leaderboard for one week. Defaults to the most recent scored or locked week.

    Under pool.weekly_tiebreak_mode "wins" (the default, Phase 3), a tie on points breaks
    outright through the full chain (prior wins entering this week, then this week's own
    submission time, then user id): every rank in the result is unique and tiebreak_reason
    explains any row a tiebreak actually decided. Under "split", identical to this function's
    pre-Phase-3 behavior: ties share a rank and app/payouts.py's own tie-splitting is what
    actually divides the pot, exactly as it always has for weekly and bowl.
    """
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
                did_not_submit=bool(entry.did_not_submit) if entry else True,
                is_winner=bool(entry and entry.is_winner),
                is_you=member.id == viewer_id,
            )
        )

    sign = _points_sort_key(pool)

    if pool.weekly_tiebreak_mode != "wins":
        rows.sort(key=lambda r: (sign * r.points, -r.correct, r.display_name.lower()))
        _assign_ranks(rows)
        return rows, week

    wins_by_user = wins_entering_week(db, pool, week)
    keyed = [
        (
            row,
            _WeeklyComponents(
                points=sign * row.points,
                wins=-wins_by_user.get(row.user_id, 0),
                submission=_submission_key(entries.get(row.user_id)),
                user_id=row.user_id,
            ),
        )
        for row in rows
    ]
    keyed.sort(key=lambda pair: _weekly_sort_key(pair[1]))
    for index, (row, _components) in enumerate(keyed, start=1):
        row.rank = index
    _attach_weekly_tiebreak_reasons(keyed)
    return [row for row, _components in keyed], week
