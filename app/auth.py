"""Per-user Bearer-token authentication and route-scope authorization."""
from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse

from app.security_store import authenticate_api_token


def authenticate_request(request: Request) -> tuple[dict | None, JSONResponse | None]:
    """Resolve a live, unexpired token into its user identity and allowed scopes."""
    authorization = request.headers.get("authorization", "").strip()
    parts = authorization.split(None, 1)
    supplied = ""
    if len(parts) == 2 and parts[0].casefold() == "bearer":
        supplied = parts[1].strip()
    if not supplied:
        supplied = request.headers.get("x-api-key", "").strip()

    principal = authenticate_api_token(supplied)
    if principal is None:
        return None, JSONResponse(
            status_code=401,
            content={"error": "Authentication failed. Send a valid, active user token as Bearer or X-API-Key."},
            headers={"WWW-Authenticate": "Bearer"},
        )
    return principal, None


def authorize_scope(request: Request, required_scope: str) -> JSONResponse | None:
    """Return 403 unless the authenticated user's token grants the route scope."""
    scopes = getattr(request.state, "scopes", frozenset())
    if required_scope not in scopes:
        return JSONResponse(
            status_code=403,
            content={"error": f"Access denied. This token requires the {required_scope} scope."},
        )
    return None
