"""Sign a browser in with an API token, for servers without Google sign-in.

The package ships no OAuth callback, so under auth a browser has no way to get
the session cookie the web app needs. ``POST /auth/token_login`` takes a person's
API token from the login page's form, resolves it as a bearer would be, and sets
the same signed session cookie the OAuth callback would.

The exchange never widens access. The session resolves to the token's user, so
the token must open everything that user does: a key capped below its user's role,
narrowed in audience, or pinned to one audience is refused, as is any key of a
service account (``lookup_user_by_id`` resolves only people). The route is off
under ``--no-auth`` (nothing to sign in to) and without
``TRACKINIZER_SESSION_SECRET`` (no key to sign the cookie with).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast
from urllib.parse import parse_qs, quote

import logging

from fastapi import Request
from fastapi.responses import RedirectResponse

from trackinizer.server.auth import (
    _AppLike,
    _config,
    _resolve_identity,
    lookup_user_by_id,
)
from trackinizer.server.session import set_session_cookie
from trackinizer.wire.wire_machine_host import CREDENTIAL_PREFIX, ENROLLMENT_PREFIX


if TYPE_CHECKING:
    from fastapi import FastAPI

    from trackinizer.server.auth import AuthIdentity


__all__ = ["add_token_login_route"]

_logger = logging.getLogger(__name__)

_PATH = "/auth/token_login"
_DEFAULT_NEXT = "/app/"
# A pasted token is short; anything larger is not a form this page sent.
_MAX_BODY_BYTES = 4096


def add_token_login_route(app: FastAPI) -> None:
    """Mount ``POST /auth/token_login`` on ``app``."""
    app.add_api_route(_PATH, _token_login, methods=["POST"], include_in_schema=False)


async def _token_login(request: Request) -> RedirectResponse:
    """Exchange a person's API token for a browser session cookie."""
    body = await request.body()
    form = parse_qs(body[:_MAX_BODY_BYTES].decode("utf-8", "replace"))
    token = (form.get("token") or [""])[0].strip()
    next_url = _safe_next((form.get("next") or [""])[0])
    state = cast("_AppLike", request.app).state
    config = _config(state)
    if config is None or config.auth_disabled:
        return _back(next_url, "disabled")
    if not config.session_secret:
        return _back(next_url, "unconfigured")
    if not token or token.startswith((CREDENTIAL_PREFIX, ENROLLMENT_PREFIX)):
        return _back(next_url, "invalid")
    keyed = await _resolve_identity(
        state.store,
        token,
        staff=config.staff,
        request_id=str(getattr(request.state, "request_id", "")),
    )
    if keyed is None:
        return _back(next_url, "invalid")
    async with state.store.engine.acquire() as conn:
        person = await lookup_user_by_id(conn, user_id=keyed.user_id, staff=config.staff)
    if person is None:
        # A service account, or a user disabled since the key was cached.
        return _back(next_url, "invalid")
    if not _opens_all_of(keyed, person):
        _logger.warning(
            "token login refused for %s: key %s is narrower than its account",
            person.email,
            keyed.api_key_id,
        )
        return _back(next_url, "narrow")
    response = RedirectResponse(url=next_url, status_code=303)
    set_session_cookie(
        response,
        user_id=str(person.user_id),
        secret=config.session_secret,
        max_age_seconds=config.session_max_age_seconds,
    )
    _logger.info("token login for %s with key %s", person.email, keyed.api_key_id)
    return response


def _opens_all_of(keyed: AuthIdentity, person: AuthIdentity) -> bool:
    """Whether the key reaches everything its user's session would."""
    return (
        keyed.role == person.role
        and keyed.read_audiences >= person.read_audiences
        and keyed.open_audiences >= person.open_audiences
        and keyed.pinned_audience is None
    )


def _safe_next(raw: str) -> str:
    """Keep ``next`` a same-origin path; anything else lands on the app."""
    if raw.startswith("/") and not raw.startswith(("//", "/\\")):
        return raw
    return _DEFAULT_NEXT


def _back(next_url: str, error: str) -> RedirectResponse:
    """Return to the login page, saying why and keeping ``next``."""
    return RedirectResponse(
        url=f"/auth/login_page?next={quote(next_url, safe='')}&error={error}",
        status_code=303,
    )
