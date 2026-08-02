"""Password hashing, cookie sessions, and the current-user dependencies."""

from __future__ import annotations

import secrets
import string

# passlib 1.7.4 reads bcrypt.__about__.__version__, which bcrypt removed in 4.1.
# Without this shim every first hash logs an alarming traceback that is not a real error.
try:  # pragma: no cover - import-time compatibility glue
    import bcrypt as _bcrypt

    if not hasattr(_bcrypt, "__about__"):

        class _About:  # noqa: D401
            __version__ = getattr(_bcrypt, "__version__", "4.x")

        _bcrypt.__about__ = _About()  # type: ignore[attr-defined]
except Exception:  # pragma: no cover
    pass

from fastapi import Depends, HTTPException, Request, status
from passlib.context import CryptContext
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Pool, PoolMember, User

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

SESSION_USER_KEY = "uid"
SESSION_POOL_KEY = "pid"
FLASH_KEY = "flashes"

_CODE_ALPHABET = string.ascii_uppercase + string.digits


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def hash_password(raw: str) -> str:
    # bcrypt silently truncates at 72 bytes. Reject rather than accept a weakened password.
    if len(raw.encode("utf-8")) > 72:
        raise ValueError("Password must be 72 bytes or fewer.")
    return pwd_context.hash(raw)


def verify_password(raw: str, hashed: str) -> bool:
    try:
        return pwd_context.verify(raw, hashed)
    except ValueError:
        return False


def generate_join_code(length: int = 8) -> str:
    """Readable code with the ambiguous characters removed."""
    alphabet = "".join(c for c in _CODE_ALPHABET if c not in "O0I1")
    return "".join(secrets.choice(alphabet) for _ in range(length))


def normalize_join_code(code: str) -> str:
    return (code or "").strip().upper().replace(" ", "")


# Flash messages -------------------------------------------------------------


def flash(request: Request, message: str, level: str = "ok") -> None:
    """Queue a one-shot message. level is ok, error or info."""
    bucket = request.session.setdefault(FLASH_KEY, [])
    bucket.append({"message": message, "level": level})


def pop_flashes(request: Request) -> list[dict]:
    return request.session.pop(FLASH_KEY, [])


# Session ---------------------------------------------------------------------


def login_user(request: Request, user: User) -> None:
    # Clearing first prevents session fixation carrying stale state across identities.
    request.session.clear()
    request.session[SESSION_USER_KEY] = user.id


def logout_user(request: Request) -> None:
    request.session.clear()


# Dependencies ----------------------------------------------------------------


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User | None:
    uid = request.session.get(SESSION_USER_KEY)
    if not uid:
        return None
    user = db.get(User, uid)
    if user is None or not user.is_active:
        request.session.clear()
        return None
    return user


def require_user(user: User | None = Depends(get_current_user)) -> User:
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            headers={"Location": "/login"},
            detail="Sign in to continue.",
        )
    return user


def require_admin(user: User = Depends(require_user)) -> User:
    if not user.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Commissioner access only.")
    return user


def get_active_pool(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
) -> Pool:
    """The pool the signed-in user is currently viewing.

    v1 ships one pool per user in practice, but the session remembers a choice so
    multi-pool membership works without further changes.
    """
    pid = request.session.get(SESSION_POOL_KEY)
    if pid:
        member = db.scalar(
            select(PoolMember).where(PoolMember.pool_id == pid, PoolMember.user_id == user.id)
        )
        if member is not None:
            pool = db.get(Pool, pid)
            if pool is not None:
                return pool
    member = db.scalars(
        select(PoolMember).where(PoolMember.user_id == user.id).order_by(PoolMember.id)
    ).first()
    if member is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "You are not a member of a pool yet.")
    request.session[SESSION_POOL_KEY] = member.pool_id
    pool = db.get(Pool, member.pool_id)
    if pool is None:  # pragma: no cover - orphaned membership
        raise HTTPException(status.HTTP_403_FORBIDDEN, "You are not a member of a pool yet.")
    return pool


def membership_for(db: Session, user: User, pool: Pool) -> PoolMember | None:
    return db.scalar(
        select(PoolMember).where(PoolMember.pool_id == pool.id, PoolMember.user_id == user.id)
    )


def is_commissioner(db: Session, user: User, pool: Pool) -> bool:
    """Global admins are treated as commissioners everywhere."""
    if user.is_admin:
        return True
    member = membership_for(db, user, pool)
    return bool(member and member.is_commissioner)


def require_commissioner(
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
    pool: Pool = Depends(get_active_pool),
) -> Pool:
    if not is_commissioner(db, user, pool):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Commissioner access only.")
    return pool
