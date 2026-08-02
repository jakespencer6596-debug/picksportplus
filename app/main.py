"""FastAPI application: middleware, routers, error pages."""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import HTTPException as FastAPIHTTPException
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

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
    https_only=not settings.is_sqlite,  # local dev runs on http, Render runs on https
    max_age=60 * 60 * 24 * 30,
)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


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


@app.get("/healthz", include_in_schema=False)
def healthz():
    return {"status": "ok"}


# Routers are imported after app creation so they can import from app.main if needed.
from app.routers import admin, auth, leaderboard, legal, picks, results  # noqa: E402

app.include_router(auth.router)
app.include_router(picks.router)
app.include_router(leaderboard.router)
app.include_router(results.router)
app.include_router(admin.router)
app.include_router(legal.router)


@app.get("/", include_in_schema=False)
def index(request: Request):
    if request.session.get("uid"):
        return RedirectResponse("/picks", status_code=303)
    return RedirectResponse("/login", status_code=303)
