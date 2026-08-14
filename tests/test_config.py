"""Ephemeral storage detection (Phase 1 remediation, see DECISIONS.md)."""

from __future__ import annotations

from app.config import is_ephemeral_sqlite_path


def test_flags_tmp_sqlite_path():
    assert is_ephemeral_sqlite_path("sqlite:////tmp/picksportplus.db") is True


def test_flags_bare_tmp_root():
    assert is_ephemeral_sqlite_path("sqlite:////tmp") is True


def test_passes_postgres_url():
    assert is_ephemeral_sqlite_path("postgresql+psycopg://user:pw@host/db") is False


def test_passes_non_tmp_sqlite_path():
    assert is_ephemeral_sqlite_path("sqlite:///./picksportplus.db") is False


def test_passes_relative_sqlite_path_containing_tmp_as_a_word():
    # "tmpfile.db" is not the /tmp directory; only a real /tmp prefix should flag.
    assert is_ephemeral_sqlite_path("sqlite:///./tmpfile.db") is False
