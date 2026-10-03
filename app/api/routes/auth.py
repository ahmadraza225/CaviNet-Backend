"""Sign-in, session refresh, sign-out and own-password change (M-01).

The refresh token travels only in an httpOnly cookie scoped to /api/auth; the access token
is returned in the response body and kept in memory by the frontend.
"""

from typing import Annotated

from fastapi import APIRouter, Cookie, Response, status

from app.api.deps import CurrentUser, DbSession
from app.core.clock import utcnow
from app.core.config import get_settings
from app.schemas.auth import ChangePasswordRequest, LoginRequest, TokenResponse
from app.schemas.users import UserOut
from app.services import auth as auth_service
from app.services.auth import AuthSession

router = APIRouter(prefix="/auth", tags=["auth"])

REFRESH_COOKIE = "cavinet_refresh"
REFRESH_COOKIE_PATH = "/api/auth"

RefreshCookie = Annotated[str | None, Cookie(alias=REFRESH_COOKIE)]


def _session_response(response: Response, session: AuthSession) -> TokenResponse:
    max_age = max(0, int((session.refresh_expires_at - utcnow()).total_seconds()))
    response.set_cookie(
        REFRESH_COOKIE,
        session.refresh_token,
        max_age=max_age,
        httponly=True,
        secure=get_settings().cookie_secure,
        samesite="strict",
        path=REFRESH_COOKIE_PATH,
    )
    response.headers["Cache-Control"] = "no-store"
    return TokenResponse(
        access_token=session.access_token,
        expires_in=session.expires_in,
        user=UserOut.from_user(session.user),
    )


def _clear_cookie(response: Response) -> None:
    response.delete_cookie(
        REFRESH_COOKIE,
        path=REFRESH_COOKIE_PATH,
        httponly=True,
        secure=get_settings().cookie_secure,
        samesite="strict",
    )


@router.post("/login", response_model=TokenResponse)
def login(body: LoginRequest, response: Response, db: DbSession) -> TokenResponse:
    user = auth_service.authenticate(db, body.email, body.password)
    return _session_response(response, auth_service.start_session(db, user))


@router.post("/refresh", response_model=TokenResponse)
def refresh(response: Response, db: DbSession, refresh_cookie: RefreshCookie = None):
    return _session_response(response, auth_service.refresh_session(db, refresh_cookie))


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(response: Response, db: DbSession, refresh_cookie: RefreshCookie = None) -> None:
    auth_service.logout(db, refresh_cookie)
    _clear_cookie(response)


@router.get("/me", response_model=UserOut)
def me(user: CurrentUser) -> UserOut:
    return UserOut.from_user(user)


@router.post("/change-password", response_model=TokenResponse)
def change_password(
    body: ChangePasswordRequest, response: Response, user: CurrentUser, db: DbSession
) -> TokenResponse:
    session = auth_service.change_password(db, user, body.current_password, body.new_password)
    return _session_response(response, session)
