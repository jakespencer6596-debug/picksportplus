"""seed-demo: a real completed week, loaded from recorded fixtures, with no network calls.

Everything here is genuine. The games, kickoff times, final scores and point spreads are the
real NFL and FBS week 5 of the 2025 season, recorded from ESPN and CollegeFootballData on
2026-08-02 and committed to tests/fixtures.

The only invented data is the players and their picks, which are generated from a fixed seed
so the demo is identical on every machine and every run.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.auth import hash_password
from app.models import Game, Pick, Pool, PoolMember, User, Week, WeekEntry, utcnow
from app.providers import cfbd, espn
from app.services.ingest import apply_slate, publish_week
from app.services.results import score_week_for_pool

FIXTURES = Path(__file__).resolve().parent.parent.parent / "tests" / "fixtures"

DEMO_POOL_NAME = "PickSportPlus Demo"
DEMO_JOIN_CODE = "DEMO2025"
DEMO_YEAR = 2025
DEMO_WEEK = 5
DEMO_PASSWORD = "demo-pass-2025"

# (display name, email local part, skill, role_in_pool). Skill is the chance of picking the
# real winner, so 0.74 is a sharp player and 0.52 is barely better than a coin flip.
#
# The first entry is the demo commissioner. Anyone opening the demo link can sign in as
# either that account to see the admin side, or as a player to see the ordinary side,
# without having to set anything up. Both use DEMO_PASSWORD.
DEMO_PLAYERS = [
    ("Riley Chen", "commissioner", 0.70, "commissioner"),
    ("Dana Whitfield", "player", 0.74, "member"),
    ("Marcus Reyes", "marcus", 0.68, "member"),
    ("Priya Raman", "priya", 0.64, "member"),
    ("Tom Bexley", "tom", 0.58, "member"),
    ("Casey Nolan", "casey", 0.52, "member"),
]

DEMO_COMMISSIONER_EMAIL = "commissioner@picksportplus.demo"
DEMO_PLAYER_EMAIL = "player@picksportplus.demo"


def _load(name: str):
    with open(FIXTURES / name, encoding="utf-8") as handle:
        return json.load(handle)


def seed_demo_pool(db: Session, reset: bool = False) -> list[str]:
    """Create the demo pool, its players, a real week, picks, and score it."""
    out: list[str] = []

    pool = db.scalar(select(Pool).where(Pool.join_code == DEMO_JOIN_CODE))
    if pool is not None and reset:
        db.delete(pool)
        db.flush()
        pool = None
        out.append("Removed the previous demo pool.")
    elif pool is not None:
        out.append(
            f"Demo pool already exists (join code {DEMO_JOIN_CODE}). "
            "Run with --reset to rebuild it."
        )
        return out

    pool = Pool(
        name=DEMO_POOL_NAME,
        join_code=DEMO_JOIN_CODE,
        season_year=DEMO_YEAR,
        num_games_per_week=20,
        target_nfl=8,
        target_ncaaf=12,
        sports=["nfl", "ncaaf"],
        auto_publish=True,
        open_registration=False,
        timezone="America/New_York",
        current_week=DEMO_WEEK,
    )
    db.add(pool)
    db.flush()
    out.append(f"Created pool {pool.name} with join code {pool.join_code}.")

    users = _ensure_players(db, pool, out)

    week = Week(
        pool_id=pool.id,
        season_year=DEMO_YEAR,
        week_number=DEMO_WEEK,
        label=f"Week {DEMO_WEEK}",
        status="draft",
    )
    db.add(week)
    db.flush()

    games = _load_real_games(db, week, out)
    if not games:
        out.append("No fixture games could be loaded. The demo was not built.")
        return out

    result = apply_slate(db, pool, week, now=None)
    out.append(
        f"Built the slate: {len(result.selected)} games "
        + ", ".join(f"{k} {v}" for k, v in sorted(result.per_league.items()))
        + "."
    )
    for note in result.notes:
        out.append(f"  note: {note}")

    publish_week(db, week)
    # The week is historical, so it is locked and scored the moment it is built.
    week.status = "locked"
    db.flush()

    _generate_picks(db, pool, week, users, out)

    report = score_week_for_pool(db, pool, week)
    out.append(report.summary())

    out.extend(demo_logins())
    return out


def demo_logins() -> list[str]:
    """The two accounts worth handing to anyone opening the demo."""
    return [
        "",
        "Demo logins:",
        f"  commissioner  {DEMO_COMMISSIONER_EMAIL}  password {DEMO_PASSWORD}",
        f"  player        {DEMO_PLAYER_EMAIL}  password {DEMO_PASSWORD}",
        "",
        "The other demo players use the same password:",
        *[
            f"  {local}@picksportplus.demo  ({name})"
            for name, local, _skill, _role in DEMO_PLAYERS
            if local not in ("commissioner", "player")
        ],
    ]


def _ensure_players(db: Session, pool: Pool, out: list[str]) -> list[User]:
    users: list[User] = []
    for name, local, _skill, role_in_pool in DEMO_PLAYERS:
        email = f"{local}@picksportplus.demo"
        user = db.scalar(select(User).where(User.email == email))
        if user is None:
            user = User(
                email=email,
                password_hash=hash_password(DEMO_PASSWORD),
                display_name=name,
                # A pool level commissioner, not a global admin. They run this pool and
                # nothing else, which is what a visitor should be shown.
                role="player",
            )
            db.add(user)
            db.flush()
        member = db.scalar(
            select(PoolMember).where(PoolMember.pool_id == pool.id, PoolMember.user_id == user.id)
        )
        if member is None:
            db.add(PoolMember(pool_id=pool.id, user_id=user.id, role_in_pool=role_in_pool))
        else:
            member.role_in_pool = role_in_pool
        users.append(user)
    db.flush()
    commissioners = sum(1 for p in DEMO_PLAYERS if p[3] == "commissioner")
    out.append(f"Added {len(users)} demo members ({commissioners} commissioner).")
    return users


def _load_real_games(db: Session, week: Week, out: list[str]) -> list[Game]:
    """Parse the recorded scoreboards and attach the recorded historical spreads."""
    parsed: list[espn.EspnGame] = []
    parsed += espn.parse_scoreboard(_load("espn_nfl_2025_w5.json"), "nfl")
    parsed += espn.parse_scoreboard(_load("espn_cfb_2025_w5.json"), "ncaaf")

    # NFL spreads come from the ESPN core API recording, which is the only ESPN surface that
    # keeps odds after a game has finished.
    core = _load("espn_core_odds_nfl_2025_w5.json")
    # College spreads come from CFBD, whose row id is the ESPN event id.
    college = cfbd.lines_by_event_id(cfbd.parse_lines(_load("cfbd_lines_2025_w5.json")))

    rows: list[Game] = []
    sources: dict[str, int] = {}
    for game in parsed:
        spread = None
        source = None
        if game.league == "nfl":
            payload = core.get(game.event_id)
            if payload:
                spread = espn.parse_core_odds(payload, game.home.abbr, game.away.abbr)
                source = "espn_core"
        else:
            line = college.get(game.event_id)
            if line is not None:
                spread = line.spread_home
                source = "cfbd"

        row = Game(
            week_id=week.id,
            league=game.league,
            espn_event_id=game.event_id,
            start_time=game.kickoff,
            home_team=game.home.name,
            away_team=game.away.name,
            home_abbr=game.home.abbr,
            away_abbr=game.away.abbr,
            home_record=game.home.record,
            away_record=game.away.record,
            canonical_home_key=game.home.canonical,
            canonical_away_key=game.away.canonical,
            spread_home=spread,
            closeness=abs(spread) if spread is not None else None,
            spread_source=source if spread is not None else None,
            status=game.status,
            home_score=game.home.score,
            away_score=game.away.score,
            winner=game.winner,
        )
        db.add(row)
        rows.append(row)
        if source and spread is not None:
            sources[source] = sources.get(source, 0) + 1

    db.flush()
    with_spread = sum(1 for r in rows if r.spread_home is not None)
    out.append(
        f"Loaded {len(rows)} real games from the {DEMO_YEAR} week {DEMO_WEEK} recordings, "
        f"{with_spread} with a real closing line "
        + "("
        + ", ".join(f"{k} {v}" for k, v in sorted(sources.items()))
        + ")."
    )
    return rows


def _generate_picks(db: Session, pool: Pool, week: Week, users: list[User], out: list[str]) -> None:
    """Deterministic picks. Each player has a skill level and stakes more on closer calls."""
    slate = list(
        db.scalars(
            select(Game)
            .where(Game.week_id == week.id, Game.in_slate.is_(True))
            .order_by(Game.slate_rank)
        )
    )
    n = len(slate)

    for user, (_name, local, skill, _role) in zip(users, DEMO_PLAYERS, strict=False):
        rng = random.Random(f"{local}-{DEMO_YEAR}-{DEMO_WEEK}")

        # Pick a side per game, weighted towards the team that actually won.
        choices: list[tuple[Game, str, float]] = []
        for game in slate:
            truth = game.winner if game.winner in ("home", "away") else "home"
            other = "away" if truth == "home" else "home"
            side = truth if rng.random() < skill else other
            # Conviction drives the ranking: a bigger spread feels like an easier call.
            conviction = (game.closeness or 0.0) + rng.uniform(0, 3.5)
            choices.append((game, side, conviction))

        # Highest conviction stakes the most points, so confidence is a clean 1..N permutation.
        choices.sort(key=lambda item: -item[2])
        for index, (game, side, _conviction) in enumerate(choices):
            db.add(
                Pick(
                    user_id=user.id,
                    pool_id=pool.id,
                    week_id=week.id,
                    game_id=game.id,
                    picked_team=side,
                    confidence=n - index,
                )
            )

        db.add(
            WeekEntry(
                user_id=user.id,
                pool_id=pool.id,
                week_id=week.id,
                submitted_at=utcnow(),
            )
        )

    db.flush()
    total = db.scalar(select(func.count(Pick.id)).where(Pick.week_id == week.id)) or 0
    out.append(f"Generated {total} picks across {len(users)} players ({n} games each).")


def clear_demo(db: Session) -> None:
    """Remove the demo pool and everything that hangs off it."""
    pool = db.scalar(select(Pool).where(Pool.join_code == DEMO_JOIN_CODE))
    if pool is None:
        return
    db.delete(pool)
    db.flush()
    emails = [f"{local}@picksportplus.demo" for _n, local, _s, _r in DEMO_PLAYERS]
    db.execute(delete(User).where(User.email.in_(emails)))
    db.flush()
