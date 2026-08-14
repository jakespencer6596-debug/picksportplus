"""FastAPI application: middleware, routers, error pages."""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import HTTPException as FastAPIHTTPException
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app.config import settings
from app.templating import STATIC_DIR, render, templates

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("picksportplus")

app = FastAPI(title="PickSportPlus", docs_url=None, redoc_url=None, openapi_url=None)

app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key,
    session_cookie="psp_session",
    same_site="lax",
    # Secure behind Render's TLS, relaxed for a local http dev server. Starlette always
    # sets HttpOnly on this cookie, so it is never readable from JavaScript.
    https_only=settings.secure_cookies,
    max_age=60 * 60 * 24 * 30,
)

# Only answer to hosts we expect. Render publishes the real hostname in the environment.
app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)

# Render terminates TLS at its edge and forwards X-Forwarded-Proto. Without this the app
# would believe every request is plain http and would refuse to set a Secure cookie.
app.add_middleware(ProxyHeadersMiddleware, trusted_hosts="*")

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.on_event("startup")
def _log_storage_status() -> None:
    """Loud, impossible-to-miss log line on boot so an operator reading the Render deploy
    log sees data-loss risk immediately, not just from a banner someone has to click into
    (Phase 1 remediation, see DECISIONS.md)."""
    from app.db import engine

    log.info("database dialect: %s", engine.dialect.name)
    if settings.is_ephemeral_storage:
        log.warning(
            "DATABASE_URL points at ephemeral storage (%s). Every account, league, pick, "
            "payout rule and award will be LOST the next time this service sleeps or "
            "redeploys. Set DATABASE_URL to a persistent Postgres database (for example "
            "Neon) before inviting real players.",
            settings.database_url,
        )


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    """Redirects for signed-out users, in-theme pages for everything else."""
    location = (exc.headers or {}).get("Location")
    if exc.status_code in (302, 303, 307) and location:
        target = location
        if exc.status_code == 303 and request.url.path not in ("/", "/login"):
            target = f"{location}?next={request.url.path}"
        return RedirectResponse(target, status_code=303)

    if request.headers.get("HX-Request") == "true":
        return templates.TemplateResponse(
            request,
            "components/error_inline.html",
            {"request": request, "detail": exc.detail},
            status_code=exc.status_code,
        )

    titles = {
        403: "Not your locker room",
        404: "That page is not on the schedule",
        500: "Something went wrong on our end",
    }
    return render(
        request,
        "error.html",
        {
            "code": exc.status_code,
            "title": titles.get(exc.status_code, "Something went wrong"),
            "detail": exc.detail if isinstance(exc.detail, str) else "",
        },
        status_code=exc.status_code,
    )


@app.exception_handler(FastAPIHTTPException)
async def fastapi_http_exception_handler(request: Request, exc: FastAPIHTTPException):
    return await http_exception_handler(request, exc)


@app.get("/health", include_in_schema=False)
@app.get("/healthz", include_in_schema=False)
def health():
    """Render's health check. Deliberately touches nothing, so a feed or database hiccup
    cannot make the platform think the service is down."""
    return {"status": "ok"}


# Routers are imported after app creation so they can import from app.main if needed.
from app.routers import (  # noqa: E402
    admin,
    admin_contacts,
    auth,
    leaderboard,
    leagues,
    legal,
    payouts,
    picks,
    public,
    results,
)

app.include_router(auth.router)
app.include_router(picks.router)
app.include_router(leaderboard.router)
app.include_router(results.router)
app.include_router(admin.router)
app.include_router(payouts.router)
app.include_router(leagues.router)
app.include_router(admin_contacts.router)
app.include_router(legal.router)
app.include_router(public.router)


@app.get("/", include_in_schema=False)
def index(request: Request):
    """A signed in visitor still lands on this week's picks, unchanged. A signed out
    visitor gets the public landing page instead of being bounced straight to /login."""
    if request.session.get("uid"):
        return RedirectResponse("/picks", status_code=303)
    return render(request, "home.html", {}, current_user=None, pool=None, is_commissioner=False)
