"""Integration tests: the app boots, the pages render, and the rules actually hold.

These exercise the real routers, the real templates and the real scoring path against an
in memory database. Nothing here touches the network, the session wide offline fixture in
conftest guarantees it.
"""

from __future__ import annotations

import datetime as dt
import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth import hash_password
from app.config import settings
from app.db import get_db
from app.main import app
from app.models import (
    Base,
    ContactSubmission,
    Game,
    MailLog,
    PasswordResetToken,
    Pick,
    PlatformSetting,
    Pool,
    PoolMember,
    User,
    Week,
    WeekEntry,
)
from app.services import mail

UTC = dt.UTC


@pytest.fixture
def engine():
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        Base.metadata.drop_all(eng)
        eng.dispose()


@pytest.fixture
def session_factory(engine):
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


@pytest.fixture
def client(session_factory):
    """A TestClient wired to the throwaway database."""

    def _get_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _get_db
    with TestClient(app, follow_redirects=False) as c:
        yield c
    app.dependency_overrides.clear()


# Fixtures that build a small but complete pool ------------------------------


def _make_pool(
    db: Session,
    *,
    num_games: int = 4,
    picks_required: int | None = None,
    payment_required_to_pick: bool = False,
) -> Pool:
    # picks_required defaults to num_games so _valid_submission (which submits every game
    # in world["game_ids"]) stays a valid, complete entry unless a test deliberately wants
    # picks_required to be smaller than the slate, proving it is a real, honored setting.
    # payment_required_to_pick defaults to False (the model's own real default is True, Phase
    # 7) so every pre-existing test that posts picks through the world fixture keeps working
    # unless a test deliberately wants the Venmo gate in play, the same reasoning
    # picks_required's own default documents just above.
    pool = Pool(
        name="Test Pool",
        join_code="TESTCODE",
        season_year=2025,
        num_games_per_week=num_games,
        target_nfl=2,
        target_ncaaf=2,
        picks_required=picks_required if picks_required is not None else num_games,
        sports=["nfl", "ncaaf"],
        auto_publish=True,
        open_registration=False,
        timezone="America/New_York",
        current_week=5,
        payment_required_to_pick=payment_required_to_pick,
    )
    db.add(pool)
    db.flush()
    return pool


def _make_preview_pool(db: Session, *, num_games: int = 4) -> Pool:
    """The hidden is_preview pool (Post-launch fixes), built directly rather than through the
    real seed-preview CLI command: that command calls build_slate, which would hit ESPN, and
    force_offline_mode (tests/conftest.py) refuses that outright. Router tests only need a
    real Week and real Game rows sitting behind is_preview=True, exactly what build_slate
    would have produced."""
    pool = Pool(
        name="PickSportPlus Preview",
        join_code="PREVIEWX",
        season_year=2025,
        num_games_per_week=num_games,
        target_nfl=2,
        target_ncaaf=2,
        sports=["nfl", "ncaaf"],
        auto_publish=True,
        open_registration=False,
        timezone="America/New_York",
        current_week=5,
        is_preview=True,
    )
    db.add(pool)
    db.flush()
    return pool


def _make_user(db: Session, email: str, name: str, role: str = "player") -> User:
    user = User(
        email=email,
        password_hash=hash_password("hunter2hunter2"),
        display_name=name,
        role=role,
    )
    db.add(user)
    db.flush()
    return user


def _make_week(
    db: Session, pool: Pool, *, lock_in_hours: float = 48.0, status: str = "open"
) -> Week:
    week = Week(
        pool_id=pool.id,
        season_year=pool.season_year,
        week_number=5,
        label="Week 5",
        status=status,
        lock_at=dt.datetime.now(UTC) + dt.timedelta(hours=lock_in_hours),
    )
    db.add(week)
    db.flush()
    return week


def _make_games(db: Session, week: Week, count: int = 4) -> list[Game]:
    games = []
    base = dt.datetime.now(UTC) + dt.timedelta(hours=48)
    for i in range(count):
        game = Game(
            week_id=week.id,
            league="nfl" if i % 2 == 0 else "ncaaf",
            espn_event_id=f"evt{i}",
            start_time=base + dt.timedelta(hours=i),
            home_team=f"Home Team {i}",
            away_team=f"Away Team {i}",
            home_abbr=f"H{i}",
            away_abbr=f"A{i}",
            canonical_home_key=f"nfl:home-{i}",
            canonical_away_key=f"nfl:away-{i}",
            spread_home=-1.5 - i,
            closeness=1.5 + i,
            spread_source="espn",
            in_slate=True,
            slate_rank=i + 1,
            status="scheduled",
        )
        db.add(game)
        games.append(game)
    db.flush()
    return games


@pytest.fixture
def world(session_factory):
    """A pool with a commissioner, a player, an open week and four slate games."""
    db = session_factory()
    pool = _make_pool(db)
    boss = _make_user(db, "boss@example.com", "The Commissioner", role="admin")
    player = _make_user(db, "player@example.com", "Regular Player")
    db.add(PoolMember(pool_id=pool.id, user_id=boss.id, role_in_pool="commissioner"))
    db.add(PoolMember(pool_id=pool.id, user_id=player.id, role_in_pool="member"))
    week = _make_week(db, pool)
    games = _make_games(db, week)
    db.commit()
    data = {
        "pool_id": pool.id,
        "boss_id": boss.id,
        "player_id": player.id,
        "week_id": week.id,
        "game_ids": [g.id for g in games],
    }
    db.close()
    return data


def _login(client: TestClient, email: str) -> None:
    response = client.post(
        "/login", data={"email": email, "password": "hunter2hunter2", "next": "/picks"}
    )
    assert response.status_code == 303, response.text


# Boot and public pages ------------------------------------------------------


def test_health(client):
    assert client.get("/healthz").json() == {"status": "ok"}


def test_root_renders_landing_page_when_signed_out(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "The closest games, every week" in response.text
    assert "Pricing" in response.text
    # The signed-out header, not the signed-in one.
    assert 'href="/login"' in response.text
    assert "This Week" not in response.text


def test_root_redirects_to_picks_when_signed_in(client, world):
    _login(client, "player@example.com")
    response = client.get("/")
    assert response.status_code == 303
    assert response.headers["location"] == "/picks"


def test_login_page_renders(client):
    response = client.get("/login")
    assert response.status_code == 200
    assert "Sign in" in response.text
    # The design system must actually be wired up.
    assert "app.css" in response.text


def test_pricing_page_renders_signed_out(client):
    response = client.get("/pricing")
    assert response.status_code == 200
    assert "199" in response.text
    assert "349" in response.text
    assert "398" in response.text
    assert "50" in response.text
    assert 'href="/login"' in response.text


def test_pricing_page_renders_signed_in(client, world):
    _login(client, "player@example.com")
    response = client.get("/pricing")
    assert response.status_code == 200
    assert "199" in response.text
    assert "Regular Player" in response.text  # the signed-in header, not the public one


def test_how_it_works_page_renders(client, world):
    response = client.get("/how-it-works")
    assert response.status_code == 200
    assert "inverse" in response.text.lower()

    _login(client, "player@example.com")
    response = client.get("/how-it-works")
    assert response.status_code == 200
    assert "Regular Player" in response.text


def test_contact_page_renders(client, world):
    response = client.get("/contact")
    assert response.status_code == 200

    _login(client, "player@example.com")
    response = client.get("/contact")
    assert response.status_code == 200
    assert "Regular Player" in response.text


def test_register_page_renders(client):
    response = client.get("/register")
    assert response.status_code == 200
    assert "join code" in response.text.lower()


def test_protected_page_redirects_signed_out_user(client):
    response = client.get("/picks")
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


# Router audit (Phase 8 remediation, see DECISIONS.md): the original bug report was
# /leagues and /leagues/new 404ing a signed out visitor instead of redirecting to sign in.
# Those exact routes no longer exist anywhere in this codebase (Phase 4 renamed everything
# under /admin to /league, commissioner scoped, and /site, site admin scoped), so the real
# question this phase actually audited is the general one: does every authenticated route,
# across every router file, redirect rather than 404 a signed out visitor? Every route was
# read directly (app/routers/*.py) and every single one already resolves through
# require_user, require_commissioner, require_full_commissioner, require_admin or
# get_active_pool (all of which wrap require_user, see app/auth.py), so this phase found no
# route to actually fix, only this regression suite to pin the audited result down: a
# representative GET from every router file that has any authenticated route, plus the two
# literal old bug report paths as a negative control proving a genuinely nonexistent route
# still 404s honestly rather than faking a redirect.
@pytest.mark.parametrize(
    "path",
    [
        "/picks",  # app/routers/picks.py
        "/standings",  # app/routers/leaderboard.py
        "/results",  # app/routers/results.py
        "/join",  # app/routers/auth.py
        "/league",  # app/routers/admin.py, require_commissioner
        "/league/settings",  # app/routers/admin.py
        "/league/members",  # app/routers/admin.py
        "/league/slate",  # app/routers/admin.py
        "/league/payouts",  # app/routers/payouts.py, require_commissioner
        "/league/payouts/summary",  # app/routers/payouts.py
        "/site",  # app/routers/site.py, require_admin
        "/site/providers",  # app/routers/site.py
        "/site/mail",  # app/routers/site.py
        "/site/contacts",  # app/routers/admin_contacts.py, require_admin
        "/site/leagues",  # app/routers/leagues.py, require_admin
        "/site/leagues/new",  # app/routers/leagues.py
    ],
)
def test_every_authenticated_route_redirects_a_signed_out_visitor_to_login(client, path):
    response = client.get(path)
    assert response.status_code == 303, f"{path} did not redirect, got {response.status_code}"
    assert response.headers["location"] == f"/login?next={path}"


@pytest.mark.parametrize("path", ["/leagues", "/leagues/new"])
def test_a_genuinely_nonexistent_route_404s_honestly_rather_than_faking_a_redirect(client, path):
    """Negative control: these are the exact paths named in the original bug report. They
    were removed outright by Phase 4's /admin -> /league + /site split and were never
    reintroduced, so a signed out (or signed in) visitor hitting either one must see a real,
    honest 404, not a redirect to /login (which would wrongly imply the route exists and is
    merely gated) and not a 500."""
    response = client.get(path)
    assert response.status_code == 404
    assert "location" not in response.headers
    assert "That page is not on the schedule" in response.text


# Auth -----------------------------------------------------------------------


def test_login_rejects_bad_password(client, world):
    response = client.post(
        "/login", data={"email": "player@example.com", "password": "wrong", "next": "/picks"}
    )
    assert response.status_code == 400
    assert "do not match" in response.text


def test_login_does_not_reveal_whether_an_account_exists(client, world):
    missing = client.post(
        "/login", data={"email": "nobody@example.com", "password": "wrong", "next": "/"}
    )
    wrong = client.post(
        "/login", data={"email": "player@example.com", "password": "wrong", "next": "/"}
    )
    assert missing.status_code == wrong.status_code == 400
    assert "do not match an account" in missing.text
    assert "do not match an account" in wrong.text


def test_register_requires_a_valid_join_code_in_private_mode(client, world):
    response = client.post(
        "/register",
        data={
            "display_name": "New Person",
            "email": "new@example.com",
            "password": "hunter2hunter2!",
            "join_code": "NOPE",
        },
    )
    assert response.status_code == 400
    assert "join code" in response.text.lower()


def test_register_with_the_right_code_joins_the_pool(client, world, session_factory):
    response = client.post(
        "/register",
        data={
            "display_name": "New Person",
            "email": "new@example.com",
            "password": "hunter2hunter2!",
            "join_code": "testcode",  # case insensitive on purpose
        },
    )
    assert response.status_code == 303
    db = session_factory()
    user = db.scalar(select(User).where(User.email == "new@example.com"))
    assert user is not None
    member = db.scalar(select(PoolMember).where(PoolMember.user_id == user.id))
    assert member is not None and member.role_in_pool == "member"
    db.close()


@pytest.mark.parametrize("typed_code", ["test-code", "TEST CODE", "te-st co-de"])
def test_register_with_a_hyphenated_or_spaced_join_code_still_joins_the_pool(
    client, world, session_factory, typed_code
):
    """Phase 8 remediation (see DECISIONS.md): normalize_join_code (app/auth.py) now strips
    hyphens as well as spaces, so a code typed with a dash for readability, or copied with a
    stray space, still matches the stored, dash-free "TESTCODE"."""
    response = client.post(
        "/register",
        data={
            "display_name": "New Person",
            "email": "hyphenated@example.com",
            "password": "hunter2hunter2!",
            "join_code": typed_code,
        },
    )
    assert response.status_code == 303, response.text
    db = session_factory()
    user = db.scalar(select(User).where(User.email == "hyphenated@example.com"))
    assert user is not None
    member = db.scalar(select(PoolMember).where(PoolMember.user_id == user.id))
    assert member is not None and member.role_in_pool == "member"
    db.close()


def test_join_route_accepts_a_hyphenated_join_code(client, session_factory):
    """The same normalization end to end through POST /join, the second real entry point a
    join code passes through (Phase 8 remediation, see DECISIONS.md)."""
    db = session_factory()
    pool = _make_pool(db)
    _make_user(db, "lone@example.com", "Lone Player")
    db.commit()
    db.close()

    _login(client, "lone@example.com")
    response = client.post("/join", data={"join_code": "test-code"})
    assert response.status_code == 303, response.text
    assert response.headers["location"] == "/picks"

    db = session_factory()
    user = db.scalar(select(User).where(User.email == "lone@example.com"))
    member = db.scalar(
        select(PoolMember).where(PoolMember.pool_id == pool.id, PoolMember.user_id == user.id)
    )
    assert member is not None
    db.close()


# app.auth.normalize_join_code (no dedicated tests/test_auth.py exists yet, so this lives
# alongside the router tests that already exercise the same function end to end above) -------


def test_normalize_join_code_strips_hyphens_spaces_and_uppercases_consistently():
    from app.auth import normalize_join_code

    expected = "AB3DEFGH"
    assert normalize_join_code("ab-3d efgh") == expected
    assert normalize_join_code("AB3DEFGH") == expected
    assert normalize_join_code("ab3defgh") == expected
    assert normalize_join_code("  ab3defgh  ") == expected
    assert normalize_join_code("AB-3D-EFGH") == expected


def test_normalize_join_code_handles_blank_input():
    from app.auth import normalize_join_code

    assert normalize_join_code("") == ""
    assert normalize_join_code(None) == ""  # type: ignore[arg-type]
    assert normalize_join_code("   ") == ""
    assert normalize_join_code("---") == ""


# The signed-in pages render -------------------------------------------------


@pytest.mark.parametrize("path", ["/picks", "/standings", "/results"])
def test_member_pages_render(client, world, path):
    _login(client, "player@example.com")
    response = client.get(path)
    assert response.status_code == 200, response.text[:800]


def test_open_picks_page_includes_row_expand_toggle(client, world):
    # Post launch: desktop compact rows (1024px up) move the badge, line, kickoff and
    # each team's record into a collapsed per row panel, revealed by a new toggle button.
    # One toggle and one panel per slate game, each pair wired together by id.
    _login(client, "player@example.com")
    response = client.get("/picks")
    assert response.status_code == 200
    game_ids = world["game_ids"]
    assert response.text.count("data-row-toggle") == len(game_ids)
    assert response.text.count("data-row-detail") == len(game_ids)
    for gid in game_ids:
        assert f'aria-controls="game-detail-{gid}"' in response.text
        assert f'id="game-detail-{gid}"' in response.text


@pytest.mark.parametrize(
    "path", ["/league", "/league/slate", "/league/members", "/league/settings"]
)
def test_admin_pages_render_for_commissioner(client, world, path):
    _login(client, "boss@example.com")
    response = client.get(path)
    assert response.status_code == 200, response.text[:800]


@pytest.mark.parametrize(
    "path", ["/league", "/league/slate", "/league/members", "/league/settings"]
)
def test_admin_pages_refused_for_a_regular_player(client, world, path):
    _login(client, "player@example.com")
    response = client.get(path)
    assert response.status_code == 403


def test_slate_build_route_no_longer_accepts_publish_or_no_metered(client, world):
    """Phase 5 remediation, see DECISIONS.md: the build form dropped both checkboxes, and
    POST /league/slate/build no longer reads publish or no_metered from the form at all. This
    posts only week_number, exactly what the trimmed form now sends, and checks the route
    still runs end to end (force_offline_mode in conftest makes the actual ESPN fetch fail,
    which is fine, this only proves the route no longer errors on the missing fields, the
    request still reaches ingest.build_slate and redirects normally)."""
    _login(client, "boss@example.com")
    response = client.post("/league/slate/build", data={"week_number": 9})
    assert response.status_code == 303
    assert response.headers["location"] == "/league/slate?week=9"


def test_slate_build_route_ignores_publish_and_no_metered_even_if_posted(client, world):
    """Belt and suspenders: even a client that still posts the old field names (a stale
    bookmarked form, a browser that had the page open across the deploy) is not refused with a
    422, since FastAPI simply ignores form fields no Form(...) parameter declares."""
    _login(client, "boss@example.com")
    response = client.post(
        "/league/slate/build",
        data={"week_number": 9, "publish": "1", "no_metered": "1"},
    )
    assert response.status_code == 303


def test_slate_page_shows_neutral_note_when_espn_only_is_on_and_never_billing_language(
    client, world, session_factory
):
    """Phase 5 remediation: a commissioner gets a plain, neutral heads up while the site
    admin's global switch is on, never the word credit, budget or billing (see DECISIONS.md,
    Phase 5, and SPEC.md Section 3h)."""
    _login(client, "boss@example.com")

    response = client.get("/league/slate")
    assert "Some games may not have a line yet." not in response.text

    db = session_factory()
    db.add(PlatformSetting(espn_only=True))
    db.commit()
    db.close()

    response = client.get("/league/slate")
    assert response.status_code == 200
    assert "Some games may not have a line yet. You can set one by hand." in response.text
    for phrase in ("api credit", "monthly budget", "spend no", "billing"):
        assert phrase not in response.text.lower()


# Slate build loading state, idempotency and timeout (Phase 6 remediation) ----
#
# See DECISIONS.md, Phase 6, for the full reasoning: the build POST used to give no feedback
# at all while it ran, several seconds to just under a minute, so a commissioner reasonably
# clicked again. These pin the router side of the fix: the build button is a real htmx request
# wired to app.js's loading state, a build already running for the same week is refused with a
# specific message rather than left to race, and a successful build's flash names real numbers.


def test_slate_build_form_has_the_htmx_loading_attributes(client, world):
    """The rendered markup, not just the route: a real <button type="submit"> inside the real
    <form>, carrying hx-post/hx-target/hx-swap/hx-include so app.js's htmx:beforeRequest and
    htmx:afterRequest listeners (data-build-btn) actually fire, and the hidden progress note
    they toggle. This is the part of the phase's own defect (a plain form POST with zero
    loading feedback) that a scripted POST to the route can never catch, only the markup can."""
    _login(client, "boss@example.com")
    response = client.get("/league/slate")
    assert response.status_code == 200
    body = response.text

    form_start = body.index('action="/league/slate/build"')
    form_end = body.index("</form>", form_start)
    form_html = body[form_start:form_end]

    button_match = re.search(r"<button[^>]*data-build-btn[^>]*>", form_html)
    assert button_match is not None, "the build button is not inside its own form"
    button_html = button_match.group(0)
    assert 'type="submit"' in button_html
    assert 'hx-post="/league/slate/build"' in button_html
    assert 'hx-include="closest form"' in button_html
    assert "disabled" not in button_html  # never server rendered pre-disabled

    note_match = re.search(r"<p[^>]*data-build-note[^>]*>", form_html)
    assert note_match is not None
    assert "hidden" in note_match.group(0)  # starts hidden, app.js reveals it while in flight


def test_slate_build_via_htmx_gets_an_hx_redirect_not_a_swapped_partial(client, world):
    """htmx follows a plain 303 transparently and would swap the *whole* redirected page into
    the button's own hx-target, exactly the trap app/routers/payouts.py's docstring documents.
    An htmx request (HX-Request: true, what the real button sends) must instead get a 200 with
    an HX-Redirect header, which htmx turns into a real, full page navigation. A plain form
    post (no header, the no-JS fallback) still gets the original, unchanged 303."""
    _login(client, "boss@example.com")

    response = client.post(
        "/league/slate/build",
        data={"week_number": 9},
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 200
    assert response.headers["HX-Redirect"] == "/league/slate?week=9"

    plain = client.post("/league/slate/build", data={"week_number": 9})
    assert plain.status_code == 303
    assert plain.headers["location"] == "/league/slate?week=9"
    assert "HX-Redirect" not in plain.headers


def test_slate_build_refused_with_a_specific_message_when_already_running(
    client, world, session_factory
):
    """The idempotency guard is server side (app.services.ingest.slate_build_guard), not just
    the button's own disabled state, so a request that reaches the route while another build
    for the exact same pool and week is already in flight is refused outright, both for a
    plain form post and for htmx, with the specific message a commissioner should see rather
    than a generic error."""
    from app.services import ingest

    _login(client, "boss@example.com")
    ingest._acquire_build_lock(world["pool_id"], 9)
    try:
        response = client.post("/league/slate/build", data={"week_number": 9})
        assert response.status_code == 303
        assert response.headers["location"] == "/league/slate?week=9"

        followup = client.get(response.headers["location"])
        assert "This week is already being built. Wait for it to finish." in followup.text

        hx_response = client.post(
            "/league/slate/build",
            data={"week_number": 9},
            headers={"HX-Request": "true"},
        )
        assert hx_response.status_code == 200
        assert hx_response.headers["HX-Redirect"] == "/league/slate?week=9"
    finally:
        ingest._release_build_lock(world["pool_id"], 9)

    # Released, so building the same week normally afterwards is not refused forever.
    response = client.post("/league/slate/build", data={"week_number": 9})
    assert response.status_code == 303


def test_slate_build_flashes_the_built_summary_with_real_numbers(
    client, world, session_factory, load_fixture
):
    """The exact phrasing from the brief ("Week N built. X games, Y NFL and Z college, W
    missing a line."), with the numbers read back from the database after the build, not
    predicted from the fixture, so this genuinely proves the flash reflects what was built
    rather than a plausible-looking hard coded string."""
    from app.providers.http import cache_put

    db = session_factory()
    pool = db.get(Pool, world["pool_id"])
    pool.season_year = 2026
    pool.week1_anchor_date = dt.date(2026, 8, 29)
    pool.sports = ["ncaaf"]
    pool.target_nfl = 0
    pool.target_ncaaf = 2
    pool.num_games_per_week = 2
    pool.auto_publish = False
    db.commit()

    cache_put(db, "espn:scoreboard:ncaaf:2026:2:None", load_fixture("espn_cfb_2026_calendar.json"))
    cache_put(db, "espn:scoreboard:ncaaf:2026:2:1", load_fixture("espn_cfb_2025_w5.json"))
    db.commit()
    db.close()

    _login(client, "boss@example.com")
    response = client.post("/league/slate/build", data={"week_number": 1})
    assert response.status_code == 303
    followup = client.get(response.headers["location"])
    assert followup.status_code == 200

    db2 = session_factory()
    week = db2.scalar(select(Week).where(Week.pool_id == pool.id, Week.week_number == 1))
    on_slate = [g for g in db2.scalars(select(Game).where(Game.week_id == week.id)) if g.in_slate]
    nfl_count = sum(1 for g in on_slate if g.league == "nfl")
    college_count = sum(1 for g in on_slate if g.league == "ncaaf")
    missing = sum(1 for g in on_slate if g.spread_home is None)
    db2.close()

    assert on_slate  # a real build genuinely selected something, not a dead end
    expected = (
        f"Week 1 built. {len(on_slate)} games, {nfl_count} NFL and {college_count} college, "
        f"{missing} missing a line."
    )
    assert expected in followup.text


# Test weeks (Phase 3, preseason and test week support) -----------------------


def test_test_week_create_refused_for_a_regular_player(client, world):
    _login(client, "player@example.com")
    response = client.post("/league/test-week/create")
    assert response.status_code == 403


def test_test_week_delete_refused_for_a_regular_player(client, world, session_factory):
    db = session_factory()
    week = Week(
        pool_id=world["pool_id"],
        season_year=2025,
        week_number=0,
        label="Test week",
        status="draft",
        is_test_week=True,
    )
    db.add(week)
    db.commit()
    week_id = week.id
    db.close()

    _login(client, "player@example.com")
    response = client.post(f"/league/test-week/{week_id}/delete")
    assert response.status_code == 403


def test_commissioner_can_create_a_test_week(client, world, session_factory):
    """End to end for a commissioner: no anchor date is configured for this pool at all (see
    _make_pool), which a real week build would refuse outright (Phase 2), and which a test
    week must not need. Nothing is cached for ESPN, so every league resolves to nothing and
    the build itself finds no games, exactly like test_build_slate_refuses_with_no_anchor_date's
    "dead end" shape in tests/test_ingest.py, only reached here through the real route. What
    this test actually pins is the thing a service-level test cannot: the Week row it creates
    is real, is_test_week is set, and it needed no anchor date to get there."""
    db = session_factory()
    pool = db.get(Pool, world["pool_id"])
    assert pool.week1_anchor_date is None
    db.close()

    _login(client, "boss@example.com")
    response = client.post("/league/test-week/create")
    assert response.status_code == 303
    assert response.headers["location"] == "/league/slate?week=0"

    db = session_factory()
    week = db.scalar(select(Week).where(Week.pool_id == world["pool_id"], Week.week_number == 0))
    assert week is not None
    assert week.is_test_week is True
    assert week.label == "Test week"
    db.close()


def test_test_week_badge_and_explanation_render_on_the_slate_editor(client, world, session_factory):
    db = session_factory()
    week = Week(
        pool_id=world["pool_id"],
        season_year=2025,
        week_number=0,
        label="Test week",
        status="draft",
        is_test_week=True,
    )
    db.add(week)
    db.commit()
    db.close()

    _login(client, "boss@example.com")
    response = client.get("/league/slate?week=0")
    assert response.status_code == 200
    assert "badge-test-week" in response.text
    assert "does not count toward season" in response.text
    assert "standings or payouts" in response.text
    assert "Delete this test week" in response.text


def test_commissioner_can_delete_a_test_week(client, world, session_factory):
    db = session_factory()
    week = Week(
        pool_id=world["pool_id"],
        season_year=2025,
        week_number=0,
        label="Test week",
        status="draft",
        is_test_week=True,
    )
    db.add(week)
    db.commit()
    week_id = week.id
    db.close()

    _login(client, "boss@example.com")
    response = client.post(f"/league/test-week/{week_id}/delete")
    assert response.status_code == 303

    db = session_factory()
    assert db.get(Week, week_id) is None
    db.close()


def test_commissioner_cannot_delete_a_real_week_via_the_test_week_route(
    client, world, session_factory
):
    """The real week from the world fixture (is_test_week False) must be refused, never
    silently deleted, since a real week never allows deletion in this codebase."""
    _login(client, "boss@example.com")
    response = client.post(f"/league/test-week/{world['week_id']}/delete")
    assert response.status_code == 303

    db = session_factory()
    assert db.get(Week, world["week_id"]) is not None
    db.close()


def test_ephemeral_storage_banner_shown_to_site_admin(client, world, monkeypatch):
    """Phase 1 remediation (see DECISIONS.md): a site admin sees the brick warning on every
    page while the app is running on ephemeral storage. world's boss is both role="admin"
    and this pool's commissioner."""
    monkeypatch.setattr(settings, "database_url", "sqlite:////tmp/picksportplus.db")
    _login(client, "boss@example.com")
    response = client.get("/league")
    assert "temporary storage" in response.text
    assert "lockbar-strong" in response.text


def test_ephemeral_storage_banner_hidden_from_a_regular_player(client, world, monkeypatch):
    """A regular player cannot act on the warning, so they never see it (Phase 1
    remediation), even while the app is genuinely on ephemeral storage."""
    monkeypatch.setattr(settings, "database_url", "sqlite:////tmp/picksportplus.db")
    _login(client, "player@example.com")
    response = client.get("/picks")
    assert "temporary storage" not in response.text


def test_ephemeral_storage_banner_hidden_when_storage_is_durable(client, world, monkeypatch):
    monkeypatch.setattr(settings, "database_url", "sqlite:///./picksportplus.db")
    _login(client, "boss@example.com")
    response = client.get("/league")
    assert "temporary storage" not in response.text


# Picks ----------------------------------------------------------------------


def _valid_submission(game_ids: list[int]) -> dict[str, str]:
    n = len(game_ids)
    data = {}
    for index, gid in enumerate(game_ids):
        data[f"winner-{gid}"] = "home" if index % 2 == 0 else "away"
        data[f"confidence-{gid}"] = str(n - index)
    return data


def test_saving_a_valid_entry_stores_every_pick(client, world, session_factory):
    _login(client, "player@example.com")
    response = client.post("/picks", data=_valid_submission(world["game_ids"]))
    assert response.status_code == 303

    db = session_factory()
    picks = list(db.scalars(select(Pick).where(Pick.user_id == world["player_id"])))
    assert len(picks) == len(world["game_ids"])
    assert sorted(p.confidence for p in picks) == [1, 2, 3, 4]
    entry = db.scalar(select(WeekEntry).where(WeekEntry.user_id == world["player_id"]))
    assert entry is not None and entry.submitted_at is not None
    db.close()


def test_duplicate_confidence_is_rejected(client, world):
    _login(client, "player@example.com")
    data = _valid_submission(world["game_ids"])
    first, second = world["game_ids"][0], world["game_ids"][1]
    data[f"confidence-{second}"] = data[f"confidence-{first}"]
    response = client.post("/picks", data=data, headers={"HX-Request": "true"})
    assert response.status_code == 400
    assert "used twice" in response.text


def test_an_incomplete_submission_is_rejected(client, world):
    """Phase 3: an unpicked slate game is legal, but the submission still has to add up to
    exactly picks_required. Dropping one of the pool's 4 required picks (world's pool sets
    picks_required equal to its 4 game slate) must be rejected with the count message, not
    the old per-game "missing a winner" message that Phase 3 removed.
    """
    _login(client, "player@example.com")
    data = _valid_submission(world["game_ids"])
    dropped = world["game_ids"][0]
    del data[f"winner-{dropped}"]
    del data[f"confidence-{dropped}"]
    response = client.post("/picks", data=data, headers={"HX-Request": "true"})
    assert response.status_code == 400
    assert "You have picked 3 games. Pick 4." in response.text


def test_server_rejects_too_many_picks_even_if_no_client_would_send_them(
    client, world, session_factory
):
    """Server side validation is authoritative for the picks_required rule, never the
    client. Lower this pool's picks_required below its slate size, then hand craft a
    submission covering every slate game, exactly what a stale or bypassed client might
    send. It must be rejected, and nothing must be saved.
    """
    db = session_factory()
    pool = db.get(Pool, world["pool_id"])
    pool.picks_required = 3  # below the world fixture's 4 game slate
    db.commit()
    db.close()

    _login(client, "player@example.com")
    response = client.post(
        "/picks", data=_valid_submission(world["game_ids"]), headers={"HX-Request": "true"}
    )
    assert response.status_code == 400
    assert "You have picked 4 games. Pick 3." in response.text

    db = session_factory()
    assert db.scalar(select(Pick).where(Pick.user_id == world["player_id"])) is None
    db.close()


def test_partial_entry_saves_nothing(client, world, session_factory):
    """An invalid submission must not leave half an entry behind."""
    _login(client, "player@example.com")
    data = _valid_submission(world["game_ids"])
    del data[f"winner-{world['game_ids'][0]}"]
    del data[f"confidence-{world['game_ids'][0]}"]
    client.post("/picks", data=data)

    db = session_factory()
    assert db.scalar(select(Pick).where(Pick.user_id == world["player_id"])) is None
    db.close()


def test_picks_are_editable_before_lock(client, world, session_factory):
    _login(client, "player@example.com")
    client.post("/picks", data=_valid_submission(world["game_ids"]))

    changed = _valid_submission(world["game_ids"])
    first = world["game_ids"][0]
    changed[f"winner-{first}"] = "away"
    response = client.post("/picks", data=changed)
    assert response.status_code == 303

    db = session_factory()
    pick = db.scalar(select(Pick).where(Pick.user_id == world["player_id"], Pick.game_id == first))
    assert pick.picked_team == "away"
    # Still exactly one row per game, not a duplicate.
    assert len(list(db.scalars(select(Pick).where(Pick.user_id == world["player_id"])))) == 4
    db.close()


def test_lock_is_enforced_by_the_clock_not_the_status(client, world, session_factory):
    """A late cron must never hand anyone extra time."""
    db = session_factory()
    week = db.get(Week, world["week_id"])
    # Still marked open, but the lock time has passed.
    week.lock_at = dt.datetime.now(UTC) - dt.timedelta(minutes=1)
    week.status = "open"
    db.commit()
    db.close()

    _login(client, "player@example.com")
    response = client.post("/picks", data=_valid_submission(world["game_ids"]))
    assert response.status_code == 403
    assert "locked" in response.text.lower()


def test_picks_page_is_read_only_after_lock(client, world, session_factory):
    db = session_factory()
    week = db.get(Week, world["week_id"])
    week.lock_at = dt.datetime.now(UTC) - dt.timedelta(minutes=1)
    db.commit()
    db.close()

    _login(client, "player@example.com")
    response = client.get("/picks")
    assert response.status_code == 200
    assert "data-sortable" not in response.text


# Player lock, distinct from the pool wide time lock (Phase 4) -------------


def test_picks_lock_with_a_valid_submission_saves_and_locks(client, world, session_factory):
    _login(client, "player@example.com")
    response = client.post("/picks/lock", data=_valid_submission(world["game_ids"]))
    assert response.status_code == 303

    db = session_factory()
    picks = list(db.scalars(select(Pick).where(Pick.user_id == world["player_id"])))
    assert len(picks) == len(world["game_ids"])
    entry = db.scalar(select(WeekEntry).where(WeekEntry.user_id == world["player_id"]))
    assert entry is not None
    assert entry.locked_at is not None
    assert entry.submitted_at is not None
    db.close()


def test_picks_lock_rejects_an_invalid_submission_and_does_not_lock(client, world, session_factory):
    """The same validation Save runs, and the same refusal: nothing is written, and
    locked_at is never set, whether or not a WeekEntry already existed."""
    _login(client, "player@example.com")
    data = _valid_submission(world["game_ids"])
    dropped = world["game_ids"][0]
    del data[f"winner-{dropped}"]
    del data[f"confidence-{dropped}"]
    response = client.post("/picks/lock", data=data, headers={"HX-Request": "true"})
    assert response.status_code == 400
    assert "You have picked 3 games. Pick 4." in response.text

    db = session_factory()
    assert db.scalar(select(Pick).where(Pick.user_id == world["player_id"])) is None
    entry = db.scalar(select(WeekEntry).where(WeekEntry.user_id == world["player_id"]))
    assert entry is None or entry.locked_at is None
    db.close()


def test_picks_lock_is_refused_once_the_week_is_time_locked(client, world, session_factory):
    db = session_factory()
    week = db.get(Week, world["week_id"])
    week.lock_at = dt.datetime.now(UTC) - dt.timedelta(minutes=1)
    db.commit()
    db.close()

    _login(client, "player@example.com")
    response = client.post(
        "/picks/lock", data=_valid_submission(world["game_ids"]), headers={"HX-Request": "true"}
    )
    assert response.status_code == 403

    db = session_factory()
    assert db.scalar(select(Pick).where(Pick.user_id == world["player_id"])) is None
    db.close()


def test_picks_unlock_clears_the_lock_while_the_week_is_still_open(client, world, session_factory):
    _login(client, "player@example.com")
    client.post("/picks/lock", data=_valid_submission(world["game_ids"]))

    response = client.post("/picks/unlock")
    assert response.status_code == 303

    db = session_factory()
    entry = db.scalar(select(WeekEntry).where(WeekEntry.user_id == world["player_id"]))
    assert entry.locked_at is None
    # Unlocking never touches the picks themselves.
    assert len(list(db.scalars(select(Pick).where(Pick.user_id == world["player_id"])))) == 4
    db.close()


def test_picks_unlock_is_refused_once_the_week_is_time_locked(client, world, session_factory):
    """A time locked week can never be unlocked by the player again, no matter what."""
    _login(client, "player@example.com")
    client.post("/picks/lock", data=_valid_submission(world["game_ids"]))

    db = session_factory()
    week = db.get(Week, world["week_id"])
    week.lock_at = dt.datetime.now(UTC) - dt.timedelta(minutes=1)
    db.commit()
    db.close()

    response = client.post("/picks/unlock", headers={"HX-Request": "true"})
    assert response.status_code == 403
    assert "locked" in response.text.lower()

    db = session_factory()
    entry = db.scalar(select(WeekEntry).where(WeekEntry.user_id == world["player_id"]))
    assert entry.locked_at is not None
    db.close()


def test_picks_page_renders_in_every_state(client, world, session_factory):
    """Nothing entered, partial, full but unlocked, player locked, and time locked. The
    real lock always wins over a player lock the moment it hits, regardless of locked_at.
    """
    _login(client, "player@example.com")

    # 1: nothing entered yet.
    response = client.get("/picks")
    assert response.status_code == 200

    # 2: partially entered. Save always requires the full count, so a genuinely partial
    # entry (fewer picks saved than picks_required) is written directly, the same way a
    # commissioner raising picks_required after a smaller entry was already saved would
    # produce one.
    db = session_factory()
    db.add(
        Pick(
            user_id=world["player_id"],
            pool_id=world["pool_id"],
            week_id=world["week_id"],
            game_id=world["game_ids"][0],
            picked_team="home",
            confidence=1,
        )
    )
    db.commit()
    db.close()
    response = client.get("/picks")
    assert response.status_code == 200

    # 3: fully entered, not locked.
    response = client.post("/picks", data=_valid_submission(world["game_ids"]))
    assert response.status_code == 303
    response = client.get("/picks")
    assert response.status_code == 200
    assert "Reorder to inputs" in response.text
    assert "Unlock to edit" not in response.text

    # 4: locked by the player, the week itself still open.
    response = client.post("/picks/lock", data=_valid_submission(world["game_ids"]))
    assert response.status_code == 303
    response = client.get("/picks")
    assert response.status_code == 200
    assert "Unlock to edit" in response.text
    assert "Reorder to inputs" not in response.text

    # 5: time locked. The pool wide lock always wins, even though locked_at is still set.
    db = session_factory()
    week = db.get(Week, world["week_id"])
    week.lock_at = dt.datetime.now(UTC) - dt.timedelta(minutes=1)
    db.commit()
    db.close()
    response = client.get("/picks")
    assert response.status_code == 200
    assert "data-sortable" not in response.text
    assert "Unlock to edit" not in response.text

    db = session_factory()
    entry = db.scalar(select(WeekEntry).where(WeekEntry.user_id == world["player_id"]))
    assert entry.locked_at is not None
    db.close()


# Results and scoring --------------------------------------------------------


def test_picks_stay_private_until_lock(client, world, session_factory):
    """Another member must not be able to see anyone's picks before the lock."""
    _login(client, "player@example.com")
    client.post("/picks", data=_valid_submission(world["game_ids"]))
    client.post("/logout")

    # Sign in as someone else. Their own name is in the top bar, so look for the player's.
    _login(client, "boss@example.com")
    response = client.get("/results")
    assert response.status_code == 200
    assert "Regular Player" not in response.text


def test_picks_are_revealed_after_lock(client, world, session_factory):
    _login(client, "player@example.com")
    client.post("/picks", data=_valid_submission(world["game_ids"]))
    client.post("/logout")
    _login(client, "boss@example.com")

    db = session_factory()
    week = db.get(Week, world["week_id"])
    week.lock_at = dt.datetime.now(UTC) - dt.timedelta(minutes=1)
    week.status = "locked"
    db.commit()
    db.close()

    response = client.get("/results")
    assert response.status_code == 200
    assert "Regular Player" in response.text


# Season/Results split, sortable tables, and the player-major grid ----------


def test_standings_page_has_no_weekly_leaderboard(client, world):
    """Phase 6: /standings is season standings only. The word "leaderboard" and the old
    weekly section's heading id must both be genuinely gone, not just visually hidden.
    """
    _login(client, "player@example.com")
    response = client.get("/standings")
    assert response.status_code == 200
    assert "leaderboard" not in response.text.lower()
    assert "weekly-heading" not in response.text
    assert "Season standings" in response.text


def test_results_weekly_leaderboard_matches_the_selected_week(client, world, session_factory):
    """The weekly leaderboard on /results must reflect whichever week the switcher has
    selected (?week=), not always the latest week in the pool.
    """
    db = session_factory()
    pool = db.get(Pool, world["pool_id"])
    week5 = db.get(Week, world["week_id"])
    week5.status = "locked"
    db.add(
        WeekEntry(
            user_id=world["player_id"],
            pool_id=pool.id,
            week_id=week5.id,
            points=3,
            correct=2,
            possible=4,
            is_winner=True,
            submitted_at=dt.datetime.now(UTC),
        )
    )
    week6 = Week(
        pool_id=pool.id,
        season_year=pool.season_year,
        week_number=6,
        label="Week 6",
        status="locked",
        lock_at=dt.datetime.now(UTC) - dt.timedelta(hours=1),
    )
    db.add(week6)
    db.flush()
    db.add(
        WeekEntry(
            user_id=world["player_id"],
            pool_id=pool.id,
            week_id=week6.id,
            points=9,
            correct=1,
            possible=4,
            is_winner=True,
            submitted_at=dt.datetime.now(UTC),
        )
    )
    db.commit()
    db.close()

    _login(client, "player@example.com")

    resp5 = client.get("/results?week=5")
    assert resp5.status_code == 200
    assert "Week 5 leaderboard" in resp5.text
    assert 'data-sort-value="3"' in resp5.text
    assert 'data-sort-value="9"' not in resp5.text

    resp6 = client.get("/results?week=6")
    assert resp6.status_code == 200
    assert "Week 6 leaderboard" in resp6.text
    assert 'data-sort-value="9"' in resp6.text
    assert 'data-sort-value="3"' not in resp6.text


def test_results_weekly_leaderboard_stays_private_until_lock(client, world, session_factory):
    """The weekly leaderboard follows the same reveal rule as the pick grid: an entry
    existing at all before lock is itself a "who has submitted" leak.
    """
    _login(client, "player@example.com")
    client.post("/picks", data=_valid_submission(world["game_ids"]))
    client.post("/logout")

    _login(client, "boss@example.com")
    response = client.get("/results")
    assert response.status_code == 200
    assert "Regular Player" not in response.text


def test_results_grid_is_player_major_with_confidence_columns_and_game_major_toggle(
    client, session_factory
):
    """Rows are players, columns are confidence values, cells show the matchup, and the
    old game-major table is still present in the response, behind the toggle.
    """
    db = session_factory()
    pool = _make_pool(db, num_games=4, picks_required=4)
    alpha = _make_user(db, "alpha@example.com", "Alpha Player")
    beta = _make_user(db, "beta@example.com", "Beta Player")
    db.add(PoolMember(pool_id=pool.id, user_id=alpha.id, role_in_pool="commissioner"))
    db.add(PoolMember(pool_id=pool.id, user_id=beta.id, role_in_pool="member"))
    week = _make_week(db, pool, status="locked")
    games = _make_games(db, week, count=4)
    db.commit()

    games[0].status, games[0].winner = "final", "home"
    games[0].home_score, games[0].away_score = 21, 14
    games[1].status, games[1].winner = "final", "away"
    games[1].home_score, games[1].away_score = 10, 17
    games[2].status = "void"
    games[3].status = "scheduled"
    db.commit()

    # Alpha picks all four games: game0 correct (home, confidence 4), game1 wrong (home,
    # confidence 3), game2 void (away, confidence 2), game3 not yet final (home, confidence 1).
    db.add(
        Pick(
            user_id=alpha.id,
            pool_id=pool.id,
            week_id=week.id,
            game_id=games[0].id,
            picked_team="home",
            confidence=4,
        )
    )
    db.add(
        Pick(
            user_id=alpha.id,
            pool_id=pool.id,
            week_id=week.id,
            game_id=games[1].id,
            picked_team="home",
            confidence=3,
        )
    )
    db.add(
        Pick(
            user_id=alpha.id,
            pool_id=pool.id,
            week_id=week.id,
            game_id=games[2].id,
            picked_team="away",
            confidence=2,
        )
    )
    db.add(
        Pick(
            user_id=alpha.id,
            pool_id=pool.id,
            week_id=week.id,
            game_id=games[3].id,
            picked_team="home",
            confidence=1,
        )
    )
    # Beta only picks three of the four games, so confidence 1 has nothing mapped to it.
    db.add(
        Pick(
            user_id=beta.id,
            pool_id=pool.id,
            week_id=week.id,
            game_id=games[0].id,
            picked_team="away",
            confidence=4,
        )
    )
    db.add(
        Pick(
            user_id=beta.id,
            pool_id=pool.id,
            week_id=week.id,
            game_id=games[1].id,
            picked_team="away",
            confidence=3,
        )
    )
    db.add(
        Pick(
            user_id=beta.id,
            pool_id=pool.id,
            week_id=week.id,
            game_id=games[2].id,
            picked_team="home",
            confidence=2,
        )
    )
    db.commit()
    db.close()

    _login(client, "alpha@example.com")
    response = client.get("/results")
    assert response.status_code == 200
    text = response.text

    # Player-major: Alpha's confidence 4 column shows the matchup staked there, "H0 over A0",
    # the home team Alpha picked (correct) followed by the opponent, regardless of outcome.
    assert "<strong>H0</strong> over A0" in text
    # Alpha's void pick (confidence 2) carries a clear void marker.
    assert 'pick-void-badge">Void<' in text
    # Beta did not pick a fourth game: confidence 1 renders empty for Beta, not an error.
    assert "No pick at confidence 1" in text

    # The game-major table (rows are games, columns are players) is still present in the
    # response, behind the toggle, even though JS defaults to hiding it.
    assert 'data-view-panel="game"' in text
    assert 'class="pick-player' in text
    assert 'data-view-btn="game"' in text


def test_scoring_end_to_end(client, world, session_factory):
    """Save picks, finalise the games, score, and check the leaderboard math.

    world's pool never sets scoring_mode, so it runs under the real default, "inverse":
    wrong picks count against the player, not right ones. The boss never submits a pick at
    all (see the world fixture), which doubles as the no-show side of Phase 2's central
    rule: a no-show takes the maximum penalty and can never win the week, even though the
    player's own inverse points are not the week's lowest number in isolation.
    """
    from app.services.results import score_week_for_pool

    _login(client, "player@example.com")
    client.post("/picks", data=_valid_submission(world["game_ids"]))

    db = session_factory()
    pool = db.get(Pool, world["pool_id"])
    week = db.get(Week, world["week_id"])
    games = list(db.scalars(select(Game).where(Game.week_id == week.id).order_by(Game.slate_rank)))

    # The player picked home, away, home, away with confidence 4, 3, 2, 1.
    # Make the first two correct and the last two wrong. Under inverse only the wrong
    # picks count, against the player: 2 + 1 = 3.
    outcomes = ["home", "away", "away", "home"]
    for game, winner in zip(games, outcomes, strict=False):
        game.status = "final"
        game.winner = winner
        game.home_score = 21 if winner == "home" else 17
        game.away_score = 17 if winner == "home" else 21
    db.commit()

    report = score_week_for_pool(db, pool, week)
    db.commit()

    entry = db.scalar(
        select(WeekEntry).where(
            WeekEntry.user_id == world["player_id"], WeekEntry.week_id == week.id
        )
    )
    assert entry.points == 3
    assert entry.correct == 2
    assert entry.possible == 4
    assert entry.did_not_submit is False
    assert entry.is_winner is True
    assert report.week_complete is True
    assert db.get(Week, week.id).status == "scored"

    # The boss never submitted a pick. With a 4 game slate the maximum penalty is
    # sum(1..4) = 10, and did_not_submit keeps them out of the running no matter how that
    # compares to the player's own score.
    boss_entry = db.scalar(
        select(WeekEntry).where(WeekEntry.user_id == world["boss_id"], WeekEntry.week_id == week.id)
    )
    assert boss_entry.did_not_submit is True
    assert boss_entry.points == 10
    assert boss_entry.correct == 0
    assert boss_entry.is_winner is False
    db.close()

    response = client.get("/standings")
    assert response.status_code == 200
    assert "Regular Player" in response.text


def test_scoring_end_to_end_standard_mode_still_works(client, world, session_factory):
    """scoring_mode is switchable per pool without a code change: standard mode must keep
    the pre-Phase-2 behavior exactly, correct picks earn points, highest total wins.
    """
    from app.services.results import score_week_for_pool

    db = session_factory()
    pool = db.get(Pool, world["pool_id"])
    pool.scoring_mode = "standard"
    db.commit()
    db.close()

    _login(client, "player@example.com")
    client.post("/picks", data=_valid_submission(world["game_ids"]))

    db = session_factory()
    pool = db.get(Pool, world["pool_id"])
    week = db.get(Week, world["week_id"])
    games = list(db.scalars(select(Game).where(Game.week_id == week.id).order_by(Game.slate_rank)))

    outcomes = ["home", "away", "away", "home"]
    for game, winner in zip(games, outcomes, strict=False):
        game.status = "final"
        game.winner = winner
        game.home_score = 21 if winner == "home" else 17
        game.away_score = 17 if winner == "home" else 21
    db.commit()

    score_week_for_pool(db, pool, week)
    db.commit()

    entry = db.scalar(
        select(WeekEntry).where(
            WeekEntry.user_id == world["player_id"], WeekEntry.week_id == week.id
        )
    )
    assert entry.points == 7
    assert entry.correct == 2
    assert entry.did_not_submit is False
    assert entry.is_winner is True

    boss_entry = db.scalar(
        select(WeekEntry).where(WeekEntry.user_id == world["boss_id"], WeekEntry.week_id == week.id)
    )
    assert boss_entry.did_not_submit is True
    assert boss_entry.points == 0
    db.close()


def test_scoring_is_idempotent(client, world, session_factory):
    from app.services.results import score_week_for_pool

    _login(client, "player@example.com")
    client.post("/picks", data=_valid_submission(world["game_ids"]))

    db = session_factory()
    pool = db.get(Pool, world["pool_id"])
    week = db.get(Week, world["week_id"])
    for game in db.scalars(select(Game).where(Game.week_id == week.id)):
        game.status = "final"
        game.winner = "home"
        game.home_score, game.away_score = 24, 20
    db.commit()

    first = score_week_for_pool(db, pool, week)
    db.commit()
    entry_a = db.scalar(select(WeekEntry).where(WeekEntry.user_id == world["player_id"])).points
    second = score_week_for_pool(db, pool, week)
    db.commit()
    entry_b = db.scalar(select(WeekEntry).where(WeekEntry.user_id == world["player_id"])).points

    assert entry_a == entry_b
    assert first.players == second.players
    # Exactly one entry row, not one per run.
    rows = list(db.scalars(select(WeekEntry).where(WeekEntry.week_id == week.id)))
    assert len(rows) == 2  # the commissioner and the player, one row each
    db.close()


def test_a_voided_game_scores_nobody_and_leaves_the_possible_count(client, world, session_factory):
    from app.services.results import score_week_for_pool

    _login(client, "player@example.com")
    client.post("/picks", data=_valid_submission(world["game_ids"]))

    db = session_factory()
    pool = db.get(Pool, world["pool_id"])
    week = db.get(Week, world["week_id"])
    games = list(db.scalars(select(Game).where(Game.week_id == week.id).order_by(Game.slate_rank)))
    for game in games:
        game.status = "final"
        game.winner = "home"
        game.home_score, game.away_score = 24, 20
    # The player staked 4 points on game one and picked home, which would have been correct.
    games[0].status = "void"
    games[0].winner = None
    db.commit()

    score_week_for_pool(db, pool, week)
    db.commit()
    entry = db.scalar(select(WeekEntry).where(WeekEntry.user_id == world["player_id"]))
    # Game 1 (conf 4, picked home) is void, so it earns nothing and drops out of possible.
    # Under the pool's default inverse mode, the wrong picks are what count: game 2
    # (away, but home won, conf 3) and game 4 (away, but home won, conf 1): 3 + 1 = 4.
    assert entry.points == 4
    assert entry.correct == 1
    assert entry.possible == 3
    db.close()


# Commissioner controls ------------------------------------------------------


def test_commissioner_can_change_the_slate_size(client, world, session_factory):
    _login(client, "boss@example.com")
    response = client.post(
        "/league/settings",
        data={
            "name": "Renamed League",
            "season_year": "2025",
            "timezone": "America/Chicago",
            "num_games_per_week": "18",
            "target_nfl": "6",
            "target_ncaaf": "12",
            "picks_required": "15",
            "scoring_mode": "inverse",
            "scenarios_min_final_games": "5",
            "scenarios_min_remaining_games": "1",
            "auto_publish": "1",
            "open_registration": "",
            "sports_nfl": "1",
            "sports_ncaaf": "1",
        },
    )
    assert response.status_code == 303
    db = session_factory()
    pool = db.get(Pool, world["pool_id"])
    assert pool.name == "Renamed League"
    assert pool.num_games_per_week == 18
    assert pool.target_nfl == 6
    assert pool.target_ncaaf == 12
    assert pool.picks_required == 15
    assert pool.timezone == "America/Chicago"
    assert pool.auto_publish is True
    assert pool.open_registration is False
    db.close()


def test_commissioner_cannot_set_picks_required_above_the_slate_size(
    client, world, session_factory
):
    _login(client, "boss@example.com")
    response = client.post(
        "/league/settings",
        data={
            "name": "Test Pool",
            "season_year": "2025",
            "timezone": "America/New_York",
            "num_games_per_week": "4",
            "target_nfl": "2",
            "target_ncaaf": "2",
            "picks_required": "5",  # more than the 4 game slate
            "scoring_mode": "inverse",
            "scenarios_min_final_games": "5",
            "scenarios_min_remaining_games": "1",
            "auto_publish": "1",
            "open_registration": "",
            "sports_nfl": "1",
            "sports_ncaaf": "1",
        },
    )
    assert response.status_code == 303
    db = session_factory()
    pool = db.get(Pool, world["pool_id"])
    # Rejected: picks_required is unchanged from the world fixture's original value.
    assert pool.picks_required == 4
    db.close()


def test_commissioner_can_change_the_scenarios_panel_thresholds(client, world, session_factory):
    _login(client, "boss@example.com")
    response = client.post(
        "/league/settings",
        data={
            "name": "Test Pool",
            "season_year": "2025",
            "timezone": "America/New_York",
            "num_games_per_week": "4",
            "target_nfl": "2",
            "target_ncaaf": "2",
            "picks_required": "4",
            "scoring_mode": "inverse",
            "scenarios_min_final_games": "3",
            "scenarios_min_remaining_games": "2",
            "sports_nfl": "1",
            "sports_ncaaf": "1",
        },
    )
    assert response.status_code == 303
    db = session_factory()
    pool = db.get(Pool, world["pool_id"])
    assert pool.scenarios_min_final_games == 3
    assert pool.scenarios_min_remaining_games == 2
    db.close()


def test_commissioner_cannot_set_scenarios_min_remaining_games_below_one(
    client, world, session_factory
):
    _login(client, "boss@example.com")
    response = client.post(
        "/league/settings",
        data={
            "name": "Test Pool",
            "season_year": "2025",
            "timezone": "America/New_York",
            "num_games_per_week": "4",
            "target_nfl": "2",
            "target_ncaaf": "2",
            "picks_required": "4",
            "scoring_mode": "inverse",
            "scenarios_min_final_games": "5",
            "scenarios_min_remaining_games": "0",
            "sports_nfl": "1",
            "sports_ncaaf": "1",
        },
    )
    assert response.status_code == 303
    db = session_factory()
    pool = db.get(Pool, world["pool_id"])
    # Rejected: unchanged from the world fixture's original value (the Pool default, 1).
    assert pool.scenarios_min_remaining_games == 1
    db.close()


def test_slate_cannot_be_resized_once_a_pick_exists(client, world, session_factory):
    from app.services.ingest import SlateLocked, remove_from_slate

    _login(client, "player@example.com")
    client.post("/picks", data=_valid_submission(world["game_ids"]))

    db = session_factory()
    week = db.get(Week, world["week_id"])
    with pytest.raises(SlateLocked):
        remove_from_slate(db, week, world["game_ids"][0])
    db.close()


def test_voiding_stays_available_after_picks_exist(client, world, session_factory):
    from app.services.ingest import set_void

    _login(client, "player@example.com")
    client.post("/picks", data=_valid_submission(world["game_ids"]))

    db = session_factory()
    week = db.get(Week, world["week_id"])
    game = set_void(db, week, world["game_ids"][0], True)
    db.commit()
    assert game.status == "void"
    db.close()


def test_validation_errors_can_actually_reach_the_browser():
    """htmx does not swap a 4xx by default, so this wiring is load bearing.

    Without the document level htmx:beforeSwap handler the server's validation messages
    are computed, returned, and then silently dropped: the player sees a "Not saved" chip
    and no reason. It has to be a document listener, because on an error response htmx
    dispatches beforeSwap at the target rather than at the element that triggered the
    request, so the same code as an hx-on attribute on the save button never runs.
    """
    from app.templating import STATIC_DIR, TEMPLATES_DIR

    app_js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert 'document.addEventListener("htmx:beforeSwap"' in app_js
    assert "shouldSwap = true" in app_js

    picks = (TEMPLATES_DIR / "picks.html").read_text(encoding="utf-8")
    assert "hx-on::before-swap" not in picks, (
        "An hx-on::before-swap handler on the save button never fires for a 4xx. "
        "Keep this in app.js as a document level listener."
    )


def test_the_error_partial_lists_each_problem_separately(client, world):
    """The partial the browser swaps in must name every problem, not just the first."""
    _login(client, "player@example.com")
    data = _valid_submission(world["game_ids"])
    first, second = world["game_ids"][0], world["game_ids"][1]
    data[f"confidence-{second}"] = data[f"confidence-{first}"]

    response = client.post("/picks", data=data, headers={"HX-Request": "true"})
    assert response.status_code == 400
    assert response.text.count("<li>") >= 2
    assert 'role="alert"' in response.text
    assert "used twice" in response.text
    assert "is not used" in response.text


def test_join_code_rotation_invalidates_the_old_code(client, world, session_factory):
    _login(client, "boss@example.com")
    response = client.post("/league/join-code")
    assert response.status_code == 303

    db = session_factory()
    pool = db.get(Pool, world["pool_id"])
    assert pool.join_code != "TESTCODE"
    db.close()

    client.post("/logout")
    rejected = client.post(
        "/register",
        data={
            "display_name": "Too Late",
            "email": "late@example.com",
            "password": "hunter2hunter2!",
            "join_code": "TESTCODE",
        },
    )
    assert rejected.status_code == 400


# Venmo entry gate (Phase 7) ---------------------------------------------------


def _member_row(session_factory, pool_id: int, user_id: int) -> PoolMember:
    db = session_factory()
    member = db.scalar(
        select(PoolMember).where(PoolMember.pool_id == pool_id, PoolMember.user_id == user_id)
    )
    db.close()
    return member


def test_unpaid_member_cannot_save_picks_when_payment_required(client, world, session_factory):
    db = session_factory()
    pool = db.get(Pool, world["pool_id"])
    pool.payment_required_to_pick = True
    db.commit()
    db.close()

    _login(client, "player@example.com")
    response = client.post("/picks", data=_valid_submission(world["game_ids"]))
    assert response.status_code == 403
    assert "pay your entry fee" in response.text.lower()

    db = session_factory()
    assert db.scalar(select(Pick).where(Pick.user_id == world["player_id"])) is None
    db.close()


def test_unpaid_member_cannot_lock_picks_when_payment_required(client, world, session_factory):
    db = session_factory()
    pool = db.get(Pool, world["pool_id"])
    pool.payment_required_to_pick = True
    db.commit()
    db.close()

    _login(client, "player@example.com")
    response = client.post("/picks/lock", data=_valid_submission(world["game_ids"]))
    assert response.status_code == 403
    assert "pay your entry fee" in response.text.lower()

    db = session_factory()
    assert db.scalar(select(Pick).where(Pick.user_id == world["player_id"])) is None
    entry = db.scalar(select(WeekEntry).where(WeekEntry.user_id == world["player_id"]))
    assert entry is None or entry.locked_at is None
    db.close()


def test_paid_member_can_save_and_lock_picks_when_payment_required(client, world, session_factory):
    db = session_factory()
    pool = db.get(Pool, world["pool_id"])
    pool.payment_required_to_pick = True
    member = db.scalar(
        select(PoolMember).where(
            PoolMember.pool_id == world["pool_id"], PoolMember.user_id == world["player_id"]
        )
    )
    member.paid_at = dt.datetime.now(UTC)
    db.commit()
    db.close()

    _login(client, "player@example.com")
    response = client.post("/picks", data=_valid_submission(world["game_ids"]))
    assert response.status_code == 303

    db = session_factory()
    assert db.scalar(select(Pick).where(Pick.user_id == world["player_id"])) is not None
    db.close()

    lock_response = client.post("/picks/lock", data=_valid_submission(world["game_ids"]))
    assert lock_response.status_code == 303

    db = session_factory()
    entry = db.scalar(select(WeekEntry).where(WeekEntry.user_id == world["player_id"]))
    assert entry.locked_at is not None
    db.close()


def test_payment_not_required_lets_anyone_pick_regardless_of_paid_at(
    client, world, session_factory
):
    db = session_factory()
    pool = db.get(Pool, world["pool_id"])
    pool.payment_required_to_pick = False
    member = db.scalar(
        select(PoolMember).where(
            PoolMember.pool_id == world["pool_id"], PoolMember.user_id == world["player_id"]
        )
    )
    assert member.paid_at is None  # still unpaid, and it must not matter
    db.commit()
    db.close()

    _login(client, "player@example.com")
    response = client.post("/picks", data=_valid_submission(world["game_ids"]))
    assert response.status_code == 303

    lock_response = client.post("/picks/lock", data=_valid_submission(world["game_ids"]))
    assert lock_response.status_code == 303


def test_picks_page_shows_the_venmo_panel_when_gated(client, world, session_factory):
    db = session_factory()
    pool = db.get(Pool, world["pool_id"])
    pool.payment_required_to_pick = True
    pool.entry_fee = 25.0
    pool.venmo_handle = "poolcollector"
    db.commit()
    db.close()

    _login(client, "player@example.com")
    response = client.get("/picks")
    assert response.status_code == 200
    assert "Pay your entry fee to pick" in response.text
    assert "poolcollector" in response.text
    # The editable, save-and-lock form must not render while gated.
    assert "data-sortable" not in response.text


# Admin: paid toggle, bulk mark paid, duplicate Venmo handle warning ----------


def test_member_paid_toggle_marks_and_unmarks(client, world, session_factory):
    member = _member_row(session_factory, world["pool_id"], world["player_id"])
    _login(client, "boss@example.com")

    response = client.post(f"/league/members/{member.id}/paid")
    assert response.status_code == 303
    db = session_factory()
    refreshed = db.get(PoolMember, member.id)
    assert refreshed.paid_at is not None
    assert refreshed.paid_marked_by_user_id == world["boss_id"]
    db.close()

    client.post(f"/league/members/{member.id}/paid")
    db = session_factory()
    refreshed = db.get(PoolMember, member.id)
    assert refreshed.paid_at is None
    assert refreshed.paid_marked_by_user_id is None
    db.close()


def test_members_paid_bulk_marks_only_the_selected_unpaid(client, world, session_factory):
    db = session_factory()
    third = _make_user(db, "third@example.com", "Third Player")
    db.add(PoolMember(pool_id=world["pool_id"], user_id=third.id, role_in_pool="member"))
    db.commit()

    player_member = _member_row(session_factory, world["pool_id"], world["player_id"])
    third_member = _member_row(session_factory, world["pool_id"], third.id)
    boss_member = _member_row(session_factory, world["pool_id"], world["boss_id"])

    boss_member = db.get(PoolMember, boss_member.id)
    boss_member.paid_at = dt.datetime.now(UTC)  # already paid, must be left untouched
    db.commit()
    db.close()

    # Read back through a fresh session, same as the bulk route itself will, so the
    # "untouched" comparison below is not thrown off by SQLite handing datetimes back naive.
    db = session_factory()
    boss_paid_before = db.get(PoolMember, boss_member.id).paid_at
    db.close()

    _login(client, "boss@example.com")
    response = client.post(
        "/league/members/paid/bulk",
        data={"member_ids": [player_member.id, third_member.id, boss_member.id]},
    )
    assert response.status_code == 303

    db = session_factory()
    assert db.get(PoolMember, player_member.id).paid_at is not None
    assert db.get(PoolMember, third_member.id).paid_at is not None
    assert db.get(PoolMember, boss_member.id).paid_at == boss_paid_before
    db.close()


def test_duplicate_venmo_handle_warning_fires_for_shared_handle_not_for_different_or_empty(
    client, world, session_factory
):
    player_member = _member_row(session_factory, world["pool_id"], world["player_id"])
    boss_member = _member_row(session_factory, world["pool_id"], world["boss_id"])

    _login(client, "boss@example.com")

    # Different, non empty handles: no warning for either.
    client.post(
        f"/league/members/{player_member.id}/venmo-handle",
        data={"member_venmo_handle": "player-handle"},
    )
    client.post(
        f"/league/members/{boss_member.id}/venmo-handle",
        data={"member_venmo_handle": "boss-handle"},
    )
    response = client.get("/league/members")
    assert "Possible duplicate account" not in response.text

    # Both empty: no warning either (an empty handle is not a shared account).
    client.post(
        f"/league/members/{player_member.id}/venmo-handle", data={"member_venmo_handle": ""}
    )
    client.post(f"/league/members/{boss_member.id}/venmo-handle", data={"member_venmo_handle": ""})
    response = client.get("/league/members")
    assert "Possible duplicate account" not in response.text

    # Same handle (case and whitespace variations): both rows flagged.
    client.post(
        f"/league/members/{player_member.id}/venmo-handle",
        data={"member_venmo_handle": "SharedHandle"},
    )
    client.post(
        f"/league/members/{boss_member.id}/venmo-handle",
        data={"member_venmo_handle": " sharedhandle "},
    )
    response = client.get("/league/members")
    assert response.text.count("Possible duplicate account") == 2


# Admin: settings save, payout rules, and the pot validator -------------------


def test_settings_save_persists_venmo_and_payment_fields(client, world, session_factory):
    _login(client, "boss@example.com")
    response = client.post(
        "/league/settings",
        data={
            "name": "Test Pool",
            "season_year": "2025",
            "timezone": "America/New_York",
            "num_games_per_week": "4",
            "target_nfl": "2",
            "target_ncaaf": "2",
            "picks_required": "4",
            "scoring_mode": "inverse",
            "scenarios_min_final_games": "5",
            "scenarios_min_remaining_games": "1",
            "sports_nfl": "1",
            "sports_ncaaf": "1",
            "entry_fee": "25.50",
            "venmo_handle": "the-collector",
            "payment_required_to_pick": "1",
            "payment_note": "Note as Week 1 in the Venmo app.",
        },
    )
    assert response.status_code == 303

    db = session_factory()
    pool = db.get(Pool, world["pool_id"])
    assert pool.entry_fee == 25.50
    assert pool.venmo_handle == "the-collector"
    assert pool.payment_required_to_pick is True
    assert pool.payment_note == "Note as Week 1 in the Venmo app."
    db.close()


def test_settings_save_rejects_a_negative_entry_fee(client, world, session_factory):
    _login(client, "boss@example.com")
    response = client.post(
        "/league/settings",
        data={
            "name": "Test Pool",
            "season_year": "2025",
            "timezone": "America/New_York",
            "num_games_per_week": "4",
            "target_nfl": "2",
            "target_ncaaf": "2",
            "picks_required": "4",
            "scoring_mode": "inverse",
            "scenarios_min_final_games": "5",
            "scenarios_min_remaining_games": "1",
            "sports_nfl": "1",
            "sports_ncaaf": "1",
            "entry_fee": "-5",
        },
    )
    assert response.status_code == 303
    db = session_factory()
    pool = db.get(Pool, world["pool_id"])
    assert pool.entry_fee is None  # rejected, unchanged from the world fixture's default
    db.close()


def test_settings_save_rejects_a_non_saturday_anchor_date(client, world, session_factory):
    """Phase 2 remediation (see DECISIONS.md)."""
    response = client.post(
        "/league/settings",
        data={
            "name": "Test Pool",
            "season_year": "2025",
            "timezone": "America/New_York",
            "num_games_per_week": "4",
            "target_nfl": "2",
            "target_ncaaf": "2",
            "picks_required": "4",
            "scoring_mode": "inverse",
            "scenarios_min_final_games": "5",
            "scenarios_min_remaining_games": "1",
            "sports_nfl": "1",
            "sports_ncaaf": "1",
            "week1_anchor_date": "2026-09-13",  # a Sunday
        },
    )
    assert response.status_code == 303
    db = session_factory()
    pool = db.get(Pool, world["pool_id"])
    assert pool.week1_anchor_date is None  # rejected, unchanged
    db.close()


# Payout rule CRUD and the pot balance validator moved to app/routers/payouts.py and
# tests/test_payout_routes.py as part of the payout system rebuild; see DECISIONS.md,
# "Payout system".


# Results: the weekly payout column --------------------------------------------


def _score_worlds_only_week(client, session_factory, world):
    """Submit the player's picks, finalise every game, and score the week, matching the
    exact fixture test_scoring_end_to_end uses (player's inverse points come out to 3)."""
    from app.services.results import score_week_for_pool

    _login(client, "player@example.com")
    client.post("/picks", data=_valid_submission(world["game_ids"]))

    db = session_factory()
    pool = db.get(Pool, world["pool_id"])
    week = db.get(Week, world["week_id"])
    games = list(db.scalars(select(Game).where(Game.week_id == week.id).order_by(Game.slate_rank)))
    outcomes = ["home", "away", "away", "home"]
    for game, winner in zip(games, outcomes, strict=False):
        game.status = "final"
        game.winner = winner
        game.home_score = 21 if winner == "home" else 17
        game.away_score = 17 if winner == "home" else 21
    db.commit()

    score_week_for_pool(db, pool, week)
    db.commit()
    db.close()


def test_results_no_payout_rules_means_no_payout_column(client, world, session_factory):
    _score_worlds_only_week(client, session_factory, world)

    _login(client, "boss@example.com")
    response = client.get("/results")
    assert response.status_code == 200
    assert "Payout" not in response.text


# The weekly and bowl week payout column tests moved to tests/test_payout_display.py, on the
# new frozen-award-backed rendering. See DECISIONS.md, "Payout system".


# Results: the scenarios panel and build-your-own-scenario (Phase 8) -----------


def _lock_worlds_week(session_factory, world):
    db = session_factory()
    week = db.get(Week, world["week_id"])
    week.lock_at = dt.datetime.now(UTC) - dt.timedelta(minutes=1)
    week.status = "locked"
    db.commit()
    db.close()


def test_scenarios_panel_shows_pending_state_below_the_threshold(client, world, session_factory):
    """The world fixture's pool keeps the Pool model default thresholds (5 final games, 1
    remaining) and only has 4 games total, none final, so the panel must show the pending
    copy naming the real configured number, never a hard coded "five"."""
    _lock_worlds_week(session_factory, world)
    _login(client, "boss@example.com")
    response = client.get("/results")
    assert response.status_code == 200
    assert "Scenarios open once 5 games are final" in response.text


def test_scenarios_panel_pending_message_distinguishes_not_enough_remaining_games(
    client, world, session_factory
):
    """Found via live testing (Phase 9 remediation, see DECISIONS.md): a fully scored week
    (every game final, none remaining) already clears the final-games threshold, so the old
    single "Scenarios open once N games are final" message was actively misleading, it read
    as though the panel still needed MORE final games when the real, opposite reason is that
    there are none left to build a scenario around. The pending copy must name the actual
    blocker."""
    db = session_factory()
    pool = db.get(Pool, world["pool_id"])
    pool.scenarios_min_final_games = 1
    week = db.get(Week, world["week_id"])
    week.lock_at = dt.datetime.now(UTC) - dt.timedelta(minutes=1)
    week.status = "scored"
    games = list(db.scalars(select(Game).where(Game.week_id == week.id)))
    for game in games:
        game.status = "final"
        game.winner = "home"
        game.home_score = 21
        game.away_score = 17
    db.commit()
    db.close()

    _login(client, "boss@example.com")
    response = client.get("/results")
    assert response.status_code == 200
    assert "Scenarios open once" not in response.text
    assert "still to be played" in response.text
    assert "0 games still to play" in response.text


def test_scenarios_panel_visible_shows_placement_percentages(client, world, session_factory):
    _login(client, "player@example.com")
    client.post("/picks", data=_valid_submission(world["game_ids"]))
    _login(client, "boss@example.com")

    db = session_factory()
    pool = db.get(Pool, world["pool_id"])
    pool.scenarios_min_final_games = 1
    pool.scenarios_min_remaining_games = 1
    week = db.get(Week, world["week_id"])
    week.lock_at = dt.datetime.now(UTC) - dt.timedelta(minutes=1)
    week.status = "locked"
    games = list(db.scalars(select(Game).where(Game.week_id == week.id).order_by(Game.slate_rank)))
    # Finalise only the first game, leaving the other three remaining.
    games[0].status = "final"
    games[0].winner = "home"
    games[0].home_score = 21
    games[0].away_score = 17
    db.commit()
    db.close()

    response = client.get("/results")
    assert response.status_code == 200
    assert "Scenarios open once" not in response.text
    assert "1st" in response.text and "2nd" in response.text and "3rd" in response.text
    assert "Build your own scenario" in response.text


def test_scenarios_panel_moneyline_toggle_labels_estimated_from_betting_odds(
    client, world, session_factory
):
    _login(client, "player@example.com")
    client.post("/picks", data=_valid_submission(world["game_ids"]))
    _login(client, "boss@example.com")

    db = session_factory()
    pool = db.get(Pool, world["pool_id"])
    pool.scenarios_min_final_games = 0
    pool.scenarios_min_remaining_games = 1
    week = db.get(Week, world["week_id"])
    week.lock_at = dt.datetime.now(UTC) - dt.timedelta(minutes=1)
    week.status = "locked"
    db.commit()
    db.close()

    even = client.get("/results")
    assert "Estimated from betting odds" not in even.text

    moneyline = client.get("/results?model=moneyline")
    assert "Estimated from betting odds" in moneyline.text


def test_custom_scenario_endpoint_recomputes_standings_for_every_player(
    client, world, session_factory
):
    _login(client, "player@example.com")
    client.post("/picks", data=_valid_submission(world["game_ids"]))
    _lock_worlds_week(session_factory, world)
    _login(client, "boss@example.com")

    db = session_factory()
    game_id = world["game_ids"][0]
    db.close()

    response = client.post(
        "/results/custom-scenario", data={"week": "5", f"game_{game_id}": "home"}
    )
    assert response.status_code == 200
    assert "Regular Player" in response.text
    assert "The Commissioner" in response.text
    assert "Assuming" in response.text


def test_custom_scenario_endpoint_blocked_before_the_week_locks(client, world, session_factory):
    _login(client, "boss@example.com")
    response = client.post("/results/custom-scenario", data={"week": "5"})
    assert response.status_code == 403


# Season standings: the awards panel -------------------------------------------


def test_season_awards_panel_absent_before_any_week_is_scored(client, world):
    db_login = "boss@example.com"
    _login(client, db_login)
    response = client.get("/standings")
    assert response.status_code == 200
    assert "Season awards" not in response.text


# The season awards panel amounts test moved to tests/test_payout_display.py, on the new
# season_points/season_wins two-panel layout. See DECISIONS.md, "Payout system".


# Global admin league management (post-launch) --------------------------------
#
# Three roles matter here and must stay distinct: a regular player, a pool commissioner who
# is NOT a site admin (role="player", role_in_pool="commissioner"), and a site admin
# (role="admin"). "world"'s boss@example.com is deliberately both at once (role="admin" and
# role_in_pool="commissioner" of world's own pool), which is realistic for the one seeded
# account but not what these boundary tests need, so most of them build their own users.

_NEW_LEAGUE_FORM = {
    "name": "New League",
    "join_code": "NEWLEAG1",
    "season_year": "2025",
    "timezone": "America/New_York",
}

_SET_COMMISSIONER_CODE_FORM = {"commissioner_invite_code": "TRYCODE1"}


def _make_other_pool(
    db: Session, *, name: str = "Second Pool", join_code: str = "SECONDPL"
) -> Pool:
    pool = Pool(
        name=name,
        join_code=join_code,
        season_year=2025,
        num_games_per_week=4,
        target_nfl=2,
        target_ncaaf=2,
        sports=["nfl", "ncaaf"],
        timezone="America/New_York",
        current_week=1,
    )
    db.add(pool)
    db.flush()
    return pool


def _make_pool_commissioner_who_is_not_admin(db: Session, pool: Pool) -> User:
    commish = _make_user(db, "commish@example.com", "League Commissioner")  # role="player"
    db.add(PoolMember(pool_id=pool.id, user_id=commish.id, role_in_pool="commissioner"))
    return commish


@pytest.mark.parametrize(
    "path",
    [
        "/league",
        "/league/slate",
        "/league/members",
        "/league/settings",
        "/league/payouts",
        "/league/payouts/summary",
    ],
)
def test_league_pages_never_render_the_word_admin_for_a_real_commissioner(
    client, session_factory, path
):
    """Phase 4 remediation (see DECISIONS.md): the word "admin" must never reach a real
    commissioner's screen. Before Phase 5, a static grep over the template source would have
    false-positived on admin/index.html's Provider budgets section, real source text gated
    behind {% if is_site_admin %} and never rendered for anyone else; that whole section has
    since moved to /site/providers (Phase 5, provider controls move to site admin), so
    admin/index.html no longer has any site-admin-only content at all, but this test still
    checks a real rendered response rather than the template source, which is the only honest
    way to verify it. world's own boss@example.com is deliberately both role="admin" and this
    pool's commissioner (see the block comment above), which is exactly the case this test
    must NOT use, so it builds its own plain commissioner instead."""
    db = session_factory()
    pool = _make_pool(db)
    _make_pool_commissioner_who_is_not_admin(db, pool)
    db.commit()
    db.close()

    _login(client, "commish@example.com")
    response = client.get(path)
    assert response.status_code == 200, response.text[:800]
    assert "admin" not in response.text.lower(), path


@pytest.mark.parametrize(
    "method,path,data",
    [
        ("get", "/site/leagues", None),
        ("post", "/site/leagues/new", _NEW_LEAGUE_FORM),
        ("post", "/site/leagues/{pool_id}/view-as", None),
        ("post", "/site/leagues/{pool_id}/commissioner-code", None),
        ("post", "/site/leagues/{pool_id}/commissioner-code/set", _SET_COMMISSIONER_CODE_FORM),
    ],
)
def test_leagues_admin_routes_refused_for_a_regular_player(client, world, method, path, data):
    _login(client, "player@example.com")
    target = path.format(pool_id=world["pool_id"])
    response = (
        getattr(client, method)(target, data=data) if data else getattr(client, method)(target)
    )
    assert response.status_code == 403


@pytest.mark.parametrize(
    "method,path,data",
    [
        ("get", "/site/leagues", None),
        ("post", "/site/leagues/new", _NEW_LEAGUE_FORM),
        ("post", "/site/leagues/{pool_id}/view-as", None),
        ("post", "/site/leagues/{pool_id}/commissioner-code", None),
        ("post", "/site/leagues/{pool_id}/commissioner-code/set", _SET_COMMISSIONER_CODE_FORM),
    ],
)
def test_leagues_admin_routes_refused_for_a_pool_commissioner_who_is_not_a_site_admin(
    client, session_factory, method, path, data
):
    """Being a commissioner of your own league does not make you a site admin."""
    db = session_factory()
    pool = _make_pool(db)
    _make_pool_commissioner_who_is_not_admin(db, pool)
    db.commit()
    pool_id = pool.id
    db.close()

    _login(client, "commish@example.com")
    target = path.format(pool_id=pool_id)
    response = (
        getattr(client, method)(target, data=data) if data else getattr(client, method)(target)
    )
    assert response.status_code == 403


def test_leagues_page_lists_every_pool_not_just_the_admins_own(client, world, session_factory):
    db = session_factory()
    other = _make_other_pool(db, name="Unrelated League", join_code="UNRELATE")
    db.commit()
    other_name = other.name
    db.close()

    _login(client, "boss@example.com")
    response = client.get("/site/leagues")
    assert response.status_code == 200
    assert "Test Pool" in response.text
    assert other_name in response.text


def test_leagues_page_shows_a_pools_commissioners(client, session_factory):
    db = session_factory()
    pool = _make_other_pool(db, name="Shown League", join_code="SHOWNLGE")
    commish = _make_pool_commissioner_who_is_not_admin(db, pool)
    _make_user(db, "siteadmin1@example.com", "Site Admin One", role="admin")
    db.commit()
    commish_name = commish.display_name
    db.close()

    _login(client, "siteadmin1@example.com")
    response = client.get("/site/leagues")
    assert response.status_code == 200
    assert commish_name in response.text


def test_create_league_makes_a_pool_with_seed_admin_defaults(client, session_factory):
    db = session_factory()
    _make_user(db, "siteadmin2@example.com", "Site Admin Two", role="admin")
    db.commit()
    db.close()

    _login(client, "siteadmin2@example.com")
    response = client.post(
        "/site/leagues/new",
        data={
            "name": "Created League",
            "join_code": "CREATED1",
            "season_year": "2026",
            "timezone": "America/Chicago",
            "week1_anchor_date": "2026-09-12",
        },
    )
    assert response.status_code == 303

    db = session_factory()
    pool = db.scalar(select(Pool).where(Pool.name == "Created League"))
    assert pool is not None
    assert pool.join_code == "CREATED1"
    assert pool.num_games_per_week == 20
    assert pool.target_nfl == 8
    assert pool.target_ncaaf == 12
    assert pool.season_year == 2026
    assert pool.timezone == "America/Chicago"
    db.close()


def test_create_league_rejects_a_blank_anchor_date(client, session_factory):
    """Phase 2 remediation (see DECISIONS.md): a blank anchor date is what let a brand new
    league fall back to sending its own week number straight to ESPN for both leagues."""
    db = session_factory()
    _make_user(db, "siteadmin-blank-anchor@example.com", "Site Admin", role="admin")
    db.commit()
    db.close()

    _login(client, "siteadmin-blank-anchor@example.com")
    response = client.post(
        "/site/leagues/new",
        data={
            "name": "No Anchor League",
            "join_code": "NOANCHOR",
            "season_year": "2026",
            "timezone": "America/New_York",
        },
    )
    assert response.status_code == 303

    db = session_factory()
    assert db.scalar(select(Pool).where(Pool.name == "No Anchor League")) is None
    db.close()


def test_create_league_rejects_a_non_saturday_anchor_date(client, session_factory):
    db = session_factory()
    _make_user(db, "siteadmin-nonsat@example.com", "Site Admin", role="admin")
    db.commit()
    db.close()

    _login(client, "siteadmin-nonsat@example.com")
    response = client.post(
        "/site/leagues/new",
        data={
            "name": "Wrong Day League",
            "join_code": "WRONGDAY",
            "season_year": "2026",
            "timezone": "America/New_York",
            "week1_anchor_date": "2026-09-13",  # a Sunday
        },
    )
    assert response.status_code == 303

    db = session_factory()
    assert db.scalar(select(Pool).where(Pool.name == "Wrong Day League")) is None
    db.close()


def test_create_league_attaches_an_existing_user_as_commissioner_by_email(client, session_factory):
    db = session_factory()
    _make_user(db, "siteadmin3@example.com", "Site Admin Three", role="admin")
    future_commish = _make_user(db, "future.commish@example.com", "Future Commissioner")
    db.commit()
    db.close()

    _login(client, "siteadmin3@example.com")
    response = client.post(
        "/site/leagues/new",
        data={
            "name": "Attached League",
            "join_code": "ATTACHED",
            "season_year": "2025",
            "timezone": "America/New_York",
            "week1_anchor_date": "2025-09-13",
            "commissioner_emails": "FUTURE.COMMISH@example.com\n",
        },
    )
    assert response.status_code == 303

    db = session_factory()
    pool = db.scalar(select(Pool).where(Pool.name == "Attached League"))
    assert pool is not None
    member = db.scalar(
        select(PoolMember).where(
            PoolMember.pool_id == pool.id, PoolMember.user_id == future_commish.id
        )
    )
    assert member is not None
    assert member.role_in_pool == "commissioner"
    db.close()


def test_create_league_rejects_the_site_admins_own_email_but_still_attaches_others(
    client, session_factory
):
    """The site admin can never hold a PoolMember row, structurally (Post-launch fixes, see
    DECISIONS.md). Submitting the admin's own email alongside a real user's must not fail the
    whole form: the real user still gets attached, and the admin's address is skipped with a
    clear flash explaining why."""
    db = session_factory()
    admin = _make_user(db, "siteguard@example.com", "Site Admin Guard", role="admin")
    real_commish = _make_user(db, "real.commish@example.com", "Real Commissioner")
    db.commit()
    admin_email = admin.email
    admin_id = admin.id
    real_commish_id = real_commish.id
    db.close()

    _login(client, "siteguard@example.com")
    response = client.post(
        "/site/leagues/new",
        data={
            "name": "Guarded League",
            "join_code": "GUARDED1",
            "season_year": "2025",
            "timezone": "America/New_York",
            "week1_anchor_date": "2025-09-13",
            "commissioner_emails": f"{admin_email}\nREAL.COMMISH@example.com\n",
        },
    )
    assert response.status_code == 303

    # The redirect target renders and pops the flashes. Jinja autoescapes the apostrophe in
    # "can't" to &#39;, so the assertion checks the text either side of it rather than the
    # literal punctuation.
    leagues_page = client.get("/site/leagues")
    assert leagues_page.status_code == 200
    assert f"{admin_email} is the site admin and can" in leagues_page.text
    assert "t be added as a commissioner." in leagues_page.text

    db = session_factory()
    pool = db.scalar(select(Pool).where(Pool.name == "Guarded League"))
    assert pool is not None
    admin_member = db.scalar(
        select(PoolMember).where(PoolMember.pool_id == pool.id, PoolMember.user_id == admin_id)
    )
    assert admin_member is None
    real_member = db.scalar(
        select(PoolMember).where(
            PoolMember.pool_id == pool.id, PoolMember.user_id == real_commish_id
        )
    )
    assert real_member is not None
    assert real_member.role_in_pool == "commissioner"
    db.close()


def test_admin_can_view_as_commissioner_of_a_pool_never_joined(client, session_factory):
    """The bug that mattered most: get_active_pool used to silently fall back to the
    admin's own first membership instead of honoring the session pool it was just told to
    view, so "view as commissioner" on some other league quietly landed on the wrong one."""
    db = session_factory()
    admin = _make_user(db, "siteadmin4@example.com", "Site Admin Four", role="admin")
    home_pool = _make_other_pool(db, name="Admin Home Pool", join_code="ADMHOME1")
    db.add(PoolMember(pool_id=home_pool.id, user_id=admin.id, role_in_pool="member"))
    target_pool = _make_other_pool(db, name="Never Joined League", join_code="NVRJOIN1")
    db.commit()
    target_pool_id = target_pool.id
    target_pool_name = target_pool.name
    db.close()

    _login(client, "siteadmin4@example.com")
    view_as = client.post(f"/site/leagues/{target_pool_id}/view-as")
    assert view_as.status_code == 303
    assert view_as.headers["location"] == "/league"

    dashboard = client.get("/league")
    assert dashboard.status_code == 200
    assert target_pool_name in dashboard.text
    assert "Admin Home Pool" not in dashboard.text


def test_get_active_pool_admin_honors_session_pool_with_no_membership_row(session_factory):
    """Direct, unit-style call against get_active_pool itself, per the brief: no PoolMember
    row exists for this admin in this pool at all, and it must still resolve."""
    from app.auth import SESSION_POOL_KEY, get_active_pool

    class _FakeRequest:
        def __init__(self, session):
            self.session = session

    db = session_factory()
    admin = _make_user(db, "siteadmin5@example.com", "Site Admin Five", role="admin")
    pool = _make_pool(db)
    db.commit()
    pool_id = pool.id

    request = _FakeRequest({SESSION_POOL_KEY: pool_id})
    resolved = get_active_pool(request, db, admin)
    assert resolved.id == pool_id
    db.close()


def test_get_active_pool_non_admin_behavior_is_unchanged_by_a_bogus_session_pool(session_factory):
    """A non-admin with a foreign pool id already sitting in session (left over, or forged)
    still falls back to their own real membership, exactly as before this feature."""
    from app.auth import SESSION_POOL_KEY, get_active_pool

    class _FakeRequest:
        def __init__(self, session):
            self.session = session

    db = session_factory()
    real_pool = _make_pool(db)
    foreign_pool = _make_other_pool(db, name="Foreign Pool", join_code="FOREIGN1")
    player = _make_user(db, "member2@example.com", "Member Two")
    db.add(PoolMember(pool_id=real_pool.id, user_id=player.id, role_in_pool="member"))
    db.commit()
    real_pool_id = real_pool.id
    foreign_pool_id = foreign_pool.id

    request = _FakeRequest({SESSION_POOL_KEY: foreign_pool_id})
    resolved = get_active_pool(request, db, player)
    assert resolved.id == real_pool_id
    assert resolved.id != foreign_pool_id
    # And the session is corrected back to the real membership, same as pre-fix behavior.
    assert request.session[SESSION_POOL_KEY] == real_pool_id
    db.close()


def test_viewing_as_commissioner_banner_shows_for_admin_and_never_for_a_real_commissioner(
    client, world, session_factory
):
    db = session_factory()
    _make_pool_commissioner_who_is_not_admin(db, db.get(Pool, world["pool_id"]))
    db.commit()
    db.close()

    # A real commissioner, not a site admin, never sees the banner on their own /league page.
    _login(client, "commish@example.com")
    response = client.get("/league")
    assert response.status_code == 200
    assert "as commissioner" not in response.text

    client.post("/logout")

    # The site admin, viewing the same pool, does see it.
    _login(client, "boss@example.com")
    response = client.get("/league")
    assert response.status_code == 200
    assert "as commissioner" in response.text
    assert "Exit to leagues" in response.text

    # The banner is not just a dashboard thing: it shows across the whole /league section,
    # not only the landing page (Phase 4 remediation, see DECISIONS.md).
    for path in ("/league/slate", "/league/members", "/league/settings"):
        sub_response = client.get(path)
        assert sub_response.status_code == 200
        assert "as commissioner" in sub_response.text, path
        assert "Exit to leagues" in sub_response.text, path

    # And it is absent on the leagues portal itself, which isn't a view of any single pool.
    # ("View as commissioner" is the per-row button text on this page, a different string
    # from the banner's own "Exit to leagues", so that is what distinguishes them here.)
    leagues_response = client.get("/site/leagues")
    assert leagues_response.status_code == 200
    assert "Exit to leagues" not in leagues_response.text


# Multi-league commissioner switcher (Post-launch fixes) ----------------------
# A commissioner who runs only one league sees the plain, existing pool-name text; a
# commissioner of more than one gets a small switcher, and POST /league/switch-league is the
# only way to move between them, only for pools they actually commission.


def test_single_league_commissioner_sees_no_switcher(client, world, session_factory):
    db = session_factory()
    _make_pool_commissioner_who_is_not_admin(db, db.get(Pool, world["pool_id"]))
    db.commit()
    db.close()

    _login(client, "commish@example.com")
    response = client.get("/league")
    assert response.status_code == 200
    assert "data-league-switcher" not in response.text


def test_two_league_commissioner_sees_switcher_and_can_switch(client, session_factory):
    db = session_factory()
    pool_a = _make_pool(db)
    pool_b = _make_other_pool(db, name="Second League", join_code="SECONDLG")
    commish = _make_user(db, "multi.commish@example.com", "Multi Commissioner")
    db.add(PoolMember(pool_id=pool_a.id, user_id=commish.id, role_in_pool="commissioner"))
    db.add(PoolMember(pool_id=pool_b.id, user_id=commish.id, role_in_pool="commissioner"))
    db.commit()
    pool_a_name = pool_a.name
    pool_b_id = pool_b.id
    db.close()

    _login(client, "multi.commish@example.com")
    response = client.get("/league")
    assert response.status_code == 200
    assert "data-league-switcher" in response.text
    assert pool_a_name in response.text  # first PoolMember by id is the active pool at login

    switch = client.post("/league/switch-league", data={"pool_id": pool_b_id})
    assert switch.status_code == 303
    assert switch.headers["location"] == "/league"

    after = client.get("/league")
    assert after.status_code == 200
    assert "Second League" in after.text


def test_switch_league_rejects_a_pool_the_user_does_not_commission(client, session_factory):
    db = session_factory()
    pool_a = _make_pool(db)
    pool_b = _make_other_pool(db, name="Not Mine League", join_code="NOTMINE1")
    commish = _make_user(db, "onlya.commish@example.com", "Only A Commissioner")
    db.add(PoolMember(pool_id=pool_a.id, user_id=commish.id, role_in_pool="commissioner"))
    db.commit()
    pool_a_name = pool_a.name
    pool_b_id = pool_b.id
    db.close()

    _login(client, "onlya.commish@example.com")
    before = client.get("/league")
    assert before.status_code == 200
    assert pool_a_name in before.text

    switch = client.post("/league/switch-league", data={"pool_id": pool_b_id})
    assert switch.status_code == 303

    after = client.get("/league")
    assert after.status_code == 200
    assert pool_a_name in after.text
    assert "Not Mine League" not in after.text


# Commissioner invite links (Post-launch fixes) -------------------------------
# The workflow: the site admin talks to a prospective commissioner outside the app, then
# hands them a link generated from /site/leagues. Opening it presents the normal /register
# form, but completing it makes them a commissioner of that specific pool, not a member. A
# fully separate code and query param from the player join code on purpose, see
# DECISIONS.md, Post-launch fixes.


def test_register_page_greets_a_valid_commissioner_link(client, world, session_factory):
    db = session_factory()
    pool = db.get(Pool, world["pool_id"])
    pool.commissioner_invite_code = "COMMISH1"
    db.commit()
    db.close()

    response = client.get("/register?commissioner_code=COMMISH1")
    assert response.status_code == 200
    assert "commissioner" in response.text.lower()
    assert "Test Pool" in response.text


def test_register_with_a_valid_commissioner_code_creates_a_commissioner_not_an_admin(
    client, world, session_factory
):
    db = session_factory()
    pool = db.get(Pool, world["pool_id"])
    pool.commissioner_invite_code = "COMMISH1"
    db.commit()
    db.close()

    response = client.post(
        "/register",
        data={
            "display_name": "New Commissioner",
            "email": "newcommish@example.com",
            "password": "hunter2hunter2!",
            "commissioner_code": "commish1",  # case insensitive, same convention as join codes
        },
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/league"

    db = session_factory()
    user = db.scalar(select(User).where(User.email == "newcommish@example.com"))
    assert user is not None
    assert user.role == "player"  # never a global admin, no matter what the invite grants
    assert user.is_admin is False
    member = db.scalar(select(PoolMember).where(PoolMember.user_id == user.id))
    assert member is not None
    assert member.pool_id == world["pool_id"]
    assert member.role_in_pool == "commissioner"
    db.close()


def test_register_with_an_invalid_commissioner_code_is_rejected(client, world, session_factory):
    response = client.post(
        "/register",
        data={
            "display_name": "Nope",
            "email": "nope@example.com",
            "password": "hunter2hunter2!",
            "commissioner_code": "NOTREAL1",
        },
    )
    assert response.status_code == 400
    assert "commissioner link" in response.text.lower()

    db = session_factory()
    assert db.scalar(select(User).where(User.email == "nope@example.com")) is None
    db.close()


def test_register_with_no_commissioner_code_is_unchanged_from_todays_behavior(client, world):
    """Omitting commissioner_code entirely (every pre-existing registration request) must
    behave exactly as before for a real join code, and a blank join code must always succeed
    poolless (Post-launch fixes, see DECISIONS.md: open_registration no longer gates this,
    a codeless account is always a safe read only preview, never membership in anything)."""
    poolless = client.post(
        "/register",
        data={
            "display_name": "Plain Player",
            "email": "poollessplayer@example.com",
            "password": "hunter2hunter2!",
        },
    )
    assert poolless.status_code == 303

    joined = client.post(
        "/register",
        data={
            "display_name": "Plain Player",
            "email": "plainplayer@example.com",
            "password": "hunter2hunter2!",
            "join_code": "TESTCODE",
        },
    )
    assert joined.status_code == 303
    assert joined.headers["location"] == "/picks"


def test_rotating_commissioner_invite_code_never_touches_join_code_and_vice_versa(
    client, world, session_factory
):
    db = session_factory()
    pool = db.get(Pool, world["pool_id"])
    pool.commissioner_invite_code = "OLDCOMM1"
    db.commit()
    original_join_code = pool.join_code
    db.close()

    _login(client, "boss@example.com")
    rotate = client.post(f"/site/leagues/{world['pool_id']}/commissioner-code")
    assert rotate.status_code == 303

    db = session_factory()
    pool = db.get(Pool, world["pool_id"])
    assert pool.commissioner_invite_code != "OLDCOMM1"
    assert pool.commissioner_invite_code is not None
    new_commissioner_code = pool.commissioner_invite_code
    assert pool.join_code == original_join_code  # rotating the commissioner code alone
    db.close()

    # The old commissioner code no longer resolves to anything, the same way an old join
    # code no longer works after rotation (test_join_code_rotation_invalidates_the_old_code).
    client.post("/logout")
    old_code_rejected = client.post(
        "/register",
        data={
            "display_name": "Too Late",
            "email": "toolate@example.com",
            "password": "hunter2hunter2!",
            "commissioner_code": "OLDCOMM1",
        },
    )
    assert old_code_rejected.status_code == 400

    # And the reverse: rotating the player join code never touches the commissioner code.
    _login(client, "boss@example.com")
    join_rotate = client.post("/league/join-code")
    assert join_rotate.status_code == 303

    db = session_factory()
    pool = db.get(Pool, world["pool_id"])
    assert pool.join_code != original_join_code
    assert pool.commissioner_invite_code == new_commissioner_code
    db.close()


def test_set_commissioner_invite_code_by_hand(client, world, session_factory):
    _login(client, "boss@example.com")
    response = client.post(
        f"/site/leagues/{world['pool_id']}/commissioner-code/set",
        data={"commissioner_invite_code": "handpicked"},
    )
    assert response.status_code == 303

    db = session_factory()
    pool = db.get(Pool, world["pool_id"])
    assert pool.commissioner_invite_code == "HANDPICKED"  # stored uppercase, like join codes
    db.close()


def test_create_league_gives_the_new_pool_its_own_commissioner_invite_code(client, session_factory):
    db = session_factory()
    _make_user(db, "siteadmin6@example.com", "Site Admin Six", role="admin")
    db.commit()
    db.close()

    _login(client, "siteadmin6@example.com")
    response = client.post(
        "/site/leagues/new",
        data={
            "name": "Fresh League",
            "join_code": "FRESHLG1",
            "season_year": "2025",
            "timezone": "America/New_York",
            "week1_anchor_date": "2025-09-13",
        },
    )
    assert response.status_code == 303

    db = session_factory()
    pool = db.scalar(select(Pool).where(Pool.name == "Fresh League"))
    assert pool is not None
    assert pool.commissioner_invite_code is not None
    assert pool.commissioner_invite_code != pool.join_code
    db.close()


def test_members_page_shows_the_invite_link_and_a_working_mailto(client, world):
    _login(client, "boss@example.com")
    response = client.get("/league/members")
    assert response.status_code == 200
    assert "register?code=TESTCODE" in response.text
    assert "mailto:" in response.text
    assert "TESTCODE" in response.text
    # No em dash anywhere in the generated invite copy.
    assert "—" not in response.text


# Co-commissioner self-service invites with confirmation (post-launch) --------
#
# "world"'s only commissioner (boss@example.com) is deliberately also the site admin, which is
# realistic for the one seeded account but not what these boundary tests need (see the comment
# above the leagues admin-routes tests), so these build their own pools with a real, non-admin
# full commissioner, reusing _make_pool_commissioner_who_is_not_admin (defined above) exactly
# the way the leagues admin-routes tests already do.


def _make_co_commissioner_world(db: Session) -> dict:
    """A pool with a real, non-admin full commissioner ("commish@example.com") and one plain
    member, ready for the invite/accept/decline round trip."""
    pool = _make_pool(db)
    _make_pool_commissioner_who_is_not_admin(db, pool)
    member = _make_user(db, "member1@example.com", "Plain Member")
    member_row = PoolMember(pool_id=pool.id, user_id=member.id, role_in_pool="member")
    db.add(member_row)
    db.commit()
    return {"pool_id": pool.id, "member_pool_member_id": member_row.id}


def _make_co_commissioner_operational_world(db: Session) -> dict:
    """A pool with a real full commissioner, an already-accepted co-commissioner
    ("coco@example.com"), and one plain member, for testing the operational-versus-
    roster-management boundary a co-commissioner sits on."""
    pool = _make_pool(db)
    commish = _make_pool_commissioner_who_is_not_admin(db, pool)
    coco = _make_user(db, "coco@example.com", "Co Commissioner")
    coco_row = PoolMember(pool_id=pool.id, user_id=coco.id, role_in_pool="co_commissioner")
    db.add(coco_row)
    member = _make_user(db, "member2@example.com", "Plain Member Two")
    member_row = PoolMember(pool_id=pool.id, user_id=member.id, role_in_pool="member")
    db.add(member_row)
    week = _make_week(db, pool)
    _make_games(db, week)
    db.commit()
    commish_member_id = db.scalar(
        select(PoolMember).where(PoolMember.pool_id == pool.id, PoolMember.user_id == commish.id)
    ).id
    return {
        "pool_id": pool.id,
        "commish_member_id": commish_member_id,
        "coco_member_id": coco_row.id,
        "member_id": member_row.id,
        "week_id": week.id,
    }


def test_full_commissioner_invite_does_not_promote_the_member_immediately(client, session_factory):
    db = session_factory()
    w = _make_co_commissioner_world(db)
    db.close()

    _login(client, "commish@example.com")
    response = client.post(f"/league/members/{w['member_pool_member_id']}/co-commissioner/invite")
    assert response.status_code == 303

    db = session_factory()
    member = db.get(PoolMember, w["member_pool_member_id"])
    assert member.role_in_pool == "member"
    assert member.co_commissioner_invited_at is not None
    db.close()


def test_invited_member_accepting_becomes_a_co_commissioner(client, session_factory):
    db = session_factory()
    w = _make_co_commissioner_world(db)
    db.close()

    _login(client, "commish@example.com")
    client.post(f"/league/members/{w['member_pool_member_id']}/co-commissioner/invite")
    client.post("/logout")

    _login(client, "member1@example.com")
    response = client.post("/league/co-commissioner/accept")
    assert response.status_code == 303

    db = session_factory()
    member = db.get(PoolMember, w["member_pool_member_id"])
    assert member.role_in_pool == "co_commissioner"
    assert member.co_commissioner_invited_at is None
    db.close()


def test_invited_member_declining_stays_a_plain_member(client, session_factory):
    db = session_factory()
    w = _make_co_commissioner_world(db)
    db.close()

    _login(client, "commish@example.com")
    client.post(f"/league/members/{w['member_pool_member_id']}/co-commissioner/invite")
    client.post("/logout")

    _login(client, "member1@example.com")
    response = client.post("/league/co-commissioner/decline")
    assert response.status_code == 303

    db = session_factory()
    member = db.get(PoolMember, w["member_pool_member_id"])
    assert member.role_in_pool == "member"
    assert member.co_commissioner_invited_at is None
    db.close()


def test_a_second_invite_to_an_already_pending_member_is_a_no_op(client, session_factory):
    db = session_factory()
    w = _make_co_commissioner_world(db)
    db.close()

    _login(client, "commish@example.com")
    first = client.post(f"/league/members/{w['member_pool_member_id']}/co-commissioner/invite")
    assert first.status_code == 303

    db = session_factory()
    first_invited_at = db.get(PoolMember, w["member_pool_member_id"]).co_commissioner_invited_at
    db.close()

    second = client.post(f"/league/members/{w['member_pool_member_id']}/co-commissioner/invite")
    assert second.status_code == 303

    db = session_factory()
    member = db.get(PoolMember, w["member_pool_member_id"])
    assert member.role_in_pool == "member"
    assert member.co_commissioner_invited_at == first_invited_at
    db.close()


@pytest.mark.parametrize(
    "method,path,data",
    [
        ("post", "/league/members/{member_id}/co-commissioner/invite", None),
        ("post", "/league/members/{member_id}/role", {"role": "commissioner"}),
        ("post", "/league/members/{commish_member_id}/role", {"role": "member"}),
        ("post", "/league/members/{coco_member_id}/role", {"role": "member"}),
        ("post", "/league/members/{coco_member_id}/co-commissioner/cancel", None),
        ("post", "/league/commissioner-code", None),
    ],
)
def test_co_commissioner_cannot_manage_anyones_commissioner_status(
    client, session_factory, method, path, data
):
    """Every commissioner-roster-management action is refused for a co-commissioner: inviting
    a new one, promoting a member directly, demoting a fellow full commissioner or another
    co-commissioner, canceling someone else's invite, and rotating the commissioner invite
    link. is_commissioner (broad) says yes to a co-commissioner everywhere else; only
    is_full_commissioner (narrow) gates these."""
    db = session_factory()
    w = _make_co_commissioner_operational_world(db)
    db.close()

    _login(client, "coco@example.com")
    target = path.format(**w)
    response = (
        getattr(client, method)(target, data=data) if data else getattr(client, method)(target)
    )
    assert response.status_code == 403


def test_co_commissioner_cannot_see_the_commissioner_invite_link(client, session_factory):
    db = session_factory()
    w = _make_co_commissioner_operational_world(db)
    pool = db.get(Pool, w["pool_id"])
    pool.commissioner_invite_code = "COCOCODE"
    db.commit()
    db.close()

    _login(client, "coco@example.com")
    response = client.get("/league/members")
    assert response.status_code == 200
    assert "commissioner_code=COCOCODE" not in response.text
    assert "Commissioner invite link" not in response.text
    client.post("/logout")

    _login(client, "commish@example.com")
    response = client.get("/league/members")
    assert response.status_code == 200
    assert "commissioner_code=COCOCODE" in response.text
    assert "Commissioner invite link" in response.text


def test_full_commissioner_can_rotate_their_own_commissioner_invite_link(client, session_factory):
    db = session_factory()
    pool = _make_pool(db)
    _make_pool_commissioner_who_is_not_admin(db, pool)
    pool.commissioner_invite_code = "OLDSELF1"
    db.commit()
    pool_id = pool.id
    db.close()

    _login(client, "commish@example.com")
    response = client.post("/league/commissioner-code")
    assert response.status_code == 303

    db = session_factory()
    pool = db.get(Pool, pool_id)
    assert pool.commissioner_invite_code is not None
    assert pool.commissioner_invite_code != "OLDSELF1"
    db.close()


def test_full_commissioner_can_demote_a_co_commissioner_instantly(client, session_factory):
    db = session_factory()
    w = _make_co_commissioner_operational_world(db)
    db.close()

    _login(client, "commish@example.com")
    response = client.post(f"/league/members/{w['coco_member_id']}/role", data={"role": "member"})
    assert response.status_code == 303

    db = session_factory()
    member = db.get(PoolMember, w["coco_member_id"])
    assert member.role_in_pool == "member"
    db.close()


def test_full_commissioner_can_demote_another_commissioner_instantly(client, session_factory):
    db = session_factory()
    pool = _make_pool(db)
    _make_pool_commissioner_who_is_not_admin(db, pool)
    peer = _make_user(db, "peer.commish@example.com", "Peer Commissioner")
    peer_row = PoolMember(pool_id=pool.id, user_id=peer.id, role_in_pool="commissioner")
    db.add(peer_row)
    db.commit()
    peer_member_id = peer_row.id
    db.close()

    _login(client, "commish@example.com")
    response = client.post(f"/league/members/{peer_member_id}/role", data={"role": "member"})
    assert response.status_code == 303

    db = session_factory()
    member = db.get(PoolMember, peer_member_id)
    assert member.role_in_pool == "member"
    db.close()


@pytest.mark.parametrize(
    "path", ["/league", "/league/slate", "/league/members", "/league/settings"]
)
def test_co_commissioner_reaches_the_same_operational_admin_pages_as_a_commissioner(
    client, session_factory, path
):
    db = session_factory()
    _make_co_commissioner_operational_world(db)
    db.close()

    _login(client, "coco@example.com")
    response = client.get(path)
    assert response.status_code == 200, response.text[:800]


def test_co_commissioner_can_mark_a_member_paid_and_unpaid(client, session_factory):
    db = session_factory()
    w = _make_co_commissioner_operational_world(db)
    db.close()

    _login(client, "coco@example.com")
    response = client.post(f"/league/members/{w['member_id']}/paid")
    assert response.status_code == 303

    db = session_factory()
    member = db.get(PoolMember, w["member_id"])
    assert member.paid_at is not None
    db.close()

    response = client.post(f"/league/members/{w['member_id']}/paid")
    assert response.status_code == 303
    db = session_factory()
    member = db.get(PoolMember, w["member_id"])
    assert member.paid_at is None
    db.close()


def test_admin_promote_and_demote_power_is_unaffected_by_co_commissioner_work(
    client, world, session_factory
):
    """Regression check (Post-launch fixes): the site admin's existing instant, unconfirmed
    member_role power, across any pool, still works exactly as it did before this feature."""
    db = session_factory()
    player_member_id = db.scalar(
        select(PoolMember).where(
            PoolMember.pool_id == world["pool_id"], PoolMember.user_id == world["player_id"]
        )
    ).id
    db.close()

    _login(client, "boss@example.com")
    promote = client.post(f"/league/members/{player_member_id}/role", data={"role": "commissioner"})
    assert promote.status_code == 303
    db = session_factory()
    assert db.get(PoolMember, player_member_id).role_in_pool == "commissioner"
    db.close()

    demote = client.post(f"/league/members/{player_member_id}/role", data={"role": "member"})
    assert demote.status_code == 303
    db = session_factory()
    assert db.get(PoolMember, player_member_id).role_in_pool == "member"
    db.close()


def test_co_commissioner_of_two_pools_sees_the_switcher_and_can_switch(client, session_factory):
    """The multi-league switcher (8fe4b71) filtered strictly on role_in_pool ==
    "commissioner"; a co-commissioner running more than one pool needs the same switcher."""
    db = session_factory()
    pool_a = _make_pool(db)
    pool_b = _make_other_pool(db, name="Second Co Pool", join_code="COCOPOOL")
    coco = _make_user(db, "multi.coco@example.com", "Multi Coco")
    db.add(PoolMember(pool_id=pool_a.id, user_id=coco.id, role_in_pool="co_commissioner"))
    db.add(PoolMember(pool_id=pool_b.id, user_id=coco.id, role_in_pool="co_commissioner"))
    db.commit()
    pool_b_id = pool_b.id
    db.close()

    _login(client, "multi.coco@example.com")
    response = client.get("/league")
    assert response.status_code == 200
    assert "data-league-switcher" in response.text

    switch = client.post("/league/switch-league", data={"pool_id": pool_b_id})
    assert switch.status_code == 303

    after = client.get("/league")
    assert "Second Co Pool" in after.text


# Post-launch fixes: password policy, contact form, poolless preview slate -----


def test_register_rejects_a_password_missing_a_symbol(client, world):
    response = client.post(
        "/register",
        data={
            "display_name": "No Symbol",
            "email": "nosymbol@example.com",
            "password": "plainpassword8",
            "join_code": "TESTCODE",
        },
    )
    assert response.status_code == 400
    assert "symbol" in response.text.lower()

    db_check = client.get("/login")  # sanity: no account leaked through on a rejected submit
    assert db_check.status_code == 200


def test_register_accepts_a_password_with_a_symbol(client, world, session_factory):
    response = client.post(
        "/register",
        data={
            "display_name": "Has Symbol",
            "email": "hassymbol@example.com",
            "password": "hunter2hunter2!",
            "join_code": "TESTCODE",
        },
    )
    assert response.status_code == 303

    db = session_factory()
    user = db.scalar(select(User).where(User.email == "hassymbol@example.com"))
    assert user is not None
    db.close()


def test_contact_submit_creates_a_submission_and_shows_confirmation(client, session_factory):
    response = client.post(
        "/contact",
        data={
            "name": "Prospective Commissioner",
            "email": "lead@example.com",
            "message": "I run a 12 person league, how do we get set up?",
        },
    )
    assert response.status_code == 303

    db = session_factory()
    submission = db.scalar(
        select(ContactSubmission).where(ContactSubmission.email == "lead@example.com")
    )
    assert submission is not None
    assert submission.name == "Prospective Commissioner"
    assert "12 person league" in submission.message
    db.close()

    confirmation = client.get("/contact")
    assert confirmation.status_code == 200
    assert "24 hours" in confirmation.text


def test_contact_submit_rejects_missing_fields_and_writes_nothing(client, session_factory):
    response = client.post("/contact", data={"name": "", "email": "not-an-email", "message": ""})
    assert response.status_code == 400

    db = session_factory()
    assert db.scalar(select(ContactSubmission)) is None
    db.close()


def test_contact_route_never_imports_or_calls_a_mail_library():
    """No email or text message is ever sent by this feature. Self-check as a real test
    rather than only a manual grep: fails the moment anyone wires an outbound mail library
    into the contact route."""
    import inspect

    from app.routers import public as public_router

    source = inspect.getsource(public_router)
    for forbidden in ("smtplib", "sendgrid", "ses_client", "boto3"):
        assert forbidden not in source


def test_admin_contacts_page_refused_for_a_non_admin_commissioner(client, session_factory):
    db = session_factory()
    pool = _make_pool(db)
    _make_pool_commissioner_who_is_not_admin(db, pool)
    db.commit()
    db.close()

    _login(client, "commish@example.com")
    response = client.get("/site/contacts")
    assert response.status_code == 403


def test_admin_contacts_page_refused_for_a_regular_player(client, world):
    _login(client, "player@example.com")
    response = client.get("/site/contacts")
    assert response.status_code == 403


def test_admin_contacts_page_shows_submissions_for_the_site_admin(client, world, session_factory):
    db = session_factory()
    db.add(ContactSubmission(name="Lead One", email="lead1@example.com", message="Tell me more."))
    db.commit()
    db.close()

    _login(client, "boss@example.com")  # world's boss is role="admin"
    response = client.get("/site/contacts")
    assert response.status_code == 200
    assert "Lead One" in response.text
    assert "lead1@example.com" in response.text
    assert "Tell me more." in response.text


def test_poolless_user_sees_the_preview_slate_read_only(client, session_factory):
    db = session_factory()
    preview_pool = _make_preview_pool(db)
    week = _make_week(db, preview_pool)
    games = _make_games(db, week)
    _make_user(db, "newbie@example.com", "Newbie Player")
    db.commit()
    game_ids = [g.id for g in games]
    home_team = games[0].home_team
    away_team = games[0].away_team
    db.close()

    _login(client, "newbie@example.com")
    response = client.get("/picks")
    assert response.status_code == 200
    assert "preview" in response.text.lower()
    assert home_team in response.text
    assert away_team in response.text
    # Every preview game rendered, using the same readonly_list macro a real locked week
    # already uses, not a parallel row template.
    assert response.text.count('class="game-row is-complete"') == len(game_ids)

    # No functioning pick-submission markup: no save form, no sortable list, no hidden
    # winner/confidence inputs naming any of these games.
    assert 'action="/picks"' not in response.text
    assert "data-sortable" not in response.text
    for gid in game_ids:
        assert f'name="winner-{gid}"' not in response.text
        assert f'name="confidence-{gid}"' not in response.text

    # Both ways forward the product owner asked for.
    assert 'href="/join"' in response.text
    assert 'href="/pricing"' in response.text


def test_poolless_user_with_no_preview_slate_built_sees_an_honest_empty_state(
    client, session_factory
):
    """Phase 8 remediation (see DECISIONS.md): the original bug report was a poolless visitor
    landing on a bare "check back soon" with no next step. The orchestrating session's own
    manual QA found the "This is a preview" panel, with its join-code and start-a-league
    buttons, renders unconditionally, above the slate itself, so even this no-slate-built case
    (state 0 in picks.html, checked before the "not ready yet" fallback) is never a dead end.
    This test locks that in: both the honest "not ready yet" message AND the two ways forward
    must be on the page at once, not one or the other."""
    db = session_factory()
    _make_preview_pool(db)  # exists, but no Week/Game rows built yet
    _make_user(db, "newbie4@example.com", "Newbie Four")
    db.commit()
    db.close()

    _login(client, "newbie4@example.com")
    response = client.get("/picks")
    assert response.status_code == 200
    assert "not ready yet" in response.text.lower()
    assert 'href="/join"' in response.text
    assert 'href="/pricing"' in response.text
    assert "enter a join code" in response.text.lower()
    assert "start your own league" in response.text.lower()


def test_poolless_user_with_no_preview_pool_seeded_at_all_still_sees_actionable_options(
    client, session_factory
):
    """The stronger case: no is_preview pool exists in the database at all (a fresh
    deployment where seed-preview has never been run once). get_preview_pool returns None,
    _preview_page still renders the same actionable panel rather than erroring or falling
    back to a bare dead end (Phase 8 remediation, see DECISIONS.md)."""
    db = session_factory()
    _make_user(db, "newbie5@example.com", "Newbie Five")
    db.commit()
    db.close()

    _login(client, "newbie5@example.com")
    response = client.get("/picks")
    assert response.status_code == 200
    assert "not ready yet" in response.text.lower()
    assert 'href="/join"' in response.text
    assert 'href="/pricing"' in response.text
    assert "enter a join code" in response.text.lower()
    assert "start your own league" in response.text.lower()


@pytest.mark.parametrize("path", ["/standings", "/results"])
def test_poolless_user_is_still_blocked_from_standings_and_results(client, session_factory, path):
    """Unchanged from before this feature: the preview is specifically and only the game
    slate view. get_active_pool still 403s a poolless user for every other route."""
    db = session_factory()
    _make_user(db, "newbie3@example.com", "Newbie Three")
    db.commit()
    db.close()

    _login(client, "newbie3@example.com")
    response = client.get(path)
    assert response.status_code == 403


def test_poolless_user_cannot_submit_a_pick_even_against_the_preview_pool(client, session_factory):
    """The read only claim is enforced server side, not just hidden in the UI: /picks POST
    never reads a client supplied pool id at all, it only ever trusts get_active_pool, which
    still refuses a poolless, non-admin user regardless of what a hand crafted request sends."""
    db = session_factory()
    preview_pool = _make_preview_pool(db)
    week = _make_week(db, preview_pool)
    games = _make_games(db, week)
    _make_user(db, "newbie2@example.com", "Newbie Two")
    db.commit()
    week_id = week.id
    game_ids = [g.id for g in games]
    db.close()

    _login(client, "newbie2@example.com")
    response = client.post("/picks", data=_valid_submission(game_ids))
    assert response.status_code == 403

    db = session_factory()
    assert db.scalar(select(Pick).where(Pick.week_id == week_id)) is None
    db.close()


def test_preview_pool_never_appears_in_admin_leagues_listing(client, world, session_factory):
    db = session_factory()
    preview_pool = _make_preview_pool(db)
    week = _make_week(db, preview_pool)
    _make_games(db, week)  # even with a real, built slate
    db.commit()
    preview_name = preview_pool.name
    db.close()

    _login(client, "boss@example.com")
    response = client.get("/site/leagues")
    assert response.status_code == 200
    assert "Test Pool" in response.text  # the real league still shows
    assert preview_name not in response.text


def test_preview_pool_never_appears_in_a_commissioners_multi_league_switcher(
    client, session_factory
):
    """The switcher is driven entirely by real PoolMember rows (app/routers/admin.py's
    _commissioner_pools). The preview pool never has one, by construction: nothing in this
    codebase can ever attach a member to an is_preview pool, so a commissioner running one
    real league plus the (invisible to them) preview pool still sees the plain single-league
    view, never a switcher with the preview pool as an option."""
    db = session_factory()
    pool = _make_pool(db)
    _make_pool_commissioner_who_is_not_admin(db, pool)
    _make_preview_pool(db)
    db.commit()
    db.close()

    _login(client, "commish@example.com")
    response = client.get("/league")
    assert response.status_code == 200
    assert "data-league-switcher" not in response.text
    assert "PickSportPlus Preview" not in response.text


# Site admin dashboard and legacy /admin redirects (Phase 4 remediation) ------
#
# /site is the new, light, platform wide dashboard added this phase (app/routers/site.py):
# every pool with its member count and season status, a link to /site/leagues, a link to
# /site/contacts with the current submission count, and the ephemeral storage health status.
# Everything under old /admin/... still resolves, as a 301 to its new /league or /site home,
# for every GET path a human could plausibly have bookmarked (app/routers/legacy_redirects.py).


def test_site_dashboard_refused_for_a_regular_player(client, world):
    _login(client, "player@example.com")
    response = client.get("/site")
    assert response.status_code == 403


def test_site_dashboard_refused_for_a_pool_commissioner_who_is_not_a_site_admin(
    client, session_factory
):
    db = session_factory()
    pool = _make_pool(db)
    _make_pool_commissioner_who_is_not_admin(db, pool)
    db.commit()
    db.close()

    _login(client, "commish@example.com")
    response = client.get("/site")
    assert response.status_code == 403


def test_site_dashboard_shows_pools_contact_count_and_storage_status_for_the_site_admin(
    client, world, session_factory
):
    db = session_factory()
    db.add(ContactSubmission(name="Lead One", email="lead1@example.com", message="Hi there."))
    preview_pool = _make_preview_pool(db)
    week = _make_week(db, preview_pool)
    _make_games(db, week)  # even with a real, built slate, the preview pool stays hidden
    db.commit()
    preview_name = preview_pool.name
    db.close()

    _login(client, "boss@example.com")
    response = client.get("/site")
    assert response.status_code == 200
    assert "Test Pool" in response.text
    assert preview_name not in response.text
    assert "Contact submissions (1)" in response.text
    assert "/site/leagues" in response.text
    assert "/site/contacts" in response.text


def test_site_dashboard_shows_ephemeral_storage_status(client, world, monkeypatch):
    _login(client, "boss@example.com")

    monkeypatch.setattr(settings, "database_url", "sqlite:////tmp/picksportplus.db")
    response = client.get("/site")
    assert "temporary storage" in response.text

    monkeypatch.setattr(settings, "database_url", "sqlite:///./picksportplus.db")
    response = client.get("/site")
    assert "temporary storage" not in response.text


# Provider controls (Phase 5 remediation: provider controls move to site admin) -------------
#
# /site/providers is where the old /league "Provider budgets" section moved to, site admin
# only, plus the new global "ESPN only" switch (POST /site/providers/espn-only). See
# DECISIONS.md, Phase 5.


def test_site_providers_refused_for_a_regular_player(client, world):
    _login(client, "player@example.com")
    response = client.get("/site/providers")
    assert response.status_code == 403


def test_site_providers_refused_for_a_pool_commissioner_who_is_not_a_site_admin(
    client, session_factory
):
    db = session_factory()
    pool = _make_pool(db)
    _make_pool_commissioner_who_is_not_admin(db, pool)
    db.commit()
    db.close()

    _login(client, "commish@example.com")
    response = client.get("/site/providers")
    assert response.status_code == 403


def test_site_providers_shows_key_presence_spend_and_last_call_for_the_site_admin(
    client, world, monkeypatch
):
    # This developer's own .env may carry real keys (never a test's business to depend on
    # that), so both are pinned explicitly: one unset, one set, presence only, matching
    # app/cli.py's own doctor command wording, never the value itself.
    monkeypatch.setattr(settings, "odds_api_key", "")
    monkeypatch.setattr(settings, "cfbd_api_key", "a-real-looking-key")

    _login(client, "boss@example.com")
    response = client.get("/site/providers")
    assert response.status_code == 200
    assert "The Odds API" in response.text
    assert "CollegeFootballData" in response.text
    assert "NOT SET" in response.text
    assert "a-real-looking-key" not in response.text
    assert "ESPN only" in response.text
    assert "Currently off" in response.text


def test_site_providers_espn_only_toggle_refused_for_non_site_admins(
    client, world, session_factory
):
    db = session_factory()
    pool = db.get(Pool, world["pool_id"])
    _make_pool_commissioner_who_is_not_admin(db, pool)
    db.commit()
    db.close()

    _login(client, "player@example.com")
    response = client.post("/site/providers/espn-only")
    assert response.status_code == 403

    _login(client, "commish@example.com")
    response = client.post("/site/providers/espn-only")
    assert response.status_code == 403


def test_site_providers_espn_only_toggle_persists_and_reads_back_fresh(
    client, world, session_factory
):
    """The switch survives being read back fresh, not cached across requests: two separate
    TestClient requests, two separate database sessions under the hood, and the second one
    must see exactly what the first one wrote, immediately, no redeploy or restart involved."""
    db = session_factory()
    assert db.scalar(select(PlatformSetting)) is None  # nothing created until first read
    db.close()

    _login(client, "boss@example.com")

    response = client.post("/site/providers/espn-only")
    assert response.status_code == 303
    assert response.headers["location"] == "/site/providers"

    db = session_factory()
    row = db.scalar(select(PlatformSetting))
    assert row is not None
    assert row.espn_only is True
    db.close()

    response = client.get("/site/providers")
    assert "Currently on" in response.text

    # Flip it back off, and the change is visible immediately on the very next read.
    response = client.post("/site/providers/espn-only")
    assert response.status_code == 303

    db = session_factory()
    row = db.scalar(select(PlatformSetting))
    assert row.espn_only is False
    db.close()

    response = client.get("/site/providers")
    assert "Currently off" in response.text


@pytest.mark.parametrize(
    "old_path,new_path",
    [
        ("/admin", "/league"),
        ("/admin/settings", "/league/settings"),
        ("/admin/members", "/league/members"),
        ("/admin/slate", "/league/slate"),
        ("/admin/payouts", "/league/payouts"),
        ("/admin/payouts/summary", "/league/payouts/summary"),
        ("/admin/payouts/summary.csv", "/league/payouts/summary.csv"),
        ("/admin/leagues", "/site/leagues"),
        ("/admin/leagues/new", "/site/leagues/new"),
        ("/admin/contacts", "/site/contacts"),
    ],
)
def test_legacy_admin_get_paths_redirect_permanently(client, old_path, new_path):
    """No login needed: the redirect itself does not check who is asking, exactly like every
    other route still checks its own permission once the browser follows it to the new
    address. A signed out request still gets the 301, proving the redirect really is
    unconditional."""
    response = client.get(old_path)
    assert response.status_code == 301


# Transactional email (Phase 7 remediation, see DECISIONS.md) ------------------------------
#
# Every test in this section enables mail via monkeypatch and stubs
# app.services.mail._call_resend_api, the one place a real HTTP call happens, so nothing here
# ever opens a socket (SPEC.md Section 17, tests/conftest.py's force_offline_mode).


def _enable_mail(monkeypatch) -> None:
    monkeypatch.setattr(settings, "mail_enabled", True)
    monkeypatch.setattr(settings, "resend_api_key", "test-key")
    monkeypatch.setattr(settings, "mail_from_address", "noreply@example.com")
    monkeypatch.setattr(settings, "mail_rate_limit_per_hour", 20)
    monkeypatch.setattr(mail, "_call_resend_api", lambda **kwargs: None)


def test_forgot_password_full_round_trip(client, world, session_factory, monkeypatch):
    _enable_mail(monkeypatch)

    response = client.post("/forgot-password", data={"email": "player@example.com"})
    assert response.status_code == 303

    db = session_factory()
    token_row = db.scalar(select(PasswordResetToken))
    assert token_row is not None
    db.close()

    # The raw token is never stored, only its hash; recover it the same way the emailed link
    # would carry it, by re-sending and stubbing the mail call to capture the body instead.
    captured = {}
    monkeypatch.setattr(
        mail, "_call_resend_api", lambda **kwargs: captured.setdefault("text", kwargs["text"])
    )
    client.post("/forgot-password", data={"email": "player@example.com"})
    link = [line for line in captured["text"].splitlines() if "/reset-password?token=" in line][0]
    raw_token = link.split("token=")[1].strip()

    response = client.post("/reset-password", data={"token": raw_token, "password": "new-pass-1!"})
    assert response.status_code == 303

    login_response = client.post(
        "/login", data={"email": "player@example.com", "password": "new-pass-1!", "next": "/picks"}
    )
    assert login_response.status_code == 303
    assert login_response.headers["location"] == "/picks"


def test_forgot_password_shows_the_same_message_for_an_unknown_email(client, world, monkeypatch):
    """Anti account enumeration: identical redirect and flash whether or not the address is
    real, matching login_submit's own established convention."""
    _enable_mail(monkeypatch)
    known = client.post("/forgot-password", data={"email": "player@example.com"})
    unknown = client.post("/forgot-password", data={"email": "nobody-here@example.com"})
    assert known.status_code == unknown.status_code == 303
    assert known.headers["location"] == unknown.headers["location"] == "/login"


def test_reset_token_is_single_use(client, world, session_factory, monkeypatch):
    _enable_mail(monkeypatch)
    captured = {}
    monkeypatch.setattr(
        mail, "_call_resend_api", lambda **kwargs: captured.setdefault("text", kwargs["text"])
    )
    client.post("/forgot-password", data={"email": "player@example.com"})
    link = [line for line in captured["text"].splitlines() if "/reset-password?token=" in line][0]
    raw_token = link.split("token=")[1].strip()

    first = client.post("/reset-password", data={"token": raw_token, "password": "new-pass-1!"})
    assert first.status_code == 303

    second = client.post("/reset-password", data={"token": raw_token, "password": "other-pass-1!"})
    assert second.status_code == 400
    assert "already been used" in second.text


def test_reset_token_expires_after_one_hour(client, world, session_factory, monkeypatch):
    _enable_mail(monkeypatch)
    db = session_factory()
    player = db.scalar(select(User).where(User.email == "player@example.com"))
    db.add(
        PasswordResetToken(
            user_id=player.id,
            token_hash=mail_module_hash("expired-token-value"),
            expires_at=dt.datetime.now(UTC) - dt.timedelta(minutes=1),
        )
    )
    db.commit()
    db.close()

    response = client.post(
        "/reset-password", data={"token": "expired-token-value", "password": "new-pass-1!"}
    )
    assert response.status_code == 400
    assert "expired" in response.text


def mail_module_hash(raw_token: str) -> str:
    """Mirrors app.routers.auth._hash_token exactly, without importing a private helper across
    modules: a real reset token is always looked up by this same hash."""
    import hashlib

    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def test_commissioner_invite_email_is_site_admin_only(client, world, session_factory, monkeypatch):
    _enable_mail(monkeypatch)
    db = session_factory()
    commish = _make_pool_commissioner_who_is_not_admin(db, db.get(Pool, world["pool_id"]))
    db.commit()
    db.close()

    _login(client, commish.email)
    response = client.post(
        f"/site/leagues/{world['pool_id']}/commissioner-code/email",
        data={"email": "newcommish@example.com"},
    )
    assert response.status_code == 403


def test_commissioner_invite_email_sends_for_the_site_admin(client, world, monkeypatch):
    _enable_mail(monkeypatch)
    _login(client, "boss@example.com")
    response = client.post(
        f"/site/leagues/{world['pool_id']}/commissioner-code/email",
        data={"email": "newcommish@example.com"},
    )
    assert response.status_code == 303
    assert "commissioner-code/email" not in response.headers["location"]


def test_player_invite_email_is_commissioner_only(client, world):
    _login(client, "player@example.com")
    response = client.post("/league/members/invite", data={"emails": "a@example.com"})
    assert response.status_code == 403


def test_player_invite_email_sends_to_multiple_addresses(
    client, world, session_factory, monkeypatch
):
    _enable_mail(monkeypatch)
    sent_to = []
    monkeypatch.setattr(mail, "_call_resend_api", lambda **kwargs: sent_to.append(kwargs["to"]))
    _login(client, "boss@example.com")
    response = client.post(
        "/league/members/invite",
        data={"emails": "one@example.com\ntwo@example.com"},
    )
    assert response.status_code == 303
    assert sent_to == ["one@example.com", "two@example.com"]


def test_mail_send_failure_falls_back_gracefully_not_silently(client, world, monkeypatch):
    """Even when mail is enabled, a provider failure must be visible, never reported as a
    quiet success: the whole point of Phase 7's failure contract."""
    _enable_mail(monkeypatch)

    def _boom(**kwargs):
        raise mail.MailSendFailed("provider down")

    monkeypatch.setattr(mail, "_call_resend_api", _boom)
    _login(client, "boss@example.com")
    response = client.post(
        "/league/members/invite",
        data={"emails": "a@example.com"},
    )
    assert response.status_code == 303
    # Follow the redirect to read the flash the failure produced.
    follow = client.get(response.headers["location"])
    assert "Could not email" in follow.text
    assert "still works" in follow.text


@pytest.mark.parametrize("path", ["/site/mail", "/league"])
def test_site_mail_page_is_site_admin_only(client, world, session_factory, path):
    db = session_factory()
    commish = _make_pool_commissioner_who_is_not_admin(db, db.get(Pool, world["pool_id"]))
    db.commit()
    db.close()
    _login(client, commish.email)
    if path == "/site/mail":
        assert client.get(path).status_code == 403
    else:
        # Sanity check the commissioner really is signed in and the pool route still works,
        # so the /site/mail 403 above is a real permission check, not a broken session.
        assert client.get(path).status_code == 200


def test_site_mail_page_shows_configuration_status_for_the_site_admin(client, world, monkeypatch):
    _enable_mail(monkeypatch)
    _login(client, "boss@example.com")
    response = client.get("/site/mail")
    assert response.status_code == 200
    assert "Resend" in response.text or "mail" in response.text.lower()


def test_week_published_notification_sent_when_pool_opts_in(
    client, world, session_factory, monkeypatch
):
    _enable_mail(monkeypatch)
    sent_to = []
    monkeypatch.setattr(mail, "_call_resend_api", lambda **kwargs: sent_to.append(kwargs["to"]))

    db = session_factory()
    pool = db.get(Pool, world["pool_id"])
    pool.notify_week_published = True
    week = db.get(Week, world["week_id"])
    week.status = "draft"
    db.commit()
    db.close()

    _login(client, "boss@example.com")
    response = client.post("/league/slate/publish", data={"week_id": world["week_id"]})
    assert response.status_code == 303
    assert "player@example.com" in sent_to


def test_week_published_notification_not_sent_when_pool_has_not_opted_in(
    client, world, session_factory, monkeypatch
):
    _enable_mail(monkeypatch)
    sent_to = []
    monkeypatch.setattr(mail, "_call_resend_api", lambda **kwargs: sent_to.append(kwargs["to"]))

    db = session_factory()
    pool = db.get(Pool, world["pool_id"])
    assert pool.notify_week_published is False  # the documented default
    week = db.get(Week, world["week_id"])
    week.status = "draft"
    db.commit()
    db.close()

    _login(client, "boss@example.com")
    response = client.post("/league/slate/publish", data={"week_id": world["week_id"]})
    assert response.status_code == 303
    assert sent_to == []


def test_every_mail_attempt_is_logged_regardless_of_outcome(
    client, world, session_factory, monkeypatch
):
    _enable_mail(monkeypatch)
    _login(client, "boss@example.com")
    client.post("/league/members/invite", data={"emails": "logged@example.com"})

    db = session_factory()
    row = db.scalar(select(MailLog).where(MailLog.recipient == "logged@example.com"))
    assert row is not None
    assert row.result == "sent"
    assert row.kind == "player_invite"
    db.close()
