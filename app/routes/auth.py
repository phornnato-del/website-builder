import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, EmailStr, Field
from pymongo.errors import DuplicateKeyError

from app.config import get_settings
from app.rate_limit import limiter

router = APIRouter()
settings = get_settings()
password_hasher = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2)
dummy_password_hash = password_hasher.hash(secrets.token_urlsafe(32))


class Credentials(BaseModel):
    email: EmailStr
    password: str = Field(min_length=12, max_length=128)


class UserResponse(BaseModel):
    id: str
    email: EmailStr
    created_at: datetime


class AuthResponse(BaseModel):
    user: UserResponse
    csrf_token: str


def _session_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _user_response(user: dict[str, Any]) -> UserResponse:
    return UserResponse(
        id=str(user["_id"]),
        email=user["email"],
        created_at=user["created_at"],
    )


def _set_session_cookie(response: Response, token: str) -> None:
    response.delete_cookie(
        key=settings.session_cookie_name,
        path="/api/auth",
        httponly=True,
        secure=settings.environment != "development",
        samesite="lax",
    )
    response.set_cookie(
        key=settings.session_cookie_name,
        value=token,
        max_age=settings.session_lifetime_seconds,
        httponly=True,
        secure=settings.environment != "development",
        samesite="lax",
        path="/",
    )


async def _create_session(
    request: Request,
    response: Response,
    user: dict[str, Any],
) -> AuthResponse:
    token = secrets.token_urlsafe(32)
    csrf_token = secrets.token_urlsafe(32)
    expires_at = datetime.now(UTC) + timedelta(
        seconds=settings.session_lifetime_seconds
    )
    sessions = request.app.state.database.sessions
    await sessions.create_index("token_hash", unique=True)
    await sessions.create_index("expires_at", expireAfterSeconds=0)
    await sessions.insert_one(
        {
            "token_hash": _session_hash(token),
            "user_id": user["_id"],
            "csrf_token": csrf_token,
            "created_at": datetime.now(UTC),
            "expires_at": expires_at,
        }
    )
    _set_session_cookie(response, token)
    return AuthResponse(user=_user_response(user), csrf_token=csrf_token)


async def _require_session(request: Request) -> dict[str, Any]:
    token = request.cookies.get(settings.session_cookie_name)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
        )

    session = await request.app.state.database.sessions.find_one(
        {
            "token_hash": _session_hash(token),
            "expires_at": {"$gt": datetime.now(UTC)},
        }
    )
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
        )

    user = await request.app.state.database.users.find_one(
        {"_id": session["user_id"]}
    )
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
        )
    return {"session": session, "user": user}


def _require_same_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if origin not in settings.cors_origins:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Request origin is not allowed",
        )


def _require_csrf_token(request: Request, session: dict[str, Any]) -> None:
    provided = request.headers.get("x-csrf-token", "")
    expected = session.get("csrf_token", "")
    if not expected or not secrets.compare_digest(provided, expected):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid CSRF token",
        )


@router.post("/register", response_model=AuthResponse, status_code=201)
@limiter.limit("5/hour")
async def register(
    request: Request,
    credentials: Credentials,
    response: Response,
) -> AuthResponse:
    _require_same_origin(request)
    database = request.app.state.database
    users = database.users
    await users.create_index("email", unique=True)

    email = str(credentials.email).lower()
    user = {
        "email": email,
        "password_hash": password_hasher.hash(credentials.password),
        "created_at": datetime.now(UTC),
    }
    try:
        result = await users.insert_one(user)
    except DuplicateKeyError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An account with this email already exists",
        ) from error

    user["_id"] = result.inserted_id
    return await _create_session(request, response, user)


@router.post("/login", response_model=AuthResponse)
@limiter.limit("5/minute")
async def login(
    request: Request,
    credentials: Credentials,
    response: Response,
) -> AuthResponse:
    _require_same_origin(request)
    email = str(credentials.email).lower()
    user = await request.app.state.database.users.find_one({"email": email})
    stored_hash = user["password_hash"] if user else dummy_password_hash
    try:
        password_matches = password_hasher.verify(
            stored_hash,
            credentials.password,
        )
    except VerificationError:
        password_matches = False

    if user is None or not password_matches:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Email or password is incorrect",
        )

    return await _create_session(request, response, user)


@router.get("/me", response_model=AuthResponse)
async def me(
    session_data: Annotated[dict[str, Any], Depends(_require_session)],
) -> AuthResponse:
    return AuthResponse(
        user=_user_response(session_data["user"]),
        csrf_token=session_data["session"]["csrf_token"],
    )


@router.post("/logout", status_code=204)
@limiter.limit("10/minute")
async def logout(
    request: Request,
    response: Response,
    session_data: Annotated[dict[str, Any], Depends(_require_session)],
) -> Response:
    _require_same_origin(request)
    _require_csrf_token(request, session_data["session"])
    await request.app.state.database.sessions.delete_one(
        {"_id": session_data["session"]["_id"]}
    )
    response.delete_cookie(
        key=settings.session_cookie_name,
        path="/",
        httponly=True,
        secure=settings.environment != "development",
        samesite="lax",
    )
    response.delete_cookie(
        key=settings.session_cookie_name,
        path="/api/auth",
        httponly=True,
        secure=settings.environment != "development",
        samesite="lax",
    )
    response.status_code = status.HTTP_204_NO_CONTENT
    return response
