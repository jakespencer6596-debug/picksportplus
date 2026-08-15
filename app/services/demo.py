"""seed-demo: a real three-week demo, loaded from recorded fixtures, with no network calls.

Everything here is genuine. The games, kickoff times, final scores and point spreads are the
real NFL and FBS weeks 5 and 6 of the 2025 season, recorded from ESPN on 2026-08-02 (week 5)
and 2026-08-08 (week 6) and committed to tests/fixtures. The only invented data is the eight
players and their picks, generated from a fixed seed so the demo is identical on every machine
and every run, and the payout dollar figures, which are clearly labelled as demo amounts.

Three weeks:

  Week 5   Fully scored. Real NFL and college week 5 of 2025. One game is voided after the
           fact (a real commissioner action, applied to a real completed game purely to make
           the void scoring rule visible on screen) and one player submits no picks at all
           (the no-show rule).
  Week 6   Fully scored by default. Real NFL and college week 6 of 2025. Pass
           scenario_week=True to instead leave it partially played (some games final, the
           rest reverted to pending) so Phase 8's Scenarios panel has a real week to open
           against. See _build_partial_week and DECISIONS.md, Phase 9.
  Week 7   Open, no picks submitted, published from a reused real historical slate (week 6's
           games again) with an artificially future lock_at so a tester can walk the live
           pick flow between now and the real launch. See _build_open_week and DECISIONS.md.

Every player picks exactly pool.picks_required of the published slate, not the whole slate
(Phase 3's rule), and which games each one sits out is varied per player, deterministically,
so the demo does not show everyone picking an identical subset.
"""

from __future__ import annotations

import datetime as dt
import json
import random
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.auth import hash_password
from app.models import Game, PayoutRule, Pick, Pool, PoolMember, User, Week, WeekEntry, utcnow
from app.providers import cfbd, espn
from app.services import payouts as payout_service
from app.services.calendar import default_week1_anchor_date
from app.services.ingest import apply_slate, set_void
from app.services.results import score_week_for_pool

FIXTURES = Path(__file__).resolve().parent.parent.parent / "tests" / "fixtures"

DEMO_POOL_NAME = "PickSportPlus Demo"
DEMO_JOIN_CODE = "DEMO2025"
DEMO_YEAR = 2025
DEMO_PASSWORD = "demo-pass-2025"

# (display name, email local part, skill, role_in_pool). Skill is the chance of picking the
# real winner, so 0.74 is a sharp player and 0.52 is barely better than a coin flip.
#
# The first entry is the demo commissioner. Anyone opening the demo link can sign in as
# either that account to see the admin side, or as a player to see the ordinary side,
# without having to set anything up. Both use DEMO_PASSWORD. Eight players, up from six in
# the first version of this demo, so a full 15-of-20 slate has real variety in who picks what.
DEMO_PLAYERS = [
    ("Riley Chen", "commissioner", 0.70, "commissioner"),
    ("Dana Whitfield", "player", 0.74, "member"),
    ("Marcus Reyes", "marcus", 0.68, "member"),
    ("Priya Raman", "priya", 0.64, "member"),
    ("Tom Bexley", "tom", 0.58, "member"),
    ("Casey Nolan", "casey", 0.52, "member"),
    ("Jordan Ellis", "jordan", 0.61, "member"),
    ("Sam Okafor", "sam", 0.55, "member"),
]

DEMO_COMMISSIONER_EMAIL = "commissioner@picksportplus.demo"
DEMO_PLAYER_EMAIL = "player@picksportplus.demo"

# Sits out week 5 entirely, so the no-show rule (max penalty, "No picks submitted") is
# visible on screen for the demo, not just proven in a test.
NO_SHOW_WEEK1_LOCAL = "casey"


@dataclass(frozen=True)
class WeekSpec:
    """Which recorded fixtures back one demo week, and how its spreads were resolved."""

    week_number: int
    label: str
    nfl_scoreboard: str
    cfb_scoreboard: str
    nfl_core_odds: str
    cfb_spread_source: str  # "cfbd" or "espn_core"
    cfb_odds_fixture: str


WEEK1 = WeekSpec(
    week_number=5,
    label="Week 5",
    nfl_scoreboard="espn_nfl_2025_w5.json",
    cfb_scoreboard="espn_cfb_2025_w5.json",
    nfl_core_odds="espn_core_odds_nfl_2025_w5.json",
    cfb_spread_source="cfbd",
    cfb_odds_fixture="cfbd_lines_2025_w5.json",
)

# Week 6 spreads, both leagues, come from ESPN's own core odds endpoint (unmetered, keyless),
# not CFBD. The build machine that captured this fixture had no CFBD_API_KEY configured, and
# a live check showed ESPN's core odds carried a real, resolvable spread for every one of
# week 6's recorded college games too, so there was no need to reach for CFBD at all here.
# See DECISIONS.md, Phase 9.
WEEK2 = WeekSpec(
    week_number=6,
    label="Week 6",
    nfl_scoreboard="espn_nfl_2025_w6.json",
    cfb_scoreboard="espn_cfb_2025_w6.json",
    nfl_core_odds="espn_core_odds_nfl_2025_w6.json",
    cfb_spread_source="espn_core",
    cfb_odds_fixture="espn_core_odds_cfb_2025_w6.json",
)

OPEN_WEEK_NUMBER = 7
OPEN_WEEK_LABEL = "Week 7"
# How many days ahead of "now" (whenever seed-demo actually runs) the open week's lock_at is
# set. Real kickoff times on its (reused, historical) games are in the past; lock_at is the
# only clock week_is_locked() actually reads, so this is what lets a tester walk the live
# pick flow before the real launch without pretending a game is about to happen. See
# DECISIONS.md, Phase 9.
OPEN_WEEK_LOCK_DAYS_AHEAD = 7

# Demo payout figures. Clearly labelled as demo amounts everywhere they render; no real money
# changes hands in this pool. See DECISIONS.md, "Payout system", and app/models.py's
# PayoutRule docstring for why the real production pool ships with none of these at all.
# The ladder mirrors the commissioner's own real structure (Payout system rebuild brief) so
# the demo shows something recognisable: weekly 105/55/25, bowl 250/100/50, season points
# 600/405/150, season wins 325/185/110. render.yaml's free demo service re-seeds this on
# every restart (ephemeral disk), which is exactly why this seed must not be skipped: see
# README.md's Render section.
#
# The fee is set so the computed pot (entry_fee times DEMO_PLAYERS' 8 paid members) lands
# exactly on the real ladder's own $4,950 grand total: 618.75 * 8 = 4950.00. This is what
# makes the demo's own Set Payouts screen show "$0 unallocated" out of the box, a much better
# first look at the balance validator than an arbitrary mismatch would be.
DEMO_ENTRY_FEE = Decimal("618.75")
DEMO_VENMO_HANDLE = "picksportplus-demo"
DEMO_PAYMENT_NOTE = (
    "Demo pool: no real money changes hands. This entry fee and Venmo handle exist only to "
    "demonstrate the payment gate and the payout column."
)
DEMO_WEEKLY_PAYOUTS = {1: Decimal("105"), 2: Decimal("55"), 3: Decimal("25")}
DEMO_BOWL_PAYOUTS = {1: Decimal("250"), 2: Decimal("100"), 3: Decimal("50")}
DEMO_SEASON_POINTS_PAYOUTS = {1: Decimal("600"), 2: Decimal("405"), 3: Decimal("150")}
DEMO_SEASON_WINS_PAYOUTS = {1: Decimal("325"), 2: Decimal("185"), 3: Decimal("110")}


def _load(name: str):
    with open(FIXTURES / name, encoding="utf-8") as handle:
        return json.load(handle)


def seed_demo_pool(db: Session, reset: bool = False, scenario_week: bool = False) -> list[str]:
    """Create the demo pool, its players, three real weeks, picks, and score what's scored.

    scenario_week: when True, week 6 is left partially played (see _build_partial_week)
    instead of fully scored, trading that week's own completeness for a real week the
    Scenarios panel has something to open against. Off by default, so a plain `seed-demo`
    still gives both historical weeks full standings and payouts.
    """
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
        current_week=OPEN_WEEK_NUMBER,
        # Required since Phase 2 remediation (see DECISIONS.md): build_slate refuses outright
        # without one. The demo's own three weeks are seeded through apply_slate directly, not
        # build_slate, so they never depended on this, but a commissioner clicking "Build the
        # slate" for a new week on the demo pool would otherwise always hit that refusal. The
        # exact date does not need to line up with the demo's replayed 2025 fixtures, since
        # OFFLINE_MODE blocks any real ESPN call the button would make anyway; it only needs
        # to be a real Saturday so the guard, and the Saturday-only validation on the settings
        # form, both pass.
        week1_anchor_date=default_week1_anchor_date(DEMO_YEAR),
        entry_fee=DEMO_ENTRY_FEE,
        venmo_handle=DEMO_VENMO_HANDLE,
        payment_note=DEMO_PAYMENT_NOTE,
    )
    db.add(pool)
    db.flush()
    out.append(f"Created pool {pool.name} with join code {pool.join_code}.")

    users = _ensure_players(db, pool, out)
    _mark_everyone_paid(db, pool, users, out)
    _seed_payout_rules(db, pool, out)

    _build_scored_week(
        db, pool, users, WEEK1, out, no_show_local=NO_SHOW_WEEK1_LOCAL, void_one_game=True
    )
    if scenario_week:
        _build_partial_week(db, pool, users, WEEK2, out)
    else:
        _build_scored_week(db, pool, users, WEEK2, out, no_show_local=None, void_one_game=False)

    _build_open_week(db, pool, users, WEEK2, out)

    # Neither demo week is a bowl week, so score_week_for_pool's own automatic season-scope
    # snapshot (Phase 3: triggered by a bowl week finishing scoring) never fires here on its
    # own. Snapshot both season scopes by hand instead, once, after the real scored weeks
    # above, so the demo's Season standings page shows both award panels rather than an empty
    # state. Safe to call even under scenario_week=True, where week 6 is left partially
    # played: season_points/season_wins still resolve against whatever week 5 alone already
    # contributed.
    payout_service.snapshot_awards(db, pool, "season_points")
    payout_service.snapshot_awards(db, pool, "season_wins")
    out.append("Snapshotted season points and season wins payout awards for the demo pool.")

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


def _mark_everyone_paid(db: Session, pool: Pool, users: list[User], out: list[str]) -> None:
    """Every demo member is marked paid, by the demo commissioner, so the Venmo gate never
    blocks a walkthrough of the demo pool itself. The gate's blocking behavior is proven
    directly against a fresh, non-demo pool instead, see tests/test_app.py."""
    commissioner = next(
        (
            u
            for u, (_n, _l, _s, role) in zip(users, DEMO_PLAYERS, strict=False)
            if role == "commissioner"
        ),
        users[0],
    )
    members = list(db.scalars(select(PoolMember).where(PoolMember.pool_id == pool.id)))
    now = utcnow()
    for member in members:
        member.paid_at = now
        member.paid_marked_by_user_id = commissioner.id
    db.flush()
    out.append(f"Marked all {len(members)} demo members paid (a demo figure, no real money).")


def _seed_payout_rules(db: Session, pool: Pool, out: list[str]) -> None:
    """A real, clearly-labelled-as-demo payout structure, so the payout column and the season
    award panels have something to show. The real production pool ships with zero rows here;
    see app/models.py's PayoutRule docstring and DECISIONS.md, "Payout system"."""
    scopes = (
        ("weekly", DEMO_WEEKLY_PAYOUTS),
        ("bowl", DEMO_BOWL_PAYOUTS),
        ("season_points", DEMO_SEASON_POINTS_PAYOUTS),
        ("season_wins", DEMO_SEASON_WINS_PAYOUTS),
    )
    ordinal_labels = {1: "1st place", 2: "2nd place", 3: "3rd place"}
    rules = [
        PayoutRule(
            pool_id=pool.id,
            scope=scope,
            place=place,
            mode="amount",
            value=amount,
            label=f"{ordinal_labels[place]} (demo)",
        )
        for scope, amounts in scopes
        for place, amount in amounts.items()
    ]
    db.add_all(rules)
    pool.weekly_payout_weeks = 15
    db.flush()
    out.append(
        "Seeded demo payout rules: weekly, bowl, season points, and season wins, "
        "all demo figures."
    )


def _load_real_games(
    db: Session, week: Week, spec: WeekSpec, out: list[str], *, unplayed: bool = False
) -> list[Game]:
    """Parse the recorded scoreboards and attach the recorded historical spreads.

    unplayed=True is for the open demo week (_build_open_week): the underlying recording is
    a real, already finished game, but the demo is supposed to look like a week nobody has
    played yet. Without this, the picks page shows real historical win/loss coloring on
    every team button before a demo player has picked anything, which reads as a bug, not a
    feature. Status, scores and the winner are reset here; everything else (matchup, spread,
    kickoff time) stays the real recording.
    """
    parsed: list[espn.EspnGame] = []
    parsed += espn.parse_scoreboard(_load(spec.nfl_scoreboard), "nfl")
    parsed += espn.parse_scoreboard(_load(spec.cfb_scoreboard), "ncaaf")

    # NFL spreads always come from the ESPN core API recording, the only ESPN surface that
    # keeps odds after a game has finished.
    nfl_core = _load(spec.nfl_core_odds)
    college_lines: dict[str, cfbd.CfbdLine] = {}
    college_core: dict = {}
    if spec.cfb_spread_source == "cfbd":
        college_lines = cfbd.lines_by_event_id(cfbd.parse_lines(_load(spec.cfb_odds_fixture)))
    else:
        college_core = _load(spec.cfb_odds_fixture)

    rows: list[Game] = []
    sources: dict[str, int] = {}
    for game in parsed:
        spread = None
        source = None
        if game.league == "nfl":
            payload = nfl_core.get(game.event_id)
            if payload:
                spread = espn.parse_core_odds(payload, game.home.abbr, game.away.abbr)
                source = "espn_core"
        elif spec.cfb_spread_source == "cfbd":
            line = college_lines.get(game.event_id)
            if line is not None:
                spread = line.spread_home
                source = "cfbd"
        else:
            payload = college_core.get(game.event_id)
            if payload:
                spread = espn.parse_core_odds(payload, game.home.abbr, game.away.abbr)
                source = "espn_core"

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
            status="scheduled" if unplayed else game.status,
            home_score=None if unplayed else game.home.score,
            away_score=None if unplayed else game.away.score,
            winner=None if unplayed else game.winner,
        )
        db.add(row)
        rows.append(row)
        if source and spread is not None:
            sources[source] = sources.get(source, 0) + 1

    db.flush()
    with_spread = sum(1 for r in rows if r.spread_home is not None)
    out.append(
        f"Loaded {len(rows)} real games from the {DEMO_YEAR} {spec.label} recordings, "
        f"{with_spread} with a real closing line "
        + "("
        + ", ".join(f"{k} {v}" for k, v in sorted(sources.items()))
        + ")."
    )
    return rows


def _generate_picks(
    db: Session,
    pool: Pool,
    week: Week,
    users: list[User],
    out: list[str],
    *,
    no_show_local: str | None,
) -> None:
    """Deterministic picks. Each player has a skill level and stakes more on closer calls.

    Every player picks exactly pool.picks_required of the published slate, chosen as a
    varied, deterministic subset per player (Phase 3: a player is never required to cover the
    whole slate). no_show_local, when set, skips generating any picks at all for that one
    player, so score_week_for_pool applies the real no-show rule to them.
    """
    slate = list(
        db.scalars(
            select(Game)
            .where(Game.week_id == week.id, Game.in_slate.is_(True))
            .order_by(Game.slate_rank)
        )
    )
    n = len(slate)
    required = min(pool.picks_required, n)

    entries = 0
    no_show_name = None
    for user, (name, local, skill, _role) in zip(users, DEMO_PLAYERS, strict=False):
        if local == no_show_local:
            no_show_name = name
            continue

        rng = random.Random(f"{local}-{DEMO_YEAR}-{week.week_number}")
        chosen = slate if required >= n else rng.sample(slate, required)

        # Pick a side per game, weighted towards the team that actually won.
        choices: list[tuple[Game, str, float]] = []
        for game in chosen:
            truth = game.winner if game.winner in ("home", "away") else "home"
            other = "away" if truth == "home" else "home"
            side = truth if rng.random() < skill else other
            # Conviction drives the ranking: a bigger spread feels like an easier call.
            conviction = (game.closeness or 0.0) + rng.uniform(0, 3.5)
            choices.append((game, side, conviction))

        # Highest conviction stakes the most points, so confidence is a clean 1..required
        # permutation, assigned only to the games this player actually picked.
        choices.sort(key=lambda item: -item[2])
        for index, (game, side, _conviction) in enumerate(choices):
            db.add(
                Pick(
                    user_id=user.id,
                    pool_id=pool.id,
                    week_id=week.id,
                    game_id=game.id,
                    picked_team=side,
                    confidence=len(choices) - index,
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
        entries += 1

    db.flush()
    total = db.scalar(select(func.count(Pick.id)).where(Pick.week_id == week.id)) or 0
    message = f"Generated {total} picks across {entries} players ({required} of {n} games each)."
    if no_show_name:
        message += f" {no_show_name} submitted no picks at all."
    out.append(message)


def _build_scored_week(
    db: Session,
    pool: Pool,
    users: list[User],
    spec: WeekSpec,
    out: list[str],
    *,
    no_show_local: str | None,
    void_one_game: bool,
) -> None:
    """A complete, historical, fully scored week: build, publish, pick, score."""
    week = Week(
        pool_id=pool.id,
        season_year=DEMO_YEAR,
        week_number=spec.week_number,
        label=spec.label,
        status="draft",
    )
    db.add(week)
    db.flush()

    games = _load_real_games(db, week, spec, out)
    if not games:
        out.append(f"No fixture games could be loaded for {spec.label}. That week was not built.")
        return

    result = apply_slate(db, pool, week, now=None)
    out.append(
        f"{spec.label}: built the slate, {len(result.selected)} games "
        + ", ".join(f"{k} {v}" for k, v in sorted(result.per_league.items()))
        + "."
    )
    for note in result.notes:
        out.append(f"  note: {note}")

    # Not ingest.publish_week: that now refuses a slate spanning more than 8 days (Phase 2
    # remediation, see DECISIONS.md), a real safety net for a live commissioner's Publish
    # click. The demo's own real historical fixtures deliberately mix an NFL week and a
    # college week that are only stand-ins for "some games", not a real matched calendar
    # week (see NFL_SOME_GAMES/CFB_SOME_GAMES in tests/test_ingest.py), so they can genuinely
    # span more than 8 days without that being a bug here. The week is historical anyway, so
    # it is locked and scored the moment it is built.
    week.published_at = utcnow()
    week.status = "locked"
    db.flush()

    _generate_picks(db, pool, week, users, out, no_show_local=no_show_local)

    if void_one_game:
        slate = list(
            db.scalars(
                select(Game)
                .where(Game.week_id == week.id, Game.in_slate.is_(True))
                .order_by(Game.slate_rank)
            )
        )
        target = slate[min(4, len(slate) - 1)]
        set_void(db, week, target.id, True)
        out.append(
            f"  Voided {target.away_abbr} at {target.home_abbr} for the demo: the exact same "
            "commissioner action available on any real week, applied here to a real, "
            "already-final game purely so the void scoring rule is visible on screen, not "
            "just in tests. See DECISIONS.md, Phase 9."
        )

    report = score_week_for_pool(db, pool, week)
    out.append(f"{spec.label}: {report.summary()}")


def _build_partial_week(
    db: Session, pool: Pool, users: list[User], spec: WeekSpec, out: list[str]
) -> None:
    """Same real week as _build_scored_week, but left mid-play: pool.scenarios_min_final_games
    games stay final, the rest are reverted to pending, so Phase 8's Scenarios panel has a
    real week to open against (its own gate is final_count >= scenarios_min_final_games and
    remaining_count >= scenarios_min_remaining_games, read from the pool, never hard coded).
    Picks are still generated for everyone; the week itself is never marked scored, since a
    week with games still pending is by definition not complete. See DECISIONS.md, Phase 9.
    """
    week = Week(
        pool_id=pool.id,
        season_year=DEMO_YEAR,
        week_number=spec.week_number,
        label=spec.label,
        status="draft",
    )
    db.add(week)
    db.flush()

    games = _load_real_games(db, week, spec, out)
    if not games:
        out.append(f"No fixture games could be loaded for {spec.label}. That week was not built.")
        return

    result = apply_slate(db, pool, week, now=None)
    out.append(
        f"{spec.label} (scenario week): built the slate, {len(result.selected)} games "
        + ", ".join(f"{k} {v}" for k, v in sorted(result.per_league.items()))
        + "."
    )

    # See the matching comment in _build_scored_week: not ingest.publish_week, since this
    # demo week's stand-in fixtures can genuinely span more than the real 8 day publish limit.
    week.published_at = utcnow()
    week.status = "locked"
    db.flush()

    _generate_picks(db, pool, week, users, out, no_show_local=None)

    slate = list(
        db.scalars(
            select(Game)
            .where(Game.week_id == week.id, Game.in_slate.is_(True))
            .order_by(Game.slate_rank)
        )
    )
    keep_final = min(pool.scenarios_min_final_games, len(slate))
    reverted = 0
    for game in slate[keep_final:]:
        game.status = "scheduled"
        game.home_score = None
        game.away_score = None
        game.winner = None
        reverted += 1
    db.flush()
    out.append(
        f"  Scenario week: kept {keep_final} games final, reverted {reverted} back to "
        "pending, so the Scenarios panel opens against a real, partially played week."
    )

    report = score_week_for_pool(db, pool, week)
    out.append(f"{spec.label}: {report.summary()}")


def _build_open_week(
    db: Session, pool: Pool, users: list[User], spec: WeekSpec, out: list[str]
) -> None:
    """The current, open week. Real teams and a real historical slate (spec's games, reused),
    no picks, and an artificially future lock_at (see OPEN_WEEK_LOCK_DAYS_AHEAD) so a tester
    can walk the live selection flow before the real launch. Documented in DECISIONS.md,
    Phase 9, so nobody mistakes the reused historical kickoff times for a bug: lock_at is the
    only clock week_is_locked() reads, not any one game's own kickoff.
    """
    del users  # no picks are generated for the open week, on purpose
    week = Week(
        pool_id=pool.id,
        season_year=DEMO_YEAR,
        week_number=OPEN_WEEK_NUMBER,
        label=OPEN_WEEK_LABEL,
        status="draft",
    )
    db.add(week)
    db.flush()

    _load_real_games(db, week, spec, out, unplayed=True)
    apply_slate(db, pool, week, now=None)
    # See the matching comment in _build_scored_week: not ingest.publish_week, since this
    # demo week's stand-in fixtures can genuinely span more than the real 8 day publish limit.
    week.status = "open"
    week.published_at = utcnow()

    week.lock_at = utcnow() + dt.timedelta(days=OPEN_WEEK_LOCK_DAYS_AHEAD)
    week.lock_at_override = True
    db.flush()

    out.append(
        f"{OPEN_WEEK_LABEL} is open with no picks submitted, locking in "
        f"{OPEN_WEEK_LOCK_DAYS_AHEAD} days (an artificial future lock time for the demo; the "
        "games themselves are a reused real historical slate, see DECISIONS.md)."
    )


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
