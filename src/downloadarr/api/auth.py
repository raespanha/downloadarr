import hmac
import secrets

from fastapi import HTTPException, Request, status

from ..settings import Settings


class SessionStore:
    def __init__(self) -> None:
        self._sessions: set[str] = set()

    def create(self) -> str:
        value = secrets.token_urlsafe(32)
        self._sessions.add(value)
        return value

    def valid(self, value: str | None) -> bool:
        return value is not None and value in self._sessions

    def remove(self, value: str | None) -> None:
        if value:
            self._sessions.discard(value)


async def require_auth(request: Request) -> None:
    settings: Settings = request.app.state.settings
    authorization = request.headers.get("Authorization", "")
    if authorization.startswith("Bearer ") and settings.api_key is not None:
        if hmac.compare_digest(authorization[7:], settings.api_key.get_secret_value()):
            return
    if request.app.state.auth_sessions.valid(request.cookies.get("SID")):
        return
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
