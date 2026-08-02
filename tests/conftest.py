"""Shared test fixtures.

Two things matter here.

1. Provider tests never touch the network. The autouse session fixture below pins
   settings.offline_mode to True, which makes fetch_json refuse to open a socket and
   either serve a cached payload or raise ProviderError. That is the belt and braces
   behind the rule that a test must never spend a metered credit.
2. Every recorded payload in tests/fixtures is a real capture from the live APIs on
   2026-08-02, so the parsers are exercised against the shapes production will see.
"""

from __future__ import annotations

import json
import pathlib
from collections.abc import Callable
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.models import Base

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"


@pytest.fixture(autouse=True, scope="session")
def force_offline_mode():
    """No test, anywhere, may make a live provider call.

    Session scoped and autouse so a module that forgets to ask for it is still covered.
    """
    previous = settings.offline_mode
    settings.offline_mode = True
    try:
        yield
    finally:
        settings.offline_mode = previous


@pytest.fixture(scope="session")
def fixtures_dir() -> pathlib.Path:
    """Directory holding the recorded provider payloads."""
    return FIXTURES


@pytest.fixture
def load_fixture(fixtures_dir: pathlib.Path) -> Callable[[str], Any]:
    """Return a callable that parses one recorded fixture by filename.

    Function scoped and re-parsed on every call, so a test that mutates a payload
    cannot leak that mutation into the next test.
    """

    def _load(filename: str) -> Any:
        path = fixtures_dir / filename
        if not path.is_file():
            raise FileNotFoundError(f"No such fixture: {path}")
        return json.loads(path.read_text(encoding="utf-8"))

    return _load


@pytest.fixture
def db():
    """A throwaway in memory SQLite session with the full schema created.

    StaticPool plus check_same_thread False keeps the one connection alive for the
    whole test, which is what an in memory database needs.
    """
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(engine)
    session = Session(engine, future=True)
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(engine)
        engine.dispose()
