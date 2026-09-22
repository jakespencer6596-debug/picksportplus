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


def _lock_has_passed(week: Week) -> bool:
    """True once week.lock_at is in the past. A real published week always has lock_at set
    (computed at publish time, app/slate.py.compute_lock_at), so a missing lock_at only ever
    happens on a week that was never really published this way, for example a test fixture
    that does not care about lock timing at all: treated as "passed" (trust whatever is
    stored), never as "still open", since a real, live pre-lock week always carries a real
    lock_at to compare against.
    """
    lock_at = week.lock_at
    if lock_at is None:
        return True
    if lock_at.tzinfo is None:
        lock_at = lock_at.replace(tzinfo=dt.UTC)
    return dt.datetime.now(dt.UTC) >= lock_at


def _effective_points(entry: WeekEntry, week: Week) -> int:
    """Correct a phantom no-show penalty at read time, without touching the stored row (the
    120 point bug, standings and ties, September). A WeekEntry can still hold the old,
    incorrect maximum penalty for a week whose lock had not passed at the moment it was last
    scored, if nothing has rescored it since (score_week_for_pool itself no longer writes
    this penalty early, but a stale row from before that fix deployed can still be sitting in
    the database, this is what makes the display correct immediately on deploy without
    touching a row). Reads as 0 whenever the entry is flagged did_not_submit and the week's
    own lock_at is still in the future; every other entry, submitted or genuinely late, is
    read exactly as stored.
    """
    if not entry.did_not_submit:
        return entry.points
    return entry.points if _lock_has_passed(week) else 0


def _effective_did_not_submit(entry: WeekEntry, week: Week) -> bool:
    """The display twin of _effective_points: a phantom no-show read back as a live
    non-submitter (did_not_submit False) rather than the stored, premature True."""
    if not entry.did_not_submit:
        return False
    return _lock_has_passed(week)


def season_live_weeks(db: Session, pool: Pool) -> list[int]:
    """Week numbers, ascending, of every non test week of the current season that is still
    open or locked (not yet fully scored) but already has at least one WeekEntry row, meaning
    it is contributing a live, still moving figure to season totals right now (the 120 point
    bug, standings and ties, September: unfinished weeks count live as games finish, and the
    UI must label that rather than let a moving number look like a settled one). Empty when
    no such week exists, the ordinary case once every started week has finished scoring.
    """
    week_ids_with_entries = set(
        db.scalars(
            select(WeekEntry.week_id)
            .join(Week, Week.id == WeekEntry.week_id)
            .where(WeekEntry.pool_id == pool.id, Week.season_year == pool.season_year)
            .distinct()
        )
    )
    if not week_ids_with_entries:
        return []
    weeks = db.scalars(
        select(Week).where(
            Week.pool_id == pool.id,
            Week.season_year == pool.season_year,
            Week.is_test_week.is_(False),
            Week.status.in_(("open", "locked")),
            Week.id.in_(week_ids_with_entries),
        )
    )
    return sorted(w.week_number for w in weeks)


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
    entries_by_user: dict[int, list[tuple[WeekEntry, Week]]] = {m.id: [] for m in members}
    pairs = db.execute(
        select(WeekEntry, Week)
        .join(Week, Week.id == WeekEntry.week_id)
        .where(
            WeekEntry.pool_id == pool.id,
            Week.season_year == pool.season_year,
            Week.is_test_week.is_(False),
        )
    )
    for entry, week in pairs:
        entries_by_user.setdefault(entry.user_id, []).append((entry, week))

    rows: list[StandingRow] = []
    for member in members:
        totals = season_totals(
            SeasonEntryInput(
                points=_effective_points(e, w),
                correct=e.correct,
                possible=e.possible,
                is_winner=e.is_winner,
            )
            for e, w in entries_by_user.get(member.id, [])
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


@dataclass(frozen=True)
class _SeasonComponents:
    """One player's position on both levels of the league's season tie rule (standings and
    ties, September: points, then wins, then split, never submission time), each already
    oriented so a plain ascending comparison means "better." points is the pool's own scoring
    direction (see _points_sort_key); wins is always -weekly_wins, more wins is always
    better, regardless of scoring mode, since season_wins can never inherit the pool's
    scoring direction (SPEC.md Section 10b, app/payouts.py's own module docstring says the
    same about the payout engine).
    """

    points: int
    wins: int


def _season_components(row: StandingRow, pool: Pool) -> _SeasonComponents:
    return _SeasonComponents(points=_points_sort_key(pool) * row.points, wins=-row.weekly_wins)


def _season_sort_key(components: _SeasonComponents, ladder: str):
    """The one place both season ladders' tie order is stated (Phase 2, restated by standings
    and ties, September: "Write it as a single sort key function so the order is stated in
    one place and cannot drift between the two ladders"). "points": points first, then wins.
    "wins": the same two levels, wins and points simply swap first and second place. Two
    players still tied on both levels are never separated further: they share a rank and
    split the combined pot for the places they occupy (app/payouts.py's own allocate()).
    """
    if ladder == "wins":
        return (components.wins, components.points)
    return (components.points, components.wins)


def _primary_component(components: _SeasonComponents, ladder: str):
    return components.wins if ladder == "wins" else components.points


def _secondary_component(components: _SeasonComponents, ladder: str):
    return components.points if ladder == "wins" else components.wins


def _tiebreak_reason(
    mine: tuple[StandingRow, _SeasonComponents],
    other: tuple[StandingRow, _SeasonComponents],
    ladder: str,
) -> str:
    """A plain sentence naming whichever level actually separated these two players, compared
    from mine's own row (see _attach_tiebreak_reasons: mine and other are always adjacent in
    the final ranked order, sharing the ladder's primary metric). When the secondary metric
    also agrees, the two players are genuinely, fully tied: the league's rule (standings and
    ties, September) is that they split the pot for the places they share rather than being
    broken any further by submission time or by anything else, so the row says that plainly
    instead of naming a tiebreak that did not actually happen.
    """
    mine_row, mine_c = mine
    other_row, other_c = other
    if _secondary_component(mine_c, ladder) != _secondary_component(other_c, ladder):
        if ladder == "wins":
            return f"Tiebreak: {mine_row.points} points to {other_row.points}."
        return f"Tiebreak: {mine_row.weekly_wins} weekly wins to {other_row.weekly_wins}."
    return "Tied on points and wins, pot split."


def _attach_tiebreak_reasons(
    ranked: list[tuple[StandingRow, _SeasonComponents]], ladder: str
) -> None:
    """Walk the already fully ranked list and, for every consecutive run of rows sharing the
    ladder's primary metric (a real tie), set each row's tiebreak_reason by comparing it to
    its nearest neighbor in that run. A run of size one (nobody else shared the primary
    metric) is left with tiebreak_reason=None: no tie at all, nothing to explain.
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
                ranked[k][0].tiebreak_reason = _tiebreak_reason(ranked[k], ranked[neighbor], ladder)
        i = j + 1


def _rank_keyed_pairs(keyed: list[tuple], sort_key) -> None:
    """Competition ranking over an already-sorted list of (row, components) pairs: equal sort
    keys share a rank and the next distinct key skips ahead, exactly _assign_ranks' rule, just
    operating on the pair's components rather than the row itself (standings and ties,
    September: a season or weekly place can genuinely stay tied now that nothing breaks a tie
    past wins, so this can no longer assume every rank comes out unique the way the old
    submission-time chain always did).
    """
    last_value = None
    last_rank = 0
    for index, (row, components) in enumerate(keyed, start=1):
        value = sort_key(components)
        if last_value is not None and value == last_value:
            row.rank = last_rank
        else:
            row.rank = index
            last_rank = index
            last_value = value


def _season_ranking(
    db: Session, pool: Pool, ladder: str, viewer_id: int | None
) -> list[StandingRow]:
    """Shared implementation for season_points_ranking/season_wins_ranking: points, then
    wins, then split (standings and ties, September). This is the pool's one and only season
    tie rule now; pool.season_tiebreak_mode is no longer read here, it applies to every pool
    regardless of that stored setting (see DECISIONS.md, "Standings and ties, September").
    Two players still tied after both levels share a rank, exactly the shape
    app/payouts.py's own allocate() needs in order to split the combined payout for the
    places they occupy.
    """
    rows = _season_base_rows(db, pool, viewer_id)
    keyed = [(row, _season_components(row, pool)) for row in rows]
    keyed.sort(
        key=lambda pair: (
            *_season_sort_key(pair[1], ladder),
            pair[0].display_name.lower(),
            pair[0].user_id,
        )
    )
    _rank_keyed_pairs(keyed, lambda c: _season_sort_key(c, ladder))
    _attach_tiebreak_reasons(keyed, ladder)
    return [row for row, _components in keyed]


def season_points_ranking(
    db: Session, pool: Pool, viewer_id: int | None = None
) -> list[StandingRow]:
    """The Season: Points ladder (SPEC.md Section 10b): ranked by season points first, ties
    broken by total weekly wins, and a tie that survives even that shares a rank and splits
    the pot (standings and ties, September). tiebreak_reason explains any row a tie or a split
    actually decided.
    """
    return _season_ranking(db, pool, "points", viewer_id)


def season_wins_ranking(db: Session, pool: Pool, viewer_id: int | None = None) -> list[StandingRow]:
    """The Season: Wins ladder (SPEC.md Section 10b): ranked by total weekly wins first,
    always descending regardless of pool.scoring_mode (the same rule app/payouts.py's
    season_wins scope has always followed), ties broken by season points, and a tie that
    survives even that shares a rank and splits the pot (standings and ties, September).
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
    """One player's position on both levels of the weekly (and, since a bowl week is just
    another week, bowl) tie rule (standings and ties, September: points, then wins, then
    split), each already oriented so a plain ascending comparison means "better." points is
    the pool's own scoring direction (see _points_sort_key). wins is -wins_entering_week, so
    more prior wins always sorts as better, mirroring _SeasonComponents' identical convention
    for the season wins level.
    """

    points: int
    wins: int


def _weekly_sort_key(components: _WeeklyComponents):
    """The one place the weekly and bowl tie order is stated (Phase 3, restated by standings
    and ties, September: "Write it as a single sort key function so weekly and bowl share one
    definition and cannot drift"): points, then prior wins. Two players still tied on both are
    never separated further: they share a rank and split the combined pot for the places they
    occupy.
    """
    return (components.points, components.wins)


def _weekly_tiebreak_reason(
    mine: tuple[WeeklyRow, _WeeklyComponents],
    other: tuple[WeeklyRow, _WeeklyComponents],
) -> str:
    """A plain sentence naming whichever level actually separated these two players. When
    prior wins also agree, the two players are genuinely, fully tied: they split the pot for
    the places they share rather than being broken any further."""
    mine_row, mine_c = mine
    other_row, other_c = other
    if mine_c.wins != other_c.wins:
        mine_wins, other_wins = -mine_c.wins, -other_c.wins
        return f"Tiebreak: {mine_wins} prior {_wins_word(mine_wins)} to {other_wins}."
    return "Tied on points and wins, pot split."


def _wins_word(count: int) -> str:
    return "win" if count == 1 else "wins"


def _attach_weekly_tiebreak_reasons(ranked: list[tuple[WeeklyRow, _WeeklyComponents]]) -> None:
    """Walk the already fully ranked list and, for every consecutive run of rows sharing the
    same points (a real tie), set each row's tiebreak_reason by comparing it to its nearest
    neighbor in that run. A run of size one is left with tiebreak_reason=None: no tie at all,
    nothing to explain. Mirrors _attach_tiebreak_reasons' identical shape for the season
    ladders."""
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

    Ranked by points, ties broken by prior wins entering this week, and a tie that survives
    even that shares a rank and splits the combined pot for the places it spans (standings and
    ties, September): pool.weekly_tiebreak_mode is no longer read here, this is the rule for
    every pool. tiebreak_reason explains any row a tie or a split actually decided.
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
                points=_effective_points(entry, week) if entry else 0,
                correct=entry.correct if entry else 0,
                possible=entry.possible if entry else 0,
                submitted=bool(entry and entry.submitted_at),
                did_not_submit=_effective_did_not_submit(entry, week) if entry else True,
                is_winner=bool(entry and entry.is_winner),
                is_you=member.id == viewer_id,
            )
        )

    sign = _points_sort_key(pool)
    wins_by_user = wins_entering_week(db, pool, week)
    keyed = [
        (row, _WeeklyComponents(points=sign * row.points, wins=-wins_by_user.get(row.user_id, 0)))
        for row in rows
    ]
    keyed.sort(
        key=lambda pair: (*_weekly_sort_key(pair[1]), pair[0].display_name.lower(), pair[0].user_id)
    )
    _rank_keyed_pairs(keyed, _weekly_sort_key)
    _attach_weekly_tiebreak_reasons(keyed)
    return [row for row, _components in keyed], week
